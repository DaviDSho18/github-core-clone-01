import { createHash } from 'node:crypto';
import { deflateSync, inflateSync } from 'node:zlib';

export type ObjectType = 'commit' | 'tree' | 'blob' | 'tag';
export type GitObject = { type: ObjectType; data: Buffer };
export type TreeEntry = { mode: string; sha: string };
export const ZERO = '0'.repeat(40);
const types: Record<number, ObjectType> = { 1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag' };
const codes = { commit: 1, tree: 2, blob: 3, tag: 4 };
const MAX_OBJECT = 128 * 1024 * 1024;
export const hash = (data: Buffer): string => createHash('sha1').update(data).digest('hex');

/** Content-addressed loose objects. The loose representation is the real zlib Git format. */
export class ObjectDB {
  objects = new Map<string, GitObject>();
  put(type: ObjectType, data: Buffer | string): string {
    const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data);
    const sha = hash(Buffer.concat([Buffer.from(`${type} ${bytes.length}\0`), bytes]));
    if (!this.objects.has(sha)) this.objects.set(sha, { type, data: bytes });
    return sha;
  }
  get(sha: string): GitObject {
    const object = this.objects.get(sha);
    if (!object) throw new Error(`Missing git object ${sha}`);
    return object;
  }
  loose(sha: string): Buffer {
    const o = this.get(sha);
    return deflateSync(Buffer.concat([Buffer.from(`${o.type} ${o.data.length}\0`), o.data]));
  }
  readLoose(bytes: Buffer): string {
    const raw = inflateSync(bytes, { maxOutputLength: MAX_OBJECT });
    const end = raw.indexOf(0);
    const [type, size] = raw.subarray(0, end).toString().split(' ');
    if (end < 0 || !(type in codes) || Number(size) !== raw.length - end - 1) throw new Error('Invalid loose object');
    return this.put(type as ObjectType, raw.subarray(end + 1));
  }
  commit(sha: string): { tree: string; parents: string[]; message: string } {
    const o = this.get(sha);
    if (o.type !== 'commit') throw new Error('Not a commit');
    const text = o.data.toString();
    const boundary = text.indexOf('\n\n');
    const headers = text.slice(0, boundary).split('\n');
    return { tree: headers.find(x => x.startsWith('tree '))!.slice(5),
      parents: headers.filter(x => x.startsWith('parent ')).map(x => x.slice(7)), message: text.slice(boundary + 2) };
  }
  tree(sha: string): Map<string, TreeEntry> {
    const o = this.get(sha);
    if (o.type !== 'tree') throw new Error('Not a tree');
    const result = new Map<string, TreeEntry>();
    let at = 0;
    while (at < o.data.length) {
      const space = o.data.indexOf(32, at), nul = o.data.indexOf(0, space);
      if (space < at || nul < space || nul + 21 > o.data.length) throw new Error('Invalid tree');
      result.set(o.data.subarray(space + 1, nul).toString(), {
        mode: o.data.subarray(at, space).toString(), sha: o.data.subarray(nul + 1, nul + 21).toString('hex'),
      });
      at = nul + 21;
    }
    return result;
  }
  files(commit?: string): Map<string, TreeEntry> {
    const result = new Map<string, TreeEntry>();
    if (!commit || commit === ZERO) return result;
    const walk = (tree: string, prefix: string, depth: number) => {
      if (depth > 128) throw new Error('Tree nesting limit');
      for (const [name, entry] of this.tree(tree)) {
        if (entry.mode === '40000' || entry.mode === '040000') walk(entry.sha, `${prefix}${name}/`, depth + 1);
        else result.set(prefix + name, entry);
      }
    };
    walk(this.commit(commit).tree, '', 0);
    return result;
  }
  writeTree(files: Map<string, TreeEntry>): string {
    type Dir = Map<string, TreeEntry | Dir>;
    const root: Dir = new Map();
    for (const [path, entry] of files) {
      const parts = path.split('/');
      let dir = root;
      for (const part of parts.slice(0, -1)) {
        if (!(dir.get(part) instanceof Map)) dir.set(part, new Map());
        dir = dir.get(part) as Dir;
      }
      dir.set(parts[parts.length - 1], entry);
    }
    const write = (dir: Dir): string => {
      const entries = [...dir].map(([name, entry]) => ({ name,
        mode: entry instanceof Map ? '40000' : entry.mode,
        sha: entry instanceof Map ? write(entry) : entry.sha }));
      entries.sort((a, b) => Buffer.compare(Buffer.from(a.name + (a.mode === '40000' ? '/' : '')),
        Buffer.from(b.name + (b.mode === '40000' ? '/' : ''))));
      return this.put('tree', Buffer.concat(entries.flatMap(e => [Buffer.from(`${e.mode} ${e.name}\0`), Buffer.from(e.sha, 'hex')])));
    };
    return write(root);
  }
  isAncestor(ancestor: string, tip: string): boolean {
    if (ancestor === ZERO) return true;
    const pending = [tip], seen = new Set<string>();
    while (pending.length) {
      const sha = pending.pop()!;
      if (sha === ancestor) return true;
      if (seen.has(sha)) continue;
      seen.add(sha);
      if (this.objects.get(sha)?.type === 'commit') pending.push(...this.commit(sha).parents);
    }
    return false;
  }
  mergeBase(a: string, b: string): string | undefined {
    const ancestors = new Set<string>(), pending = [a];
    while (pending.length) {
      const s = pending.pop()!;
      if (ancestors.has(s)) continue;
      ancestors.add(s);
      if (this.objects.get(s)?.type === 'commit') pending.push(...this.commit(s).parents);
    }
    const queue = [b], seen = new Set<string>();
    for (let i = 0; i < queue.length; i++) {
      const s = queue[i];
      if (ancestors.has(s)) return s;
      if (seen.has(s)) continue;
      seen.add(s);
      if (this.objects.get(s)?.type === 'commit') queue.push(...this.commit(s).parents);
    }
  }
  reachable(tips: string[]): Set<string> {
    const result = new Set<string>(), pending = [...tips];
    while (pending.length) {
      const sha = pending.pop()!;
      if (result.has(sha) || sha === ZERO) continue;
      const o = this.get(sha);
      result.add(sha);
      if (o.type === 'commit') { const c = this.commit(sha); pending.push(c.tree, ...c.parents); }
      if (o.type === 'tree') for (const e of this.tree(sha).values()) if (e.mode !== '160000') pending.push(e.sha);
      if (o.type === 'tag') { const match = /^object ([0-9a-f]{40})/m.exec(o.data.toString()); if (match) pending.push(match[1]); }
    }
    return result;
  }
}

