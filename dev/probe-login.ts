#!/usr/bin/env node
// Scratch probe: create a repository through the entry's REST surface and then read it back over
// git's info/refs, to see exactly which login the entry thinks the token owns.
import { createApp } from '/home/agentuser/makefun/entry-gh/app.ts';

const token = 'shpat_' + 'ab'.repeat(16);
process.env.MAKEFUN_SHOP_TOKEN = token;
delete process.env.MAKEFUN_SHOP_LOGIN;
delete process.env.MAKEFUN_SHOP_USER;

const server = createApp();
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = (server.address() as any).port;
const base = `http://127.0.0.1:${port}`;
const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };

const created = await fetch(`${base}/api/v3/user/repos`, {
  method: 'POST',
  headers,
  body: JSON.stringify({ name: 'gitconf-probe', private: true, description: 'probe' }),
});
const body = await created.json();
console.log('POST /user/repos ->', created.status, 'owner:', body.owner?.login, 'full_name:', body.full_name);

const refs = await fetch(`${base}/adaiversen/gitconf-probe.git/info/refs?service=git-receive-pack`, { headers });
console.log('GET /adaiversen/gitconf-probe.git/info/refs ->', refs.status);

const own = await fetch(`${base}/${body.owner?.login}/gitconf-probe.git/info/refs?service=git-receive-pack`, { headers });
console.log(`GET /${body.owner?.login}/gitconf-probe.git/info/refs ->`, own.status);

server.close();
