"""The REST v3 operations behind the route table in restroute.py.

Every handler answers (status, headers, body) through the shapes in restapi.py and the objects in
store.py, and raises rest.ApiError for anything that is not 2xx. Nothing here reads a clock or a
random source: ids, numbers and dates come from the seeded scenario and the store's own counters.
"""

from __future__ import annotations

import base64
import binascii

from . import restapi as rest
from . import scenario as sc
from .store import Repo, Store, StoreError, HEADS, TAGS

#: The token's login owns every repository this surface creates.
PER_PAGE = rest.PER_PAGE


def _now_iso() -> str:
    """The scenario's day one: every timestamp this surface writes comes from the seed, never a clock."""
    return sc.iso_of(sc.SCENARIO_EPOCH_SEC)


def _body_json(body: bytes) -> dict:
    import json

    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except Exception:
        raise rest.ApiError(400, "Problems parsing JSON") from None
    if not isinstance(value, dict):
        raise rest.ApiError(400, "Problems parsing JSON")
    return value


def _query_one(query: dict, key: str, default=None):
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
    return str(raw).lower() in ("1", "true", "yes")


class Ops:
    """The REST operations, bound to one Forge (its store, its login and its token)."""

    def __init__(self, forge) -> None:
        self.forge = forge
        #: The Host header of the request being served, so Link headers can be absolute.
        self.host = ""
        #: The parsed query of the request being served, so a Link can preserve per_page.
        self.raw_query: dict = {}

    def base_url(self, path: str) -> str:
        """An absolute URL for ``path`` on the origin the request arrived on.

        The request's own query string rides along (so ``per_page`` survives a Link walk); ``page``
        is dropped because link_header sets it.
        """
        host = self.host or f"127.0.0.1:{self.forge.port}"
        kept = [f"{k}={v[0]}" for k, v in self.raw_query.items() if k != "page" and v]
        tail = ("?" + "&".join(kept)) if kept else ""
        return f"http://{host}{path}{tail}"

    # ------------------------------------------------------------------ helpers

    @property
    def store(self) -> Store:
        return self.forge.store

    @property
    def login(self) -> str:
        return self.forge.login

    @property
    def login_id(self) -> int:
        return self.forge.login_id

    def repo_of(self, owner: str, name: str) -> Repo:
        """A repository the token's login owns; someone else's name is the 404 GitHub answers."""
        if owner != self.login:
            raise rest.ApiError(404, "Not Found")
        repo = self.store.get(owner, name)
        if repo is None:
            raise rest.ApiError(404, "Not Found")
        return repo

    def require_repo(self, owner: str, name: str) -> Repo:
        return self.repo_of(owner, name)

    def check_auth(self, headers: dict) -> None:
        token = headers.get("authorization", "")
        if not token.lower().startswith("bearer "):
            raise rest.ApiError(401, "Requires authentication")
        if self.store.token and token[7:].strip() != self.store.token:
            raise rest.ApiError(401, "Bad credentials")

    # ------------------------------------------------------------------ the API root

    def root(self, method: str, query: dict, headers: dict, body: bytes):
        self.check_auth(headers)
        payload = {
            "current_user_url": f"/api/v3/user",
            "current_user_authorizations_html_url": "https://github.com/settings/connections/applications{/client_id}",
            "authorizations_url": "/api/v3/authorizations",
            "code_search_url": "/api/v3/search/code?q={query}{&page,per_page,sort,order}",
            "commit_search_url": "/api/v3/search/commits?q={query}{&page,per_page,sort,order}",
            "emails_url": "/api/v3/user/emails",
            "emojis_url": "/api/v3/emojis",
            "events_url": "/api/v3/events",
            "feeds_url": "/api/v3/feeds",
            "followers_url": "/api/v3/user/followers",
            "following_url": "/api/v3/user/following{/target}",
            "gists_url": "/api/v3/gists{/gist_id}",
            "hub_url": "/api/v3/hub",
            "issue_search_url": "/api/v3/search/issues?q={query}{&page,per_page,sort,order}",
            "issues_url": "/api/v3/issues",
            "keys_url": "/api/v3/user/keys{/key_id}",
            "label_search_url": "/api/v3/search/labels?q={query}&repository_id={repository_id}{&page,per_page}",
            "notifications_url": "/api/v3/notifications",
            "organization_url": "/api/v3/orgs/{org}",
            "organization_repositories_url": "/api/v3/orgs/{org}/repos{?type,page,per_page,sort}",
            "organization_teams_url": "/api/v3/orgs/{org}/teams",
            "public_gists_url": "/api/v3/gists/public",
            "rate_limit_url": "/api/v3/rate_limit",
            "repository_url": "/api/v3/repos/{owner}/{repo}",
            "repository_search_url": "/api/v3/search/repositories?q={query}{&page,per_page,sort,order}",
            "current_user_repositories_url": "/api/v3/user/repos{?type,page,per_page,sort}",
            "starred_url": "/api/v3/user/starred{/owner}{/repo}",
            "starred_gists_url": "/api/v3/gists/starred",
            "user_url": "/api/v3/users/{user}",
            "user_organizations_url": "/api/v3/user/orgs",
            "user_repositories_url": "/api/v3/users/{user}/repos{?type,page,per_page,sort}",
            "user_search_url": "/api/v3/search/users?q={query}{&page,per_page,sort,order}",
        }
        return 200, {"x-oauth-scopes": rest.OAUTH_SCOPES}, payload

    def meta(self, method: str, query: dict, headers: dict, body: bytes):
        self.check_auth(headers)
        return 200, {}, {
            "verifiable_password_authentication": False,
            "installed_version": rest.INSTALLED_VERSION,
            "ssh_key_fingerprints": {},
            "hooks": [],
            "web": [],
            "api": [],
            "git": [],
            "packages": [],
            "pages": [],
            "importer": [],
            "actions": [],
            "dependabot": [],
            "domains": {},
        }

    def user(self, method: str, query: dict, headers: dict, body: bytes):
        self.check_auth(headers)
        viewer = self.forge.scenario["viewer"]
        return 200, {}, rest.user_json(viewer["login"], viewer["id"], viewer["name"], viewer["email"])

    # ------------------------------------------------------------------ repositories

    def user_repos(self, method: str, query: dict, headers: dict, body: bytes):
        self.check_auth(headers)
        if method == "POST":
            payload = _body_json(body)
            name = payload.get("name")
            if not isinstance(name, str) or name == "":
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Repository", "field": "name", "code": "missing_field"}])
            if self.store.get(self.login, name) is not None:
                raise rest.ApiError(422, "Repository creation failed.", [
                    {"resource": "Repository", "code": "custom", "field": "name", "message": "name already exists on this account"}
                ])
            repo = self.store.create(
                self.login,
                name,
                payload.get("description") or "",
                bool(payload.get("private", False)),
                _now_iso(),
            )
            repo.default_branch = "main"
            return 201, {}, rest.repo_json(repo, self._repo_id(repo))

        repos = self.store.list_for(self.login)
        base = f"/api/v3/user/repos"
        page, per_page = self._page_params(query)
        chunk, has_next = rest.paginate(repos, page, per_page)
        last_page = max(1, (len(repos) + per_page - 1) // per_page)
        headers_out = {}
        link = rest.link_header(self.base_url(base), page, has_next, page > 1, last_page)
        if link:
            headers_out["link"] = link
        return 200, headers_out, [rest.repo_json(r, self._repo_id(r)) for r in chunk]

    def _repo_id(self, repo: Repo) -> int:
        """A stable numeric id per repository: the scenario's own id when it names one."""
        for entry in self.forge.scenario["repos"]:
            if entry["owner"] == repo.owner and entry["name"] == repo.name:
                return entry["id"]
        for entry in self.forge.scenario["org_repos"]:
            if entry["owner"] == repo.owner and entry["name"] == repo.name:
                return entry["id"]
        for entry in self.forge.scenario["stranger_repos"]:
            if entry["owner"] == repo.owner and entry["name"] == repo.name:
                return entry["id"]
        # A repository a check created: a deterministic id from its place in the sorted list.
        keys = sorted(self.store.repos.keys())
        return 9000 + keys.index(f"{repo.owner}/{repo.name}")

    def repo(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        if method == "GET":
            repo = self.repo_of(owner, name)
            return 200, {}, rest.repo_json(repo, self._repo_id(repo))
        if method == "PATCH":
            repo = self.repo_of(owner, name)
            payload = _body_json(body)
            if "description" in payload:
                value = payload["description"]
                repo.description = value if isinstance(value, str) else ""
            if "private" in payload:
                repo.private = bool(payload["private"])
            if "name" in payload and isinstance(payload["name"], str) and payload["name"] != repo.name:
                new_name = payload["name"]
                if self.store.get(owner, new_name) is not None:
                    raise rest.ApiError(422, "Repository creation failed.")
                self.store.repos.pop(self.store.key(owner, repo.name), None)
                repo.name = new_name
                self.store.repos[self.store.key(owner, new_name)] = repo
            return 200, {}, rest.repo_json(repo, self._repo_id(repo))
        if method == "DELETE":
            repo = self.repo_of(owner, name)
            self.store.delete(owner, repo.name)
            return 204, {}, None
        raise rest.ApiError(405, "Method Not Allowed")

    def branches(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        out = []
        for ref, oid in sorted(repo.refs.items()):
            if not ref.startswith(HEADS):
                continue
            out.append({
                "name": ref[len(HEADS):],
                "commit": {
                    "sha": oid,
                    "url": f"/api/v3/repos/{owner}/{name}/commits/{oid}",
                },
                "protected": False,
            })
        base = f"/api/v3/repos/{owner}/{name}/branches"
        page, per_page = self._page_params(query)
        chunk, has_next = rest.paginate(out, page, per_page)
        headers_out = {}
        link = rest.link_header(self.base_url(base), page, has_next, page > 1, max(1, (len(out) + per_page - 1) // per_page))
        if link:
            headers_out["link"] = link
        return 200, headers_out, chunk

    def branch(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, branch: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        oid = repo.refs.get(HEADS + branch)
        if oid is None:
            raise rest.ApiError(404, "Branch not found")
        return 200, {}, {"name": branch, "commit": {"sha": oid}, "protected": False}

    def commits(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        sha = _query_one(query, "sha")
        tip = None
        if sha:
            tip = repo.resolve(sha)
            if tip is None:
                raise rest.ApiError(409, "Git Repository is empty.")
        else:
            tip = repo.refs.get("HEAD")
            if tip and tip.startswith("ref: "):
                tip = repo.refs.get(tip[5:])
            if tip is None:
                tip = repo.refs.get(HEADS + repo.default_branch)
        if tip is None:
            return 200, {}, []
        out = self._commit_list(repo, tip, owner, name)
        base = f"/api/v3/repos/{owner}/{name}/commits"
        page, per_page = self._page_params(query)
        chunk, has_next = rest.paginate(out, page, per_page)
        headers_out = {}
        link = rest.link_header(self.base_url(base), page, has_next, page > 1, max(1, (len(out) + per_page - 1) // per_page))
        if link:
            headers_out["link"] = link
        return 200, headers_out, chunk

    def _commit_list(self, repo: Repo, tip: str, owner: str, name: str) -> list[dict]:
        """Every commit reachable from ``tip`` by first-parent history, newest first."""
        import json as _json

        from . import gitobj as g

        out: list[dict] = []
        seen: set[str] = set()
        cur = tip
        guard = 0
        while cur and guard < 100000:
            guard += 1
            if cur in seen:
                break
            seen.add(cur)
            obj = repo.objects.get(cur)
            if obj is None or obj[0] != g.TYPE_COMMIT:
                break
            c = g.parse_commit(obj[1])
            author = (c["author"] or b"").decode("utf-8", "replace")
            committer = (c["committer"] or b"").decode("utf-8", "replace")
            out.append({
                "sha": cur,
                "node_id": f"C_{cur}",
                "commit": {
                    "author": _ident(author),
                    "committer": _ident(committer),
                    "message": c["message"].decode("utf-8", "replace"),
                    "tree": {"sha": c["tree"], "url": f"/api/v3/repos/{owner}/{name}/git/trees/{c['tree']}"},
                    "url": f"/api/v3/repos/{owner}/{name}/git/commits/{cur}",
                    "comment_count": 0,
                    "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
                },
                "url": f"/api/v3/repos/{owner}/{name}/commits/{cur}",
                "html_url": f"https://github.com/{owner}/{name}/commit/{cur}",
                "comments_url": f"/api/v3/repos/{owner}/{name}/commits/{cur}/comments",
                "author": None,
                "committer": None,
                "parents": [{"sha": p, "url": f"/api/v3/repos/{owner}/{name}/commits/{p}", "html_url": f"https://github.com/{owner}/{name}/commit/{p}"} for p in c["parents"]],
            })
            parents = c["parents"]
            if not parents:
                break
            cur = parents[0]
        return out

    def commit(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, sha: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        oid = repo.resolve(sha)
        if oid is None:
            raise rest.ApiError(404, "No commit found for SHA: " + sha)
        listed = self._commit_list(repo, oid, owner, name)
        if not listed:
            raise rest.ApiError(404, "No commit found for SHA: " + sha)
        return 200, {}, listed[0]

    def git_refs(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "POST":
            payload = _body_json(body)
            ref = payload.get("ref")
            sha = payload.get("sha")
            if not isinstance(ref, str) or not ref.startswith("refs/"):
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Ref", "field": "ref", "code": "invalid"}])
            if not isinstance(sha, str) or repo.resolve(sha) is None:
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Ref", "field": "sha", "code": "invalid"}])
            if ref in repo.refs:
                raise rest.ApiError(422, "Reference already exists")
            repo.refs[ref] = repo.resolve(sha)
            return 201, {}, {
                "ref": ref,
                "node_id": f"MDM6UmVm{ref}",
                "url": f"/api/v3/repos/{owner}/{name}/git/refs/{ref[len('refs/'):]}",
                "object": {"sha": repo.refs[ref], "type": "commit", "url": f"/api/v3/repos/{owner}/{name}/git/commits/{repo.refs[ref]}"},
            }
        out = []
        for ref, oid in sorted(repo.refs.items()):
            if ref == "HEAD" or oid.startswith("ref: "):
                continue
            out.append({
                "ref": ref,
                "node_id": f"MDM6UmVm{ref}",
                "url": f"/api/v3/repos/{owner}/{name}/git/refs/{ref[len('refs/'):]}",
                "object": {"sha": oid, "type": "commit", "url": f"/api/v3/repos/{owner}/{name}/git/commits/{oid}"},
            })
        return 200, {}, out

    def git_ref(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, ref: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        full = ref if ref.startswith("refs/") else f"refs/{ref}"
        if method == "GET":
            oid = repo.refs.get(full)
            if oid is None or oid.startswith("ref: "):
                raise rest.ApiError(404, "Not Found")
            return 200, {}, {
                "ref": full,
                "node_id": f"MDM6UmVm{full}",
                "url": f"/api/v3/repos/{owner}/{name}/git/refs/{full[len('refs/'):]}",
                "object": {"sha": oid, "type": "commit", "url": f"/api/v3/repos/{owner}/{name}/git/commits/{oid}"},
            }
        if method == "PATCH":
            payload = _body_json(body)
            sha = payload.get("sha")
            if full not in repo.refs:
                raise rest.ApiError(404, "Not Found")
            if not isinstance(sha, str) or repo.resolve(sha) is None:
                raise rest.ApiError(422, "Validation Failed")
            repo.refs[full] = repo.resolve(sha)
            return 200, {}, {"ref": full, "object": {"sha": repo.refs[full], "type": "commit"}}
        if method == "DELETE":
            if full not in repo.refs:
                raise rest.ApiError(404, "Not Found")
            repo.refs.pop(full, None)
            return 204, {}, None
        raise rest.ApiError(405, "Method Not Allowed")

    # ------------------------------------------------------------------ contents

    def contents(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, path: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "GET":
            return self._contents_get(repo, owner, name, path, query)
        if method == "PUT":
            return self._contents_put(repo, owner, name, path, body)
        if method == "DELETE":
            return self._contents_delete(repo, owner, name, path, body)
        raise rest.ApiError(405, "Method Not Allowed")

    def _branch_tip(self, repo: Repo, branch: str | None) -> str | None:
        if branch:
            oid = repo.refs.get(HEADS + branch)
            if oid is None:
                raise rest.ApiError(422, "Branch not found")
            return oid
        tip = repo.refs.get("HEAD")
        if tip and tip.startswith("ref: "):
            tip = repo.refs.get(tip[5:])
        if tip is None:
            tip = repo.refs.get(HEADS + repo.default_branch)
        return tip

    def _contents_get(self, repo: Repo, owner: str, name: str, path: str, query: dict):
        ref = _query_one(query, "ref")
        tip = self._branch_tip(repo, ref)
        if tip is None:
            raise rest.ApiError(404, "Not Found")
        tree = repo.tree_of(tip)
        if tree is None:
            raise rest.ApiError(404, "Not Found")
        files = repo.flatten_tree(tree)
        if path in files:
            mode, oid = files[path]
            obj = repo.objects.get(oid)
            raw = obj[1] if obj else b""
            return 200, {}, {
                "type": "file",
                "encoding": "base64",
                "size": len(raw),
                "name": path.split("/")[-1],
                "path": path,
                "content": base64.b64encode(raw).decode("ascii"),
                "sha": oid,
                "url": f"/api/v3/repos/{owner}/{name}/contents/{path}",
                "git_url": f"/api/v3/repos/{owner}/{name}/git/blobs/{oid}",
                "html_url": f"https://github.com/{owner}/{name}/blob/{ref or repo.default_branch}/{path}",
                "download_url": f"https://raw.githubusercontent.com/{owner}/{name}/{ref or repo.default_branch}/{path}",
                "_links": {
                    "self": f"/api/v3/repos/{owner}/{name}/contents/{path}",
                    "git": f"/api/v3/repos/{owner}/{name}/git/blobs/{oid}",
                    "html": f"https://github.com/{owner}/{name}/blob/{ref or repo.default_branch}/{path}",
                },
            }
        prefix = path.rstrip("/") + "/"
        entries: dict[str, dict] = {}
        for file_path, (mode, oid) in files.items():
            if not file_path.startswith(prefix):
                continue
            tail = file_path[len(prefix):]
            if "/" in tail:
                head = tail.split("/", 1)[0]
                sub = f"{prefix}{head}"
                sub_tree = self._tree_oid_for(repo, tree, sub)
                entries[head] = {
                    "type": "dir",
                    "size": 0,
                    "name": head,
                    "path": sub,
                    "sha": sub_tree or oid,
                    "url": f"/api/v3/repos/{owner}/{name}/contents/{sub}",
                    "html_url": f"https://github.com/{owner}/{name}/tree/{ref or repo.default_branch}/{sub}",
                }
            else:
                obj = repo.objects.get(oid)
                raw = obj[1] if obj else b""
                entries[tail] = {
                    "type": "file",
                    "size": len(raw),
                    "name": tail,
                    "path": file_path,
                    "sha": oid,
                    "url": f"/api/v3/repos/{owner}/{name}/contents/{file_path}",
                    "html_url": f"https://github.com/{owner}/{name}/blob/{ref or repo.default_branch}/{file_path}",
                }
        if not entries and path not in ("", "."):
            raise rest.ApiError(404, "Not Found")
        return 200, {}, [entries[k] for k in sorted(entries)]

    def _tree_oid_for(self, repo: Repo, root_tree: str, path: str) -> str | None:
        from . import gitobj as g

        cur = root_tree
        for segment in path.split("/"):
            obj = repo.objects.get(cur)
            if obj is None or obj[0] != g.TYPE_TREE:
                return None
            found = None
            for mode, entry_name, oid in g.parse_tree(obj[1]):
                if entry_name.decode("utf-8", "replace") == segment:
                    found = oid
                    break
            if found is None:
                return None
            cur = found
        return cur

    def _contents_put(self, repo: Repo, owner: str, name: str, path: str, body: bytes):
        payload = _body_json(body)
        message = payload.get("message")
        if not isinstance(message, str) or message == "":
            raise rest.ApiError(422, "Validation Failed", [{"resource": "Commit", "field": "message", "code": "missing_field"}])
        raw = rest.decode_content(payload)
        content = raw[1]
        branch = payload.get("branch") if isinstance(payload.get("branch"), str) else None
        tip = self._branch_tip(repo, branch)
        files: dict[str, tuple[bytes, str]] = {}
        if tip is not None:
            tree = repo.tree_of(tip)
            if tree is not None:
                files = dict(repo.flatten_tree(tree))
        existing = files.get(path)
        given_sha = payload.get("sha")
        if existing is not None:
            if given_sha is None:
                raise rest.ApiError(422, "Invalid request.\n\n\"sha\" wasn't supplied.")
            if str(given_sha) != existing[1]:
                raise rest.ApiError(409, "sha does not match")
        elif given_sha is not None:
            raise rest.ApiError(422, "Invalid request.\n\n\"sha\" wasn't supplied.")
        blob_oid = repo.put_object(b"blob", content)
        files[path] = (b"100644", blob_oid)
        author = self._author_bytes()
        commit_oid = repo.commit_files(files, [tip] if tip else [], author, author, message.encode("utf-8"))
        if branch:
            repo.refs[HEADS + branch] = commit_oid
        else:
            repo.refs[HEADS + repo.default_branch] = commit_oid
            repo.refs["HEAD"] = f"ref: {HEADS}{repo.default_branch}"
        status = 200 if existing is not None else 201
        return status, {}, {
            "content": {
                "name": path.split("/")[-1],
                "path": path,
                "sha": blob_oid,
                "size": len(content),
                "url": f"/api/v3/repos/{owner}/{name}/contents/{path}",
                "html_url": f"https://github.com/{owner}/{name}/blob/{repo.default_branch}/{path}",
                "git_url": f"/api/v3/repos/{owner}/{name}/git/blobs/{blob_oid}",
                "download_url": f"https://raw.githubusercontent.com/{owner}/{name}/{repo.default_branch}/{path}",
                "type": "file",
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
                "_links": {
                    "self": f"/api/v3/repos/{owner}/{name}/contents/{path}",
                    "git": f"/api/v3/repos/{owner}/{name}/git/blobs/{blob_oid}",
                    "html": f"https://github.com/{owner}/{name}/blob/{repo.default_branch}/{path}",
                },
            },
            "commit": {
                "sha": commit_oid,
                "node_id": f"C_{commit_oid}",
                "url": f"/api/v3/repos/{owner}/{name}/git/commits/{commit_oid}",
                "html_url": f"https://github.com/{owner}/{name}/commit/{commit_oid}",
                "author": None,
                "committer": None,
                "message": message,
                "tree": {"sha": repo.tree_of(commit_oid), "url": f"/api/v3/repos/{owner}/{name}/git/trees/{repo.tree_of(commit_oid)}"},
                "parents": [{"sha": tip, "url": f"/api/v3/repos/{owner}/{name}/commits/{tip}"}] if tip else [],
                "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
            },
        }

    def _contents_delete(self, repo: Repo, owner: str, name: str, path: str, body: bytes):
        payload = _body_json(body)
        message = payload.get("message") or "Delete " + path
        branch = payload.get("branch") if isinstance(payload.get("branch"), str) else None
        tip = self._branch_tip(repo, branch)
        if tip is None:
            raise rest.ApiError(404, "Not Found")
        tree = repo.tree_of(tip)
        files = dict(repo.flatten_tree(tree)) if tree is not None else {}
        if path not in files:
            raise rest.ApiError(404, "Not Found")
        given_sha = payload.get("sha")
        if given_sha is not None and str(given_sha) != files[path][1]:
            raise rest.ApiError(409, "sha does not match")
        files.pop(path, None)
        author = self._author_bytes()
        commit_oid = repo.commit_files(files, [tip], author, author, str(message).encode("utf-8"))
        if branch:
            repo.refs[HEADS + branch] = commit_oid
        else:
            repo.refs[HEADS + repo.default_branch] = commit_oid
            repo.refs["HEAD"] = f"ref: {HEADS}{repo.default_branch}"
        return 200, {}, {
            "content": None,
            "commit": {
                "sha": commit_oid,
                "node_id": f"C_{commit_oid}",
                "url": f"/api/v3/repos/{owner}/{name}/git/commits/{commit_oid}",
                "html_url": f"https://github.com/{owner}/{name}/commit/{commit_oid}",
                "message": str(message),
                "tree": {"sha": repo.tree_of(commit_oid)},
                "parents": [{"sha": tip}],
                "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
            },
        }

    def _author_bytes(self) -> bytes:
        viewer = self.forge.scenario["viewer"]
        return f"{viewer['name']} <{viewer['email']}> {sc.SCENARIO_EPOCH_SEC} +0000".encode("utf-8")

    # ------------------------------------------------------------------ labels

    def labels(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "POST":
            payload = _body_json(body)
            label_name = payload.get("name")
            if not isinstance(label_name, str) or label_name == "":
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Label", "field": "name", "code": "missing_field"}])
            if label_name in repo.labels:
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Label", "field": "name", "code": "already_exists"}])
            label = {
                "name": label_name,
                "color": payload.get("color") or "ededed",
                "description": payload.get("description"),
                "url": f"/api/v3/repos/{owner}/{name}/labels/{label_name}",
                "id": self.store.next_seq(),
            }
            repo.labels[label_name] = label
            return 201, {}, rest.label_json(label, label["id"])
        out = [rest.label_json(repo.labels[k], repo.labels[k].get("id", 1)) for k in sorted(repo.labels)]
        return 200, {}, out

    def label(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, label_name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "GET":
            label = repo.labels.get(label_name)
            if label is None:
                raise rest.ApiError(404, "Not Found")
            return 200, {}, rest.label_json(label, label.get("id", 1))
        if method == "DELETE":
            if repo.labels.pop(label_name, None) is None:
                raise rest.ApiError(404, "Not Found")
            return 204, {}, None
        raise rest.ApiError(405, "Method Not Allowed")

    # ------------------------------------------------------------------ issues

    def _issue_labels(self, repo: Repo, names) -> list[dict]:
        """The label objects an issue carries; a name the repository does not hold is a 422."""
        out: list[dict] = []
        if names is None:
            return out
        if not isinstance(names, list):
            raise rest.ApiError(422, "Validation Failed", [{"resource": "Issue", "field": "labels", "code": "invalid"}])
        for entry in names:
            label_name = entry.get("name") if isinstance(entry, dict) else entry
            if not isinstance(label_name, str):
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Issue", "field": "labels", "code": "invalid"}])
            label = repo.labels.get(label_name)
            if label is None:
                raise rest.ApiError(422, "Validation Failed", [
                    {"resource": "Issue", "field": "labels", "code": "invalid", "message": f"Label does not exist: {label_name}"}
                ])
            out.append(label)
        return out

    def issues(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "POST":
            payload = _body_json(body)
            title = payload.get("title")
            if not isinstance(title, str) or title == "":
                raise rest.ApiError(422, "Validation Failed", [{"resource": "Issue", "field": "title", "code": "missing_field"}])
            labels = self._issue_labels(repo, payload.get("labels"))
            number = repo.next_issue
            repo.next_issue += 1
            issue = {
                "id": self.store.next_seq(),
                "number": number,
                "title": title,
                "body": payload.get("body") if isinstance(payload.get("body"), str) else "",
                "state": "open",
                "state_reason": None,
                "user": self.login,
                "labels": labels,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "closed_at": None,
            }
            repo.issues[number] = issue
            return 201, {}, rest.issue_json(repo, issue, self.login_id)

        state = _query_one(query, "state", "open")
        labels_filter = query.get("labels")
        wanted_labels = []
        if labels_filter:
            wanted_labels = [p for p in labels_filter[0].split(",") if p]
        items = []
        for number in sorted(repo.issues):
            issue = repo.issues[number]
            if state not in (None, "all") and issue["state"] != state:
                continue
            if wanted_labels:
                have = {l["name"] for l in issue.get("labels", [])}
                if not all(w in have for w in wanted_labels):
                    continue
            items.append(issue)
        sort = _query_one(query, "sort", "created")
        direction = _query_one(query, "direction", "desc")
        if sort in ("created", "updated"):
            items.sort(key=lambda i: (i.get("created_at") or "", i["number"]), reverse=(direction == "desc"))
        else:
            items.sort(key=lambda i: i["number"], reverse=(direction == "desc"))
        base = f"/api/v3/repos/{owner}/{name}/issues"
        page, per_page = self._page_params(query)
        chunk, has_next = rest.paginate(items, page, per_page)
        headers_out = {}
        link = rest.link_header(self.base_url(base), page, has_next, page > 1, max(1, (len(items) + per_page - 1) // per_page))
        if link:
            headers_out["link"] = link
        return 200, headers_out, [rest.issue_json(repo, i, self.login_id) for i in chunk]

    def issue(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        issue = repo.issues.get(num)
        if issue is None:
            raise rest.ApiError(404, "Not Found")
        if method == "GET":
            return 200, {}, rest.issue_json(repo, issue, self.login_id)
        if method == "PATCH":
            payload = _body_json(body)
            if isinstance(payload.get("title"), str):
                issue["title"] = payload["title"]
            if isinstance(payload.get("body"), str):
                issue["body"] = payload["body"]
            if isinstance(payload.get("state"), str) and payload["state"] in ("open", "closed"):
                issue["state"] = payload["state"]
                issue["closed_at"] = _now_iso() if payload["state"] == "closed" else None
            if "labels" in payload:
                issue["labels"] = self._issue_labels(repo, payload.get("labels"))
            issue["updated_at"] = _now_iso()
            return 200, {}, rest.issue_json(repo, issue, self.login_id)
        raise rest.ApiError(405, "Method Not Allowed")

    def issue_comments(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        if num not in repo.issues:
            raise rest.ApiError(404, "Not Found")
        if method == "POST":
            payload = _body_json(body)
            text = payload.get("body")
            if not isinstance(text, str):
                raise rest.ApiError(422, "Validation Failed", [{"resource": "IssueComment", "field": "body", "code": "missing_field"}])
            comment = {
                "id": self.store.next_seq(),
                "body": text,
                "user": self.login,
                "issue": num,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            repo.comments.setdefault(num, []).append(comment)
            return 201, {}, rest.comment_json(repo, comment, self.login_id)
        out = [rest.comment_json(repo, c, self.login_id) for c in repo.comments.get(num, [])]
        return 200, {}, out

    def issue_comment(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, comment_id: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            cid = int(comment_id)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        for comments in repo.comments.values():
            for comment in comments:
                if comment["id"] == cid:
                    if method == "GET":
                        return 200, {}, rest.comment_json(repo, comment, self.login_id)
                    if method == "PATCH":
                        payload = _body_json(body)
                        if isinstance(payload.get("body"), str):
                            comment["body"] = payload["body"]
                            comment["updated_at"] = _now_iso()
                        return 200, {}, rest.comment_json(repo, comment, self.login_id)
                    if method == "DELETE":
                        comments.remove(comment)
                        return 204, {}, None
        raise rest.ApiError(404, "Not Found")

    # ------------------------------------------------------------------ pull requests

    def pulls(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        if method == "POST":
            payload = _body_json(body)
            title = payload.get("title")
            head = payload.get("head")
            base = payload.get("base")
            if not isinstance(title, str) or title == "":
                raise rest.ApiError(422, "Validation Failed", [{"resource": "PullRequest", "field": "title", "code": "missing_field"}])
            if not isinstance(head, str) or not isinstance(base, str):
                raise rest.ApiError(422, "Validation Failed", [{"resource": "PullRequest", "field": "head", "code": "missing_field"}])
            head_ref = head.split(":", 1)[1] if ":" in head else head
            head_sha = repo.refs.get(HEADS + head_ref)
            base_sha = repo.refs.get(HEADS + base)
            if head_sha is None:
                raise rest.ApiError(422, "Validation Failed", [{"resource": "PullRequest", "field": "head", "code": "invalid"}])
            if base_sha is None:
                raise rest.ApiError(422, "Validation Failed", [{"resource": "PullRequest", "field": "base", "code": "invalid"}])
            number = repo.next_pull
            repo.next_pull += 1
            pull = {
                "id": self.store.next_seq(),
                "number": number,
                "title": title,
                "body": payload.get("body") if isinstance(payload.get("body"), str) else "",
                "state": "open",
                "user": self.login,
                "head": head_ref,
                "base": base,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "merged": False,
                "mergeable": self._mergeable(repo, head_sha, base_sha),
                "mergeable_state": "clean" if self._mergeable(repo, head_sha, base_sha) else "dirty",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "closed_at": None,
                "merged_at": None,
                "merge_commit_sha": None,
                "files": rest.diff_files(repo, {"base_sha": base_sha, "head_sha": head_sha}),
            }
            repo.pulls[number] = pull
            return 201, {}, rest.pull_json(repo, pull, self.login_id)

        state = _query_one(query, "state", "open")
        items = []
        for number in sorted(repo.pulls):
            pull = repo.pulls[number]
            if state not in (None, "all") and pull["state"] != state:
                continue
            items.append(pull)
        base_path = f"/api/v3/repos/{owner}/{name}/pulls"
        page, per_page = self._page_params(query)
        chunk, has_next = rest.paginate(items, page, per_page)
        headers_out = {}
        link = rest.link_header(self.base_url(base_path), page, has_next, page > 1, max(1, (len(items) + per_page - 1) // per_page))
        if link:
            headers_out["link"] = link
        return 200, headers_out, [rest.pull_json(repo, p, self.login_id) for p in chunk]

    def _mergeable(self, repo: Repo, head_sha: str, base_sha: str) -> bool:
        """A pull request is mergeable when the base has not moved past their common ancestor."""
        if head_sha == base_sha:
            return False
        return repo.is_ancestor(base_sha, head_sha)

    def pull(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        pull = repo.pulls.get(num)
        if pull is None:
            raise rest.ApiError(404, "Not Found")
        if method == "GET":
            return 200, {}, rest.pull_json(repo, pull, self.login_id)
        if method == "PATCH":
            payload = _body_json(body)
            for key in ("title", "body", "state"):
                if isinstance(payload.get(key), str):
                    pull[key] = payload[key]
            pull["updated_at"] = _now_iso()
            return 200, {}, rest.pull_json(repo, pull, self.login_id)
        raise rest.ApiError(405, "Method Not Allowed")

    def pull_files(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        pull = repo.pulls.get(num)
        if pull is None:
            raise rest.ApiError(404, "Not Found")
        return 200, {}, rest.diff_files(repo, pull)

    def pull_commits(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        pull = repo.pulls.get(num)
        if pull is None:
            raise rest.ApiError(404, "Not Found")
        head_sha = pull.get("head_sha")
        base_sha = pull.get("base_sha")
        if not head_sha:
            return 200, {}, []
        return 200, {}, self._commit_list(repo, head_sha, owner, name)

    def pull_merge(self, method: str, query: dict, headers: dict, body: bytes, owner: str, name: str, number: str):
        self.check_auth(headers)
        repo = self.repo_of(owner, name)
        try:
            num = int(number)
        except ValueError:
            raise rest.ApiError(404, "Not Found") from None
        pull = repo.pulls.get(num)
        if pull is None:
            raise rest.ApiError(404, "Not Found")
        if pull["state"] != "open" or pull.get("merged"):
            raise rest.ApiError(405, "Pull Request is not mergeable")
        base_sha = repo.refs.get(HEADS + pull["base"])
        head_sha = pull.get("head_sha")
        if base_sha is None or head_sha is None:
            raise rest.ApiError(405, "Pull Request is not mergeable")
        if base_sha != pull.get("base_sha"):
            # The base moved after the pull request opened: mergeable only if it is still a fast-forward.
            if not repo.is_ancestor(base_sha, head_sha):
                raise rest.ApiError(405, "Pull Request is not mergeable")
        # A squash commit lands on the default branch: the head's tree, one parent (the base tip).
        head_tree = repo.tree_of(head_sha)
        if head_tree is None:
            raise rest.ApiError(405, "Pull Request is not mergeable")
        files = repo.flatten_tree(head_tree)
        author = self._author_bytes()
        message = f"{pull['title']}\n\n{pull.get('body') or ''}".rstrip() + "\n"
        merge_sha = repo.commit_files(files, [base_sha], author, author, message.encode("utf-8"))
        repo.refs[HEADS + pull["base"]] = merge_sha
        if pull["base"] == repo.default_branch:
            repo.refs["HEAD"] = f"ref: {HEADS}{repo.default_branch}"
        pull["merged"] = True
        pull["state"] = "closed"
        pull["merged_at"] = _now_iso()
        pull["closed_at"] = _now_iso()
        pull["merge_commit_sha"] = merge_sha
        pull["updated_at"] = _now_iso()
        return 200, {}, {"sha": merge_sha, "merged": True, "message": "Pull Request successfully merged"}

    def rate_limit(self, method: str, query: dict, headers: dict, body: bytes):
        self.check_auth(headers)
        return 200, {}, {
            "resources": {
                "core": {"limit": 5000, "remaining": 4999, "reset": sc.SCENARIO_EPOCH_SEC + 3600, "used": 1},
                "search": {"limit": 30, "remaining": 30, "reset": sc.SCENARIO_EPOCH_SEC + 60, "used": 0},
                "graphql": {"limit": 5000, "remaining": 5000, "reset": sc.SCENARIO_EPOCH_SEC + 3600, "used": 0},
                "integration_manifest": {"limit": 5000, "remaining": 5000, "reset": sc.SCENARIO_EPOCH_SEC + 3600, "used": 0},
                "code_scanning_upload": {"limit": 500, "remaining": 500, "reset": sc.SCENARIO_EPOCH_SEC + 3600, "used": 0},
            },
            "rate": {"limit": 5000, "remaining": 4999, "reset": sc.SCENARIO_EPOCH_SEC + 3600, "used": 1},
        }

    # ------------------------------------------------------------------ helpers

    def _page_params(self, query: dict) -> tuple[int, int]:
        page = 1
        per_page = PER_PAGE
        raw_page = _query_one(query, "page")
        if raw_page:
            try:
                page = max(1, int(raw_page))
            except ValueError:
                page = 1
        raw_per = _query_one(query, "per_page")
        if raw_per:
            try:
                per_page = max(1, min(100, int(raw_per)))
            except ValueError:
                per_page = PER_PAGE
        return page, per_page


def _ident(line: str) -> dict:
    """A git ``Name <email> epoch +0000`` line as GitHub's author/committer object."""
    import datetime

    name = line
    email = ""
    if "<" in line and ">" in line:
        name = line.split("<", 1)[0].strip()
        email = line.split("<", 1)[1].split(">", 1)[0].strip()
    tail = line.rsplit(">", 1)[-1].strip() if ">" in line else ""
    epoch = 0
    tz = "+0000"
    parts = tail.split()
    if parts:
        try:
            epoch = int(parts[0])
        except ValueError:
            epoch = 0
    if len(parts) > 1:
        tz = parts[1]
    dt = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=epoch)
    return {
        "name": name,
        "email": email,
        "date": f"{dt:%Y-%m-%dT%H:%M:%S}Z",
        "login": name.replace(" ", "").lower() or None,
    }