export function applyDelta(base: Buffer, delta: Buffer): Buffer {
  let at = 0;
  const byte = () => { if (at >= delta.length) throw new Error('Truncated delta'); return delta[at++]; };
  const size = () => {
    let n = 0, shift = 0, b: number;
    do { b = byte(); n += (b & 127) * 2 ** shift; shift += 7; if (shift > 49) throw new Error('Invalid delta size'); } while (b & 128);
    return n;
  };
  if (size() !== base.length) throw new Error('Delta base size mismatch');
  const length = size();
  if (length > MAX_OBJECT) throw new Error('Delta too large');
  const out = Buffer.alloc(length);
  let cursor = 0;
  while (at < delta.length) {
    const op = byte();
    if (op & 128) {
      let offset = 0, count = 0;
      for (let i = 0; i < 4; i++) if (op & (1 << i)) offset += byte() * 2 ** (8 * i);
      for (let i = 0; i < 3; i++) if (op & (16 << i)) count += byte() * 2 ** (8 * i);
      if (count === 0) count = 0x10000;
      if (offset + count > base.length || cursor + count > length) throw new Error('Delta copy out of bounds');
      base.copy(out, cursor, offset, offset + count); cursor += count;
    } else {
      if (!op || at + op > delta.length || cursor + op > length) throw new Error('Invalid delta insert');
      delta.copy(out, cursor, at, at + op); at += op; cursor += op;
    }
  }
  if (cursor !== length) throw new Error('Delta result size mismatch');
  return out;
}

