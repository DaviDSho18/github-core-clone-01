You are continuing work on the entrypoint `main.ts` of this repository (a standalone GitHub-compatible
git forge, Node 22, TypeScript, standard library only). In an earlier session you already rewrote
`main.ts` from scratch so it owns the entry <-> env lifecycle. Now finish the job: verify it, make one
concrete revision, and verify again.

IMPORTANT — work in explicit narrated steps. Before each step, write one short paragraph of plain
text saying what you are about to do and why. Produce a separate text message for each step:
Step 1 — Read the current main.ts and say what the lifecycle does today.
Step 2 — Run the test suite and the type check, and report exactly what you observed.
Step 3 — Revise main.ts in place: fix or improve one concrete thing you found (edit the file, do not
         rewrite it wholesale). Say which line you changed and why.
Step 4 — Re-run the tests to confirm the revision still passes.
Step 5 — Summarize the final lifecycle and why it is correct.

## The lifecycle contract (main.ts must satisfy it exactly)

1. `MAKEFUN_ENV_URL` names the env's origin. Strip trailing slashes. If unset or empty, write one line
   to stderr and `process.exit(1)` immediately.
2. `createApp()` then `listen(0)` so the kernel picks a free ephemeral port. Never bind 8080.
3. POST `$MAKEFUN_ENV_URL/register` with JSON `{"port": N}`. 200 normal; 409 means another entry
   already registered (stop retrying and continue); 400 and 405 are contract errors and end the
   program; network errors and other statuses are retried with bounded exponential backoff.
4. GET `$MAKEFUN_ENV_URL/done` until the body is `{"done": true}`; never exit before that.
5. Close the server and `process.exit(0)`.

Use `fetch` with `AbortSignal.timeout(...)`. Import only from `./app.ts`.

## How to verify

- `node --experimental-strip-types --test forge.test.ts` must pass all tests.
- `npx tsc --noEmit -p tsconfig.json` should report no errors; if TypeScript is unavailable, say so and
  rely on the test run.

Work only inside this repository. Do not touch app.ts, store.ts, objects.ts, git.ts, http.ts, rest.ts,
graphql.ts or mcp.ts.
