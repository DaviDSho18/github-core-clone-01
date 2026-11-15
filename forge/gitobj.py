"""Git object model and packfile codec — standard library only.

Everything here is deterministic: no clock, no randomness, no port. Object ids are SHA-1 of the
canonical git serialisation, so the same bytes always hash to the same id.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

# ----------------------------------------------------------------------------- object types

TYPE_COMMIT = b"commit"
TYPE_TREE = b"tree"
TYPE_BLOB = b"blob"
TYPE_TAG = b"tag"

TYPE_BY_NUM = {1: TYPE_COMMIT, 2: TYPE_TREE, 3: TYPE_BLOB, 4: TYPE_TAG}
NUM_BY_TYPE = {v: k for k, v in TYPE_BY_NUM.items()}

ZERO_OID = "0" * 40

#: The mode git records for a regular file, a directory, and a symlink.
MODE_FILE = b"100644"
MODE_EXEC = b"100755"
MODE_LINK = b"120000"
MODE_TREE = b"40000"


def is_hex_oid(text: str) -> bool:
    """True when ``text`` is a 40-character lowercase hex object id."""
    if not isinstance(text, str) or len(text) != 40:
        return False
    return all(c in "0123456789abcdef" for c in text)


def hash_object(obj_type: bytes, payload: bytes) -> str:
    """The git object id of a payload: sha1("<type> <len>\\0" + payload)."""
    header = obj_type + b" " + str(len(payload)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + payload).hexdigest()


def loose_object(obj_type: bytes, payload: bytes) -> bytes:
    """The zlib-deflated loose object file body for a payload."""
    header = obj_type + b" " + str(len(payload)).encode("ascii") + b"\x00"
    return zlib.compress(header + payload, 6)


def inflate_all(data: bytes) -> bytes:
    """Inflate a complete zlib stream, tolerating trailing bytes."""
    d = zlib.decompressobj()
    out = d.decompress(data)
    out += d.flush()
    return out


# ----------------------------------------------------------------------------- tree encoding


def parse_tree(payload: bytes) -> list[tuple[bytes, bytes, str]]:
    """A tree payload as [(mode, name, oid)] in stored order."""
    entries: list[tuple[bytes, bytes, str]] = []
    i = 0
    n = len(payload)
    while i < n:
        sp = payload.index(b" ", i)
        mode = payload[i:sp]
        nul = payload.index(b"\x00", sp)
        name = payload[sp + 1 : nul]
        oid = payload[nul + 1 : nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def build_tree(entries: list[tuple[bytes, bytes, str]]) -> bytes:
    """A tree payload from [(mode, name, oid)], sorted the way git sorts tree entries."""
    ordered = sorted(entries, key=lambda e: _tree_sort_key(e[1], e[0]))
    out = bytearray()
    for mode, name, oid in ordered:
        out += mode + b" " + name + b"\x00" + bytes.fromhex(oid)
    return bytes(out)


def _tree_sort_key(name: bytes, mode: bytes) -> bytes:
    """Git sorts a tree by name, a subtree's name compared as if it ended in '/'."""
    if mode == MODE_TREE:
        return name + b"/"
    return name


# ----------------------------------------------------------------------------- commit encoding


def parse_commit(payload: bytes) -> dict:
    """A commit payload as a dict: tree, parents, author, committer, message, and the extra headers."""
    head, _, message = payload.partition(b"\n\n")
    out: dict = {"tree": None, "parents": [], "author": None, "committer": None, "message": message, "extra": []}
    for line in head.split(b"\n"):
        if not line:
            continue
        key, _, value = line.partition(b" ")
        k = key.decode("ascii", "replace")
        if k == "tree":
            out["tree"] = value.decode("ascii", "replace")
        elif k == "parent":
            out["parents"].append(value.decode("ascii", "replace"))
        elif k == "author":
            out["author"] = value
        elif k == "committer":
            out["committer"] = value
        else:
            out["extra"].append((k, value))
    return out


