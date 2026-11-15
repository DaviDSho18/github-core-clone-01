import type { IncomingMessage, ServerResponse } from 'node:http';
import { readBody } from './http.ts';
import { HttpError, Store, type Repository } from './store.ts';
import { ZERO, readPack, writePack } from './objects.ts';

export const FLUSH = Buffer.from('0000'), DELIM = Buffer.from('0001');
export function pkt(data: string | Buffer): Buffer {
  const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data);
  if (bytes.length > 65516) throw new Error('Packet too large');
  return Buffer.concat([Buffer.from((bytes.length + 4).toString(16).padStart(4, '0')), bytes]);
}
export function packets(bytes: Buffer): { lines: Buffer[]; offset: number } {
  let at = 0;
  const lines: Buffer[] = [];
  while (at + 4 <= bytes.length) {
    const prefix = bytes.subarray(at, at + 4).toString();
    if (!/^[0-9a-f]{4}$/i.test(prefix)) break;
    const n = parseInt(prefix, 16); at += 4;
    if (n <= 2) { lines.push(Buffer.from(n === 1 ? '\x01' : n === 2 ? '\x02' : '')); continue; }
    if (n < 4 || at + n - 4 > bytes.length) throw new HttpError(400, 'Invalid pkt-line');
    lines.push(bytes.subarray(at, at + n - 4)); at += n - 4;
  }
  return { lines, offset: at };
}
function sideband(data: Buffer, channel = 1): Buffer[] {
  const out: Buffer[] = [];
  for (let at = 0; at < data.length; at += 65514) out.push(pkt(Buffer.concat([Buffer.from([channel]), data.subarray(at, at + 65514)])));
  return out;
}
function reply(res: ServerResponse, service: string, data: Buffer): void {
  res.writeHead(200, { 'content-type': `application/x-${service}`, 'content-length': data.length, 'cache-control': 'no-cache' });
  res.end(data);
}
function advertised(r: Repository): [string, string][] {
  const refs = [...r.refs].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  const head = r.refs.get(`refs/heads/${r.defaultBranch}`);
  return head ? [['HEAD', head], ...refs] : refs;
}
function advertise(r: Repository, service: string, v2: boolean): Buffer {
  if (service === 'git-upload-pack' && v2) return Buffer.concat([
    pkt('version 2\n'), pkt('agent=makefun-forge/1\n'), pkt('ls-refs=unborn\n'), pkt('fetch=\n'), pkt('object-format=sha1\n'), FLUSH,
  ]);
  const upload = service === 'git-upload-pack';
  const caps = upload
    ? `multi_ack_detailed no-done side-band-64k thin-pack ofs-delta include-tag symref=HEAD:refs/heads/${r.defaultBranch} agent=makefun-forge/1`
    : 'report-status delete-refs side-band-64k ofs-delta agent=makefun-forge/1';
  const refs = advertised(r).filter(([name]) => upload || name !== 'HEAD');
  if (!refs.length) refs.push(['capabilities^{}', ZERO]);
  return Buffer.concat([pkt(`# service=${service}\n`), FLUSH,
    ...refs.map(([name, sha], i) => pkt(`${sha} ${name}${i === 0 ? `\0${caps}` : ''}\n`)), FLUSH]);
}
function upload(r: Repository, bytes: Buffer, v2: boolean): Buffer {
  const lines = packets(bytes).lines.map(b => b.toString().trimEnd());
  if (v2 || lines.some(l => l.startsWith('command='))) {
    if (lines.includes('command=ls-refs')) {
      const prefixes = lines.filter(l => l.startsWith('ref-prefix ')).map(l => l.slice(11));
      const refs = advertised(r).filter(([name]) => !prefixes.length || prefixes.some(p => name.startsWith(p)));
      const chunks = refs.map(([name, sha]) => pkt(`${sha} ${name}${name === 'HEAD' && lines.includes('symrefs') ? ` symref-target:refs/heads/${r.defaultBranch}` : ''}\n`));
      if (!r.refs.size && lines.includes('unborn') && (!prefixes.length || prefixes.some(p => 'HEAD'.startsWith(p)))) chunks.push(pkt(`unborn HEAD symref-target:refs/heads/${r.defaultBranch}\n`));
      return Buffer.concat([...chunks, FLUSH]);
    }
    if (!lines.includes('command=fetch')) return Buffer.concat([pkt('ERR unsupported command\n'), FLUSH]);
  }
  const wants = lines.filter(l => l.startsWith('want ')).map(l => l.split(' ')[1]);
  const haves = lines.filter(l => l.startsWith('have ')).map(l => l.split(' ')[1]).filter(s => r.db.objects.has(s));
  for (const sha of wants) if (!/^[0-9a-f]{40}$/.test(sha) || !r.db.objects.has(sha)) return Buffer.concat([pkt(`ERR not our ref ${sha}\n`), FLUSH]);
  if (!wants.length) return v2 ? FLUSH : pkt('NAK\n');
  const done = lines.includes('done');
  if (!done) {
    if (v2) return Buffer.concat([pkt('acknowledgments\n'), ...(haves.length ? haves.map(s => pkt(`ACK ${s}\n`)) : [pkt('NAK\n')]), FLUSH]);
    return Buffer.concat([...(haves.length ? haves.map(s => pkt(`ACK ${s} common\n`)) : []), pkt('NAK\n')]);
  }
  const excluded = r.db.reachable(haves), objects = [...r.db.reachable(wants)].filter(s => !excluded.has(s));
  const pack = writePack(r.db, objects);
  if (v2) return Buffer.concat([pkt('packfile\n'), ...sideband(pack), FLUSH]);
  const band = lines.some(l => l.startsWith('want ') && /\bside-band(?:-64k)?\b/.test(l));
  return Buffer.concat([pkt(haves.length ? `ACK ${haves[0]}\n` : 'NAK\n'), ...(band ? [...sideband(pack), FLUSH] : [pack])]);
}
function validRef(ref: string): boolean {
  return /^refs\/(heads|tags)\/.+/.test(ref) && !/[\x00-\x20\x7f~^:?*\[\\]/.test(ref)
    && !ref.includes('..') && !ref.includes('@{') && !ref.includes('//') && !ref.endsWith('/')
    && !ref.split('/').some(p => p.startsWith('.') || p.endsWith('.') || p.endsWith('.lock'));
}
function receive(r: Repository, bytes: Buffer): Buffer {
  // Command pkt-lines end at the first flush; pack data immediately follows it.
  const commands: { old: string; next: string; ref: string }[] = [];
  let at = 0, caps = '';
  while (at + 4 <= bytes.length) {
    const prefix = bytes.subarray(at, at + 4).toString();
    if (!/^[0-9a-f]{4}$/i.test(prefix)) throw new HttpError(400, 'Invalid receive-pack framing');
    const n = parseInt(prefix, 16); at += 4;
    if (n === 0) break;
    if (n < 4 || at + n - 4 > bytes.length) throw new HttpError(400, 'Invalid receive-pack command');
    let text = bytes.subarray(at, at + n - 4).toString().trimEnd(); at += n - 4;
    const nul = text.indexOf('\0');
    if (nul >= 0) { caps = text.slice(nul + 1); text = text.slice(0, nul); }
    const match = /^([0-9a-f]{40}) ([0-9a-f]{40}) (.+)$/.exec(text);
    if (!match) throw new HttpError(400, 'Invalid ref update');
    commands.push({ old: match[1], next: match[2], ref: match[3] });
  }
  let unpackError = '';
  try { if (at < bytes.length) readPack(r.db, bytes.subarray(at)); }
  catch (error) { unpackError = error instanceof Error ? error.message : 'bad pack'; }
  const results: string[] = [];
  for (const c of commands) {
    let error = unpackError ? 'unpacker error' : '';
    const current = r.refs.get(c.ref) ?? ZERO;
    if (!error && !validRef(c.ref)) error = 'invalid ref name';
    if (!error && current !== c.old) error = 'non-fast-forward';
    if (!error && c.next !== ZERO && !r.db.objects.has(c.next)) error = 'missing object';
    if (!error && c.next !== ZERO && c.ref.startsWith('refs/heads/') && r.db.objects.get(c.next)?.type !== 'commit') error = 'not a commit';
    if (!error && c.next !== ZERO && current !== ZERO && !r.db.isAncestor(current, c.next)) error = 'non-fast-forward';
    if (!error && c.next !== ZERO) {
      try { r.db.reachable([c.next]); } catch { error = 'missing object'; }
    }
    if (error) results.push(`ng ${c.ref} ${error}\n`);
    else { if (c.next === ZERO) r.refs.delete(c.ref); else r.refs.set(c.ref, c.next); results.push(`ok ${c.ref}\n`); }
  }
  if (!caps.split(' ').includes('report-status')) return Buffer.alloc(0);
  const status = Buffer.concat([pkt(`unpack ${unpackError || 'ok'}\n`), ...results.map(pkt), FLUSH]);
  return caps.split(' ').includes('side-band-64k') ? Buffer.concat([...sideband(status), FLUSH]) : status;
}
export async function handleGit(req: IncomingMessage, res: ServerResponse, url: URL, store: Store): Promise<boolean> {
  const match = /^\/([^/]+)\/([^/]+)\.git\/(info\/refs|git-upload-pack|git-receive-pack)$/.exec(url.pathname);
  if (!match) return false;
  const r = store.repo(decodeURIComponent(match[1]), decodeURIComponent(match[2]));
  const v2 = String(req.headers['git-protocol'] ?? '').split(':').includes('version=2');
  if (req.method === 'GET' && match[3] === 'info/refs') {
    const service = url.searchParams.get('service');
    if (service !== 'git-upload-pack' && service !== 'git-receive-pack') throw new HttpError(400, 'Unknown git service');
    reply(res, `${service}-advertisement`, advertise(r, service, v2)); return true;
  }
  if (req.method === 'POST' && match[3] !== 'info/refs') {
    const bytes = await readBody(req);
    reply(res, `${match[3]}-result`, match[3] === 'git-upload-pack' ? upload(r, bytes, v2) : receive(r, bytes)); return true;
  }
  throw new HttpError(404, 'Not Found');
}
