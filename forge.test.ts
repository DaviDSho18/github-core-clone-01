import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { deflateSync } from 'node:zlib';
import { createApp } from './app.ts';
import { Store } from './store.ts';
import { ObjectDB, readPack, writePack, ZERO, hash } from './objects.ts';
import { executeGraphQL } from './graphql.ts';
import { pkt, packets, FLUSH } from './git.ts';

process.env.MAKEFUN_SHOP_TOKEN = 'test-token';
process.env.MAKEFUN_SHOP_LOGIN = 'tester';

test('loose objects, pack roundtrip, ofs-delta and forward ref-delta', () => {
  const db = new ObjectDB(), sha = db.put('blob', 'hello');
  assert.equal(new ObjectDB().readLoose(db.loose(sha)), sha);
  const second = new ObjectDB();
  assert.deepEqual(readPack(second, writePack(db, [sha])), [sha]);
  assert.equal(second.get(sha).data.toString(), 'hello');
  const delta = Buffer.from([5, 6, 0x90, 5, 1, 33]); // copy hello, insert !
  const base = Buffer.concat([Buffer.from([0x35]), deflateSync(Buffer.from('hello'))]);
  const deltaHeader = Buffer.from([0x66, base.length]);
  const header = Buffer.alloc(12); header.write('PACK'); header.writeUInt32BE(2, 4); header.writeUInt32BE(2, 8);
  const raw = Buffer.concat([header, base, deltaHeader, deflateSync(delta)]);
  const pack = Buffer.concat([raw, createHash('sha1').update(raw).digest()]);
  const third = new ObjectDB(); readPack(third, pack);
  const next = third.put('blob', 'hello!'); assert.equal(third.get(next).data.toString(), 'hello!');
  const forward = Buffer.concat([header, Buffer.from([0x76]), Buffer.from(sha, 'hex'), deflateSync(delta), base]);
  const fourth = new ObjectDB(); readPack(fourth, Buffer.concat([forward, Buffer.from(hash(forward), 'hex')]));
  assert.equal(fourth.get(next).data.toString(), 'hello!');
  const broken = Buffer.from(pack); broken[broken.length - 1] ^= 1;
  assert.throws(() => readPack(new ObjectDB(), broken), /checksum/);
});