def build_commit(tree: str, parents: list[str], author: bytes, committer: bytes, message: bytes, extra=None) -> bytes:
    """A commit payload. ``extra`` is a list of (key, value) header pairs appended after committer."""
    out = bytearray()
    out += b"tree " + tree.encode("ascii") + b"\n"
    for p in parents:
        out += b"parent " + p.encode("ascii") + b"\n"
    out += b"author " + author + b"\n"
    out += b"committer " + committer + b"\n"
    for key, value in extra or []:
        out += key + b" " + value + b"\n"
    out += b"\n" + message
    return bytes(out)


def parse_tag(payload: bytes) -> dict:
    """An annotated tag payload as a dict: object, type, tag, tagger, message."""
    head, _, message = payload.partition(b"\n\n")
    out: dict = {"object": None, "type": None, "tag": None, "tagger": None, "message": message}
    for line in head.split(b"\n"):
        if not line:
            continue
        key, _, value = line.partition(b" ")
        k = key.decode("ascii", "replace")
        if k in ("object", "type", "tag"):
            out[k] = value.decode("ascii", "replace")
        elif k == "tagger":
            out["tagger"] = value
    return out


def build_tag(obj: str, obj_type: bytes, name: str, tagger: bytes, message: bytes) -> bytes:
    """An annotated tag payload."""
    return (
        b"object " + obj.encode("ascii") + b"\n"
        b"type " + obj_type + b"\n"
        b"tag " + name.encode("utf-8") + b"\n"
        b"tagger " + tagger + b"\n\n" + message
    )


# ----------------------------------------------------------------------------- packfile reading


class PackError(Exception):
    """A packfile that cannot be read."""


class PackObject:
    """One object read from a packfile: its type, its inflated payload, and its delta base."""

    __slots__ = ("type_num", "payload", "base_offset", "base_oid")

    def __init__(self, type_num, payload, base_offset=None, base_oid=None):
        self.type_num = type_num
        self.payload = payload
        self.base_offset = base_offset
        self.base_oid = base_oid


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    """A little-endian base-128 varint (git's size encoding); returns (value, next index)."""
    value = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, i
        shift += 7


def parse_pack(data: bytes, resolve_base=None) -> list[tuple[int, bytes, bytes]]:
    """Read a packfile into [(offset, type, payload)] with every delta resolved.

    ``resolve_base(oid)`` returns (type, payload) for a ref-delta whose base is outside the pack.
    """
    if data[:4] != b"PACK":
        raise PackError("not a packfile: bad signature")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise PackError(f"unsupported pack version {version}")

    by_offset: dict[int, PackObject] = {}
    order: list[int] = []
    i = 12
    for _ in range(count):
        start = i
        b = data[i]
        i += 1
        type_num = (b >> 4) & 0x07
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = data[i]
            i += 1
            size |= (b & 0x7F) << shift
            shift += 7

        if type_num in (1, 2, 3, 4):
            payload = _inflate_at(data, i, size)
            i = _consumed_after(data, i, size)
            by_offset[start] = PackObject(type_num, payload)
        elif type_num == 6:  # ofs-delta
            neg, i = _read_varint(data, i)
            base_off = start - neg
            if base_off not in by_offset:
                raise PackError(f"ofs-delta at {start} names unknown base {base_off}")
            delta = _inflate_at(data, i, size)
            i = _consumed_after(data, i, size)
            by_offset[start] = PackObject(0, delta, base_offset=base_off)
        elif type_num == 7:  # ref-delta
            base_oid = data[i : i + 20].hex()
            i += 20
            delta = _inflate_at(data, i, size)
            i = _consumed_after(data, i, size)
            by_offset[start] = PackObject(0, delta, base_oid=base_oid)
        else:
            raise PackError(f"unknown pack object type {type_num}")
        order.append(start)

    out: list[tuple[int, bytes, bytes]] = []
    for start in order:
        obj = by_offset[start]
        out.append((start, *_resolve(obj, by_offset, resolve_base)))
    return out


