"""The GraphQL v4 surface: a small document reader, an executor and the schema's types.

The suite (and gh, and github-mcp-server) post ``{"query": ..., "variables": {...}}`` to
``/api/graphql`` and read back ``{data, errors}``. There is no GraphQL library in the pod, so this
module reads the document itself: it parses a selection set, resolves each field against the store,
and shapes the answer the way GitHub v4 shapes it.

What it must answer, from the suite's own documents:

* ``viewer { login }``
* ``repository(owner:, name:)`` with ``id``, ``databaseId``, ``name``, ``owner { login }``,
  ``defaultBranchRef { name }``, ``viewerPermission``, ``hasIssuesEnabled``, ``issue(number:)``,
  ``issues(first:, states:, orderBy:, after:)`` with ``nodes`` and ``pageInfo``,
  ``pullRequests(first:)`` and ``pullRequest(number:)``
* ``Issue`` and ``PullRequest`` with ``files(first:)``
* ``__type(name:)`` with ``fields(includeDeprecated:)`` and ``enumValues(includeDeprecated:)``
* ``node(id:)`` with a ``... on Repository`` fragment
* the mutations ``createIssue``, ``closeIssue``, ``createPullRequest``, ``mergePullRequest`` and
  ``addComment``
* two operation documents in one body, answered in order
* an unknown field: HTTP 200 with an ``errors`` array naming it
* an unknown repository: ``data.repository = null`` with no errors

Global ids are opaque: ``R_<id>`` for a repository, ``I_<id>`` for an issue, ``PR_<id>`` for a pull
request, ``C_<id>`` for a comment, each base64-wrapped the way Relay spells them. They are stable
across reads and are never the plain number.
"""

from __future__ import annotations

import base64
import json
import re

from .store import Repo, HEADS

#: The permission the token's own login holds on its repositories.
VIEWER_PERMISSION = "ADMIN"

#: How many nodes a connection answers when the document names no ``first``.
DEFAULT_PAGE = 30
#: The largest page a document may ask for.
MAX_PAGE = 100

#: The fields each introspected type lists, in the order GitHub lists them.
SCHEMA_FIELDS: dict[str, list[str]] = {
    "Issue": ["id", "number", "title", "state", "body", "author", "createdAt", "updatedAt", "closed", "comments", "labels", "url"],
    "PullRequest": [
        "id", "number", "title", "state", "merged", "mergeable", "headRefName", "baseRefName",
        "headRefOid", "baseRefOid", "files", "body", "author", "createdAt", "updatedAt", "closed", "url",
    ],
    "Repository": [
        "id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef", "viewerPermission",
        "hasIssuesEnabled", "issue", "issues", "pullRequests", "pullRequest", "description", "isPrivate",
        "createdAt", "url",
    ],
    "User": ["id", "login", "name", "email", "url"],
    "IssueConnection": ["nodes", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "pageInfo", "totalCount"],
    "PullRequestChangedFile": ["path", "additions", "deletions"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor", "endCursor"],
}

#: The enums the introspection probes read.
SCHEMA_ENUMS: dict[str, list[str]] = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
}

#: The scalars the schema names, so an introspection read of one is not "unknown".
SCHEMA_SCALARS = {"String", "Int", "Boolean", "ID", "Float", "DateTime", "URI", "HTML", "Base64String"}

#: The field names the executor knows on each type: a document that names anything else is the
#: 200-with-errors answer the suite's invalid-field check reads.
KNOWN_FIELDS: dict[str, set[str]] = {
    "Query": {"viewer", "repository", "node", "__type", "__schema", "__typename"},
    "Repository": {
        "id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef", "viewerPermission",
        "hasIssuesEnabled", "issue", "issues", "pullRequests", "pullRequest", "description", "isPrivate",
        "createdAt", "url", "isFork", "isEmpty", "visibility", "__typename",
    },
    "User": {"id", "login", "name", "email", "url", "avatarUrl", "databaseId", "__typename"},
    "Ref": {"name", "target", "prefix", "__typename"},
    "Issue": {
        "id", "number", "title", "state", "body", "author", "createdAt", "updatedAt", "closed",
        "closedAt", "url", "comments", "labels", "repository", "__typename",
    },
    "PullRequest": {
        "id", "number", "title", "state", "merged", "mergeable", "headRefName", "baseRefName",
        "headRefOid", "baseRefOid", "files", "body", "author", "createdAt", "updatedAt", "closed",
        "closedAt", "url", "repository", "__typename",
    },
    "IssueConnection": {"nodes", "pageInfo", "totalCount", "edges", "__typename"},
    "PullRequestConnection": {"nodes", "pageInfo", "totalCount", "edges", "__typename"},
    "PullRequestChangedFileConnection": {"nodes", "pageInfo", "totalCount", "edges", "__typename"},
    "PullRequestChangedFile": {"path", "additions", "deletions", "__typename"},
    "PageInfo": {"hasNextPage", "hasPreviousPage", "startCursor", "endCursor", "__typename"},
    "IssueCommentEdge": {"node", "cursor", "__typename"},
    "IssueComment": {"id", "body", "author", "createdAt", "url", "__typename"},
    "IssueCommentConnection": {"nodes", "edges", "pageInfo", "totalCount", "__typename"},
    "CreateIssuePayload": {"issue", "clientMutationId", "__typename"},
    "CloseIssuePayload": {"issue", "clientMutationId", "__typename"},
    "CreatePullRequestPayload": {"pullRequest", "clientMutationId", "__typename"},
    "MergePullRequestPayload": {"pullRequest", "clientMutationId", "__typename"},
    "AddCommentPayload": {"commentEdge", "clientMutationId", "__typename"},
    "Mutation": {"createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment", "__typename"},
    "__Type": {"name", "kind", "fields", "enumValues", "description", "__typename"},
    "__Field": {"name", "description", "type", "isDeprecated", "deprecationReason", "__typename"},
    "__EnumValue": {"name", "description", "isDeprecated", "deprecationReason", "__typename"},
    "__Schema": {"types", "queryType", "mutationType", "__typename"},
}

