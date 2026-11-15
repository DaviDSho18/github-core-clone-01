"""git smart HTTP: the v0 and v2 advertisement, upload-pack (clone/fetch) and receive-pack (push).

Standard library only. Deterministic: no clock, no randomness, no port in any answer.
"""

from __future__ import annotations

from . import gitobj as g
from . import pktline as pk
from .store import Repo, Store, HEADS, TAGS

#: The capabilities a v0 upload-pack advertises.
UPLOAD_CAPS = (
    "multi_ack thin-pack side-band side-band-64k ofs-delta shallow deepen-since deepen-not "
    "deepen-relative no-progress include-tag multi_ack_detailed symref=HEAD:refs/heads/main "
    "object-format=sha1 agent=makefun-forge/1.0"
).split()

#: The capabilities a v0 receive-pack advertises. report-status, side-band-64k and delete-refs are required.
RECEIVE_CAPS = (
    "report-status report-status-v2 delete-refs side-band-64k quiet atomic ofs-delta "
    "object-format=sha1 agent=makefun-forge/1.0"
).split()

#: The capabilities a v2 upload-pack advertises.
V2_CAPS = ["version 2", "agent=makefun-forge/1.0", "ls-refs=unborn", "fetch=shallow wait-for-done", "object-format=sha1"]


def service_advertisement(service: str, caps: list[str], refs: dict[str, str], head_target: str | None) -> bytes:
    """A v0 smart-HTTP advertisement: the service preamble, then the refs with caps on the first line.

    ``HEAD`` is stored as the symbolic ref ``ref: refs/heads/<branch>``; it is advertised as the
    object id that ref resolves to, with ``symref=HEAD:<target>`` among the capabilities.
    """
    out = bytearray()
    out += pk.pkt_text(f"# service={service}\n")
    out += pk.FLUSH

    # Resolve every ref to a real object id; a symbolic HEAD becomes its target's id.
    resolved: dict[str, str] = {}
    for ref, oid in refs.items():
        if oid.startswith("ref: "):
            target = oid[5:]
            if ref == "HEAD":
                target_oid = refs.get(target)
                if target_oid and not target_oid.startswith("ref: "):
                    resolved["HEAD"] = target_oid
            continue
        resolved[ref] = oid

    # HEAD first (git's own order), then the refs in sorted order.
    ordered: list[tuple[str, str]] = []
    if "HEAD" in resolved:
        ordered.append(("HEAD", resolved["HEAD"]))
    for ref in sorted(resolved):
        if ref == "HEAD":
            continue
        ordered.append((ref, resolved[ref]))

    first = True
    if not ordered:
        # An empty repository advertises the zero id with the capabilities.
        line = f"{g.ZERO_OID} capabilities^{{}}"
        out += pk.pkt_line(line.encode("ascii") + b"\x00" + " ".join(caps).encode("ascii") + b"\n")
    else:
        for ref, oid in ordered:
            if first:
                cap_text = " ".join(caps)
                if ref == "HEAD" and head_target:
                    cap_text += f" symref=HEAD:{head_target}"
                out += pk.pkt_line(f"{oid} {ref}".encode("ascii") + b"\x00" + cap_text.encode("ascii") + b"\n")
                first = False
            else:
                out += pk.pkt_text(f"{oid} {ref}\n")
    out += pk.FLUSH
    return bytes(out)


def v2_advertisement() -> bytes:
    """The v2 capability advertisement: no preamble, no refs, just the capability lines."""
    out = bytearray()
    for cap in V2_CAPS:
        out += pk.pkt_text(cap + "\n")
    out += pk.FLUSH
    return bytes(out)


def _head_target(refs: dict[str, str]) -> str | None:
    head = refs.get("HEAD")
    if head and head.startswith("ref: "):
        return head[5:]
    return None


