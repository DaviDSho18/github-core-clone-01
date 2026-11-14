"""The Model Context Protocol endpoint at /mcp: JSON-RPC 2.0 over the Streamable HTTP transport.

The suite's client (sdk/conformance/mcp-client.ts) speaks two eras and this server answers the
modern one, with the handshake era kept working for an older client:

* modern (2026-07-28): no handshake. Every request declares its revision in
  ``_meta['io.modelcontextprotocol/protocolVersion']`` and mirrors it in the ``MCP-Protocol-Version``
  header; ``server/discover`` answers the server's identity, its capabilities and its
  ``supportedVersions`` in one request. There is no session.
* legacy (2025-11-25 and earlier): ``initialize`` answers the negotiated revision and a
  ``serverInfo``, ``notifications/initialized`` is accepted, and an ``Mcp-Session-Id`` rides every
  answer.

The six tools the agent arm needs are ``create_repo``, ``get_repo``, ``create_issue``,
``list_issues``, ``create_pull_request`` and ``merge_pull_request``, named the way
github-mcp-server names them. Their arguments are that server's: ``create_repo {name, description?,
private?}``, ``get_repo {owner, repo}``, ``create_issue {owner, repo, title, body?}``,
``list_issues {owner, repo, state?}``, ``create_pull_request {owner, repo, title, head, base}`` and
``merge_pull_request {owner, repo, pull_number}``. Every property the suite may fill is optional
except the ones it always sends, so a mapped call never trips over a required property.

The tools act on the same store as REST: a repository and an issue created here read back over
``/api/v3`` as the same objects, their ids unchanged.
"""

from __future__ import annotations

import json

from . import restapi as rest
from .store import HEADS

#: The revision this server speaks first, and the ones it still answers.
PREFERRED_REVISION = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

#: The server's identity, in every surface that names one.
SERVER_NAME = "makefun-forge"
SERVER_VERSION = "1.0.0"

#: JSON-RPC's codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: The MCP endpoint's content type: one JSON body, never a stream.
CONTENT_TYPE = "application/json"


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

#: The tools, in a fixed order: two ``tools/list`` answers are byte-identical.
TOOLS: list[dict] = [
    {
        "name": "create_repo",
        "description": "Create a repository on the forge, owned by the token's login.",
        "inputSchema": _obj(
            {
                "name": {**_STR, "description": "The repository name."},
                "description": {**_STR, "description": "The repository description."},
                "private": {**_BOOL, "description": "Whether the repository is private."},
                "owner": {**_STR, "description": "The owner login (defaults to the token's login)."},
            },
            ["name"],
        ),
    },
    {
        "name": "get_repo",
        "description": "Read one repository by owner and name.",
        "inputSchema": _obj(
            {
                "owner": {**_STR, "description": "The repository owner's login."},
                "repo": {**_STR, "description": "The repository name."},
                "full_name": {**_STR, "description": "The owner/name pair, when owner and repo are not given."},
            },
            ["owner", "repo"],
        ),
    },
    {
        "name": "create_issue",
        "description": "Open an issue on a repository.",
        "inputSchema": _obj(
            {
                "owner": {**_STR, "description": "The repository owner's login."},
                "repo": {**_STR, "description": "The repository name."},
                "title": {**_STR, "description": "The issue title."},
                "body": {**_STR, "description": "The issue body."},
                "full_name": {**_STR, "description": "The owner/name pair, when owner and repo are not given."},
            },
            ["owner", "repo", "title"],
        ),
    },
    {
        "name": "list_issues",
        "description": "List the issues a repository holds, newest first.",
        "inputSchema": _obj(
            {
                "owner": {**_STR, "description": "The repository owner's login."},
                "repo": {**_STR, "description": "The repository name."},
                "state": {**_STR, "description": "open, closed or all."},
                "full_name": {**_STR, "description": "The owner/name pair, when owner and repo are not given."},
            },
            ["owner", "repo"],
        ),
    },
    {
        "name": "create_pull_request",
        "description": "Open a pull request from a head branch into a base branch.",
        "inputSchema": _obj(
            {
                "owner": {**_STR, "description": "The repository owner's login."},
                "repo": {**_STR, "description": "The repository name."},
                "title": {**_STR, "description": "The pull request title."},
                "body": {**_STR, "description": "The pull request body."},
                "head": {**_STR, "description": "The branch the change comes from."},
                "base": {**_STR, "description": "The branch the change lands on."},
                "full_name": {**_STR, "description": "The owner/name pair, when owner and repo are not given."},
            },
            ["owner", "repo", "title", "head", "base"],
        ),
    },
    {
        "name": "merge_pull_request",
        "description": "Merge an open pull request, squashing its commits onto the base branch.",
        "inputSchema": _obj(
            {
                "owner": {**_STR, "description": "The repository owner's login."},
                "repo": {**_STR, "description": "The repository name."},
                "pull_number": {**_INT, "description": "The pull request number."},
                "full_name": {**_STR, "description": "The owner/name pair, when owner and repo are not given."},
            },
            ["owner", "repo", "pull_number"],
        ),
    },
]

