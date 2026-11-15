Rewrite the entry lifecycle file `main.ts` in this repository from scratch, in your own words and
structure, and then revise it once more.

Context: this repository is a standalone GitHub-compatible git forge (Node 22, TypeScript, standard
library only). `app.ts` exports `createApp(): Server` and builds the whole forge. `main.ts` is the
entrypoint the grader runs: it owns the entry <-> env lifecycle, and it must keep working exactly as
the contract below says. The current `main.ts` was copied from the starter; you are replacing it with
a version you author yourself, so the transcripts show the entrypoint being written and revised.

IMPORTANT — work in explicit narrated steps. Before each step, write one short paragraph of plain
text saying what you are about to do and why. Produce a separate text message for each step:
Step 1 — Read the current main.ts and app.ts, and plan the lifecycle you are about to write.
Step 2 — Write the new main.ts from scratch.
Step 3 — Run the tests and the type check, and inspect the result.
Step 4 — Revise main.ts: fix or improve one concrete thing you found, editing the file in place.
Step 5 — Re-run the tests to confirm the revision still passes.
Step 6 — Summarize what the lifecycle does and why.

## The lifecycle contract (main.ts must satisfy it exactly)

1. `MAKEFUN_ENV_URL` names the env's origin (e.g. `http://store:8080`). Strip trailing slashes. If it
   is unset or empty, write one line to stderr and `process.exit(1)` immediately.
2. Create the server with `createApp()` and `listen(0)` so the kernel picks a free ephemeral port.
   Never bind 8080 — the env owns it.
3. POST `$MAKEFUN_ENV_URL/register` with JSON body `{"port": N}`. The env records the request's
   source address plus N as your base URL. 200 is the normal answer; 409 means another entry already
   registered (not this process's to fix — stop retrying and continue); 400 and 405 are contract
   errors and end the program. Network errors and other statuses are retried with a short exponential
   backoff.
4. GET `$MAKEFUN_ENV_URL/done` until the body is `{"done": true}`. The env answers at once with
   `{"done": false}` while the suite runs, so this is a poll. A failed or slow poll is retried with
   backoff. The process never exits before the env says done.
5. Close the server and `process.exit(0)`.

Use `fetch` with `AbortSignal.timeout(...)` for each request. Keep the retry backoff bounded
(exponential from a small initial value, capped). Import only from `./app.ts` — no package, no model
call.

## How to verify

- `node --experimental-strip-types --test forge.test.ts` must pass all tests.
- `npx tsc --noEmit -p tsconfig.json` (or `node_modules/.bin/tsc`) should report no errors; if
  TypeScript is not installed, note that and rely on the test run.

Work only inside this repository. Do not touch app.ts, store.ts, objects.ts, git.ts, http.ts, rest.ts,
graphql.ts or mcp.ts.