test('REST, GraphQL, MCP and git share state and handle errors', async () => {
  const server = createApp(); server.listen(0, '127.0.0.1');
  await new Promise<void>(resolve => server.once('listening', resolve));
  const address = server.address() as { port: number }, base = `http://127.0.0.1:${address.port}`;
  const headers = { authorization: 'Bearer test-token', 'content-type': 'application/json' };
  const api = async (path: string, method = 'GET', body?: any, status = 200): Promise<any> => {
    const res = await fetch(base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    assert.equal(res.status, status, `${method} ${path}: ${text}`);
    assert.match(res.headers.get('content-type')!, /application\/json/);
    assert.ok(res.headers.get('x-github-request-id'));
    return text ? JSON.parse(text) : undefined;
  };
  try {
    assert.equal((await fetch(base + '/api/v3/user')).status, 401);
    assert.equal((await fetch(base + '/api/graphql', { method: 'POST', body: '{}' })).status, 401);
    await api('/api/v3/nope', 'GET', undefined, 404);
    const malformed = await fetch(base + '/api/v3/user/repos', { method: 'POST', headers, body: '{' }); assert.equal(malformed.status, 400);
    const r = await api('/api/v3/user/repos', 'POST', { name: 'demo', auto_init: true }, 201);
    assert.equal(r.owner.login, 'tester');
    await api('/api/v3/repos/other/demo', 'GET', undefined, 404);
    await api('/api/v3/repos/tester/demo', 'PATCH', { description: 'changed' });
    const content = await api('/api/v3/repos/tester/demo/contents/dir/file.txt', 'PUT', { message: 'create', content: Buffer.from('first').toString('base64') }, 201);
    await api('/api/v3/repos/tester/demo/contents/dir/file.txt', 'PUT', { message: 'stale', content: '', sha: ZERO }, 409);
    const updated = await api('/api/v3/repos/tester/demo/contents/dir/file.txt', 'PUT', { message: 'update', content: Buffer.from('second').toString('base64'), sha: content.content.sha });
    assert.equal((await api('/api/v3/repos/tester/demo/contents/dir')).length, 1);
    await api('/api/v3/repos/tester/demo/contents/dir/file.txt', 'DELETE', { message: 'delete', sha: updated.content.sha });
    await api('/api/v3/repos/tester/demo/contents/dir/file.txt', 'GET', undefined, 404);
    await api('/api/v3/repos/tester/demo/issues', 'POST', { title: 'bad', labels: ['missing'] }, 422);
    await api('/api/v3/repos/tester/demo/labels', 'POST', { name: 'bug' }, 201);
    const i = await api('/api/v3/repos/tester/demo/issues', 'POST', { title: 'one', labels: ['bug'] }, 201);
    await api('/api/v3/repos/tester/demo/issues', 'POST', { title: 'two' }, 201);
    await api('/api/v3/repos/tester/demo/issues/1', 'PATCH', { state: 'closed' });
    assert.ok(Array.isArray(await api('/api/v3/repos/tester/demo/issues')));
    const gql = (query: string, variables?: any) => api('/api/graphql', 'POST', { query, variables });
    const q = await gql('query($owner:String!, $name:String!){repository(owner:$owner,name:$name){id issues(first:10,states:[OPEN],orderBy:{field:CREATED_AT,direction:DESC}){nodes{number title state}}}}', { owner: 'tester', name: 'demo' });
    assert.deepEqual(q.data.repository.issues.nodes.map((i: any) => i.number), [2]);
    assert.equal((await gql('{repository(owner:"tester",name:"missing"){name}}')).data.repository, null);
    const unknown = await gql('{repository(owner:"tester",name:"demo"){wrongField}}'); assert.match(unknown.errors[0].message, /wrongField/);
    const batch = await api('/api/graphql', 'POST', [{ query: '{viewer{login}}' }, { query: '{__type(name:"Issue"){fields{name}}}' }]);
    assert.equal(batch.length, 2); assert.equal(batch[0].data.viewer.login, 'tester');
    assert.ok(batch[1].data.__type.fields.some((f: any) => f.name === 'title'));
    const docs = await gql('query A{viewer{login}} query B{viewer{id}}'); assert.equal(docs.length, 2);
    const mcp = (id: number, method: string, params: any = {}) => api('/mcp', 'POST', { jsonrpc: '2.0', id, method, params });
    const tools1 = await mcp(1, 'tools/list'), tools2 = await mcp(1, 'tools/list'); assert.equal(JSON.stringify(tools1), JSON.stringify(tools2));
    assert.equal((await mcp(1, 'server/discover')).result.supportedVersions[0], '2026-07-28');
    const calls = await Promise.all([mcp(20, 'tools/call', { name: 'create_issue', arguments: { owner: 'tester', repo: 'demo', title: 'mcp one' } }), mcp(30, 'tools/call', { name: 'create_issue', arguments: { owner: 'tester', repo: 'demo', title: 'mcp two' } })]);
    assert.deepEqual(calls.map(c => c.id), [20, 30]);
    for (const response of calls) { const item = JSON.parse(response.result.content[0].text); const rest = await api(`/api/v3/repos/tester/demo/issues/${item.number}`); assert.equal(rest.id, item.id); }
    assert.equal((await mcp(40, 'tools/call', { name: 'bad' })).error.code, -32602);
    assert.equal((await mcp(40, 'tools/call', { name: 'get_repo', arguments: {} })).error.code, -32602);
    const parseError = await fetch(base + '/mcp', { method: 'POST', headers, body: '{' }); assert.equal(parseError.status, 400); assert.equal((await parseError.json()).error.code, -32700);
    const refs = await fetch(base + '/tester/demo.git/info/refs?service=git-upload-pack', { headers: { ...headers, 'git-protocol': 'version=2' } });
    assert.equal(packets(Buffer.from(await refs.arrayBuffer())).lines[0].toString(), 'version 2\n');
    const wire = async (service: string, bytes: Buffer, v2 = false): Promise<Buffer> => {
      const res = await fetch(base + `/tester/demo.git/${service}`, { method: 'POST', headers: { ...headers, ...(v2 ? { 'git-protocol': 'version=2' } : {}) }, body: bytes }); assert.equal(res.status, 200); return Buffer.from(await res.arrayBuffer());
    };
    const ls = await wire('git-upload-pack', Buffer.concat([pkt('command=ls-refs\n'), Buffer.from('0001'), pkt('symrefs\n'), FLUSH]), true);
    const main = packets(ls).lines.find(b => b.toString().includes(' refs/heads/main'))!.toString().slice(0, 40);
    const clone = await wire('git-upload-pack', Buffer.concat([pkt('command=fetch\n'), Buffer.from('0001'), pkt(`want ${main}\n`), pkt('done\n'), FLUSH]), true);
    const packLines = packets(clone).lines; assert.equal(packLines[0].toString(), 'packfile\n');
    const local = new ObjectDB(); readPack(local, Buffer.concat(packLines.slice(1).filter(b => b[0] === 1).map(b => b.subarray(1))));
    assert.ok(local.objects.has(main));
    const files = local.files(main); files.set('feature.txt', { mode: '100644', sha: local.put('blob', 'feature') });
    const tree = local.writeTree(files), next = local.put('commit', `tree ${tree}\nparent ${main}\nauthor Tester <tester@localhost> 1704067200 +0000\ncommitter Tester <tester@localhost> 1704067200 +0000\n\nfeature\n`);
    const push = async (commands: string[], objects: string[]) => {
      const payload = Buffer.concat([...commands.map((c, i) => pkt(c + (i === 0 ? '\0report-status side-band-64k' : '') + '\n')), FLUSH, ...(objects.length ? [writePack(local, objects)] : [])]);
      const result = await wire('git-receive-pack', payload);
      return Buffer.concat(packets(result).lines.filter(b => b[0] === 1).map(b => b.subarray(1))).toString();
    };
    assert.match(await push([`${ZERO} ${next} refs/heads/feature`], [...local.reachable([next])]), /ok refs\/heads\/feature/);
    const pull = await gql('mutation($input:CreatePullRequestInput!){createPullRequest(input:$input){pullRequest{id number headRefName baseRefName}}}', { input: { repositoryId: q.data.repository.id, title: 'feature', headRefName: 'feature', baseRefName: 'main' } });
    assert.ok(pull.data, JSON.stringify(pull)); const number = pull.data.createPullRequest.pullRequest.number;
    assert.equal((await api(`/api/v3/repos/tester/demo/pulls/${number}`)).head.ref, 'feature');
    const merged = await api(`/api/v3/repos/tester/demo/pulls/${number}/merge`, 'PUT', { merge_method: 'squash' }); assert.equal(merged.merged, true);
    assert.equal((await api('/api/v3/repos/tester/demo/contents/feature.txt')).content, Buffer.from('feature').toString('base64'));
    const old = local.commit(main).parents[0];
    const mixed = await push([`${next} ${main} refs/heads/feature`, `${ZERO} ${main} refs/heads/other`], []);
    assert.match(mixed, /ng refs\/heads\/feature non-fast-forward/); assert.match(mixed, /ok refs\/heads\/other/);
    await api('/api/v3/user/repos', 'POST', { name: 'second' }, 201);
    const page = await fetch(base + '/api/v3/user/repos?per_page=1', { headers }); assert.match(page.headers.get('link')!, /rel="next"/); await page.arrayBuffer();
    await api('/missing/no.git/info/refs?service=git-receive-pack', 'GET', undefined, 404);
    await api('/missing/no.git/info/refs?service=git-upload-pack', 'GET', undefined, 404);
    await api('/api/v3/repos/tester/second', 'DELETE', undefined, 204);
    await api('/api/v3/repos/tester/second', 'GET', undefined, 404);
  } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
});

test('GraphQL fragments, defaults, introspection and deterministic stores', () => {
  const run = () => {
    const s = new Store(); const r = s.createRepo({ name: 'test', auto_init: true });
    const i = s.createIssue(r, { title: 'one' }); s.createIssue(r, { title: 'two' });
    const response = executeGraphQL(s, { query: 'query($name:String!="test"){x:repository(owner:"tester",name:$name){...Repo}} fragment Repo on Repository{id issues(orderBy:{field:CREATED_AT,direction:DESC}){edges{node{... on Issue {number title}}}}}' });
    assert.ok(response.data, JSON.stringify(response)); assert.equal(response.data.x.issues.edges[0].node.number, 2);
    const introspection = executeGraphQL(s, { query: '{__type(name:"PullRequest"){name fields{name type{kind name ofType{kind name}}}}}' }); assert.ok(introspection.data, JSON.stringify(introspection));
    return JSON.stringify({ response, introspection, ids: [...r.db.objects.keys()] });
  };
  assert.equal(run(), run());
});