#: The mutation fields the executor serves.
MUTATIONS = {"createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"}


class GraphQLError(Exception):
    """A document that cannot be served: the answer is a 200 with an ``errors`` array."""

    def __init__(self, message: str, path: list | None = None):
        super().__init__(message)
        self.message = message
        self.path = path


# --------------------------------------------------------------------------- the document reader


class Field:
    """One field of a selection set: its name, its alias, its arguments and its sub-selections."""

    __slots__ = ("name", "alias", "args", "selection")

    def __init__(self, name: str, alias: str, args: dict, selection: list["Field"]):
        self.name = name
        self.alias = alias
        self.args = args
        self.selection = selection

    @property
    def key(self) -> str:
        return self.alias or self.name


class InlineFragment:
    """A ``... on Type { ... }`` selection: kept as its type condition and its fields."""

    __slots__ = ("type_condition", "selection")

    def __init__(self, type_condition: str, selection: list):
        self.type_condition = type_condition
        self.selection = selection


class Operation:
    """One operation document: its kind, its name, its variables and its root selection."""

    __slots__ = ("kind", "name", "variables", "selection")

    def __init__(self, kind: str, name: str, variables: list[str], selection: list):
        self.kind = kind
        self.name = name
        self.variables = variables
        self.selection = selection


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPREAD_RE = re.compile(r"\.\.\.")


