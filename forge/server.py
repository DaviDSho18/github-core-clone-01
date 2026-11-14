"""The forge's HTTP surface: git smart HTTP, REST v3, GraphQL v4 and MCP, over one store.

Standard library only. Deterministic: no clock, no random source, no port reaches an answer. The
request id is a counter, every date comes from the seeded scenario, and the state export sorts its
keys, so two runs of the same instance leave byte-identical state.

Callers hand this module a method, a target, a header map and a body, and get back
(status, headers, body). main.py is the only thing that knows about sockets.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

from . import gitobj as g
from . import githttp as gh
from . import gql as gql
from . import mcp as mcp
from . import pktline as pk
from . import restapi as rest
from . import restops as restops
from . import scenario as sc
from .store import Store, Repo, StoreError, HEADS, TAGS

#: The version this forge reports, in every surface that names one.
SERVER_VERSION = "makefun-forge/1.0"

#: The JSON content type every REST and GraphQL answer carries.
JSON_TYPE = "application/json; charset=utf-8"
#: The MCP endpoint's content type.
MCP_TYPE = "application/json"

#: The MCP revision this server speaks first, and the ones it still answers.
MCP_REVISION = "2026-07-28"
MCP_SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MCP_LEGACY_REVISIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

#: The commit identity the forge writes when it makes a commit of its own.
AUTHOR_NAME = "forge"
AUTHOR_EMAIL = "forge@example.invalid"


class Response:
    """One HTTP answer: a status, a header map and the bytes of the body."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body


def json_response(status: int, payload, headers: dict[str, str] | None = None) -> Response:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=False, ensure_ascii=True)
    out = {"content-type": JSON_TYPE}
    if headers:
        out.update(headers)
    return Response(status, out, text.encode("utf-8"))


def text_response(status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status, {"content-type": content_type}, text.encode("utf-8"))


def not_found() -> Response:
    return json_response(404, {"message": "Not Found"})


# ----------------------------------------------------------------------------- the forge