#: The tool names, for the unknown-tool answer.
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def tools_result() -> dict:
    """The ``tools/list`` result. Built once per call from the frozen table, so it never drifts."""
    return {"tools": [dict(tool) for tool in TOOLS]}


class RpcError(Exception):
    """A JSON-RPC error answer: its code and its message."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Session:
    """One /mcp request: the forge, the token check and the JSON-RPC dispatch."""

    def __init__(self, forge, headers: dict):
        self.forge = forge
        self.headers = headers

    # -------------------------------------------------------------- transport

    def handle(self, body: bytes, method: str) -> tuple[int, dict | None, dict]:
        """One POST /mcp body as (status, payload, extra headers)."""
        extra: dict = {}
        if method != "POST":
            return 405, _rpc_error(None, INVALID_REQUEST, "the MCP endpoint answers POST"), extra
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            # A body that is not JSON is the parse error, with the transport's 400.
            return 400, _rpc_error(None, PARSE_ERROR, "Parse error: the body is not JSON"), extra
        if isinstance(parsed, list):
            answers = [self._one(entry) for entry in parsed]
            answers = [a for a in answers if a is not None]
            if not answers:
                return 202, None, extra
            return 200, answers, extra
        if not isinstance(parsed, dict):
            return 400, _rpc_error(None, PARSE_ERROR, "Parse error: the body is not a JSON object"), extra
        answer = self._one(parsed)
        if answer is None:
            # A notification: the transport answers 202 with no body.
            return 202, None, extra
        if "error" in answer and isinstance(answer["error"], dict) and answer["error"].get("data", {}).get("status") == 401:
            # The token is required: an unauthenticated call is HTTP 401, not a JSON-RPC-level answer.
            return 401, answer, extra
        return 200, answer, extra

    def _one(self, entry) -> dict | None:
        if not isinstance(entry, dict):
            return _rpc_error(None, INVALID_REQUEST, "each message must be a JSON object")
        rpc_id = entry.get("id")
        method = entry.get("method")
        params = entry.get("params")
        if not isinstance(params, dict):
            params = {}
        if not isinstance(method, str) or method == "":
            return _rpc_error(rpc_id, INVALID_REQUEST, "a JSON-RPC method is required")
        notification = "id" not in entry
        try:
            result = self.dispatch(method, params)
        except RpcError as err:
            if notification:
                return None
            return _rpc_error(rpc_id, err.code, err.message, err.data)
        except Exception as err:  # never a 500 with an HTML body
            if notification:
                return None
            return _rpc_error(rpc_id, INTERNAL_ERROR, f"the tool failed: {type(err).__name__}: {err}")
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    # -------------------------------------------------------------- methods

    def dispatch(self, method: str, params: dict):
        if method == "server/discover":
            return self.discover()
        if method == "initialize":
            return self.initialize(params)
        if method == "notifications/initialized":
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return tools_result()
        if method == "tools/call":
            return self.tools_call(params)
        if method in ("resources/list", "resources/templates/list", "prompts/list"):
            return {"resources": []} if method == "resources/list" else ({"resourceTemplates": []} if method == "resources/templates/list" else {"prompts": []})
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def discover(self) -> dict:
        return {
            "resultType": "discover",
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "A GitHub-compatible forge: repositories, issues and pull requests.",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "io.modelcontextprotocol/protocolVersion": PREFERRED_REVISION,
            },
        }

    def initialize(self, params: dict) -> dict:
        offered = params.get("protocolVersion")
        revision = offered if isinstance(offered, str) and offered in SUPPORTED_VERSIONS else PREFERRED_REVISION
        return {
            "protocolVersion": revision,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    # -------------------------------------------------------------- tools/call

    def tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or name == "":
            raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        # The token is required: an unauthenticated call is refused before any tool exists.
        self._check_auth()
        if name not in TOOL_NAMES:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        handler = getattr(self, f"tool_{name}")
        try:
            payload = handler(arguments)
        except RpcError:
            raise
        except Exception as err:
            return _tool_error(f"{type(err).__name__}: {err}")
        return _tool_ok(payload)

    def _check_auth(self) -> None:
        token = self.headers.get("authorization", "")
        if not token.lower().startswith("bearer "):
            raise RpcError(INVALID_PARAMS, "Requires authentication", {"status": 401})
        expected = self.forge.store.token
        if expected and token[7:].strip() != expected:
            raise RpcError(INVALID_PARAMS, "Bad credentials", {"status": 401})

    # -------------------------------------------------------------- the tools

    def _owner_of(self, arguments: dict) -> str:
        owner = arguments.get("owner") or arguments.get("owner_login")
        if not isinstance(owner, str) or owner == "":
            full = arguments.get("full_name")
            if isinstance(full, str) and "/" in full:
                owner = full.split("/", 1)[0]
        return owner if isinstance(owner, str) and owner != "" else self.forge.login

    def _repo_of(self, arguments: dict) -> str:
        name = arguments.get("repo") or arguments.get("repository") or arguments.get("repo_name")
        if not isinstance(name, str) or name == "":
            full = arguments.get("full_name")
            if isinstance(full, str) and "/" in full:
                name = full.split("/", 1)[1]
        if not isinstance(name, str) or name == "":
            # create_repo names the repository with `name`; every other tool names it with `repo`.
            name = arguments.get("name")
        if not isinstance(name, str) or name == "":
            raise RpcError(INVALID_PARAMS, "a repository name is required")
        return name

    def _repo(self, arguments: dict):
        owner = self._owner_of(arguments)
        name = self._repo_of(arguments)
        if owner != self.forge.login:
            raise RpcError(INVALID_PARAMS, f"no repository {owner}/{name}")
        repo = self.forge.store.get(owner, name)
        if repo is None:
            raise RpcError(INVALID_PARAMS, f"no repository {owner}/{name}")
        return repo

    def tool_create_repo(self, arguments: dict) -> dict:
        name = self._repo_of(arguments)
        owner = self._owner_of(arguments)
        if owner != self.forge.login:
            raise RpcError(INVALID_PARAMS, f"the token may only create repositories for {self.forge.login}")
        if self.forge.store.get(owner, name) is not None:
            raise RpcError(INVALID_PARAMS, f"the repository {owner}/{name} already exists")
        repo = self.forge.store.create(
            owner,
            name,
            arguments.get("description") if isinstance(arguments.get("description"), str) else "",
            bool(arguments.get("private", False)),
            self.forge.now_iso(),
        )
        repo.default_branch = "main"
        return rest.repo_json(repo, self.forge.repo_id(repo))

    def tool_get_repo(self, arguments: dict) -> dict:
        repo = self._repo(arguments)
        return rest.repo_json(repo, self.forge.repo_id(repo))

    def tool_create_issue(self, arguments: dict) -> dict:
        repo = self._repo(arguments)
        title = arguments.get("title")
        if not isinstance(title, str) or title == "":
            raise RpcError(INVALID_PARAMS, "create_issue needs a title")
        number = repo.next_issue
        repo.next_issue += 1
        issue = {
            "id": self.forge.store.next_seq(),
            "number": number,
            "title": title,
            "body": arguments.get("body") if isinstance(arguments.get("body"), str) else "",
            "state": "open",
            "state_reason": None,
            "user": self.forge.login,
            "labels": [],
            "created_at": self.forge.now_iso(),
            "updated_at": self.forge.now_iso(),
            "closed_at": None,
        }
        repo.issues[number] = issue
        return rest.issue_json(repo, issue, self.forge.login_id)

    def tool_list_issues(self, arguments: dict) -> dict:
        repo = self._repo(arguments)
        state = arguments.get("state")
        if not isinstance(state, str) or state == "":
            state = "open"
        items = []
        for number in sorted(repo.issues, reverse=True):
            issue = repo.issues[number]
            if state not in ("all",) and issue["state"] != state:
                continue
            items.append(rest.issue_json(repo, issue, self.forge.login_id))
        return {"issues": items, "total_count": len(items)}

    def tool_create_pull_request(self, arguments: dict) -> dict:
        repo = self._repo(arguments)
        head = arguments.get("head")
        base = arguments.get("base")
        title = arguments.get("title")
        if not isinstance(head, str) or head == "":
            raise RpcError(INVALID_PARAMS, "create_pull_request needs a head branch")
        if not isinstance(base, str) or base == "":
            base = repo.default_branch
        if not isinstance(title, str) or title == "":
            title = f"{head} into {base}"
        head_sha = repo.refs.get(HEADS + head)
        base_sha = repo.refs.get(HEADS + base)
        if head_sha is None:
            raise RpcError(INVALID_PARAMS, f"no branch {head!r} in {repo.owner}/{repo.name}")
        if base_sha is None:
            raise RpcError(INVALID_PARAMS, f"no branch {base!r} in {repo.owner}/{repo.name}")
        number = repo.next_pull
        repo.next_pull += 1
        mergeable = head_sha != base_sha and repo.is_ancestor(base_sha, head_sha)
        pull = {
            "id": self.forge.store.next_seq(),
            "number": number,
            "title": title,
            "body": arguments.get("body") if isinstance(arguments.get("body"), str) else "",
            "state": "open",
            "user": self.forge.login,
            "head": head,
            "base": base,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "merged": False,
            "mergeable": mergeable,
            "mergeable_state": "clean" if mergeable else "dirty",
            "created_at": self.forge.now_iso(),
            "updated_at": self.forge.now_iso(),
            "closed_at": None,
            "merged_at": None,
            "merge_commit_sha": None,
            "files": rest.diff_files(repo, {"base_sha": base_sha, "head_sha": head_sha}),
        }
        repo.pulls[number] = pull
        return rest.pull_json(repo, pull, self.forge.login_id)

    def tool_merge_pull_request(self, arguments: dict) -> dict:
        repo = self._repo(arguments)
        raw = arguments.get("pull_number")
        if isinstance(raw, str) and raw.isdigit():
            raw = int(raw)
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise RpcError(INVALID_PARAMS, "merge_pull_request needs a pull_number")
        pull = repo.pulls.get(raw)
        if pull is None:
            raise RpcError(INVALID_PARAMS, f"no pull request #{raw} in {repo.owner}/{repo.name}")
        if pull["state"] != "open" or pull.get("merged"):
            raise RpcError(INVALID_PARAMS, f"pull request #{raw} is not open")
        base_sha = repo.refs.get(HEADS + pull["base"])
        head_sha = pull.get("head_sha")
        if base_sha is None or head_sha is None:
            raise RpcError(INVALID_PARAMS, f"pull request #{raw} is not mergeable")
        if base_sha != pull.get("base_sha") and not repo.is_ancestor(base_sha, head_sha):
            raise RpcError(INVALID_PARAMS, f"pull request #{raw} is not mergeable")
        head_tree = repo.tree_of(head_sha)
        if head_tree is None:
            raise RpcError(INVALID_PARAMS, f"pull request #{raw} is not mergeable")
        files = repo.flatten_tree(head_tree)
        author = self.forge.author_bytes()
        message = f"{pull['title']}\n\n{pull.get('body') or ''}".rstrip() + "\n"
        merge_sha = repo.commit_files(files, [base_sha], author, author, message.encode("utf-8"))
        repo.refs[HEADS + pull["base"]] = merge_sha
        if pull["base"] == repo.default_branch:
            repo.refs["HEAD"] = f"ref: {HEADS}{repo.default_branch}"
        pull["merged"] = True
        pull["state"] = "closed"
        pull["merged_at"] = self.forge.now_iso()
        pull["closed_at"] = pull["merged_at"]
        pull["merge_commit_sha"] = merge_sha
        pull["updated_at"] = pull["merged_at"]
        return {"sha": merge_sha, "merged": True, "message": "Pull Request successfully merged", "number": raw}


# --------------------------------------------------------------------------- JSON-RPC shapes


def _rpc_error(rpc_id, code: int, message: str, data=None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def _tool_ok(payload) -> dict:
    """A successful tools/call: the JSON in a text block, and again as structured content."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    """A tool that ran and failed: a result with isError, never a JSON-RPC error."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