class _Reader:
    """A cursor over a GraphQL document: whitespace, comments, names, values and selection sets."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def skip(self) -> None:
        text = self.text
        n = len(text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in " \t\r\n,":
                self.pos += 1
                continue
            if ch == "#":
                while self.pos < n and text[self.pos] not in "\r\n":
                    self.pos += 1
                continue
            break

    def eof(self) -> bool:
        self.skip()
        return self.pos >= len(self.text)

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self, expected: str) -> None:
        self.skip()
        if not self.text.startswith(expected, self.pos):
            raise GraphQLError(f"expected {expected!r} at offset {self.pos}")
        self.pos += len(expected)

    def name(self) -> str:
        self.skip()
        m = _NAME_RE.match(self.text, self.pos)
        if m is None:
            raise GraphQLError(f"expected a name at offset {self.pos}")
        self.pos = m.end()
        return m.group(0)

    def value(self) -> object:
        """One GraphQL value: a variable, a literal, a list or an input object."""
        self.skip()
        ch = self.text[self.pos] if self.pos < len(self.text) else ""
        if ch == "$":
            self.pos += 1
            return {"__variable__": self.name()}
        if ch == '"':
            return self._string()
        if ch == "[":
            self.pos += 1
            items = []
            while True:
                self.skip()
                if self.peek() == "]":
                    self.pos += 1
                    return items
                items.append(self.value())
        if ch == "{":
            self.pos += 1
            out: dict[str, object] = {}
            while True:
                self.skip()
                if self.peek() == "}":
                    self.pos += 1
                    return out
                key = self.name()
                self.take(":")
                out[key] = self.value()
        # A bare token: a number, true/false/null or an enum name.
        m = re.match(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", self.text[self.pos:])
        if m is not None:
            self.pos += m.end()
            text = m.group(0)
            return float(text) if ("." in text or "e" in text or "E" in text) else int(text)
        word = self.name()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "null":
            return None
        return word

    def _string(self) -> str:
        self.pos += 1
        out: list[str] = []
        text = self.text
        n = len(text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == "\\":
                self.pos += 1
                esc = text[self.pos] if self.pos < n else ""
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(esc, esc))
                self.pos += 1
                continue
            if ch == '"':
                self.pos += 1
                return "".join(out)
            out.append(ch)
            self.pos += 1
        raise GraphQLError("unterminated string")

    def selection_set(self) -> list:
        self.take("{")
        out: list = []
        while True:
            self.skip()
            ch = self.peek()
            if ch == "":
                raise GraphQLError("unterminated selection set")
            if ch == "}":
                self.pos += 1
                return out
            if ch == "." and _SPREAD_RE.match(self.text, self.pos):
                self.pos += 3
                self.skip()
                if self.text.startswith("on", self.pos) and not _NAME_RE.match(self.text, self.pos + 2):
                    self.pos += 2
                    type_condition = self.name()
                    out.append(InlineFragment(type_condition, self.selection_set()))
                    continue
                # A named fragment spread: the suite sends none, but skip it rather than fail.
                self.name()
                continue
            name = self.name()
            self.skip()
            alias = name
            if self.peek() == ":":
                self.pos += 1
                alias = name
                name = self.name()
            else:
                alias = ""
            args: dict[str, object] = {}
            self.skip()
            if self.peek() == "(":
                self.pos += 1
                while True:
                    self.skip()
                    if self.peek() == ")":
                        self.pos += 1
                        break
                    key = self.name()
                    self.take(":")
                    args[key] = self.value()
            self.skip()
            sub: list = []
            if self.peek() == "{":
                sub = self.selection_set()
            out.append(Field(name, alias, args, sub))


def parse_document(text: str) -> Operation:
    """One operation document as an Operation."""
    reader = _Reader(text)
    reader.skip()
    kind = "query"
    name = ""
    variables: list[str] = []
    if reader.peek() and _NAME_RE.match(reader.text, reader.pos):
        word = reader.name()
        if word in ("query", "mutation", "subscription"):
            kind = word
            reader.skip()
            if reader.peek() and _NAME_RE.match(reader.text, reader.pos):
                name = reader.name()
            reader.skip()
            if reader.peek() == "(":
                reader.pos += 1
                while True:
                    reader.skip()
                    if reader.peek() == ")":
                        reader.pos += 1
                        break
                    reader.take("$")
                    variables.append(reader.name())
                    reader.take(":")
                    _skip_type(reader)
        else:
            raise GraphQLError(f"unknown operation kind {word!r}")
    selection = reader.selection_set()
    return Operation(kind, name, variables, selection)


def _skip_type(reader: _Reader) -> None:
    """Skip a type reference: a name, a list or a non-null wrapper, with its default value."""
    reader.skip()
    if reader.peek() == "[":
        reader.pos += 1
        _skip_type(reader)
        reader.skip()
        if reader.peek() == "]":
            reader.pos += 1
    else:
        reader.name()
    reader.skip()
    if reader.peek() == "!":
        reader.pos += 1
    reader.skip()
    if reader.peek() == "=":
        reader.pos += 1
        reader.value()


# --------------------------------------------------------------------------- global ids


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _unb64(text: str) -> str | None:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None


def repo_gid(repo: Repo) -> str:
    return _b64(f"R_{repo.owner}/{repo.name}")


def issue_gid(repo: Repo, number: int) -> str:
    return _b64(f"I_{repo.owner}/{repo.name}#{number}")


def pull_gid(repo: Repo, number: int) -> str:
    return _b64(f"PR_{repo.owner}/{repo.name}#{number}")


def comment_gid(repo: Repo, comment_id: int) -> str:
    return _b64(f"C_{repo.owner}/{repo.name}#{comment_id}")


def cursor_of(kind: str, index: int) -> str:
    """A cursor is an opaque base64 of its kind and its offset: stable, and never a bare number."""
    return _b64(f"cursor:{kind}:{index}")


def index_of_cursor(cursor: str) -> int:
    raw = _unb64(cursor)
    if raw is None or not raw.startswith("cursor:"):
        return 0
    try:
        return max(0, int(raw.rsplit(":", 1)[1]))
    except (IndexError, ValueError):
        return 0


# --------------------------------------------------------------------------- the executor


#: The type -> handler table: a type's handler is not always its name lowercased (PullRequest is
#: served by ``_pull_field``, PullRequestChangedFile by ``_pullrequestchangedfile_field``).
TYPE_HANDLERS: dict[str, str] = {
    "User": "_user_field",
    "Repository": "_repository_field",
    "Ref": "_ref_field",
    "Issue": "_issue_field",
    "IssueComment": "_issuecomment_field",
    "PullRequest": "_pull_field",
    "PullRequestChangedFile": "_pullrequestchangedfile_field",
    "PageInfo": "_pageinfo_field",
    "IssueCommentEdge": "_issuecommentedge_field",
    "CreateIssuePayload": "_createissuepayload_field",
    "CloseIssuePayload": "_closeissuepayload_field",
    "CreatePullRequestPayload": "_createpullrequestpayload_field",
    "MergePullRequestPayload": "_mergepullrequestpayload_field",
    "AddCommentPayload": "_addcommentpayload_field",
    "__Type": "_type_field",
    "__Schema": "_schema_field",
    "__Field": "_field_field",
    "__EnumValue": "_enumvalue_field",
}


def _handler_for(type_name: str):
    """The unbound method that resolves one type's fields, or None."""
    name = TYPE_HANDLERS.get(type_name)
    if name is None:
        name = f"_{type_name.strip('_').lower()}_field"
    handler = getattr(Executor, name, None)
    if handler is None:
        return None
    if isinstance(handler, property):
        return handler.fget
    return handler


def iso_of(epoch_sec: int) -> str:
    import datetime

    dt = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=epoch_sec)
    return f"{dt:%Y-%m-%dT%H:%M:%SZ}"


