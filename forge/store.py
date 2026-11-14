"""The forge's own store: objects, refs, repositories, issues, pull requests, labels.

Everything lives in memory and is written out as a canonical JSON export so two runs of the same
instance leave byte-identical state. Nothing here reads a clock or a random source: every id, number
and date comes from the data the caller supplies.
"""

from __future__ import annotations

import json
import threading

from . import gitobj as g

#: The ref namespace git keeps branches and tags in.
HEADS = "refs/heads/"
TAGS = "refs/tags/"


class StoreError(Exception):
    """A store operation the caller asked for that cannot be done."""


class Repo:
    """One repository: its objects, refs, issues, pull requests and labels."""

    def __init__(self, owner: str, name: str, description: str = "", private: bool = False, default_branch: str = "main", created_at: str = ""):
        self.owner = owner
        self.name = name
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.created_at = created_at
        self.objects: dict[str, tuple[bytes, bytes]] = {}
        self.refs: dict[str, str] = {}
        self.issues: dict[int, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.labels: dict[str, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.next_issue = 1
        self.next_pull = 1
        self.next_comment = 1

    # ------------------------------------------------------------------ objects

    def put_object(self, obj_type: bytes, payload: bytes) -> str:
        oid = g.hash_object(obj_type, payload)
        self.objects.setdefault(oid, (obj_type, payload))
        return oid

    def get_object(self, oid: str) -> tuple[bytes, bytes] | None:
        return self.objects.get(oid)

    def has_object(self, oid: str) -> bool:
        return oid in self.objects

    def resolve(self, rev: str) -> str | None:
        """A revision-ish string as an object id: a full oid, a ref, HEAD, or a short prefix.

        A bare name that is not a full ref is also tried as a branch and a tag, which is what the
        REST ``?sha=`` and ``?ref=`` parameters carry.
        """
        if not rev:
            return None
        if rev == "HEAD":
            target = self.refs.get("HEAD")
            if target and target.startswith("ref: "):
                return self.refs.get(target[5:])
            return target
        if rev in self.refs:
            value = self.refs[rev]
            if value.startswith("ref: "):
                return self.refs.get(value[5:])
            return value
        # A bare branch or tag name.
        for candidate in (HEADS + rev, TAGS + rev):
            value = self.refs.get(candidate)
            if value is not None and not value.startswith("ref: "):
                return value
        if g.is_hex_oid(rev) and rev in self.objects:
            return rev
        # A short prefix.
        if 4 <= len(rev) <= 40 and all(c in "0123456789abcdef" for c in rev):
            hits = [oid for oid in self.objects if oid.startswith(rev)]
            if len(hits) == 1:
                return hits[0]
        return None

    def walk(self, roots: list[str], shallow: set[str] | None = None) -> list[str]:
        """Every object reachable from ``roots``, in a deterministic order.

        A commit named in ``shallow`` is a boundary: it is sent (the client needs its bytes), but its
        parents are never visited, which is what makes a depth-limited fetch stop there.
        """
        seen: set[str] = set()
        order: list[str] = []
        stack = list(roots)
        shallow = shallow or set()
        while stack:
            oid = stack.pop()
            if oid in seen or oid not in self.objects:
                continue
            seen.add(oid)
            order.append(oid)
            obj_type, payload = self.objects[oid]
            if obj_type == g.TYPE_COMMIT:
                if oid in shallow:
                    # A boundary commit: its tree is sent, its history is not.
                    c = g.parse_commit(payload)
                    if c["tree"]:
                        stack.append(c["tree"])
                    continue
                c = g.parse_commit(payload)
                if c["tree"]:
                    stack.append(c["tree"])
                stack.extend(c["parents"])
            elif obj_type == g.TYPE_TREE:
                for _mode, _name, child in g.parse_tree(payload):
                    stack.append(child)
            elif obj_type == g.TYPE_TAG:
                t = g.parse_tag(payload)
                if t["object"]:
                    stack.append(t["object"])
        return order

    def commit_depth(self, oid: str) -> int:
        """How many commits precede ``oid`` (0 for a root commit)."""
        depth = 0
        cur = oid
        guard = 0
        while cur and guard < 100000:
            guard += 1
            obj = self.objects.get(cur)
            if obj is None or obj[0] != g.TYPE_COMMIT:
                break
            c = g.parse_commit(obj[1])
            parents = c["parents"]
            if not parents:
                break
            cur = parents[0]
            depth += 1
        return depth

    def shallow_roots(self, tip: str, depth: int) -> tuple[list[str], set[str]]:
        """(roots, shallow) for a depth-limited clone.

        ``roots`` are the commits to send (the kept levels). ``shallow`` names the commits at the
        depth boundary -- the deepest kept commits -- because that is what git's ``shallow-info``
        reports: the client records "this commit is shallow, its parents are absent". For depth 1
        that is the tip itself.
        """
        if depth <= 0:
            return [tip], {tip}
        level = [tip]
        keep: list[str] = []
        boundary: list[str] = []
        for _ in range(depth):
            nxt: list[str] = []
            boundary = []
            for oid in level:
                obj = self.objects.get(oid)
                if obj is None or obj[0] != g.TYPE_COMMIT:
                    continue
                keep.append(oid)
                boundary.append(oid)
                c = g.parse_commit(obj[1])
                nxt.extend(c["parents"])
            level = nxt
            if not level:
                break
        return keep, set(boundary)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when ``ancestor`` is reachable from ``descendant`` by first-parent history."""
        if ancestor == descendant:
            return True
        seen: set[str] = set()
        stack = [descendant]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            obj = self.objects.get(oid)
            if obj is None or obj[0] != g.TYPE_COMMIT:
                continue
            c = g.parse_commit(obj[1])
            for p in c["parents"]:
                if p == ancestor:
                    return True
                stack.append(p)
        return False

    def merge_base(self, a: str, b: str) -> str | None:
        """The best common ancestor of two commits, or None."""
        if a == b:
            return a
        ancestors_a: list[str] = []
        seen: set[str] = set()
        stack = [a]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            ancestors_a.append(oid)
            obj = self.objects.get(oid)
            if obj and obj[0] == g.TYPE_COMMIT:
                stack.extend(g.parse_commit(obj[1])["parents"])
        seen_b: set[str] = set()
        stack = [b]
        while stack:
            oid = stack.pop()
            if oid in seen_b:
                continue
            seen_b.add(oid)
            if oid in ancestors_a:
                return oid
            obj = self.objects.get(oid)
            if obj and obj[0] == g.TYPE_COMMIT:
                stack.extend(g.parse_commit(obj[1])["parents"])
        return None

    def tree_of(self, commit_oid: str) -> str | None:
        obj = self.objects.get(commit_oid)
        if obj is None or obj[0] != g.TYPE_COMMIT:
            return None
        return g.parse_commit(obj[1])["tree"]

    def flatten_tree(self, tree_oid: str, prefix: str = "") -> dict[str, tuple[bytes, str]]:
        """Every blob under a tree as {path: (mode, oid)}."""
        out: dict[str, tuple[bytes, str]] = {}
        obj = self.objects.get(tree_oid)
        if obj is None or obj[0] != g.TYPE_TREE:
            return out
        for mode, name, oid in g.parse_tree(obj[1]):
            path = f"{prefix}{name.decode('utf-8', 'replace')}"
            if mode == g.MODE_TREE:
                out.update(self.flatten_tree(oid, path + "/"))
            else:
                out[path] = (mode, oid)
        return out

    def build_tree_from(self, files: dict[str, tuple[bytes, str]]) -> str:
        """A tree object id from {path: (mode, oid)}, creating the subtrees it needs."""
        root: dict[str, object] = {}
        for path, (mode, oid) in files.items():
            parts = path.split("/")
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})  # type: ignore[assignment]
            node[parts[-1]] = (mode, oid)
        return self._write_tree_node(root)

    def _write_tree_node(self, node: dict) -> str:
        entries: list[tuple[bytes, bytes, str]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                sub = self._write_tree_node(value)
                entries.append((g.MODE_TREE, name.encode("utf-8"), sub))
            else:
                mode, oid = value
                entries.append((mode, name.encode("utf-8"), oid))
        payload = g.build_tree(entries)
        return self.put_object(g.TYPE_TREE, payload)

    def commit_files(self, files: dict[str, tuple[bytes, str]], parents: list[str], author: bytes, committer: bytes, message: bytes) -> str:
        """A commit over a whole file map; returns its oid."""
        tree = self.build_tree_from(files)
        payload = g.build_commit(tree, parents, author, committer, message)
        return self.put_object(g.TYPE_COMMIT, payload)

    # ------------------------------------------------------------------ export

    def export(self) -> dict:
        """The repo's canonical state, with every dict key sorted so two runs serialise identically."""
        return {
            "owner": self.owner,
            "name": self.name,
            "description": self.description,
            "private": self.private,
            "default_branch": self.default_branch,
            "created_at": self.created_at,
            "refs": dict(sorted(self.refs.items())),
            "objects": {oid: [t.decode("ascii"), p.hex()] for oid, (t, p) in sorted(self.objects.items())},
            "issues": {str(n): _sorted_issue(i) for n, i in sorted(self.issues.items())},
            "pulls": {str(n): _sorted_pull(p) for n, p in sorted(self.pulls.items())},
            "labels": {k: v for k, v in sorted(self.labels.items())},
            "comments": {str(n): c for n, c in sorted(self.comments.items())},
            "next_issue": self.next_issue,
            "next_pull": self.next_pull,
            "next_comment": self.next_comment,
        }


def _sorted_issue(issue: dict) -> dict:
    out = dict(issue)
    if "labels" in out and isinstance(out["labels"], list):
        out["labels"] = sorted(out["labels"], key=lambda x: x.get("name", "") if isinstance(x, dict) else str(x))
    return {k: out[k] for k in sorted(out)}


def _sorted_pull(pull: dict) -> dict:
    return {k: pull[k] for k in sorted(pull)}


class Store:
    """Every repository the forge holds, guarded by one lock so requests serialise in order."""

    def __init__(self, login: str, login_id: int = 1, token: str = ""):
        self.login = login
        self.login_id = login_id
        self.token = token
        self.repos: dict[str, Repo] = {}
        self.lock = threading.RLock()
        #: A monotonic counter for object ids that must be unique but not time-based.
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def key(self, owner: str, name: str) -> str:
        return f"{owner}/{name}"

    def get(self, owner: str, name: str) -> Repo | None:
        return self.repos.get(self.key(owner, name))

    def create(self, owner: str, name: str, description: str = "", private: bool = False, created_at: str = "") -> Repo:
        k = self.key(owner, name)
        if k in self.repos:
            raise StoreError(f"repository {k} already exists")
        repo = Repo(owner, name, description, private, "main", created_at)
        self.repos[k] = repo
        return repo

    def delete(self, owner: str, name: str) -> bool:
        return self.repos.pop(self.key(owner, name), None) is not None

    def list_for(self, owner: str) -> list[Repo]:
        return [r for k, r in sorted(self.repos.items()) if r.owner == owner]

    def all_repos(self) -> list[Repo]:
        return [r for _k, r in sorted(self.repos.items())]

    def export(self) -> dict:
        return {
            "login": self.login,
            "login_id": self.login_id,
            "seq": self.seq,
            "repos": {k: r.export() for k, r in sorted(self.repos.items())},
        }

    def export_json(self) -> str:
        """The canonical export: sorted keys, no whitespace, ASCII-safe."""
        return json.dumps(self.export(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
