"""The REST v3 routing: the paths, the auth rule and the operations behind them.

This is the layer the conformance suite drives. It keeps the JSON shapes in restapi.py and the
objects in store.py, and adds only what a request needs: the token check, the path match, the
query parameters and the status codes.
"""

from __future__ import annotations

import base64
import binascii
import json

from . import restapi as rest
from . import scenario as sc
from .store import Repo, StoreError, HEADS, TAGS

#: The token scopes GET /api/v3/ reports.
OAUTH_SCOPES = rest.OAUTH_SCOPES

#: The route table: a method, a pattern, and the handler name.
ROUTES: list[tuple[str, str]] = []


def route(pattern: str, handler: str) -> None:
    ROUTES.append((pattern, handler))


def split_path(path: str) -> list[str]:
    """The path under /api/v3 as segments; `contents/a/b.txt` keeps its slashes."""
    tail = path[len("/api/v3") :]
    return [seg for seg in tail.split("/") if seg != ""]


def match_route(path: str):
    """The first route whose pattern matches, with its captured groups."""
    segments = split_path(path)
    for pattern, handler in ROUTES:
        parts = pattern.split("/")
        if len(parts) != len(segments):
            continue
        captures: list[str] = []
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


# ----------------------------------------------------------------------------- the routes

route("", "root")
route("meta", "meta")
route("user", "user")
route("user/repos", "user_repos")
route("repos/:owner/:repo", "repo")
route("repos/:owner/:repo/branches", "branches")
route("repos/:owner/:repo/branches/:branch", "branch")
route("repos/:owner/:repo/commits", "commits")
route("repos/:owner/:repo/commits/:sha", "commit")
route("repos/:owner/:repo/git/refs", "git_refs")
route("repos/:owner/:repo/git/refs/:ref", "git_ref")
route("repos/:owner/:repo/contents/:path", "contents")
route("repos/:owner/:repo/labels", "labels")
route("repos/:owner/:repo/labels/:name", "label")
route("repos/:owner/:repo/issues", "issues")
route("repos/:owner/:repo/issues/:number", "issue")
route("repos/:owner/:repo/issues/:number/comments", "issue_comments")
route("repos/:owner/:repo/issues/comments/:id", "issue_comment")
route("repos/:owner/:repo/pulls", "pulls")
route("repos/:owner/:repo/pulls/:number", "pull")
route("repos/:owner/:repo/pulls/:number/files", "pull_files")
route("repos/:owner/:repo/pulls/:number/commits", "pull_commits")
route("repos/:owner/:repo/pulls/:number/merge", "pull_merge")
route("repos/:owner/:repo/rate_limit", "rate_limit")


# ----------------------------------------------------------------------------- helpers

def iso(epoch_sec: int) -> str:
    return sc.iso_of(epoch_sec)


def page_params(query: dict) -> tuple[int, int]:
    """(page, per_page) from a query string, clamped the way GitHub clamps them."""
    page = 1
    per_page = rest.PER_PAGE
    raw_page = query.get("page")
    if raw_page:
        try:
            page = max(1, int(raw_page[0]))
        except ValueError:
            page = 1
    raw_per = query.get("per_page")
    if raw_per:
        try:
            per_page = max(1, min(100, int(raw_per[0])))
        except ValueError:
            per_page = rest.PER_PAGE
    return page, per_page


def paginated(items: list, query: dict, base_path: str) -> tuple[list, dict[str, str]]:
    """One page of items plus the Link and pagination headers."""
    page, per_page = page_params(query)
    chunk, has_next = rest.paginate(items, page, per_page)
    last_page = max(1, (len(items) + per_page - 1) // per_page)
    headers: dict[str, str] = {}
    link = rest.link_header(base_path, page, has_next, page > 1, last_page)
    if link:
        headers["link"] = link
    return chunk, headers
