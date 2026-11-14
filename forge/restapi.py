"""The REST v3 API surface: repositories, contents, issues, labels, comments and pull requests.

Standard library only. Every reply is JSON with a non-empty X-GitHub-Request-Id; nothing here reads a
clock or a random source, so two runs answer identically.
"""

from __future__ import annotations

import base64
import json

from . import gitobj as g
from .store import Repo, Store, HEADS, TAGS

#: The token's scopes, as GET /api/v3/ reports them.
OAUTH_SCOPES = "repo, read:org"

#: The forge's version, as GET /api/v3/meta reports it.
INSTALLED_VERSION = "makefun-forge/1.0"

#: How many items one page holds.
PER_PAGE = 30


class ApiError(Exception):
    """An API answer that is not 2xx: its status and its JSON body."""

    def __init__(self, status: int, message: str, errors=None, documentation_url: str | None = None):
        super().__init__(message)
        self.status = status
        self.body: dict = {"message": message}
        if errors:
            self.body["errors"] = errors
        if documentation_url:
            self.body["documentation_url"] = documentation_url


def _iso(epoch_sec: int) -> str:
    """An epoch second as GitHub's ISO-8601 form, computed without a clock."""
    import datetime

    dt = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=epoch_sec)
    return f"{dt:%Y-%m-%dT%H:%M:%SZ}"


def user_json(login: str, uid: int, name: str | None = None, email: str | None = None) -> dict:
    return {
        "login": login,
        "id": uid,
        "node_id": f"U_{uid}",
        "avatar_url": f"/api/avatars/{uid}.png",
        "gravatar_id": "",
        "url": f"/api/v3/users/{login}",
        "html_url": f"https://github.com/{login}",
        "type": "User",
        "site_admin": False,
        "name": name if name is not None else login,
        "email": email,
    }


def repo_json(repo: Repo, uid: int = 1) -> dict:
    full = f"{repo.owner}/{repo.name}"
    return {
        "id": uid,
        "node_id": f"R_{uid}",
        "name": repo.name,
        "full_name": full,
        "private": repo.private,
        "owner": user_json(repo.owner, uid),
        "html_url": f"https://github.com/{full}",
        "description": repo.description,
        "fork": False,
        "url": f"/api/v3/repos/{full}",
        "created_at": repo.created_at,
        "updated_at": repo.created_at,
        "pushed_at": repo.created_at,
        "clone_url": f"https://github.com/{full}.git",
        "ssh_url": f"git@github.com:{full}.git",
        "git_url": f"git://github.com/{full}.git",
        "default_branch": repo.default_branch,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "has_issues": True,
        "has_wiki": False,
        "has_pages": False,
        "forks_count": 0,
        "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "license": None,
        "topics": [],
        "permissions": {"admin": True, "push": True, "pull": True},
    }


def label_json(label: dict, uid: int = 1) -> dict:
    return {
        "id": uid,
        "node_id": f"L_{uid}",
        "name": label["name"],
        "color": label.get("color", "ededed"),
        "description": label.get("description"),
        "default": False,
        "url": label.get("url", ""),
    }


def issue_json(repo: Repo, issue: dict, uid: int = 1) -> dict:
    full = f"{repo.owner}/{repo.name}"
    number = issue["number"]
    return {
        "id": issue["id"],
        "node_id": f"I_{issue['id']}",
        "number": number,
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "state_reason": issue.get("state_reason"),
        "user": user_json(issue["user"], uid),
        "labels": [label_json(l, l.get("id", 1)) for l in issue.get("labels", [])],
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "locked": False,
        "comments": len(repo.comments.get(number, [])),
        "created_at": issue["created_at"],
        "updated_at": issue["updated_at"],
        "closed_at": issue.get("closed_at"),
        "url": f"/api/v3/repos/{full}/issues/{number}",
        "html_url": f"https://github.com/{full}/issues/{number}",
        "repository_url": f"/api/v3/repos/{full}",
        "comments_url": f"/api/v3/repos/{full}/issues/{number}/comments",
        "labels_url": f"/api/v3/repos/{full}/issues/{number}/labels{{/name}}",
    }


def comment_json(repo: Repo, comment: dict, uid: int = 1) -> dict:
    full = f"{repo.owner}/{repo.name}"
    return {
        "id": comment["id"],
        "node_id": f"C_{comment['id']}",
        "body": comment["body"],
        "user": user_json(comment["user"], uid),
        "created_at": comment["created_at"],
        "updated_at": comment["updated_at"],
        "url": f"/api/v3/repos/{full}/issues/comments/{comment['id']}",
        "html_url": f"https://github.com/{full}/issues/{comment['issue']}#issuecomment-{comment['id']}",
        "issue_url": f"/api/v3/repos/{full}/issues/{comment['issue']}",
    }