def _inflate_at(data: bytes, i: int, expected: int) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(data[i:])
    if len(out) != expected:
        # A short read means the stream was truncated; git tolerates it in some cases, we do not.
        raise PackError(f"inflated {len(out)} bytes, expected {expected}")
    return out


def _consumed_after(data: bytes, i: int, expected: int) -> int:
    """The index just past the zlib stream that produced ``expected`` bytes starting at ``i``."""
    d = zlib.decompressobj()
    d.decompress(data[i:])
    return len(data) - len(d.unused_data)


def _resolve(obj: PackObject, by_offset: dict, resolve_base):
    """A pack object's final (type, payload), following delta chains."""
    if obj.type_num != 0:
        return TYPE_BY_NUM[obj.type_num], obj.payload
    if obj.base_offset is not None:
        base = by_offset.get(obj.base_offset)
        if base is None:
            raise PackError(f"delta base offset {obj.base_offset} missing")
        base_type, base_payload = _resolve(base, by_offset, resolve_base)
    else:
        if resolve_base is None:
            raise PackError(f"ref-delta needs base {obj.base_oid} but no resolver was given")
        found = resolve_base(obj.base_oid)
        if found is None:
            raise PackError(f"delta base {obj.base_oid} not found")
        base_type, base_payload = found
    return base_type, apply_delta(base_payload, obj.payload)


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git delta to a base payload."""
    i = 0
    base_size, i = _read_varint(delta, i)
    if base_size != len(base):
        raise PackError(f"delta base size {base_size} != {len(base)}")
    result_size, i = _read_varint(delta, i)
    out = bytearray()
    n = len(delta)
    while i < n:
        op = delta[i]
        i += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[i] << (bit * 8)
                    i += 1
            for bit in range(3):
                if op & (1 << (bit + 4)):
                    size |= delta[i] << (bit * 8)
                    i += 1
            if size == 0:
                size = 0x10000
            out += base[offset : offset + size]
        elif op:
            out += delta[i : i + op]
            i += op
        else:
            raise PackError("delta opcode 0 is reserved")
    if len(out) != result_size:
        raise PackError(f"delta produced {len(out)} bytes, expected {result_size}")
    return bytes(out)


# ----------------------------------------------------------------------------- packfile writing


def encode_obj_header(type_num: int, size: int) -> bytes:
    """The pack object header: type in bits 4-6, size as a little-endian base-128 varint."""
    out = bytearray()
    b = (type_num << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(b | 0x80)
        b = size & 0x7F
        size >>= 7
    out.append(b)
    return bytes(out)


def build_pack(objects: list[tuple[bytes, bytes]]) -> bytes:
    """A complete packfile (no deltas) from [(type, payload)], in the order given."""
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(objects))
    for obj_type, payload in objects:
        out += encode_obj_header(NUM_BY_TYPE[obj_type], len(payload))
        out += zlib.compress(payload, 6)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def build_pack_from_deflated(entries: list[tuple[int, int, bytes]]) -> bytes:
    """A complete packfile from [(type_num, raw_size, deflated_payload)].

    Byte-identical to ``build_pack`` for the same objects, but it skips the compression: the caller
    supplies payloads that were deflated once and kept. A shallow clone of a 200-commit repository
    deflates 600 small objects (~29 ms on this box, dominated by per-call zlib setup rather than the
    bytes), and 50 concurrent clones pay that 50 times over for data that cannot change while the ref
    stands still -- so the deflated form is what a repository caches, not the payload.
    """
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(entries))
    for type_num, raw_size, deflated in entries:
        out += encode_obj_header(type_num, raw_size)
        out += deflated
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def verify_pack_checksum(data: bytes) -> bool:
    """True when a packfile's trailing sha1 matches its bytes."""
    if len(data) < 32 or data[:4] != b"PACK":
        return False
    return hashlib.sha1(data[:-20]).digest() == data[-20:]