class Executor:
    """One GraphQL request: the forge's store, the token's login and the document's variables."""

    def __init__(self, forge, variables: dict | None = None):
        self.forge = forge
        self.store = forge.store
        self.login = forge.login
        self.variables = variables or {}
        self.errors: list[dict] = []

    # -------------------------------------------------------------- value plumbing

    def resolve(self, value):
        """A parsed argument with its ``$variables`` substituted."""
        if isinstance(value, dict):
            if set(value.keys()) == {"__variable__"}:
                return self.variables.get(value["__variable__"])  # type: ignore[index]
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def args_of(self, field: Field) -> dict:
        return {k: self.resolve(v) for k, v in field.args.items()}

    def error(self, message: str, path: list | None = None) -> None:
        entry: dict = {"message": message}
        if path:
            entry["path"] = path
        self.errors.append(entry)

    # -------------------------------------------------------------- the root

    def run(self, operation: Operation) -> dict:
        """One operation as its ``{data, errors}`` envelope."""
        if operation.kind == "mutation":
            data = self.run_selection("Mutation", operation.selection, None, [])
        else:
            data = self.run_selection("Query", operation.selection, None, [])
        out: dict = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    def run_selection(self, type_name: str, selection: list, subject, path: list) -> dict:
        out: dict = {}
        for entry in selection:
            if isinstance(entry, InlineFragment):
                if self._fragment_applies(entry.type_condition, type_name):
                    out.update(self.run_selection(entry.type_condition, entry.selection, subject, path))
                continue
            key = entry.key
            try:
                out[key] = self.resolve_field(type_name, entry, subject, path + [key])
            except GraphQLError as err:
                self.error(err.message, err.path or path + [key])
                out[key] = None
        return out

    def _fragment_applies(self, type_condition: str, type_name: str) -> bool:
        if type_condition == type_name:
            return True
        # The only interface the suite fragments on is Node, which Repository satisfies.
        return type_condition in ("Node",)

    def resolve_field(self, type_name: str, field: Field, subject, path: list):
        name = field.name
        if name == "__typename":
            return type_name
        known = KNOWN_FIELDS.get(type_name)
        if known is not None and name not in known:
            raise GraphQLError(f"Field '{name}' doesn't exist on type '{type_name}'", path)
        if type_name == "Query":
            return self.query_field(name, field, path)
        if type_name == "Mutation":
            return self.mutation_field(name, field, path)
        handler = _handler_for(type_name)
        if handler is None:
            raise GraphQLError(f"Field '{name}' cannot be resolved on type '{type_name}'", path)
        return handler(self, name, field, subject, path)

    # -------------------------------------------------------------- Query

    def query_field(self, name: str, field: Field, path: list):
        args = self.args_of(field)
        if name == "viewer":
            return self.run_selection("User", field.selection, {"login": self.login}, path)
        if name == "repository":
            owner = args.get("owner")
            repo_name = args.get("name")
            if not isinstance(owner, str) or not isinstance(repo_name, str):
                raise GraphQLError("repository needs owner and name", path)
            repo = self.store.get(owner, repo_name)
            if repo is None:
                return None
            return self.run_selection("Repository", field.selection, repo, path)
        if name == "node":
            return self.node_field(args.get("id"), field, path)
        if name == "__type":
            return self.type_field(args.get("name"), field, path)
        if name == "__schema":
            return self.run_selection("__Schema", field.selection, None, path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'Query'", path)

    def node_field(self, gid, field: Field, path: list):
        if not isinstance(gid, str):
            return None
        raw = _unb64(gid)
        if raw is None:
            return None
        if raw.startswith("R_"):
            full = raw[2:]
            if "/" not in full:
                return None
            owner, repo_name = full.split("/", 1)
            repo = self.store.get(owner, repo_name)
            if repo is None:
                return None
            return self.run_selection("Repository", field.selection, repo, path)
        if raw.startswith("I_"):
            repo, number = self._issue_from_gid(raw[2:])
            if repo is None or number is None or number not in repo.issues:
                return None
            return self.run_selection("Issue", field.selection, (repo, repo.issues[number]), path)
        if raw.startswith("PR_"):
            repo, number = self._issue_from_gid(raw[3:])
            if repo is None or number is None or number not in repo.pulls:
                return None
            return self.run_selection("PullRequest", field.selection, (repo, repo.pulls[number]), path)
        return None

    def _issue_from_gid(self, raw: str):
        """``owner/name#number`` as (repo, number); the callers strip the global id's prefix."""
        if "#" not in raw or "/" not in raw:
            return None, None
        full, number = raw.split("#", 1)
        owner, repo_name = full.split("/", 1)
        repo = self.store.get(owner, repo_name)
        try:
            return repo, int(number)
        except ValueError:
            return repo, None

    def type_field(self, type_name, field: Field, path: list):
        if not isinstance(type_name, str):
            return None
        if type_name in SCHEMA_FIELDS:
            subject = ("type", type_name)
        elif type_name in SCHEMA_ENUMS:
            subject = ("enum", type_name)
        elif type_name in SCHEMA_SCALARS:
            subject = ("scalar", type_name)
        else:
            return None
        return self.run_selection("__Type", field.selection, subject, path)

    # -------------------------------------------------------------- __Type

    def _type_field(self, name: str, field: Field, subject, path: list):
        kind, type_name = subject
        if name == "name":
            return type_name
        if name == "kind":
            return {"type": "OBJECT", "enum": "ENUM", "scalar": "SCALAR"}[kind]
        if name == "description":
            return f"the {type_name} type"
        if name == "fields":
            if kind != "type":
                return None
            return [{"name": f} for f in SCHEMA_FIELDS[type_name]]
        if name == "enumValues":
            if kind != "enum":
                return None
            return [{"name": e} for e in SCHEMA_ENUMS[type_name]]
        raise GraphQLError(f"Field '{name}' doesn't exist on type '__Type'", path)

    def _schema_field(self, name: str, field: Field, subject, path: list):
        if name == "types":
            out = []
            for type_name in SCHEMA_FIELDS:
                out.append(self.run_selection("__Type", field.selection, ("type", type_name), path))
            for type_name in SCHEMA_ENUMS:
                out.append(self.run_selection("__Type", field.selection, ("enum", type_name), path))
            return out
        if name == "queryType":
            return {"name": "Query"}
        if name == "mutationType":
            return {"name": "Mutation"}
        raise GraphQLError(f"Field '{name}' doesn't exist on type '__Schema'", path)

    def _field_field(self, name: str, field: Field, subject, path: list):
        if isinstance(subject, dict):
            return subject.get(name)
        raise GraphQLError(f"Field '{name}' doesn't exist on type '__Field'", path)

    def _enumvalue_field(self, name: str, field: Field, subject, path: list):
        if isinstance(subject, dict):
            return subject.get(name)
        raise GraphQLError(f"Field '{name}' doesn't exist on type '__EnumValue'", path)

    # -------------------------------------------------------------- User

    def _user_field(self, name: str, field: Field, subject, path: list):
        login = subject["login"] if isinstance(subject, dict) else self.login
        uid = self.forge.login_id
        if name == "login":
            return login
        if name == "id":
            return _b64(f"U_{login}")
        if name == "databaseId":
            return uid
        if name == "name":
            return login
        if name == "email":
            return self.forge.scenario["viewer"]["email"]
        if name == "url":
            return f"https://github.com/{login}"
        if name == "avatarUrl":
            return f"/api/avatars/{uid}.png"
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'User'", path)

    # -------------------------------------------------------------- Repository

    def _repository_field(self, name: str, field: Field, subject: Repo, path: list):
        args = self.args_of(field)
        if name == "id":
            return repo_gid(subject)
        if name == "databaseId":
            return self.forge.repo_id(subject)
        if name == "name":
            return subject.name
        if name == "nameWithOwner":
            return f"{subject.owner}/{subject.name}"
        if name == "description":
            return subject.description
        if name == "isPrivate":
            return subject.private
        if name == "visibility":
            return "PRIVATE" if subject.private else "PUBLIC"
        if name == "isFork":
            return False
        if name == "isEmpty":
            return self._branch_tip(subject) is None
        if name == "url":
            return f"https://github.com/{subject.owner}/{subject.name}"
        if name == "createdAt":
            return subject.created_at
        if name == "owner":
            return self.run_selection("User", field.selection, {"login": subject.owner}, path)
        if name == "defaultBranchRef":
            tip = self._branch_tip(subject)
            if tip is None:
                return None
            return self.run_selection("Ref", field.selection, {"name": subject.default_branch}, path)
        if name == "viewerPermission":
            return VIEWER_PERMISSION if subject.owner == self.login else "READ"
        if name == "hasIssuesEnabled":
            return True
        if name == "issue":
            number = args.get("number")
            if not isinstance(number, int) or number not in subject.issues:
                return None
            return self.run_selection("Issue", field.selection, (subject, subject.issues[number]), path)
        if name == "issues":
            nodes = [(subject, issue) for issue in self._issue_nodes(subject, args)]
            return self._connection("IssueConnection", field, nodes, "issues", path)
        if name == "pullRequests":
            nodes = [(subject, subject.pulls[n]) for n in sorted(subject.pulls, reverse=True)]
            states = args.get("states")
            if isinstance(states, list):
                wanted = {s for s in states if isinstance(s, str)}
                nodes = [p for p in nodes if self._pull_state(p[1]) in wanted]
            first = self._first_of(args)
            nodes = nodes[:first]
            return self._connection("PullRequestConnection", field, nodes, "pulls", path)
        if name == "pullRequest":
            number = args.get("number")
            if not isinstance(number, int) or number not in subject.pulls:
                return None
            return self.run_selection("PullRequest", field.selection, (subject, subject.pulls[number]), path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'Repository'", path)

    def _branch_tip(self, repo: Repo) -> str | None:
        tip = repo.refs.get(HEADS + repo.default_branch)
        if tip is not None:
            return tip
        head = repo.refs.get("HEAD")
        if head and head.startswith("ref: "):
            return repo.refs.get(head[5:])
        return None

    def _first_of(self, args: dict) -> int:
        first = args.get("first")
        if isinstance(first, bool) or not isinstance(first, int):
            return DEFAULT_PAGE
        return max(0, min(MAX_PAGE, first))

    def _issue_nodes(self, repo: Repo, args: dict) -> list[dict]:
        """The repository's issues, filtered by states and ordered by orderBy (CREATED_AT DESC default)."""
        nodes = [repo.issues[n] for n in sorted(repo.issues)]
        states = args.get("states")
        if isinstance(states, list) and states:
            wanted = {s for s in states if isinstance(s, str)}
            nodes = [i for i in nodes if self._issue_state(i) in wanted]
        order_by = args.get("orderBy")
        field_name = "CREATED_AT"
        direction = "DESC"
        if isinstance(order_by, dict):
            if isinstance(order_by.get("field"), str):
                field_name = order_by["field"]
            if isinstance(order_by.get("direction"), str):
                direction = order_by["direction"]
        if field_name == "CREATED_AT":
            # Creation order is the issue number: ties break by number, which is the same key.
            nodes.sort(key=lambda i: i["number"], reverse=(direction != "ASC"))
        elif field_name == "UPDATED_AT":
            nodes.sort(key=lambda i: (i.get("updated_at") or "", i["number"]), reverse=(direction != "ASC"))
        elif field_name == "COMMENTS":
            nodes.sort(key=lambda i: (len(repo.comments.get(i["number"], [])), i["number"]), reverse=(direction != "ASC"))
        return nodes

    def _connection(self, type_name: str, field: Field, nodes: list, kind: str, path: list) -> dict:
        """A connection: the nodes of one page plus its pageInfo, cursor-paged by ``after``."""
        args = self.args_of(field)
        after = args.get("after")
        start = 0
        if isinstance(after, str) and after != "":
            start = index_of_cursor(after)
        first = self._first_of(args)
        page = nodes[start : start + first]
        has_next = start + len(page) < len(nodes)
        end_cursor = cursor_of(kind, start + len(page)) if page else None
        out: dict = {}
        for entry in field.selection:
            if isinstance(entry, InlineFragment):
                continue
            key = entry.key
            if entry.name == "nodes":
                out[key] = [self.run_selection(self._node_type(type_name), entry.selection, n, path + [key]) for n in page]
            elif entry.name == "totalCount":
                out[key] = len(nodes)
            elif entry.name == "edges":
                out[key] = [
                    {
                        "cursor": cursor_of(kind, start + index + 1),
                        "node": self.run_selection(self._node_type(type_name), entry.selection, n, path + [key]),
                    }
                    for index, n in enumerate(page)
                ]
            elif entry.name == "pageInfo":
                out[key] = {
                    "hasNextPage": has_next,
                    "hasPreviousPage": start > 0,
                    "startCursor": cursor_of(kind, start) if page else None,
                    "endCursor": end_cursor,
                }
            else:
                self.error(f"Field '{entry.name}' doesn't exist on type '{type_name}'", path + [key])
                out[key] = None
        return out

    def _node_type(self, connection_type: str) -> str:
        return {
            "IssueConnection": "Issue",
            "PullRequestConnection": "PullRequest",
            "PullRequestChangedFileConnection": "PullRequestChangedFile",
            "IssueCommentConnection": "IssueComment",
        }.get(connection_type, "Issue")

    # -------------------------------------------------------------- Issue

    def _issue_field(self, name: str, field: Field, subject, path: list):
        repo, issue = subject
        if name == "id":
            return issue_gid(repo, issue["number"])
        if name == "number":
            return issue["number"]
        if name == "title":
            return issue["title"]
        if name == "body":
            return issue["body"]
        if name == "state":
            return self._issue_state(issue)
        if name == "closed":
            return issue["state"] == "closed"
        if name == "createdAt":
            return issue["created_at"]
        if name == "updatedAt":
            return issue["updated_at"]
        if name == "closedAt":
            return issue.get("closed_at")
        if name == "url":
            return f"https://github.com/{repo.owner}/{repo.name}/issues/{issue['number']}"
        if name == "author":
            return self.run_selection("User", field.selection, {"login": issue["user"]}, path)
        if name == "repository":
            return self.run_selection("Repository", field.selection, repo, path)
        if name == "labels":
            return self.run_selection("IssueCommentConnection", field.selection, None, path)
        if name == "comments":
            comments = [(repo, c) for c in repo.comments.get(issue["number"], [])]
            return self._connection("IssueCommentConnection", field, comments, "comments", path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'Issue'", path)

    def _issue_state(self, issue: dict) -> str:
        return "CLOSED" if issue["state"] == "closed" else "OPEN"

    def _issuecomment_field(self, name: str, field: Field, subject, path: list):
        repo, comment = subject
        if name == "id":
            return comment_gid(repo, comment["id"])
        if name == "body":
            return comment["body"]
        if name == "createdAt":
            return comment["created_at"]
        if name == "url":
            return f"https://github.com/{repo.owner}/{repo.name}/issues/{comment['issue']}#issuecomment-{comment['id']}"
        if name == "author":
            return self.run_selection("User", field.selection, {"login": comment["user"]}, path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'IssueComment'", path)

    # -------------------------------------------------------------- PullRequest

    def _pull_state(self, pull: dict) -> str:
        if pull.get("merged"):
            return "MERGED"
        return "CLOSED" if pull["state"] == "closed" else "OPEN"

    def _pull_field(self, name: str, field: Field, subject, path: list):
        repo, pull = subject
        if name == "id":
            return pull_gid(repo, pull["number"])
        if name == "number":
            return pull["number"]
        if name == "title":
            return pull["title"]
        if name == "body":
            return pull["body"]
        if name == "state":
            return self._pull_state(pull)
        if name == "merged":
            return bool(pull.get("merged"))
        if name == "closed":
            return pull["state"] == "closed"
        if name == "headRefName":
            return pull["head"]
        if name == "baseRefName":
            return pull["base"]
        if name == "headRefOid":
            return pull.get("head_sha", "")
        if name == "baseRefOid":
            return pull.get("base_sha", "")
        if name == "mergeable":
            return "MERGEABLE" if pull.get("mergeable") else "CONFLICTING"
        if name == "createdAt":
            return pull["created_at"]
        if name == "updatedAt":
            return pull["updated_at"]
        if name == "closedAt":
            return pull.get("closed_at")
        if name == "url":
            return f"https://github.com/{repo.owner}/{repo.name}/pull/{pull['number']}"
        if name == "author":
            return self.run_selection("User", field.selection, {"login": pull["user"]}, path)
        if name == "repository":
            return self.run_selection("Repository", field.selection, repo, path)
        if name == "files":
            from . import restapi as rest

            files = rest.diff_files(repo, pull)
            first = self._first_of(self.args_of(field))
            return self._connection("PullRequestChangedFileConnection", field, files[:first], "files", path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'PullRequest'", path)

    def _pullrequestchangedfile_field(self, name: str, field: Field, subject, path: list):
        if name == "path":
            return subject.get("filename")
        if name == "additions":
            return subject.get("additions", 0)
        if name == "deletions":
            return subject.get("deletions", 0)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'PullRequestChangedFile'", path)

    def _ref_field(self, name: str, field: Field, subject, path: list):
        if name == "name":
            return subject["name"]
        if name == "prefix":
            return "refs/heads/"
        if name == "target":
            return None
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'Ref'", path)

    # -------------------------------------------------------------- Mutation

    def mutation_field(self, name: str, field: Field, path: list):
        args = self.args_of(field)
        if name not in MUTATIONS:
            raise GraphQLError(f"Field '{name}' doesn't exist on type 'Mutation'", path)
        payload = self._mutation(name, args, path)
        payload_type = {
            "createIssue": "CreateIssuePayload",
            "closeIssue": "CloseIssuePayload",
            "createPullRequest": "CreatePullRequestPayload",
            "mergePullRequest": "MergePullRequestPayload",
            "addComment": "AddCommentPayload",
        }[name]
        return self.run_selection(payload_type, field.selection, payload, path)

    def _mutation(self, name: str, args: dict, path: list) -> dict:
        from . import restapi as rest

        payload = args.get("input")
        if not isinstance(payload, dict):
            payload = {}
        if name == "createIssue":
            repo = self._repo_from_gid(payload.get("repositoryId"))
            if repo is None:
                raise GraphQLError("createIssue needs a repositoryId", path)
            title = payload.get("title")
            if not isinstance(title, str) or title == "":
                raise GraphQLError("createIssue needs a title", path)
            number = repo.next_issue
            repo.next_issue += 1
            repo.issues[number] = {
                "id": self.store.next_seq(),
                "number": number,
                "title": title,
                "body": payload.get("body") if isinstance(payload.get("body"), str) else "",
                "state": "open",
                "state_reason": None,
                "user": self.login,
                "labels": [],
                "created_at": iso_of(self.forge.scenario_epoch),
                "updated_at": iso_of(self.forge.scenario_epoch),
                "closed_at": None,
            }
            return {"issue": (repo, repo.issues[number]), "clientMutationId": payload.get("clientMutationId")}
        if name == "closeIssue":
            repo, number = self._issue_from_gid(self._gid_body(payload.get("issueId"), "I_"))
            if repo is None or number is None or number not in repo.issues:
                raise GraphQLError("closeIssue needs a known issueId", path)
            issue = repo.issues[number]
            issue["state"] = "closed"
            issue["closed_at"] = iso_of(self.forge.scenario_epoch)
            issue["updated_at"] = iso_of(self.forge.scenario_epoch)
            return {"issue": (repo, issue), "clientMutationId": payload.get("clientMutationId")}
        if name == "createPullRequest":
            repo = self._repo_from_gid(payload.get("repositoryId"))
            if repo is None:
                raise GraphQLError("createPullRequest needs a repositoryId", path)
            head = payload.get("headRefName")
            base = payload.get("baseRefName")
            title = payload.get("title")
            if not isinstance(head, str) or not isinstance(base, str):
                raise GraphQLError("createPullRequest needs headRefName and baseRefName", path)
            if not isinstance(title, str) or title == "":
                title = f"{head} into {base}"
            head_sha = repo.refs.get(HEADS + head)
            base_sha = repo.refs.get(HEADS + base)
            if head_sha is None:
                raise GraphQLError(f"createPullRequest: no branch {head!r}", path)
            if base_sha is None:
                raise GraphQLError(f"createPullRequest: no branch {base!r}", path)
            number = repo.next_pull
            repo.next_pull += 1
            mergeable = head_sha != base_sha and repo.is_ancestor(base_sha, head_sha)
            pull = {
                "id": self.store.next_seq(),
                "number": number,
                "title": title,
                "body": payload.get("body") if isinstance(payload.get("body"), str) else "",
                "state": "open",
                "user": self.login,
                "head": head,
                "base": base,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "merged": False,
                "mergeable": mergeable,
                "mergeable_state": "clean" if mergeable else "dirty",
                "created_at": iso_of(self.forge.scenario_epoch),
                "updated_at": iso_of(self.forge.scenario_epoch),
                "closed_at": None,
                "merged_at": None,
                "merge_commit_sha": None,
                "files": rest.diff_files(repo, {"base_sha": base_sha, "head_sha": head_sha}),
            }
            repo.pulls[number] = pull
            return {"pullRequest": (repo, pull), "clientMutationId": payload.get("clientMutationId")}
        if name == "mergePullRequest":
            repo, number = self._issue_from_gid(self._gid_body(payload.get("pullRequestId"), "PR_"))
            if repo is None or number is None or number not in repo.pulls:
                raise GraphQLError("mergePullRequest needs a known pullRequestId", path)
            pull = repo.pulls[number]
            if pull["state"] != "open" or pull.get("merged"):
                raise GraphQLError("mergePullRequest: the pull request is not open", path)
            base_sha = repo.refs.get(HEADS + pull["base"])
            head_sha = pull.get("head_sha")
            if base_sha is None or head_sha is None:
                raise GraphQLError("mergePullRequest: the pull request is not mergeable", path)
            if base_sha != pull.get("base_sha") and not repo.is_ancestor(base_sha, head_sha):
                raise GraphQLError("mergePullRequest: the pull request is not mergeable", path)
            head_tree = repo.tree_of(head_sha)
            if head_tree is None:
                raise GraphQLError("mergePullRequest: the pull request is not mergeable", path)
            files = repo.flatten_tree(head_tree)
            author = self.forge.author_bytes()
            message = f"{pull['title']}\n\n{pull.get('body') or ''}".rstrip() + "\n"
            merge_sha = repo.commit_files(files, [base_sha], author, author, message.encode("utf-8"))
            repo.refs[HEADS + pull["base"]] = merge_sha
            if pull["base"] == repo.default_branch:
                repo.refs["HEAD"] = f"ref: {HEADS}{repo.default_branch}"
            pull["merged"] = True
            pull["state"] = "closed"
            pull["merged_at"] = iso_of(self.forge.scenario_epoch)
            pull["closed_at"] = pull["merged_at"]
            pull["merge_commit_sha"] = merge_sha
            pull["updated_at"] = pull["merged_at"]
            return {"pullRequest": (repo, pull), "clientMutationId": payload.get("clientMutationId")}
        if name == "addComment":
            repo, number = self._issue_from_gid(self._gid_body(payload.get("subjectId"), "I_"))
            if repo is None or number is None or number not in repo.issues:
                raise GraphQLError("addComment needs a known subjectId", path)
            body = payload.get("body")
            if not isinstance(body, str):
                raise GraphQLError("addComment needs a body", path)
            comment = {
                "id": self.store.next_seq(),
                "body": body,
                "user": self.login,
                "issue": number,
                "created_at": iso_of(self.forge.scenario_epoch),
                "updated_at": iso_of(self.forge.scenario_epoch),
            }
            repo.comments.setdefault(number, []).append(comment)
            return {"commentEdge": {"node": (repo, comment)}, "clientMutationId": payload.get("clientMutationId")}
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'Mutation'", path)

    def _gid_body(self, gid, prefix: str) -> str:
        """A global id's payload without its type prefix: ``I_owner/name#1`` -> ``owner/name#1``."""
        if not isinstance(gid, str):
            return ""
        raw = _unb64(gid)
        if raw is None:
            return ""
        return raw[len(prefix):] if raw.startswith(prefix) else raw

    def _repo_from_gid(self, gid) -> Repo | None:
        if not isinstance(gid, str):
            return None
        raw = _unb64(gid)
        if raw is None or not raw.startswith("R_"):
            return None
        full = raw[2:]
        if "/" not in full:
            return None
        owner, name = full.split("/", 1)
        return self.store.get(owner, name)

    # -------------------------------------------------------------- payload wrappers

    def _createissuepayload_field(self, name: str, field: Field, subject, path: list):
        return self._payload_field(name, field, subject, path, "issue", "Issue")

    def _closeissuepayload_field(self, name: str, field: Field, subject, path: list):
        return self._payload_field(name, field, subject, path, "issue", "Issue")

    def _createpullrequestpayload_field(self, name: str, field: Field, subject, path: list):
        return self._payload_field(name, field, subject, path, "pullRequest", "PullRequest")

    def _mergepullrequestpayload_field(self, name: str, field: Field, subject, path: list):
        return self._payload_field(name, field, subject, path, "pullRequest", "PullRequest")

    def _addcommentpayload_field(self, name: str, field: Field, subject, path: list):
        if name == "clientMutationId":
            return subject.get("clientMutationId")
        if name == "commentEdge":
            node = subject.get("commentEdge", {}).get("node")
            if node is None:
                return None
            return self.run_selection("IssueCommentEdge", field.selection, node, path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'AddCommentPayload'", path)

    def _payload_field(self, name: str, field: Field, subject, path: list, key: str, type_name: str):
        if name == "clientMutationId":
            return subject.get("clientMutationId")
        if name == key:
            node = subject.get(key)
            if node is None:
                return None
            return self.run_selection(type_name, field.selection, node, path)
        raise GraphQLError(f"Field '{name}' doesn't exist on type '{type_name}Payload'", path)

    def _issuecommentedge_field(self, name: str, field: Field, subject, path: list):
        if name == "node":
            return self.run_selection("IssueComment", field.selection, subject, path)
        if name == "cursor":
            return None
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'IssueCommentEdge'", path)

    def _pageinfo_field(self, name: str, field: Field, subject, path: list):
        if isinstance(subject, dict) and name in subject:
            return subject[name]
        raise GraphQLError(f"Field '{name}' doesn't exist on type 'PageInfo'", path)


def execute(forge, body: bytes) -> tuple[int, dict | list]:
    """One POST /api/graphql body as (status, payload).

    The body is one operation or a JSON array of them (GitHub's operation batching). A malformed
    body is a 400 with a message; a document that cannot be served is a 200 with an ``errors``
    array, which is what the wire promises.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, {"message": "Problems parsing JSON"}
    if isinstance(parsed, list):
        out = []
        for entry in parsed:
            out.append(_one(forge, entry))
        return 200, out
    if not isinstance(parsed, dict):
        return 400, {"message": "Problems parsing JSON"}
    return 200, _one(forge, parsed)


def _one(forge, entry) -> dict:
    if not isinstance(entry, dict):
        return {"data": None, "errors": [{"message": "each operation must be a JSON object"}]}
    query = entry.get("query")
    if not isinstance(query, str) or query.strip() == "":
        return {"data": None, "errors": [{"message": "a GraphQL document is required"}]}
    variables = entry.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    try:
        operation = parse_document(query)
    except GraphQLError as err:
        return {"data": None, "errors": [{"message": err.message}]}
    executor = Executor(forge, variables)
    return executor.run(operation)