/** Parse a pack transactionally; thin ref-deltas can refer to existing loose objects. */
export function readPack(db: ObjectDB, pack: Buffer): string[] {
  if (pack.length < 32 || pack.subarray(0, 4).toString() !== 'PACK') throw new Error('Invalid pack header');
  const version = pack.readUInt32BE(4), count = pack.readUInt32BE(8);
  if ((version !== 2 && version !== 3) || count > 1000000) throw new Error('Unsupported pack');
  if (hash(pack.subarray(0, -20)) !== pack.subarray(-20).toString('hex')) throw new Error('Pack checksum mismatch');
  type Entry = { offset: number; type: number; data: Buffer; baseOffset?: number; baseSha?: string; sha?: string; object?: GitObject };
  const entries: Entry[] = [], offsets = new Map<number, Entry>(), resolved = new Map<string, GitObject>();
  let at = 12;
  const byte = () => { if (at >= pack.length - 20) throw new Error('Truncated pack'); return pack[at++]; };
  for (let i = 0; i < count; i++) {
    const offset = at;
    let b = byte(), size = b & 15, shift = 4;
    const type = (b >> 4) & 7;
    while (b & 128) { b = byte(); size += (b & 127) * 2 ** shift; shift += 7; if (shift > 49) throw new Error('Pack object too large'); }
    if (size > MAX_OBJECT || (!types[type] && type !== 6 && type !== 7)) throw new Error('Invalid pack object');
    const entry: Entry = { offset, type, data: Buffer.alloc(0) };
    if (type === 6) {
      b = byte(); let distance = b & 127;
      while (b & 128) { b = byte(); distance = (distance + 1) * 128 + (b & 127); }
      entry.baseOffset = offset - distance;
      if (!offsets.has(entry.baseOffset)) throw new Error('Invalid ofs-delta base');
    }
    if (type === 7) { if (at + 20 > pack.length - 20) throw new Error('Truncated ref-delta'); entry.baseSha = pack.subarray(at, at + 20).toString('hex'); at += 20; }
    const inflated = inflateSync(pack.subarray(at, -20), { info: true, maxOutputLength: MAX_OBJECT }) as unknown as { buffer: Buffer; engine: { bytesWritten: number } };
    entry.data = inflated.buffer; at += inflated.engine.bytesWritten;
    if (entry.data.length !== size) throw new Error('Pack object size mismatch');
    entries.push(entry); offsets.set(offset, entry);
  }
  if (at !== pack.length - 20) throw new Error('Trailing pack data');
  let remaining = entries.length;
  while (remaining) {
    let progress = false;
    for (const e of entries) {
      if (e.object) continue;
      let object: GitObject;
      if (types[e.type]) object = { type: types[e.type], data: e.data };
      else {
        const base = e.baseOffset !== undefined ? offsets.get(e.baseOffset)?.object : resolved.get(e.baseSha!) ?? db.objects.get(e.baseSha!);
        if (!base) continue;
        object = { type: base.type, data: applyDelta(base.data, e.data) };
      }
      e.object = object;
      e.sha = hash(Buffer.concat([Buffer.from(`${object.type} ${object.data.length}\0`), object.data]));
      resolved.set(e.sha, object); remaining--; progress = true;
    }
    if (!progress) throw new Error('Unresolved delta base');
  }
  for (const [sha, object] of resolved) db.objects.set(sha, object);
  return entries.map(e => e.sha!);
}

export function writePack(db: ObjectDB, shas: Iterable<string>): Buffer {
  const unique = [...new Set(shas)], header = Buffer.alloc(12);
  header.write('PACK'); header.writeUInt32BE(2, 4); header.writeUInt32BE(unique.length, 8);
  const chunks = [header];
  for (const sha of unique) {
    const o = db.get(sha), bytes: number[] = [];
    let size = o.data.length, b = (codes[o.type] << 4) | (size & 15);
    size = Math.floor(size / 16);
    if (size) b |= 128;
    bytes.push(b);
    while (size) { b = size & 127; size = Math.floor(size / 128); bytes.push(b | (size ? 128 : 0)); }
    chunks.push(Buffer.from(bytes), deflateSync(o.data, { level: 1 }));
  }
  const data = Buffer.concat(chunks);
  return Buffer.concat([data, Buffer.from(hash(data), 'hex')]);
}
