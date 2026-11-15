import { createServer, type Server } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { Store, HttpError } from './store.ts';
import { sendJson } from './http.ts';
import { handleGit } from './git.ts';
import { handleRest } from './rest.ts';
import { handleGraphQL } from './graphql.ts';
import { handleMcp } from './mcp.ts';

/** Every listener owns an isolated, shared-by-all-protocols in-memory forge. */
export function createApp(): Server {
  const store = new Store();
  let requestId = 0;
  const server = createServer(async (req, res) => {
    res.setHeader('x-github-request-id', `MAKEFUN:${(++requestId).toString(16).padStart(12, '0')}`);
    res.setHeader('x-content-type-options', 'nosniff');
    try {
      const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`);
      if (req.method === 'GET' && url.pathname === '/healthz') { sendJson(res, 200, { ok: true }); return; }
      const auth = String(req.headers.authorization ?? '');
      // Git clients commonly answer an HTTP Basic challenge with login:token, while
      // REST, GraphQL and MCP use the Bearer form. Both verify the same configured token.
      let token = /^Bearer (.+)$/i.exec(auth)?.[1] ?? '';
      if (/\.git\//.test(url.pathname) && /^Basic /i.test(auth)) {
        const value = Buffer.from(auth.slice(6), 'base64').toString();
        token = value.slice(value.indexOf(':') + 1);
      }
      const expected = process.env.MAKEFUN_SHOP_TOKEN ?? '';
      const a = Buffer.from(token), b = Buffer.from(expected);
      if (!a.length || !b.length || a.length !== b.length || !timingSafeEqual(a, b)) {
        res.setHeader('www-authenticate', 'Basic realm="GitHub forge"');
        sendJson(res, 401, { message: 'Bad credentials' }); return;
      }
      if (url.pathname === '/api/graphql' && req.method === 'POST') { await handleGraphQL(req, res, store); return; }
      if (url.pathname === '/mcp') { await handleMcp(req, res, store); return; }
      if (await handleRest(req, res, url, store)) return;
      if (await handleGit(req, res, url, store)) return;
      sendJson(res, 404, { message: 'Not Found' });
    } catch (error) {
      if (res.headersSent) { res.end(); return; }
      const status = error instanceof HttpError ? error.status : error instanceof URIError ? 400 : 500;
      sendJson(res, status, { message: error instanceof HttpError ? error.message : status === 400 ? 'Malformed URL' : 'Internal Server Error' });
    }
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 30000;
  return server;
}
