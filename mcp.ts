import type { IncomingMessage, ServerResponse } from 'node:http';
import { readBody, sendJson, originOf } from './http.ts';
import { Store, HttpError, type RecordData } from './store.ts';

const VERSIONS = ['2026-07-28', '2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'];
const specs: [string, string, Record<string, string>, string[]][] = [
  ['create_repo', 'Create a repository owned by the authenticated user.', { name: 'string', description: 'string', private: 'boolean', auto_init: 'boolean' }, ['name']],
  ['get_repo', 'Get a repository by owner and repository name.', { owner: 'string', repo: 'string' }, ['owner', 'repo']],
  ['create_issue', 'Create an open issue in a repository.', { owner: 'string', repo: 'string', title: 'string', body: 'string', labels: 'array' }, ['owner', 'repo', 'title']],
  ['list_issues', 'List repository issues, optionally filtered by open, closed, or all state.', { owner: 'string', repo: 'string', state: 'string', per_page: 'integer', page: 'integer' }, ['owner', 'repo']],
  ['create_pull_request', 'Open a pull request from an existing head branch into an existing base branch.', { owner: 'string', repo: 'string', title: 'string', body: 'string', head: 'string', base: 'string' }, ['owner', 'repo', 'title', 'head', 'base']],
  ['merge_pull_request', 'Squash and merge a conflict-free pull request, closing it and updating its base branch.', { owner: 'string', repo: 'string', pull_number: 'integer', merge_method: 'string' }, ['owner', 'repo', 'pull_number']],
  ['get_issue', 'Read an issue by its repository-local issue number.', { owner: 'string', repo: 'string', issue_number: 'integer' }, ['owner', 'repo', 'issue_number']],
  ['get_pull_request', 'Read a pull request with its head, base, and merge status.', { owner: 'string', repo: 'string', pull_number: 'integer' }, ['owner', 'repo', 'pull_number']],
  ['list_repos', 'List repositories owned by the authenticated user.', { per_page: 'integer', page: 'integer' }, []],
  ['get_file_contents', 'Read a file or directory from a repository branch.', { owner: 'string', repo: 'string', path: 'string', ref: 'string' }, ['owner', 'repo', 'path']],
  ['create_or_update_file', 'Create or update a base64-encoded file, committing it to a repository branch.', { owner: 'string', repo: 'string', path: 'string', content: 'string', message: 'string', sha: 'string', branch: 'string' }, ['owner', 'repo', 'path', 'content', 'message']],
];
const TOOLS = specs.map(([name, description, properties, required]) => ({ name, description,
  inputSchema: { type: 'object', properties: Object.fromEntries(Object.entries(properties).map(([key, type]) => [key, { type, description: `${key.replaceAll('_', ' ')} argument`, ...(type === 'array' ? { items: { type: 'string' } } : {}) }])), required, additionalProperties: false } }));
