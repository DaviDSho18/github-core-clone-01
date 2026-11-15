import { createHash } from 'node:crypto';
import { ObjectDB, ZERO, type TreeEntry } from './objects.ts';

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}
export function requireValue(condition: unknown, message: string, status = 422): asserts condition {
  if (!condition) throw new HttpError(status, message);
}
export type RecordData = Record<string, any>;
export type Repository = {
  id: number; owner: string; name: string; description: string; private: boolean; defaultBranch: string;
  created: string; db: ObjectDB; refs: Map<string, string>; labels: Map<string, RecordData>;
  issues: Map<number, RecordData>; pulls: Map<number, RecordData>; comments: Map<number, RecordData[]>; nextNumber: number;
};
/**
 * The login the bearer token owns.
 *
 * The trial's token is opaque (`shpat_` and 32 hex digits, minted per instance and identical on the
 * live pass and the replay), so the login cannot be read out of it: it is derived from it instead.
 * Deriving keeps the two properties the suite checks: the owner is stable across a trial and its
 * replay, and the same token always names the same login. A JWT that does carry a login is honoured
 * first, and MAKEFUN_SHOP_LOGIN overrides both when a run wants a fixed owner.
 */
export function tokenLogin(): string {
  const override = process.env.MAKEFUN_SHOP_LOGIN;
  if (typeof override === 'string' && override !== '') return override;
  const token = process.env.MAKEFUN_SHOP_TOKEN ?? '';
  try {
    const data = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
    if (typeof (data.login ?? data.username) === 'string') return data.login ?? data.username;
  } catch { /* An opaque token is the normal case. */ }
  if (token === '') return 'makefun';
  const digest = createHash('sha256').update(`makefun/entry-login/v1\n${token}`).digest('hex');
  return `user-${digest.slice(0, 12)}`;
}
export class Store {
  repos = new Map<string, Repository>();
  login = tokenLogin();
  sequence = 0;
  clock = 0;
  nextId(): number { return ++this.sequence; }
  now(): string { return new Date(1704067200000 + this.clock++ * 1000).toISOString(); }
  user(login = this.login): RecordData { return { login, id: 1, node_id: 'U_1', type: 'User', name: login,
    avatar_url: '', html_url: `/${login}`, url: `/api/v3/users/${login}`, site_admin: false }; }
  key(owner: string, name: string): string { return `${owner}/${name}`.toLowerCase(); }
  find(owner: string, name: string): Repository | undefined {
    if (owner.toLowerCase() !== this.login.toLowerCase()) return;
    return this.repos.get(this.key(owner, name));
  }
  repo(owner: string, name: string): Repository {
    const r = this.find(owner, name);
    if (!r) throw new HttpError(404, 'Not Found');
    return r;
  }
  byId(id: string | number): Repository {
    const repo = [...this.repos.values()].find(r => String(r.id) === String(id) || `R_${r.id}` === id);
    if (!repo) throw new HttpError(404, 'Could not resolve repository');
    return repo;
  }
  createRepo(input: RecordData): Repository {
    requireValue(typeof input.name === 'string' && /^[\w.-]+$/.test(input.name) && input.name !== '.' && input.name !== '..', 'Invalid repository name');
    requireValue(!this.find(this.login, input.name), 'Repository already exists');
    const repo: Repository = { id: this.nextId(), owner: this.login, name: input.name,
      description: input.description ?? '', private: input.private ?? false,
      defaultBranch: input.default_branch ?? 'main', created: this.now(), db: new ObjectDB(), refs: new Map(),
      labels: new Map(), issues: new Map(), pulls: new Map(), comments: new Map(), nextNumber: 1 };
    this.repos.set(this.key(repo.owner, repo.name), repo);
    if (input.auto_init) {
      const files = new Map([['README.md', { mode: '100644', sha: repo.db.put('blob', `# ${repo.name}\n`) }]]);
      this.commitFiles(repo, repo.defaultBranch, files, 'Initial commit');
    }
    return repo;
  }
  repoJSON(r: Repository, origin = ''): RecordData {
    const full = `${r.owner}/${r.name}`, api = `${origin}/api/v3/repos/${full}`, html = `${origin}/${full}`;
    return { id: r.id, node_id: `R_${r.id}`, name: r.name, full_name: full, owner: this.user(r.owner),
      private: r.private, description: r.description, fork: false, archived: false, disabled: false,
      html_url: html, url: api, clone_url: `${html}.git`, git_url: `${html}.git`, ssh_url: `${html}.git`,
      default_branch: r.defaultBranch, created_at: r.created, updated_at: r.created, pushed_at: r.created,
      size: r.db.objects.size, language: null, has_issues: true, has_projects: false, has_wiki: false,
      open_issues_count: [...r.issues.values(), ...r.pulls.values()].filter(i => i.state === 'open').length,
      forks_count: 0, stargazers_count: 0, watchers_count: 0, permissions: { admin: true, push: true, pull: true },
      contents_url: `${api}/contents/{+path}`, branches_url: `${api}/branches{/branch}`, commits_url: `${api}/commits{/sha}`,
      issues_url: `${api}/issues{/number}`, pulls_url: `${api}/pulls{/number}`, labels_url: `${api}/labels{/name}`, license: null };
  }
  resolve(r: Repository, ref?: string): string | undefined {
    ref ||= r.defaultBranch;
    return r.refs.get(ref) ?? r.refs.get(`refs/heads/${ref}`) ?? r.refs.get(`refs/tags/${ref}`) ?? (r.db.objects.has(ref) ? ref : undefined);
  }
  commitFiles(r: Repository, branch: string, files: Map<string, TreeEntry>, message: string, parents?: string[], author?: RecordData): string {
    const old = this.resolve(r, branch);
    const tree = r.db.writeTree(files), timestamp = Math.floor((1704067200000 + this.clock++ * 1000) / 1000);
    const clean = (s: unknown) => String(s).replace(/[\n\r<>]/g, '');
    const identity = `${clean(author?.name ?? this.login)} <${clean(author?.email ?? `${this.login}@localhost`)}> ${timestamp} +0000`;
    const sha = r.db.put('commit', `tree ${tree}\n${(parents ?? (old ? [old] : [])).map(p => `parent ${p}\n`).join('')}author ${identity}\ncommitter ${identity}\n\n${message}\n`);
    r.refs.set(branch.startsWith('refs/') ? branch : `refs/heads/${branch}`, sha);
    return sha;
  }
  commitJSON(r: Repository, sha: string): RecordData {
    const c = r.db.commit(sha), raw = r.db.get(sha).data.toString();
    const parse = (kind: string) => { const m = new RegExp(`^${kind} (.*) <(.*)> (\\d+) ([+-]\\d+)`, 'm').exec(raw);
      return { name: m?.[1] ?? this.login, email: m?.[2] ?? '', date: new Date(Number(m?.[3] ?? 1704067200) * 1000).toISOString() }; };
    return { sha, node_id: `C_${sha}`, url: `/api/v3/repos/${r.owner}/${r.name}/commits/${sha}`,
      html_url: `/${r.owner}/${r.name}/commit/${sha}`, commit: { message: c.message.trimEnd(), author: parse('author'), committer: parse('committer'), tree: { sha: c.tree }, comment_count: 0 },
      author: this.user(), committer: this.user(), parents: c.parents.map(s => ({ sha: s })), stats: { total: 0, additions: 0, deletions: 0 } };
  }
  content(r: Repository, path: string, ref?: string): RecordData | RecordData[] {
    const files = r.db.files(this.resolve(r, ref)), entry = files.get(path);
    const describe = (p: string, e: TreeEntry) => ({ type: 'file', encoding: 'base64', size: r.db.get(e.sha).data.length,
      name: p.split('/').pop(), path: p, sha: e.sha, url: `/api/v3/repos/${r.owner}/${r.name}/contents/${p}`,
      git_url: `/api/v3/repos/${r.owner}/${r.name}/git/blobs/${e.sha}`, html_url: `/${r.owner}/${r.name}/blob/${ref ?? r.defaultBranch}/${p}`,
      download_url: null, content: r.db.get(e.sha).data.toString('base64') });
    if (entry) return describe(path, entry);
    const prefix = path ? `${path.replace(/\/$/, '')}/` : '', listing = new Map<string, RecordData>();
    for (const [p, e] of files) if (p.startsWith(prefix)) {
      const name = p.slice(prefix.length).split('/')[0];
      if (!listing.has(name)) listing.set(name, p.slice(prefix.length).includes('/') ? { type: 'dir', name, path: prefix + name, sha: '', size: 0 } : describe(p, e));
    }
    if (!listing.size) throw new HttpError(404, 'Not Found');
    return [...listing.values()].sort((a, b) => a.name.localeCompare(b.name));
  }
  writeContent(r: Repository, path: string, input: RecordData, remove = false): { created: boolean; content: RecordData | null; commit: RecordData } {
    requireValue(path && !path.split('/').some(p => !p || p === '.' || p === '..') && !path.includes('\0'), 'Invalid path');
    requireValue(typeof input.message === 'string', 'message is required');
    const branch = input.branch ?? r.defaultBranch;
    requireValue(typeof branch === 'string' && (branch === r.defaultBranch || this.resolve(r, branch)), 'Branch not found', 404);
    const files = r.db.files(this.resolve(r, branch)), old = files.get(path);
    if (remove && !old) throw new HttpError(404, 'Not Found');
    if ((old && input.sha !== old.sha) || (!old && input.sha)) throw new HttpError(409, 'sha does not match the current blob');
    if (remove) files.delete(path);
    else {
      requireValue(typeof input.content === 'string' && /^[A-Za-z0-9+/=\r\n]*$/.test(input.content), 'content must be base64');
      files.set(path, { mode: old?.mode ?? '100644', sha: r.db.put('blob', Buffer.from(input.content, 'base64')) });
    }
    const sha = this.commitFiles(r, branch, files, input.message, undefined, input.author);
    return { created: !old, content: remove ? null : this.content(r, path, branch) as RecordData, commit: { ...this.commitJSON(r, sha).commit, sha } };
  }
  label(r: Repository, input: RecordData): RecordData {
    requireValue(typeof input.name === 'string' && input.name.length > 0, 'name is required');
    requireValue(!r.labels.has(input.name), 'Label already exists');
    const label = { id: this.nextId(), node_id: '', name: input.name, color: input.color ?? 'ededed', description: input.description ?? '', default: false };
    r.labels.set(label.name, label); return label;
  }
  labels(r: Repository, names: any): RecordData[] {
    requireValue(Array.isArray(names), 'labels must be an array');
    return names.map((value: any) => {
      const label = r.labels.get(typeof value === 'string' ? value : value?.name);
      requireValue(label, 'Validation Failed: label does not exist'); return label;
    });
  }
  createIssue(r: Repository, input: RecordData): RecordData {
    requireValue(typeof input.title === 'string' && input.title.length > 0, 'title is required');
    const labels = this.labels(r, input.labels ?? []), id = this.nextId(), number = r.nextNumber++, date = this.now();
    const issue = { id, node_id: `I_${id}`, number, title: input.title, body: input.body ?? '', state: 'open', state_reason: null,
      user: this.user(), assignee: null, assignees: [], labels, comments: 0, locked: false, created_at: date, updated_at: date, closed_at: null,
      url: `/api/v3/repos/${r.owner}/${r.name}/issues/${number}`, html_url: `/${r.owner}/${r.name}/issues/${number}` };
    r.issues.set(number, issue); return issue;
  }
  createPull(r: Repository, input: RecordData): RecordData {
    requireValue(typeof input.title === 'string' && input.title.length > 0, 'title is required');
    requireValue(typeof input.head === 'string' && typeof input.base === 'string', 'head and base are required');
    const head = input.head.includes(':') ? input.head.split(':').pop() : input.head;
    const headSha = this.resolve(r, head), baseSha = this.resolve(r, input.base);
    requireValue(headSha && baseSha && head !== input.base, 'Validation Failed: invalid head or base');
    const id = this.nextId(), number = r.nextNumber++, date = this.now();
    const branch = (ref: string, sha: string) => ({ label: `${r.owner}:${ref}`, ref, sha, user: this.user(), repo: this.repoJSON(r) });
    const pull: RecordData = { id, node_id: `PR_${id}`, number, title: input.title, body: input.body ?? '', state: 'open', user: this.user(),
      head: branch(head, headSha), base: branch(input.base, baseSha), draft: input.draft ?? false, merged: false, merged_at: null,
      merge_commit_sha: null, mergeable: true, mergeable_state: 'clean', maintainer_can_modify: true,
      created_at: date, updated_at: date, closed_at: null, labels: [], assignees: [], comments: 0, commits: 1,
      url: `/api/v3/repos/${r.owner}/${r.name}/pulls/${number}`, html_url: `/${r.owner}/${r.name}/pull/${number}` };
    r.pulls.set(number, pull); this.refreshPull(r, pull); return pull;
  }
  mergeFiles(r: Repository, pull: RecordData): Map<string, TreeEntry> | null {
    const head = this.resolve(r, pull.head.ref), base = this.resolve(r, pull.base.ref);
    if (!head || !base) return null;
    const common = r.db.mergeBase(base, head);
    if (!common) return null;
    const ancestor = r.db.files(common), ours = r.db.files(base), theirs = r.db.files(head), merged = new Map(ours);
    const equal = (a?: TreeEntry, b?: TreeEntry) => a?.sha === b?.sha && a?.mode === b?.mode;
    for (const path of new Set([...ancestor.keys(), ...ours.keys(), ...theirs.keys()])) {
      const a = ancestor.get(path), b = ours.get(path), h = theirs.get(path);
      if (equal(a, h)) continue;
      if (!equal(a, b) && !equal(b, h)) return null;
      if (h) merged.set(path, h); else merged.delete(path);
    }
    return merged;
  }
  refreshPull(r: Repository, pull: RecordData): RecordData {
    if (!pull.merged) {
      pull.head.sha = this.resolve(r, pull.head.ref) ?? pull.head.sha;
      pull.base.sha = this.resolve(r, pull.base.ref) ?? pull.base.sha;
      pull.mergeable = this.mergeFiles(r, pull) !== null;
      pull.mergeable_state = pull.mergeable ? 'clean' : 'dirty';
    }
    return pull;
  }
  pull(r: Repository, number: number): RecordData {
    const p = r.pulls.get(number); if (!p) throw new HttpError(404, 'Not Found'); return this.refreshPull(r, p);
  }
  mergePull(r: Repository, number: number, input: RecordData = {}): RecordData {
    const pull = this.pull(r, number);
    if (pull.merged) return { sha: pull.merge_commit_sha, merged: true, message: 'Pull Request already merged' };
    requireValue(pull.state === 'open', 'Pull Request is not open', 405);
    if (input.sha && input.sha !== pull.head.sha) throw new HttpError(409, 'Head branch was modified');
    const files = this.mergeFiles(r, pull);
    if (!files) throw new HttpError(405, 'Pull Request is not mergeable');
    const sha = this.commitFiles(r, pull.base.ref, files, input.commit_title ?? `${pull.title} (#${number})`);
    pull.state = 'closed'; pull.merged = true; pull.merge_commit_sha = sha; pull.merged_at = this.now(); pull.closed_at = pull.merged_at;
    return { sha, merged: true, message: 'Pull Request successfully merged' };
  }
  pullFiles(r: Repository, p: RecordData): RecordData[] {
    const head = this.resolve(r, p.head.ref), base = this.resolve(r, p.base.ref);
    const before = r.db.files(head && base ? r.db.mergeBase(base, head) ?? base : base), after = r.db.files(head);
    return [...new Set([...before.keys(), ...after.keys()])].filter(path => before.get(path)?.sha !== after.get(path)?.sha).sort().map(path => ({
      sha: after.get(path)?.sha ?? before.get(path)!.sha, filename: path, status: !after.has(path) ? 'removed' : !before.has(path) ? 'added' : 'modified',
      additions: 0, deletions: 0, changes: 0, patch: '', blob_url: `/${r.owner}/${r.name}/blob/${head}/${path}` }));
  }
}
