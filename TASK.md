Build a standalone, GitHub-compatible git forge in Node 22 (TypeScript, standard library only) for
make.fun's "github-core-clone-01" bounty. Grow `app.ts` (the server). Keep `main.ts` as it is.

IMPORTANT — work in explicit narrated steps. Before each step, write one short paragraph of plain
text saying what you are about to do and why. Produce a separate text message for each step:
Step 1 — Plan the module layout and the store.
Step 2 — Build the store and the git object model (loose objects, refs, packfile read/write).
Step 3 — Build git smart HTTP: upload-pack (protocol v2 + v0) and receive-pack (v0).
Step 4 — Build the REST v3 routes.
Step 5 — Build the GraphQL v4 endpoint.
Step 6 — Build the MCP endpoint.
Step 7 — Review, fix, and summarize.

## The wire contract (the suite drives these)
Base URL = the address the register request came from + your port. Auth: `Authorization: Bearer
<token>` where the token is `process.env.MAKEFUN_SHOP_TOKEN`. No token → 401. A repo belongs to the
login the token names; the same name under another owner is 404, never 403.
Every REST reply is `application/json`, never HTML, and carries a non-empty `X-GitHub-Request-Id`
header. Errors carry a JSON body with a string `message`. Unknown route → 404 JSON. Malformed JSON
body → 400.

REST v3 (under `/api/v3`):
- `GET /api/v3/` → 200 JSON + an `X-OAuth-Scopes` header
- `GET /api/v3/meta` → 200 with a non-empty `installed_version`
- `GET /api/v3/user/repos` → 200 list; when more than one page, a `Link` header with rel="next"
- `POST /api/v3/user/repos` → 201, the repository object
- `GET /api/v3/repos/{owner}/{repo}` → 200 the repo; 404 JSON when missing
- `PATCH /api/v3/repos/{owner}/{repo}` → 200 with the description updated
- `DELETE /api/v3/repos/{owner}/{repo}` → 204; a later GET is 404
- `PUT /api/v3/repos/{owner}/{repo}/contents/{path}` → 201 create, 200 update, 409 stale sha
- `DELETE /api/v3/repos/{owner}/{repo}/contents/{path}` → 200 `{commit:{sha}}`; later GET 404
- `POST /api/v3/repos/{owner}/{repo}/labels` → 201 `{name}`; a label must exist before an issue
  carries it
- `POST /api/v3/repos/{owner}/{repo}/issues` → 201 an open issue; 422 when it names a missing label
- `POST /api/v3/repos/{owner}/{repo}/pulls` → 201, state open, with head and base
- `GET /api/v3/repos/{owner}/{repo}/pulls/{number}` → 200; 404 when unknown
- `PUT /api/v3/repos/{owner}/{repo}/pulls/{number}/merge` → 200 `{sha, merged:true, message}`: the
  pull is closed and merged and its squash commit is on the default branch; 404 unknown, 405 conflicted

Git smart HTTP at `/{owner}/{repo}.git` (no git binary anywhere — implement the protocol yourself):
- `GET /{owner}/{repo}.git/info/refs?service=git-upload-pack` — protocol v2 when the request sends
  `Git-Protocol: version=2` (advertisement opens with "version 2" and must support ls-refs and
  fetch); v0 otherwise (opens with `# service=git-upload-pack`, advertises side-band-64k).
- `GET .../info/refs?service=git-receive-pack` — v0 only, opens with `# service=git-receive-pack`,
  advertises report-status, side-band-64k and delete-refs.
- `POST /{owner}/{repo}.git/git-upload-pack` and `/git-receive-pack`.
- A missing repository is 404 on both services' GET /info/refs, and the server stays up.
- Every update must fast-forward: a non-fast-forward push is refused per ref with
  `ng <ref> non-fast-forward` and the ref does not move. A push with one fast-forward and one refused
  ref applies the fast-forward and answers one report line each (ok / ng non-fast-forward).
- Use pkt-line framing (4-hex-digit length prefix), and implement packfile parsing (ofs-delta and
  ref-delta) and generation. A repository is created through REST; a push cannot invent one.

GraphQL at `POST /api/graphql` with `{"query":..., "variables":{...}}` and the bearer token
(unauthenticated → exactly 401):
- Introspection must describe the documents gh and github-mcp-server send:
  `__type(name:"Issue")` lists id, number, title, state, body;
  `__type(name:"PullRequest")` lists id, number, headRefName, baseRefName, mergeable, state, merged,
  files; `__type(name:"Repository")` lists id, databaseId, name, nameWithOwner, owner,
  defaultBranchRef, viewerPermission, hasIssuesEnabled, issue, issues, pullRequests, pullRequest;
  SearchType's enumValues include ISSUE.
- `repository.issues(orderBy:{field:CREATED_AT, direction:DESC})` returns issue numbers descending,
  ties by number DESC; `states:[OPEN]` omits closed issues.
- `createPullRequest` takes a repository id and a head branch pushed over git, and opens a PR REST
  reads back with the same head and base refs.
- Two operation documents in one body answer two results in order. A query naming an unknown field is
  HTTP 200 with an `errors` array naming it. An unknown repository is `data.repository = null` with no
  errors.

MCP at `POST /mcp`: JSON-RPC 2.0 over the Streamable HTTP transport, answering `application/json` or
`text/event-stream` (SSE), each request inside 3 seconds. Speaks revision `2026-07-28` (every request
declares it; `server/discover` answers a result whose `supportedVersions` is a non-empty list of
YYYY-MM-DD revisions), or a handshake revision (2025-11-25 or earlier) via `initialize` +
`notifications/initialized` with an optional `Mcp-Session-Id`.
- `tools/list` offers at least six tools including create_repo, get_repo, create_issue, list_issues,
  create_pull_request, merge_pull_request (a server prefix like `github__create_issue` is allowed,
  matched as a suffix, case-insensitively). Every tool has a non-empty description and an inputSchema
  of type object. Two tools/list calls answer the same list, byte for byte.
- Two tools/call requests in flight keep their own JSON-RPC ids and answer for their own arguments.
- Arguments: create_repo {name}, get_repo {owner, repo}, create_issue {owner, repo, title, body?},
  list_issues {owner, repo, state?}, create_pull_request {owner, repo, title, head, base},
  merge_pull_request {owner, repo, pull_number}. Map each onto your schema's nearest property name and
  fill a `full_name` property with `owner/repo`.
- An unknown tool answers JSON-RPC -32602 or -32601, never HTTP 500. A tools/call missing a required
  argument answers `isError:true` or -32602. A body that is not JSON answers HTTP 400 with -32700. A
  call with no Authorization header answers 401.
- The tools act on the SAME store as REST: a repo and issue created over MCP read back over /api/v3
  as the same objects, ids unchanged.

## Constraints
- Node 22 standard library only. No packages, no native addons, no network beyond the suite.
- Read-only /bot, 2 CPUs, 1024 MB, 256 processes, 900-second wall.
- Deterministic: the trial is replayed and the state export must be byte-identical. The export holds
  only pass/fail bits and counts — never a time, port or date.
- Performance matters (200 of 1000 points): keep hot paths fast (in-memory store, no fsync per write).
- Keep the module split: app.ts (HTTP routing), plus modules like store.ts, git.ts, rest.ts,
  graphql.ts, mcp.ts.

## Verify
Run: node --experimental-strip-types main.ts   (with MAKEFUN_ENV_URL unset it must exit 1 cleanly)
Run: node --check on each .ts file (or confirm they parse).
Report the module list and line counts.
