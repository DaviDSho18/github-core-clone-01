import type { IncomingMessage, ServerResponse } from 'node:http';
import { Store, HttpError, requireValue, type RecordData, type Repository } from './store.ts';
import { readJson, sendJson, paginate, originOf } from './http.ts';

export async function handleRest(req: IncomingMessage, res: ServerResponse, url: URL, store: Store): Promise<boolean> {
  if (!url.pathname.startsWith('/api/v3')) return false;
  const path = url.pathname.slice(7).replace(/\/$/, '') || '/', method = req.method ?? 'GET', origin = originOf(req);
  const send = (status: number, value: unknown) => sendJson(res, status, value);
  const list = (items: any[]) => send(200, paginate(items, url, res));
  const body = async (): Promise<RecordData> => {
    const value = await readJson(req); requireValue(value && typeof value === 'object' && !Array.isArray(value), 'Expected a JSON object', 400); return value;
  };
  if (method === 'GET' && path === '/') {
    res.setHeader('x-oauth-scopes', 'repo, read:org');
    send(200, { current_user_url: `${origin}/api/v3/user`, repository_url: `${origin}/api/v3/repos/{owner}/{repo}`, rate_limit_url: `${origin}/api/v3/rate_limit` }); return true;
  }
  if (method === 'GET' && path === '/meta') { send(200, { installed_version: 'makefun-forge-1.0', verifiable_password_authentication: true }); return true; }
  if (method === 'GET' && (path === '/user' || path === `/users/${store.login}`)) { send(200, store.user()); return true; }
  if (method === 'GET' && path === '/rate_limit') { const rate = { limit: 500000, remaining: 500000, reset: 4102444800, used: 0 }; send(200, { resources: { core: rate, graphql: rate, search: rate }, rate }); return true; }
  if (path === '/user/repos' || path === `/users/${store.login}/repos`) {
    if (method === 'POST' && path === '/user/repos') { send(201, store.repoJSON(store.createRepo(await body()), origin)); return true; }
    if (method === 'GET') {
      let repos = [...store.repos.values()];
      const sort = url.searchParams.get('sort');
      if (sort === 'full_name') repos.sort((a, b) => a.name.localeCompare(b.name));
      if (url.searchParams.get('direction') === 'desc') repos.reverse();
      list(repos.map(r => store.repoJSON(r, origin))); return true;
    }
  }
  if (method === 'GET' && path === '/search/issues') {
    const q = url.searchParams.get('q') ?? '', repoMatch = /repo:([^\s/]+)\/([^\s]+)/.exec(q);
    const repos = repoMatch ? [store.repo(repoMatch[1], repoMatch[2])] : [...store.repos.values()];
    let items = repos.flatMap(r => q.includes('is:pr') ? [...r.pulls.values()] : [...r.issues.values()]);
    if (q.includes('is:open')) items = items.filter(i => i.state === 'open');
    if (q.includes('is:closed')) items = items.filter(i => i.state === 'closed');
    send(200, { total_count: items.length, incomplete_results: false, items: paginate(items, url, res) }); return true;
  }
  const match = /^\/repos\/([^/]+)\/([^/]+)(.*)$/.exec(path);
  if (!match) throw new HttpError(404, 'Not Found');
  const r = store.repo(decodeURIComponent(match[1]), decodeURIComponent(match[2])), tail = match[3];
  if (!tail) {
    if (method === 'GET') send(200, store.repoJSON(r, origin));
    else if (method === 'PATCH') {
      const b = await body();
      if (b.description !== undefined) { requireValue(typeof b.description === 'string' || b.description === null, 'Invalid description'); r.description = b.description ?? ''; }
      if (b.default_branch !== undefined) { requireValue(typeof b.default_branch === 'string', 'Invalid branch'); r.defaultBranch = b.default_branch; }
      if (typeof b.private === 'boolean') r.private = b.private;
      if (b.name !== undefined && b.name !== r.name) {
        requireValue(typeof b.name === 'string' && /^[\w.-]+$/.test(b.name), 'Invalid repository name');
        requireValue(!store.find(r.owner, b.name), 'Repository already exists');
        store.repos.delete(store.key(r.owner, r.name)); r.name = b.name; store.repos.set(store.key(r.owner, r.name), r);
      }
      send(200, store.repoJSON(r, origin));
    } else if (method === 'DELETE') { store.repos.delete(store.key(r.owner, r.name)); send(204, undefined); }
    else throw new HttpError(404, 'Not Found');
    return true;
  }
  const contents = /^\/contents(?:\/(.*))?$/.exec(tail);
  if (contents) {
    const file = decodeURIComponent(contents[1] ?? '');
    if (method === 'GET') send(200, store.content(r, file, url.searchParams.get('ref') ?? undefined));
    else if (method === 'PUT' || method === 'DELETE') { const result = store.writeContent(r, file, await body(), method === 'DELETE'); send(method === 'DELETE' ? 200 : result.created ? 201 : 200, { content: result.content, commit: result.commit }); }
    else throw new HttpError(404, 'Not Found');
    return true;
  }
  if (tail === '/labels') {
    if (method === 'POST') send(201, store.label(r, await body()));
    else if (method === 'GET') list([...r.labels.values()]);
    else throw new HttpError(404, 'Not Found');
    return true;
  }
  const label = /^\/labels\/(.+)$/.exec(tail);
  if (label) {
    const name = decodeURIComponent(label[1]), value = r.labels.get(name);
    if (!value) throw new HttpError(404, 'Not Found');
    if (method === 'DELETE') { r.labels.delete(name); send(204, undefined); }
    else if (method === 'PATCH') { const b = await body(); r.labels.delete(name); Object.assign(value, b); r.labels.set(value.name, value); send(200, value); }
    else if (method === 'GET') send(200, value); else throw new HttpError(404, 'Not Found');
    return true;
  }
  if (tail === '/issues') {
    if (method === 'POST') send(201, store.createIssue(r, await body()));
    else if (method === 'GET') {
      const state = url.searchParams.get('state') ?? 'open';
      let items = [...r.issues.values(), ...r.pulls.values().map(p => ({ ...p, pull_request: { url: p.url } }))].filter(i => state === 'all' || i.state === state);
      const labels = url.searchParams.get('labels')?.split(',');
      if (labels) items = items.filter(i => labels.every(l => i.labels.some((x: any) => x.name === l)));
      items.sort((a, b) => b.number - a.number); if (url.searchParams.get('direction') === 'asc') items.reverse();
      list(items);
    } else throw new HttpError(404, 'Not Found');
    return true;
  }
  const issue = /^\/issues\/(\d+)(?:\/(comments|labels))?$/.exec(tail);
  if (issue) {
    const number = Number(issue[1]), value = r.issues.get(number) ?? r.pulls.get(number);
    if (!value) throw new HttpError(404, 'Not Found');
    if (issue[2] === 'comments') {
      if (method === 'GET') list(r.comments.get(number) ?? []);
      else if (method === 'POST') {
        const b = await body(); requireValue(typeof b.body === 'string', 'body is required');
        const id = store.nextId(), comment = { id, node_id: `IC_${id}`, body: b.body, user: store.user(), created_at: store.now(), updated_at: store.now(), html_url: `${value.html_url}#issuecomment-${id}` };
        const comments = r.comments.get(number) ?? []; comments.push(comment); r.comments.set(number, comments); value.comments = comments.length; send(201, comment);
      } else throw new HttpError(404, 'Not Found');
    } else if (issue[2] === 'labels') {
      if (method === 'GET') send(200, value.labels);
      else if (method === 'POST' || method === 'PUT') { const b = await body(); const labels = store.labels(r, b.labels ?? b); value.labels = method === 'PUT' ? labels : [...new Map([...value.labels, ...labels].map((l: any) => [l.name, l])).values()]; send(200, value.labels); }
      else if (method === 'DELETE') { value.labels = []; send(204, undefined); } else throw new HttpError(404, 'Not Found');
    } else if (method === 'GET') send(200, value);
    else if (method === 'PATCH') {
      const b = await body(); if (b.labels) value.labels = store.labels(r, b.labels);
      for (const key of ['title', 'body']) if (typeof b[key] === 'string') value[key] = b[key];
      if (b.state !== undefined) { requireValue(b.state === 'open' || b.state === 'closed', 'Invalid state'); value.state = b.state; value.closed_at = b.state === 'closed' ? store.now() : null; }
      value.updated_at = store.now(); send(200, value);
    } else throw new HttpError(404, 'Not Found');
    return true;
  }
  if (tail === '/pulls') {
    if (method === 'POST') send(201, store.createPull(r, await body()));
    else if (method === 'GET') {
      const state = url.searchParams.get('state') ?? 'open';
      let pulls = [...r.pulls.values()].map(p => store.refreshPull(r, p)).filter(p => state === 'all' || p.state === state);
      if (url.searchParams.has('head')) pulls = pulls.filter(p => p.head.ref === url.searchParams.get('head')?.split(':').pop());
      if (url.searchParams.has('base')) pulls = pulls.filter(p => p.base.ref === url.searchParams.get('base'));
      list(pulls.sort((a, b) => b.number - a.number));
    } else throw new HttpError(404, 'Not Found');
    return true;
  }
  const pull = /^\/pulls\/(\d+)(?:\/(merge|files|commits|reviews|comments))?$/.exec(tail);
  if (pull) {
    const p = store.pull(r, Number(pull[1]));
    if (pull[2] === 'merge' && method === 'PUT') send(200, store.mergePull(r, p.number, await body()));
    else if (pull[2] === 'merge' && method === 'GET') { if (!p.merged) throw new HttpError(404, 'Not Found'); send(204, undefined); }
    else if (pull[2] === 'files' && method === 'GET') list(store.pullFiles(r, p));
    else if (pull[2] === 'commits' && method === 'GET') list(commitList(store, r, p.head.sha).filter(c => !r.db.isAncestor(c.sha, p.base.sha)));
    else if ((pull[2] === 'reviews' || pull[2] === 'comments') && method === 'GET') send(200, []);
    else if (!pull[2] && method === 'GET') send(200, p);
    else if (!pull[2] && method === 'PATCH') { const b = await body(); for (const k of ['title', 'body', 'state']) if (b[k] !== undefined) p[k] = b[k]; send(200, p); }
    else throw new HttpError(404, 'Not Found');
    return true;
  }
  if (tail === '/branches' && method === 'GET') { list([...r.refs].filter(([ref]) => ref.startsWith('refs/heads/')).map(([ref, sha]) => ({ name: ref.slice(11), commit: { sha, url: '' }, protected: false }))); return true; }
  const branch = /^\/branches\/(.+)$/.exec(tail);
  if (branch && method === 'GET') { const name = decodeURIComponent(branch[1]), sha = r.refs.get(`refs/heads/${name}`); if (!sha) throw new HttpError(404, 'Not Found'); send(200, { name, commit: store.commitJSON(r, sha), protected: false }); return true; }
  if (tail === '/commits' && method === 'GET') { const sha = store.resolve(r, url.searchParams.get('sha') ?? undefined); list(sha ? commitList(store, r, sha) : []); return true; }
  const commit = /^\/commits\/(.+)$/.exec(tail);
  if (commit && method === 'GET') { const sha = store.resolve(r, decodeURIComponent(commit[1])); if (!sha) throw new HttpError(404, 'Not Found'); send(200, { ...store.commitJSON(r, sha), files: [] }); return true; }
  if ((tail === '/git/refs' || tail === '/git/refs/') && method === 'POST') {
    const b = await body(); requireValue(typeof b.ref === 'string' && b.ref.startsWith('refs/') && r.db.objects.has(b.sha), 'Invalid ref'); requireValue(!r.refs.has(b.ref), 'Reference already exists'); r.refs.set(b.ref, b.sha); send(201, refJSON(b.ref, b.sha)); return true;
  }
  const refs = /^\/git\/(refs|ref|matching-refs)(?:\/(.*))?$/.exec(tail);
  if (refs) {
    const name = refs[2] ? `refs/${decodeURIComponent(refs[2])}` : 'refs/';
    if (refs[1] === 'matching-refs' || name === 'refs/') { if (method !== 'GET') throw new HttpError(404, 'Not Found'); list([...r.refs].filter(([ref]) => ref.startsWith(name)).map(([ref, sha]) => refJSON(ref, sha))); return true; }
    const sha = r.refs.get(name); if (!sha) throw new HttpError(404, 'Not Found');
    if (method === 'GET') send(200, refJSON(name, sha));
    else if (method === 'DELETE') { r.refs.delete(name); send(204, undefined); }
    else if (method === 'PATCH') { const b = await body(); requireValue(r.db.objects.has(b.sha), 'Invalid sha'); requireValue(r.db.isAncestor(sha, b.sha), 'Update is not a fast forward'); r.refs.set(name, b.sha); send(200, refJSON(name, b.sha)); }
    else throw new HttpError(404, 'Not Found'); return true;
  }
  const blob = /^\/git\/blobs(?:\/([0-9a-f]{40}))?$/.exec(tail);
  if (blob) {
    if (method === 'POST' && !blob[1]) { const b = await body(); requireValue(typeof b.content === 'string', 'content is required'); const sha = r.db.put('blob', Buffer.from(b.content, b.encoding === 'base64' ? 'base64' : 'utf8')); send(201, { sha, url: '' }); }
    else if (method === 'GET' && blob[1]) { const o = r.db.objects.get(blob[1]); if (!o) throw new HttpError(404, 'Not Found'); send(200, { sha: blob[1], size: o.data.length, content: o.data.toString('base64'), encoding: 'base64' }); }
    else throw new HttpError(404, 'Not Found'); return true;
  }
  if (tail === '/git/commits' && method === 'POST') {
    const b = await body(); requireValue(r.db.objects.get(b.tree)?.type === 'tree' && Array.isArray(b.parents), 'Invalid tree or parents');
    const timestamp = Math.floor(Date.parse(store.now()) / 1000), ident = `${store.login} <${store.login}@localhost> ${timestamp} +0000`;
    const sha = r.db.put('commit', `tree ${b.tree}\n${b.parents.map((p: string) => `parent ${p}\n`).join('')}author ${ident}\ncommitter ${ident}\n\n${b.message ?? ''}\n`);
    send(201, { sha, message: b.message, tree: { sha: b.tree }, parents: b.parents.map((sha: string) => ({ sha })) }); return true;
  }
  const tree = /^\/git\/trees(?:\/([0-9a-f]{40}))?$/.exec(tail);
  if (tree) {
    if (method === 'POST' && !tree[1]) {
      const b = await body(); requireValue(Array.isArray(b.tree), 'tree is required');
      const files = new Map<string, { mode: string; sha: string }>();
      const flatten = (sha: string, prefix = '') => { for (const [name, e] of r.db.tree(sha)) if (e.mode === '40000') flatten(e.sha, prefix + name + '/'); else files.set(prefix + name, e); };
      if (b.base_tree) flatten(b.base_tree);
      for (const item of b.tree) { if (item.sha === null) files.delete(item.path); else if (item.type === 'tree') flatten(item.sha, item.path + '/'); else files.set(item.path, { mode: item.mode ?? '100644', sha: typeof item.content === 'string' ? r.db.put('blob', item.content) : item.sha }); }
      const sha = r.db.writeTree(files); send(201, { sha, tree: [] });
    } else if (method === 'GET' && tree[1]) {
      const entries: any[] = [];
      const walk = (sha: string, prefix = '') => { for (const [name, e] of r.db.tree(sha)) { const dir = e.mode === '40000'; entries.push({ path: prefix + name, mode: e.mode.padStart(6, '0'), type: dir ? 'tree' : 'blob', sha: e.sha, size: dir ? undefined : r.db.get(e.sha).data.length }); if (dir && url.searchParams.has('recursive')) walk(e.sha, prefix + name + '/'); } };
      walk(tree[1]); send(200, { sha: tree[1], tree: entries, truncated: false });
    } else throw new HttpError(404, 'Not Found'); return true;
  }
  throw new HttpError(404, 'Not Found');
}
function refJSON(ref: string, sha: string): RecordData { return { ref, node_id: Buffer.from(ref).toString('base64'), url: '', object: { type: 'commit', sha, url: '' } }; }
function commitList(store: Store, r: Repository, sha: string): RecordData[] {
  const pending = [sha], seen = new Set<string>(), result: RecordData[] = [];
  while (pending.length) { const s = pending.shift()!; if (seen.has(s)) continue; seen.add(s); result.push(store.commitJSON(r, s)); pending.push(...r.db.commit(s).parents); }
  return result;
}
