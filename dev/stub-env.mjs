// A local stand-in for the sealed env, for running the kit's suite against the entry by hand.
//
// It answers the two routes main.ts drives: POST /register records the port the entry bound (and writes
// it to /tmp/ghrun/entry-port so the runner can dial it), and GET /done answers {"done":false} until the
// runner has finished, then {"done":true}. This file is a development tool: it is never part of the entry
// and the trial's real env replaces it.
import { createServer } from 'node:http';
import { writeFileSync } from 'node:fs';

const port = Number(process.env.STUB_PORT ?? 18080);
let done = false;

const server = createServer((req, res) => {
  const send = (status, body) => {
    const text = JSON.stringify(body);
    res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) });
    res.end(text);
  };
  if (req.method === 'POST' && req.url === '/register') {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      try {
        const body = JSON.parse(raw || '{}');
        if (!Number.isInteger(body.port) || body.port <= 0) return send(400, { message: 'port is required' });
        writeFileSync('/tmp/ghrun/entry-port', String(body.port));
        send(200, { ok: true });
      } catch {
        send(400, { message: 'malformed JSON' });
      }
    });
    return;
  }
  if (req.method === 'GET' && req.url === '/done') return send(200, { done });
  send(404, { message: 'Not Found' });
});

// The runner flips this once the suite has finished, by hitting /finish.
server.on('request', () => {});
const original = server.listeners('request')[0];
server.removeAllListeners('request');
server.on('request', (req, res) => {
  if (req.method === 'POST' && req.url === '/finish') { done = true; res.writeHead(204).end(); return; }
  original(req, res);
});

server.listen(port, '127.0.0.1', () => process.stdout.write(`[stub-env] listening on ${port}\n`));
