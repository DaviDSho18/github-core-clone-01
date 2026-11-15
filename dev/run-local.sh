#!/bin/sh
# Run the kit's sealed local suite against the entry's forge, with the lifecycle contract satisfied.
#
#   sh dev/run-local.sh [--perf]
#
# It starts main.ts with MAKEFUN_ENV_URL pointed at a tiny stub env (dev/stub-env.mjs) that answers
# /register with 200 and /done with {"done":true} once the suite has run, then runs sdk/run.ts against
# the port the entry registered. The stub is a local test double only: in a real trial the sealed env
# plays that role. Nothing here is part of the entry.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
kit=${KIT:-/home/agentuser/makefun/kits/gh/github-core-clone-01-kit}
port=${STUB_PORT:-18080}
token=${MAKEFUN_SHOP_TOKEN:-test-token}

MAKEFUN_SHOP_TOKEN="$token" MAKEFUN_SHOP_LOGIN=tester STUB_PORT="$port" \
  node "$here/stub-env.mjs" > /tmp/ghrun/stub.log 2>&1 &
stub=$!
trap 'kill $stub 2>/dev/null || true' EXIT

sleep 1
# No MAKEFUN_SHOP_LOGIN here: the trial sets only the token, so the entry must derive its own login.
MAKEFUN_SHOP_TOKEN="$token" MAKEFUN_ENV_URL="http://127.0.0.1:$port" \
  node --experimental-strip-types "$repo/main.ts" > /tmp/ghrun/server.log 2>&1 &
entry=$!
trap 'kill $entry $stub 2>/dev/null || true' EXIT

# Wait for the entry to register its ephemeral port with the stub.
for _ in $(seq 1 40); do
  [ -s /tmp/ghrun/entry-port ] && break
  sleep 0.25
done
if [ ! -s /tmp/ghrun/entry-port ]; then
  echo "the entry never registered; server log:" >&2
  cat /tmp/ghrun/server.log >&2
  exit 1
fi
entryport=$(cat /tmp/ghrun/entry-port)
echo "entry registered on port $entryport"

node --experimental-strip-types "$kit/sdk/run.ts" --url "http://127.0.0.1:$entryport" --token "$token" "$@"