const rpcError = (id: any, code: number, message: string) => ({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });
function callTool(store: Store, name: string, a: RecordData, origin: string): any {
  if (name === 'create_repo') return store.repoJSON(store.createRepo(a), origin);
  if (name === 'list_repos') return [...store.repos.values()].map(r => store.repoJSON(r, origin));
  const r = store.repo(a.owner, a.repo), full_name = `${r.owner}/${r.name}`;
  switch (name) {
    case 'get_repo': return store.repoJSON(r, origin);
    case 'create_issue': return { ...store.createIssue(r, a), full_name };
    case 'list_issues': {
      const state = String(a.state ?? 'open').toLowerCase();
      return [...r.issues.values()].filter(i => state === 'all' || i.state === state).sort((x, y) => y.number - x.number).map(i => ({ ...i, full_name }));
    }
    case 'create_pull_request': return { ...store.createPull(r, a), full_name };
    case 'merge_pull_request': return { ...store.mergePull(r, Number(a.pull_number), a), full_name };
    case 'get_issue': { const issue = r.issues.get(Number(a.issue_number)); if (!issue) throw new HttpError(404, 'Not Found'); return { ...issue, full_name }; }
    case 'get_pull_request': return { ...store.pull(r, Number(a.pull_number)), full_name };
    case 'get_file_contents': return store.content(r, a.path, a.ref);
    case 'create_or_update_file': return store.writeContent(r, a.path, a);
    default: throw new HttpError(422, 'Unknown tool');
  }
}
function dispatch(store: Store, request: any, origin: string): any {
  if (!request || typeof request !== 'object' || Array.isArray(request) || request.jsonrpc !== '2.0' || typeof request.method !== 'string') return rpcError(request?.id, -32600, 'Invalid Request');
  const { id, method } = request, params = request.params ?? {};
  const result = (result: any) => ({ jsonrpc: '2.0', id: id ?? null, result });
  if (method === 'server/discover') return result({ name: 'makefun-github-forge', version: '1.0.0', supportedVersions: VERSIONS, capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'makefun-github-forge', version: '1.0.0' } });
  if (method === 'initialize') return result({ protocolVersion: VERSIONS.includes(params.protocolVersion) ? params.protocolVersion : VERSIONS[0], capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'makefun-github-forge', version: '1.0.0' }, instructions: 'Repository, issue, and pull request tools share the REST and git store.' });
  if (method.startsWith('notifications/')) return undefined;
  if (method === 'ping') return result({});
  if (method === 'tools/list') return result({ tools: TOOLS });
  if (method !== 'tools/call') return rpcError(id, -32601, `Unknown method: ${method}`);
  const tool = TOOLS.find(tool => tool.name.toLowerCase() === String(params.name ?? '').toLowerCase());
  if (!tool) return rpcError(id, -32602, `Unknown tool: ${params.name ?? ''}`);
  const args = params.arguments ?? {};
  if (!args || typeof args !== 'object' || Array.isArray(args)) return rpcError(id, -32602, 'Tool arguments must be an object');
  for (const key of tool.inputSchema.required) if (!(key in args)) return rpcError(id, -32602, `Missing required argument: ${key}`);
  for (const [key, value] of Object.entries(args)) {
    const spec = tool.inputSchema.properties[key];
    if (!spec) continue;
    if (spec.type === 'integer' ? !Number.isInteger(value) || Number(value) < 1 : spec.type === 'array' ? !Array.isArray(value) : typeof value !== spec.type) return rpcError(id, -32602, `Invalid argument: ${key}`);
    if (tool.inputSchema.required.includes(key) && typeof value === 'string' && !value.length) return rpcError(id, -32602, `Missing required argument: ${key}`);
  }
  try {
    const output = callTool(store, tool.name, args, origin);
    return result({ content: [{ type: 'text', text: JSON.stringify(output) }], structuredContent: Array.isArray(output) ? { items: output } : output, isError: false });
  } catch (error) {
    return result({ content: [{ type: 'text', text: JSON.stringify({ message: error instanceof Error ? error.message : String(error) }) }], isError: true });
  }
}
export async function handleMcp(req: IncomingMessage, res: ServerResponse, store: Store): Promise<void> {
  if (req.method === 'DELETE') { sendJson(res, 204, undefined); return; }
  if (req.method !== 'POST') { sendJson(res, 405, { message: 'Use POST for MCP requests' }, { allow: 'POST, DELETE' }); return; }
  let request: any;
  try { request = JSON.parse((await readBody(req, 16 * 1024 * 1024)).toString()); }
  catch { sendJson(res, 400, rpcError(null, -32700, 'Parse error')); return; }
  const origin = originOf(req);
  if (Array.isArray(request)) {
    if (!request.length) { sendJson(res, 400, rpcError(null, -32600, 'Invalid Request')); return; }
    const results = request.map(item => dispatch(store, item, origin)).filter(item => item !== undefined);
    sendJson(res, results.length ? 200 : 202, results.length ? results : undefined); return;
  }
  const response = dispatch(store, request, origin);
  sendJson(res, response === undefined ? 202 : 200, response);
}