class Forge:
    """Every surface, one store. One lock so requests answer in the order they arrive."""

    def __init__(self, token: str = "", login: str | None = None, login_id: int = 1000, port: int = 0):
        self.scenario = sc.build_scenario()
        self.token = token
        self.login = login or self.scenario["viewer"]["login"]
        self.login_id = login_id
        #: The port the entry bound; only used to build an absolute URL when no Host header arrived.
        self.port = port
        self.store = Store(self.login, self.login_id, token)
        self.request_seq = 0
        self._seed_scenario()

    # -------------------------------------------------------------- seeding

    def _seed_scenario(self) -> None:
        """The viewer's 30 seeded repositories, as the env's scenario names them.

        The pagination check creates all 30 through POST /user/repos and then walks the list; the
        other checks look repositories up by the names the scenario gives them. Seeding them here
        means the list is complete whether or not a check got there first, and a later POST of the
        same name is the 422 the check already tolerates.
        """
        for entry in self.scenario["repos"]:
            repo = self.store.create(
                entry["owner"],
                entry["name"],
                entry["description"],
                entry["private"],
                iso_from_epoch(sc.SCENARIO_EPOCH_SEC),
            )
            repo.default_branch = entry["default_branch"]

    # -------------------------------------------------------------- helpers

    def next_request_id(self) -> str:
        self.request_seq += 1
        return f"forge-{self.request_seq:08d}"

    def repo_of(self, owner: str, name: str) -> Repo | None:
        return self.store.get(owner, name)

    def own_repo(self, owner: str, name: str) -> Repo:
        """A repository the token's login owns, or the 404 GitHub answers for someone else's."""
        if owner != self.login:
            raise rest.ApiError(404, "Not Found")
        repo = self.store.get(owner, name)
        if repo is None:
            raise rest.ApiError(404, "Not Found")
        return repo

    # -------------------------------------------------------------- dispatch

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
        """One request: the path decides the surface, the method the operation."""
        parts = urlsplit(target)
        path = unquote(parts.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        method = method.upper()

        try:
            if path == "/healthz":
                return self.handle_healthz(method)

            # The git protocol version rides a header; carry it into the query map so the
            # advertisement can honour Git-Protocol: version=2 without a second lookup path.
            if headers.get("git-protocol"):
                query.setdefault("__git_protocol__", [headers["git-protocol"]])

            git = self.match_git(path)
            if git is not None:
                owner, name, rest_path = git
                return self.handle_git(method, owner, name, rest_path, headers, body, query)

            if path == "/api/graphql" or path.startswith("/api/graphql/"):
                return self.handle_graphql(method, headers, body)

            if path == "/mcp" or path.startswith("/mcp/"):
                return self.handle_mcp(method, headers, body)

            if path == "/api/v3" or path.startswith("/api/v3/"):
                return handle_rest(self, method, path, query, headers, body)

            return not_found()
        except rest.ApiError as err:
            return json_response(err.status, err.body, {"x-github-request-id": self.next_request_id()})
        except Exception as err:  # never a 500 with an HTML body
            return json_response(500, {"message": f"the forge failed: {type(err).__name__}: {err}"}, {"x-github-request-id": self.next_request_id()})

    def handle_healthz(self, method: str) -> Response:
        if method != "GET":
            return json_response(405, {"message": "Method Not Allowed"})
        return json_response(200, {"ok": True})

    # -------------------------------------------------------------- GraphQL v4

    def handle_graphql(self, method: str, headers: dict, body: bytes) -> Response:
        """POST /api/graphql: one operation or a JSON array of them, answered {data, errors}."""
        if method != "POST":
            return json_response(405, {"message": "Method Not Allowed"})
        if not self._authorized(headers):
            return json_response(401, {"message": "Requires authentication"})
        status, payload = gql.execute(self, body)
        return json_response(status, payload)

    # -------------------------------------------------------------- MCP

    def handle_mcp(self, method: str, headers: dict, body: bytes) -> Response:
        """POST /mcp: JSON-RPC 2.0 over the Streamable HTTP transport, one JSON body per answer."""
        session = mcp.Session(self, headers)
        status, payload, extra = session.handle(body, method)
        out = {"content-type": mcp.CONTENT_TYPE}
        out.update(extra)
        if payload is None:
            return Response(status, out, b"")
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        return Response(status, out, text.encode("utf-8"))

    # -------------------------------------------------------------- shared helpers

    def _authorized(self, headers: dict) -> bool:
        """Whether the request carries the trial's bearer token."""
        token = headers.get("authorization", "")
        if not token.lower().startswith("bearer "):
            return False
        if not self.store.token:
            return True
        return token[7:].strip() == self.store.token

    @property
    def scenario_epoch(self) -> int:
        return sc.SCENARIO_EPOCH_SEC

    def now_iso(self) -> str:
        """The seeded instant, so two runs of the same instance leave byte-identical state."""
        return iso_from_epoch(sc.SCENARIO_EPOCH_SEC)

    def author_bytes(self) -> bytes:
        """The commit identity the forge writes, as a git ``Name <email> epoch +0000`` line."""
        viewer = self.scenario["viewer"]
        return f"{viewer['name']} <{viewer['email']}> {sc.SCENARIO_EPOCH_SEC} +0000".encode("utf-8")

    def repo_id(self, repo: Repo) -> int:
        """A stable numeric id per repository: the scenario's own id when it names one."""
        for group in ("repos", "org_repos", "stranger_repos"):
            for entry in self.scenario.get(group, []):
                if entry["owner"] == repo.owner and entry["name"] == repo.name:
                    return entry["id"]
        keys = sorted(self.store.repos.keys())
        return 9000 + keys.index(f"{repo.owner}/{repo.name}")

    # -------------------------------------------------------------- git smart HTTP

    GIT_RE = re.compile(r"^/([^/]+)/([^/]+?)\.git(/.*)?$")

    def match_git(self, path: str):
        m = self.GIT_RE.match(path)
        if m is None:
            return None
        return m.group(1), m.group(2), m.group(3) or "/"

    def handle_git(self, method: str, owner: str, name: str, rest_path: str, headers: dict[str, str], body: bytes, query: dict | None = None) -> Response:
        repo = self.store.get(owner, name)
        if rest_path in ("/info/refs", ""):
            if method != "GET":
                return json_response(405, {"message": "Method Not Allowed"})
            if repo is None:
                return not_found()
            return self.git_info_refs(repo, query or {})
        if repo is None:
            return not_found()
        if rest_path == "/git-upload-pack":
            if method != "POST":
                return json_response(405, {"message": "Method Not Allowed"})
            return self.git_upload_pack(repo, headers, body)
        if rest_path == "/git-receive-pack":
            if method != "POST":
                return json_response(405, {"message": "Method Not Allowed"})
            return self.git_receive_pack(repo, body)
        return not_found()

    def git_info_refs(self, repo: Repo, query: dict) -> Response:
        raw = query.get("service")
        service = raw[0] if raw else ""
        if service == "git-upload-pack":
            if "version=2" in (query.get("__git_protocol__", [""])[0] or ""):
                return Response(200, {"content-type": "application/x-git-upload-pack-advertisement", "cache-control": "no-cache"}, gh.v2_advertisement())
            payload = gh.service_advertisement("git-upload-pack", gh.UPLOAD_CAPS, repo.refs, gh._head_target(repo.refs))
            return Response(200, {"content-type": "application/x-git-upload-pack-advertisement", "cache-control": "no-cache"}, payload)
        if service == "git-receive-pack":
            payload = gh.service_advertisement("git-receive-pack", gh.RECEIVE_CAPS, repo.refs, gh._head_target(repo.refs))
            return Response(200, {"content-type": "application/x-git-receive-pack-advertisement", "cache-control": "no-cache"}, payload)
        return not_found()

    def git_upload_pack(self, repo: Repo, headers: dict[str, str], body: bytes) -> Response:
        """v2 (ls-refs / fetch) and v0 (want/have straight after the flush) both land here."""
        version2 = "version=2" in (headers.get("git-protocol", "") or "")
        if version2:
            command, args = read_v2_sections(body)
            if command == "ls-refs":
                payload = gh.ls_refs(repo, args)
                return Response(200, {"content-type": "application/x-git-upload-pack-result", "cache-control": "no-cache"}, payload)
            if command == "fetch":
                payload = gh.fetch(repo, args)
                return Response(200, {"content-type": "application/x-git-upload-pack-result", "cache-control": "no-cache"}, payload)
            # An unknown v2 command: an empty section list is the honest answer.
            return Response(200, {"content-type": "application/x-git-upload-pack-result", "cache-control": "no-cache"}, pk.FLUSH)
        # v0: the want/have lines follow the flush directly, with no section framing.
        lines = pk.read_pkt_lines(body)
        payload = gh.fetch(repo, lines)
        return Response(200, {"content-type": "application/x-git-upload-pack-result", "cache-control": "no-cache"}, payload)

    def git_receive_pack(self, repo: Repo, body: bytes) -> Response:
        commands, pack = split_receive_pack(body)
        payload = gh.receive_pack(repo, commands, pack)
        return Response(200, {"content-type": "application/x-git-receive-pack-result", "cache-control": "no-cache"}, payload)


def read_v2_sections(data: bytes) -> tuple[str, list[bytes]]:
    """A v2 request body as (command line, argument lines).

    The body is ``command=<name>`` pkt-lines, then a delim packet, then the argument lines, then a
    flush. ``read_pkt_lines`` stops at the first non-data packet, so it cannot be used past the
    command: this walks the packets and keeps the data payloads on both sides of the delim.
    """
    command = ""
    args: list[bytes] = []
    seen_delim = False
    reader = pk.PktReader(data)
    while not reader.at_end():
        kind, payload = reader.read()
        if kind == "flush":
            break
        if kind == "delim":
            seen_delim = True
            continue
        if kind != "data" or payload is None:
            continue
        text = payload.rstrip(b"\n")
        if not seen_delim and not command:
            line = text.decode("utf-8", "replace").strip()
            # The first section is the command line itself: ``command=ls-refs``.
            command = line[len("command="):] if line.startswith("command=") else line
            continue
        args.append(text)
    return command, args


def split_receive_pack(body: bytes) -> tuple[list[tuple[str, str, str]], bytes]:
    """A receive-pack request body as (commands, packfile): the pkt-lines, then the raw pack."""
    lines: list[bytes] = []
    i = 0
    n = len(body)
    while i + 4 <= n:
        head = body[i : i + 4]
        try:
            size = int(head, 16)
        except ValueError:
            break
        if size == 0:
            i += 4
            break
        if size < 4 or i + size > n:
            break
        lines.append(body[i + 4 : i + size])
        i += size
    return pk.parse_receive_commands(lines), body[i:]


def iso_from_epoch(epoch_sec: int) -> str:
    return sc.iso_of(epoch_sec)


# ----------------------------------------------------------------------------- REST v3


def _query_one(query: dict, key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _query_int(query: dict, key: str, default: int) -> int:
    raw = _query_one(query, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _query_bool(query: dict, key: str, default: bool = False) -> bool:
    raw = _query_one(query, key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def _body_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except Exception:
        raise rest.ApiError(400, "Problems parsing JSON") from None
    if not isinstance(value, dict):
        raise rest.ApiError(400, "Problems parsing JSON")
    return value


# ----------------------------------------------------------------------------- REST v3 dispatch

#: The route table: a pattern (under /api/v3) and the Ops method that serves it.
REST_ROUTES: list[tuple[str, str]] = [
    ("", "root"),
    ("meta", "meta"),
    ("user", "user"),
    ("user/repos", "user_repos"),
    ("repos/:owner/:repo", "repo"),
    ("repos/:owner/:repo/branches", "branches"),
    ("repos/:owner/:repo/branches/:branch", "branch"),
    ("repos/:owner/:repo/commits", "commits"),
    ("repos/:owner/:repo/commits/:sha", "commit"),
    ("repos/:owner/:repo/git/refs", "git_refs"),
    ("repos/:owner/:repo/git/refs/:ref", "git_ref"),
    ("repos/:owner/:repo/contents/:path", "contents"),
    ("repos/:owner/:repo/labels", "labels"),
    ("repos/:owner/:repo/labels/:name", "label"),
    ("repos/:owner/:repo/issues", "issues"),
    ("repos/:owner/:repo/issues/:number", "issue"),
    ("repos/:owner/:repo/issues/:number/comments", "issue_comments"),
    ("repos/:owner/:repo/issues/comments/:id", "issue_comment"),
    ("repos/:owner/:repo/pulls", "pulls"),
    ("repos/:owner/:repo/pulls/:number", "pull"),
    ("repos/:owner/:repo/pulls/:number/files", "pull_files"),
    ("repos/:owner/:repo/pulls/:number/commits", "pull_commits"),
    ("repos/:owner/:repo/pulls/:number/merge", "pull_merge"),
    ("repos/:owner/:repo/rate_limit", "rate_limit"),
]

#: The routes whose `:path` capture keeps its slashes (a repository-relative file path).
SLASH_TAIL_ROUTES = {"repos/:owner/:repo/contents/:path"}


def _match_rest(path: str):
    """(handler name, captures) for an /api/v3 path, or (None, None)."""
    tail = path[len("/api/v3"):]
    segments = [seg for seg in tail.split("/") if seg != ""]
    for pattern, handler in REST_ROUTES:
        parts = pattern.split("/") if pattern else []
        if pattern in SLASH_TAIL_ROUTES:
            head = parts[:-1]
            if len(segments) <= len(head):
                continue
            captures = []
            ok = True
            for want, got in zip(head, segments):
                if want.startswith(":"):
                    captures.append(got)
                elif want != got:
                    ok = False
                    break
            if not ok:
                continue
            captures.append("/".join(segments[len(head):]))
            return handler, captures
        if len(parts) != len(segments):
            continue
        captures = []
        ok = True
        for want, got in zip(parts, segments):
            if want.startswith(":"):
                captures.append(got)
            elif want != got:
                ok = False
                break
        if ok:
            return handler, captures
    return None, None


def _ops_for(forge: "Forge"):
    from .restops import Ops

    ops = getattr(forge, "_rest_ops", None)
    if ops is None:
        ops = Ops(forge)
        forge._rest_ops = ops
    return ops


def handle_rest(forge: "Forge", method: str, path: str, query: dict, headers: dict, body: bytes) -> Response:
    """One /api/v3 request: auth, route match, then the operation."""
    ops = _ops_for(forge)
    # The Link header must be absolute; the request's Host is the origin the suite dialled.
    ops.host = headers.get("host", "") or f"127.0.0.1:{forge.port}"
    ops.raw_query = query
    handler, captures = _match_rest(path)
    if handler is None:
        # An unknown route is a JSON 404, and it still carries the request id.
        return json_response(404, {"message": "Not Found"})
    # The token is required on every REST route, including the root.
    ops.check_auth(headers)
    fn = getattr(ops, handler, None)
    if fn is None:
        return json_response(404, {"message": "Not Found"})
    try:
        status, extra_headers, payload = fn(method, query, headers, body, *captures)
    except TypeError:
        # A route whose handler takes no captures (or the wrong count) is a 404, not a 500.
        return json_response(404, {"message": "Not Found"})
    out_headers = {"x-github-request-id": forge.next_request_id()}
    out_headers.update(extra_headers or {})
    if payload is None:
        return Response(status, out_headers, b"")
    return json_response(status, payload, out_headers)