def pull_json(repo: Repo, pull: dict, uid: int = 1) -> dict:
    full = f"{repo.owner}/{repo.name}"
    number = pull["number"]
    return {
        "id": pull["id"],
        "node_id": f"PR_{pull['id']}",
        "number": number,
        "title": pull["title"],
        "body": pull["body"],
        "state": pull["state"],
        "locked": False,
        "draft": False,
        "user": user_json(pull["user"], uid),
        "labels": [],
        "milestone": None,
        "head": {
            "label": f"{repo.owner}:{pull['head']}",
            "ref": pull["head"],
            "sha": pull.get("head_sha", ""),
            "user": user_json(repo.owner, uid),
            "repo": repo_json(repo, uid),
        },
        "base": {
            "label": f"{repo.owner}:{pull['base']}",
            "ref": pull["base"],
            "sha": pull.get("base_sha", ""),
            "user": user_json(repo.owner, uid),
            "repo": repo_json(repo, uid),
        },
        "merged": pull.get("merged", False),
        "mergeable": pull.get("mergeable", True),
        "mergeable_state": pull.get("mergeable_state", "clean"),
        "merged_by": user_json(pull["user"], uid) if pull.get("merged") else None,
        "merge_commit_sha": pull.get("merge_commit_sha"),
        "comments": 0,
        "commits": pull.get("commits", 0),
        "additions": 0,
        "deletions": 0,
        "changed_files": len(pull.get("files", [])),
        "created_at": pull["created_at"],
        "updated_at": pull["updated_at"],
        "closed_at": pull.get("closed_at"),
        "merged_at": pull.get("merged_at"),
        "url": f"/api/v3/repos/{full}/pulls/{number}",
        "html_url": f"https://github.com/{full}/pull/{number}",
        "diff_url": f"https://github.com/{full}/pull/{number}.diff",
        "patch_url": f"https://github.com/{full}/pull/{number}.patch",
        "files_url": f"/api/v3/repos/{full}/pulls/{number}/files",
        "commits_url": f"/api/v3/repos/{full}/pulls/{number}/commits",
        "comments_url": f"/api/v3/repos/{full}/issues/{number}/comments",
        "requested_reviewers": [],
        "requested_teams": [],
    }


def diff_files(repo: Repo, pull: dict) -> list[dict]:
    """The files a pull request changes, from its base and head trees."""
    base_sha = pull.get("base_sha") or ""
    head_sha = pull.get("head_sha") or ""
    base_tree = repo.tree_of(base_sha) if base_sha else None
    head_tree = repo.tree_of(head_sha) if head_sha else None
    base_files = repo.flatten_tree(base_tree) if base_tree else {}
    head_files = repo.flatten_tree(head_tree) if head_tree else {}
    out: list[dict] = []
    for path in sorted(set(base_files) | set(head_files)):
        in_base = path in base_files
        in_head = path in head_files
        if in_base and in_head and base_files[path][1] == head_files[path][1]:
            continue
        status = "added" if not in_base else "removed" if not in_head else "modified"
        old_content = _blob_text(repo, base_files[path][1]) if in_base else ""
        new_content = _blob_text(repo, head_files[path][1]) if in_head else ""
        out.append(
            {
                "sha": head_files[path][1] if in_head else base_files[path][1],
                "filename": path,
                "status": status,
                "additions": _count_added(old_content, new_content),
                "deletions": _count_removed(old_content, new_content),
                "changes": _count_added(old_content, new_content) + _count_removed(old_content, new_content),
                "blob_url": f"https://github.com/{repo.owner}/{repo.name}/blob/{head_sha}/{path}",
                "raw_url": f"https://github.com/{repo.owner}/{repo.name}/raw/{head_sha}/{path}",
                "contents_url": f"/api/v3/repos/{repo.owner}/{repo.name}/contents/{path}",
                "patch": "",
            }
        )
    return out


def _blob_text(repo: Repo, oid: str) -> str:
    obj = repo.objects.get(oid)
    if obj is None or obj[0] != g.TYPE_BLOB:
        return ""
    return obj[1].decode("utf-8", "replace")


def _count_added(old: str, new: str) -> int:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    return max(0, len(new_lines) - len(old_lines))


def _count_removed(old: str, new: str) -> int:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    return max(0, len(old_lines) - len(new_lines))


def paginate(items: list, page: int, per_page: int = PER_PAGE) -> tuple[list, bool]:
    """One page of items and whether a next page exists."""
    if page < 1:
        page = 1
    start = (page - 1) * per_page
    chunk = items[start : start + per_page]
    has_next = start + per_page < len(items)
    return chunk, has_next


def link_header(base: str, page: int, has_next: bool, has_last: bool, last_page: int) -> str | None:
    """The Link header for a paginated answer, or None when there is nowhere to go.

    ``base`` is an absolute URL that already carries the request's own query string (so ``per_page``
    survives the walk); only ``page`` is added.
    """
    parts: list[str] = []

    def url_for(target: int) -> str:
        if "per_page=" in base or "?" in base:
            head, _, tail = base.partition("?")
            kept = [kv for kv in tail.split("&") if kv and not kv.startswith("page=")]
            kept.append(f"page={target}")
            return f"{head}?{'&'.join(kept)}"
        return f"{base}?page={target}"

    if has_next:
        parts.append(f'<{url_for(page + 1)}>; rel="next"')
        parts.append(f'<{url_for(last_page)}>; rel="last"')
    if page > 1:
        parts.append(f'<{url_for(page - 1)}>; rel="prev"')
        parts.append(f'<{url_for(1)}>; rel="first"')
    if not parts:
        return None
    return ", ".join(parts)


def decode_content(payload: dict) -> tuple[str, bytes]:
    """A contents payload as (path, bytes).

    GitHub's contents API takes ``content`` as base64 unless ``encoding`` says otherwise; a body with
    no ``encoding`` field is base64, which is what the suite sends.
    """
    path = payload.get("path") or payload.get("name") or ""
    if "content" in payload and payload["content"] is not None:
        raw = payload["content"]
        if not isinstance(raw, str):
            raise ApiError(400, "content is not a string")
        if payload.get("encoding", "base64") == "base64":
            try:
                return path, base64.b64decode(raw, validate=False)
            except Exception:
                raise ApiError(400, "content is not valid base64") from None
        return path, raw.encode("utf-8")
    return path, b""
