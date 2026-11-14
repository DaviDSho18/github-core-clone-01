"""The entry <-> env lifecycle, as env/src/wire.ts settles it.

Bind an ephemeral port, POST {"port": N} to $MAKEFUN_ENV_URL/register, serve the forge, then poll
GET $MAKEFUN_ENV_URL/done until {"done": true} and exit 0. The first register wins; a 409 means
another entry already registered, which is not this process's to fix.

Standard library only. Nothing here reads a clock, a random source or a port into an answer: the
port is the kernel's, and it never reaches the state export.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# `python3 -P` (safe_path) does not put the script's own directory on sys.path, and the harness runs
# `python3 -P -s /bot/main.py`. The forge package sits beside this file, so put that directory back
# before importing it. This is the only sys.path write in the entry.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from forge.server import Forge  # noqa: E402 - after the sys.path fix above

#: How long one register or done request may take before it is abandoned and retried.
REQUEST_TIMEOUT_SEC = 30.0
#: The first wait between retries, and the longest.
RETRY_INITIAL_SEC = 0.25
RETRY_MAX_SEC = 2.0
#: The largest request body the forge will read (a push pack can be large; the bench sends ~1 MiB).
MAX_BODY_BYTES = 64 * 1024 * 1024


def retry_delay(attempt: int) -> float:
    return min(RETRY_MAX_SEC, RETRY_INITIAL_SEC * (2 ** min(attempt, 8)))


class ForgeHandler(BaseHTTPRequestHandler):
    """One HTTP request -> the forge's (status, headers, body)."""

    protocol_version = "HTTP/1.1"
    server_version = "makefun-forge/1.0"
    sys_version = ""

    #: The forge instance, installed on the server object by main().
    forge: Forge

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler's name
        """Quiet by default: the harness reads stdout as the entry log, not every request."""

    def _run(self, method: str) -> None:
        target = self.path
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = 0
        raw_length = self.headers.get("content-length")
        if raw_length:
            try:
                length = max(0, min(MAX_BODY_BYTES, int(raw_length)))
            except ValueError:
                length = 0
        body = self.rfile.read(length) if length else b""
        # The git advertisement needs the query string; the forge reads it off the target itself, so
        # the only extra thing to carry is the git protocol header, which is already a real header.
        try:
            response = self.forge.handle(method, target, headers, body)
        except Exception as err:  # never a dropped connection
            payload = json.dumps({"message": f"the forge failed: {type(err).__name__}: {err}"}).encode("utf-8")
            response = type("R", (), {"status": 500, "headers": {"content-type": "application/json; charset=utf-8"}, "body": payload})()

        out_headers = dict(response.headers)
        out_headers["content-length"] = str(len(response.body))
        # The env's checks read the request id off every REST reply; the forge's own header wins if set.
        if "x-github-request-id" not in {k.lower() for k in out_headers}:
            out_headers["x-github-request-id"] = self.forge.next_request_id()
        self.send_response(response.status)
        for name, value in out_headers.items():
            self.send_header(name, value)
        self.end_headers()
        if response.body:
            try:
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        self._run("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._run("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._run("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._run("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._run("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._run("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._run("OPTIONS")


class ForgeServer(ThreadingHTTPServer):
    """One thread per connection, so 50 concurrent shallow clones do not serialise."""

    daemon_threads = True
    allow_reuse_address = True
    #: Keep-alive is what the read benchmark measures; HTTP/1.1 gives it by default.
    request_queue_size = 256


def register(env_url: str, port: int) -> None:
    """POST the bound port until the env accepts it."""
    body = json.dumps({"port": port}).encode("utf-8")
    for attempt in range(100000):
        request = urllib.request.Request(
            f"{env_url}/register",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
                if response.status in (200, 409):
                    return
                if response.status in (400, 405):
                    raise SystemExit(f"register answered HTTP {response.status}: {response.read()[:200]!r}")
        except urllib.error.HTTPError as err:
            if err.code in (200, 409):
                return
            if err.code in (400, 405):
                raise SystemExit(f"register answered HTTP {err.code}")
        except SystemExit:
            raise
        except Exception:
            pass
        time.sleep(retry_delay(attempt))


def wait_for_done(env_url: str) -> None:
    """Poll GET /done until it answers {"done": true}; never return before that."""
    for attempt in range(100000):
        try:
            with urllib.request.urlopen(f"{env_url}/done", timeout=REQUEST_TIMEOUT_SEC) as response:
                if response.status == 200:
                    try:
                        if json.loads(response.read().decode("utf-8")).get("done") is True:
                            return
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(retry_delay(attempt))


def main() -> int:
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    if env_url == "":
        sys.stderr.write("[entry] MAKEFUN_ENV_URL is not set: there is no env to register with\n")
        return 1

    token = os.environ.get("MAKEFUN_SHOP_TOKEN") or ""
    forge = Forge(token=token)
    ForgeHandler.forge = forge

    server = ForgeServer(("0.0.0.0", 0), ForgeHandler)
    port = server.server_address[1]
    forge.port = port
    sys.stdout.write(f"[entry] listening on port {port}\n")
    sys.stdout.flush()

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()

    register(env_url, port)
    sys.stdout.write("[entry] registered; waiting for the env to say done\n")
    sys.stdout.flush()

    wait_for_done(env_url)
    server.shutdown()
    sys.stdout.write("[entry] done\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
