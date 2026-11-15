import type { IncomingMessage, ServerResponse } from 'node:http';
import { HttpError } from './store.ts';

export function sendJson(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const bytes = status === 204 || status === 202 && body === undefined ? '' : `${JSON.stringify(body)}\n`;
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(bytes), ...headers });
  res.end(bytes);
}
export async function readBody(req: IncomingMessage, limit = 256 * 1024 * 1024): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > limit) throw new HttpError(413, 'Request body too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, length);
}
export function parseJson(bytes: Buffer): any {
  try { return JSON.parse(bytes.toString()); }
  catch { throw new HttpError(400, 'Malformed JSON body'); }
}
export async function readJson(req: IncomingMessage): Promise<any> { return parseJson(await readBody(req, 16 * 1024 * 1024)); }
export function originOf(req: IncomingMessage): string {
  return `http://${req.headers.host ?? 'localhost'}`;
}
export function paginate<T>(items: T[], url: URL, res: ServerResponse): T[] {
  const size = Math.max(1, Math.min(100, Number(url.searchParams.get('per_page')) || 30));
  const page = Math.max(1, Number(url.searchParams.get('page')) || 1), start = (page - 1) * size;
  const links: string[] = [];
  const link = (p: number, rel: string) => { const next = new URL(url); next.searchParams.set('page', String(p)); next.searchParams.set('per_page', String(size)); links.push(`<${next}>; rel="${rel}"`); };
  if (start + size < items.length) { link(page + 1, 'next'); link(Math.ceil(items.length / size), 'last'); }
  if (page > 1) { link(page - 1, 'prev'); link(1, 'first'); }
  if (links.length) res.setHeader('link', links.join(', '));
  return items.slice(start, start + size);
}