def ls_refs(repo: Repo, lines: list[bytes]) -> bytes:
    """A v2 ls-refs answer: HEAD (with symref) then every ref, honouring ref-prefix filters."""
    prefixes: list[str] = []
    peel = False
    symrefs = False
    unborn = False
    for line in lines:
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("ref-prefix "):
            prefixes.append(text[11:])
        elif text == "peel":
            peel = True
        elif text == "symrefs":
            symrefs = True
        elif text == "unborn":
            unborn = True

    def wanted(ref: str) -> bool:
        if not prefixes:
            return True
        return any(ref.startswith(p) for p in prefixes)

    out = bytearray()
    head = repo.refs.get("HEAD")
    head_oid = None
    if head and head.startswith("ref: "):
        head_oid = repo.refs.get(head[5:])
    elif head:
        head_oid = head
    if wanted("HEAD"):
        if head_oid:
            line = f"{head_oid} HEAD"
            if symrefs and head and head.startswith("ref: "):
                line += f" symref-target:{head[5:]}"
            out += pk.pkt_text(line + "\n")
        elif unborn:
            line = "unborn HEAD"
            if symrefs:
                line += " symref-target:refs/heads/main"
            out += pk.pkt_text(line + "\n")
    for ref, oid in sorted(repo.refs.items()):
        if ref == "HEAD" or oid.startswith("ref: "):
            continue
        if not wanted(ref):
            continue
        line = f"{oid} {ref}"
        if peel and ref.startswith(TAGS):
            peeled = _peel(repo, oid)
            if peeled:
                # v2 carries the peel as a token on the ref's own line, not a `^{}` line.
                line += f" peeled:{peeled}"
        out += pk.pkt_text(line + "\n")
    out += pk.FLUSH
    return bytes(out)


def _peel(repo: Repo, oid: str) -> str | None:
    """The commit a tag points at, when it is an annotated tag."""
    obj = repo.objects.get(oid)
    if obj is None or obj[0] != g.TYPE_TAG:
        return None
    return g.parse_tag(obj[1])["object"]


def fetch(repo: Repo, lines: list[bytes]) -> bytes:
    """A v2 fetch answer: the acknowledgments and shallow-info sections, then the packfile section.

    The v2 response is sectioned: ``acknowledgments``, then ``shallow-info`` when the boundary moved,
    then ``packfile`` whose payload is side-band framed. Each section is introduced by its own name
    line and closed with a delim packet; the whole response ends with a flush.
    """
    req = pk.parse_want_lines(lines)
    wants = [w for w in req["wants"] if repo.has_object(w)]
    haves = [h for h in req["haves"] if repo.has_object(h)]

    out = bytearray()

    # --- acknowledgments: the haves the server holds, then ready/NAK.
    out += pk.pkt_text("acknowledgments\n")
    acknowledged = False
    for h in haves:
        out += pk.pkt_text(f"ACK {h}\n")
        acknowledged = True
    if not acknowledged:
        out += pk.pkt_text("NAK\n")
    out += pk.DELIM

    # --- shallow-info: the boundary lines, only when the request asked for a shallow fetch.
    # A deepen replaces the old boundary: the commits the client reported as shallow are unshallowed
    # when the new depth reaches past them, so the boundary is computed from the new depth alone.
    requested_shallows = set(req["shallows"])
    shallow_set: set[str] = set(requested_shallows)
    unshallow_set: set[str] = set()
    if req["deepen"] is not None:
        new_boundary = _deepen(repo, wants, haves, req["deepen"], req["deepen_relative"])
        # Anything the client called shallow that the new depth now includes is unshallowed.
        kept = set()
        for w in wants:
            kept.update(repo.shallow_roots(w, req["deepen"])[0])
        unshallow_set = {oid for oid in requested_shallows if oid in kept}
        shallow_set = new_boundary
    if requested_shallows or req["deepen"] is not None:
        out += pk.pkt_text("shallow-info\n")
        for s in sorted(shallow_set):
            out += pk.pkt_text(f"shallow {s}\n")
        for s in sorted(unshallow_set):
            out += pk.pkt_text(f"unshallow {s}\n")
        out += pk.DELIM

    # --- packfile: everything reachable from the wants that the haves do not already cover.
    # A have the client reported as shallow is a boundary: the client holds that commit's own bytes
    # (and its tree), but not its history. So the have-walk stops at every still-shallow commit's
    # parents -- which is what makes a deepen deliver exactly the next generation.
    have_set: set[str] = set()
    for h in haves:
        have_set.update(repo.walk([h], shallow=requested_shallows))
    send: list[str] = []
    seen: set[str] = set()
    for w in sorted(wants):
        for oid in repo.walk([w], shallow=shallow_set):
            if oid in have_set or oid in seen:
                continue
            seen.add(oid)
            send.append(oid)

    pack = _pack_of(repo, send, shallow_set)
    out += pk.pkt_text("packfile\n")
    out += pk.sideband(1, pack)
    out += pk.FLUSH
    return bytes(out)


def _pack_of(repo: Repo, send: list[str], shallow_set: set[str]) -> bytes:
    """The packfile for one fetch, built once per (object set, shallow boundary) and then reused.

    The object set is fully determined by the wants and the shallow boundary -- the refs it came from
    are already resolved -- so the same key returns the same bytes. A 50-clone round asks for the
    identical pack 50 times; without this each one re-deflates 600 objects. The repository drops the
    cache whenever a ref moves (store.RefMap), so a stale pack cannot outlive the state it describes.
    """
    key = (tuple(send), tuple(sorted(shallow_set)))
    cached = repo.pack_cache.get(key)
    if cached is not None:
        return cached
    entries = []
    for oid in send:
        entry = repo.deflated_of(oid)
        if entry is None:
            continue
        entries.append(entry)
    pack = g.build_pack_from_deflated(entries)
    # Bound the cache: a push round writes 200 new tips, and each would otherwise keep a whole pack.
    if len(repo.pack_cache) >= 8:
        repo.pack_cache.clear()
    repo.pack_cache[key] = pack
    return pack


def _deepen(repo: Repo, wants: list[str], haves: list[str], depth: int, relative: bool) -> set[str]:
    """The shallow boundary for a depth-limited fetch: the commits just past the kept levels."""
    shallow: set[str] = set()
    for w in wants:
        _roots, cut = repo.shallow_roots(w, depth)
        shallow |= cut
    return shallow


def receive_pack(repo: Repo, commands: list[tuple[str, str, str]], pack: bytes) -> bytes:
    """A receive-pack answer: apply the commands, then one report-status line each, side-band framed."""
    # Read the pack first: a command may need the objects it carries.
    if pack and pack[:4] == b"PACK":
        try:
            for _off, obj_type, payload in g.parse_pack(pack, resolve_base=lambda oid: repo.get_object(oid)):
                repo.put_object(obj_type, payload)
        except g.PackError as err:
            body = bytearray()
            body += pk.pkt_text("unpack " + str(err).replace("\n", " ") + "\n")
            for _old, _new, ref in commands:
                body += pk.report_status_line(False, ref, "unpacker error")
            body += pk.FLUSH
            return pk.sideband(1, bytes(body)) + pk.FLUSH

    results: list[tuple[bool, str, str]] = []
    for old, new, ref in commands:
        ok, reason = _apply_command(repo, old, new, ref)
        results.append((ok, ref, reason))

    body = bytearray()
    body += pk.pkt_text("unpack ok\n")
    for ok, ref, reason in results:
        body += pk.report_status_line(ok, ref, reason)
    body += pk.FLUSH
    return pk.sideband(1, bytes(body)) + pk.FLUSH


def _apply_command(repo: Repo, old: str, new: str, ref: str) -> tuple[bool, str]:
    """One ref update: create, fast-forward, or delete. Returns (ok, reason)."""
    current = repo.refs.get(ref)
    if current and current.startswith("ref: "):
        current = repo.refs.get(current[5:])

    if new == g.ZERO_OID:
        # A delete.
        if current is None:
            return False, "no such ref"
        if old != g.ZERO_OID and current != old:
            return False, "stale info"
        repo.refs.pop(ref, None)
        return True, ""

    if not repo.has_object(new):
        return False, "missing necessary objects"

    if current is None:
        # A create: only valid from the zero id (or a matching old id).
        if old != g.ZERO_OID and not repo.has_object(old):
            return False, "stale info"
        repo.refs[ref] = new
        if ref.startswith(HEADS) and repo.refs.get("HEAD") is None:
            repo.refs["HEAD"] = f"ref: {ref}"
        return True, ""

    if current == new:
        return True, ""
    if old != g.ZERO_OID and current != old:
        return False, "stale info"
    if not repo.is_ancestor(current, new):
        return False, "non-fast-forward"
    repo.refs[ref] = new
    return True, ""
