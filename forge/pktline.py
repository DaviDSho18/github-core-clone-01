"""The git pkt-line framing and the smart-HTTP protocol v0/v2 codec.

Standard library only, deterministic. No clock, no randomness.
"""

from __future__ import annotations

#: The largest pkt-line payload git allows (the 4-byte length prefix caps a line at 65520 bytes).
PKT_LINE_MAX = 65516

FLUSH = b"0000"
DELIM = b"0001"
RESPONSE_END = b"0002"


def pkt_line(payload: bytes) -> bytes:
    """One pkt-line: a 4-hex-digit length (payload + 4) then the payload."""
    n = len(payload) + 4
    if n > 65520:
        raise ValueError(f"pkt-line payload of {len(payload)} bytes is too long")
    return f"{n:04x}".encode("ascii") + payload


def pkt_text(text: str) -> bytes:
    return pkt_line(text.encode("utf-8"))


class PktReader:
    """A pkt-line reader over a byte buffer: flush, delim and response-end are distinct signals."""

    def __init__(self, data: bytes):
        self.data = data
        self.i = 0

    def at_end(self) -> bool:
        return self.i >= len(self.data)

    def read(self) -> tuple[str, bytes | None]:
        """The next packet as (kind, payload): kind is 'data', 'flush', 'delim' or 'end'."""
        if self.i + 4 > len(self.data):
            return "end", None
        head = self.data[self.i : self.i + 4]
        try:
            n = int(head, 16)
        except ValueError:
            # Not a length: treat the rest as one raw data line (git tolerates a bare line).
            rest = self.data[self.i :]
            self.i = len(self.data)
            return "data", rest
        if n == 0:
            self.i += 4
            return "flush", None
        if n == 1:
            self.i += 4
            return "delim", None
        if n == 2:
            self.i += 4
            return "end", None
        if n < 4:
            raise ValueError(f"invalid pkt-line length {n}")
        payload = self.data[self.i + 4 : self.i + n]
        self.i += n
        return "data", payload

    def read_text(self) -> str | None:
        """The next data packet's payload as text, or None at a flush/delim/end."""
        kind, payload = self.read()
        if kind != "data":
            return None
        return payload.decode("utf-8", "replace").rstrip("\n")


def read_pkt_lines(data: bytes) -> list[bytes]:
    """Every data packet's payload, stopping at the first flush."""
    out: list[bytes] = []
    r = PktReader(data)
    while not r.at_end():
        kind, payload = r.read()
        if kind != "data":
            break
        out.append(payload.rstrip(b"\n"))
    return out


def sideband(channel: int, payload: bytes) -> bytes:
    """A side-band-64k frame: one channel byte then the payload, chunked to fit a pkt-line."""
    out = bytearray()
    limit = PKT_LINE_MAX - 1
    for i in range(0, len(payload), limit):
        chunk = payload[i : i + limit]
        out += pkt_line(bytes([channel]) + chunk)
    return bytes(out)


def capabilities_of(line: bytes) -> tuple[str, list[str]]:
    """A v0 ref advertisement line as (ref name, capability list); the caps ride on the first ref."""
    text = line.decode("utf-8", "replace")
    if "\x00" in text:
        ref, _, caps = text.partition("\x00")
        return ref, caps.split()
    return text, []


def parse_want_lines(lines: list[bytes]) -> dict:
    """A v2 fetch request as a dict: wants, haves, shallows, deepen, done, and the raw commands."""
    out: dict = {"wants": [], "haves": [], "shallows": [], "deepen": None, "deepen_relative": False, "done": False, "filters": [], "sideband": False, "ofs_delta": False, "thin": False, "no_progress": False, "include_tag": False}
    for line in lines:
        text = line.decode("utf-8", "replace")
        if text.startswith("want "):
            out["wants"].append(text[5:].strip())
        elif text.startswith("have "):
            out["haves"].append(text[5:].strip())
        elif text.startswith("shallow "):
            out["shallows"].append(text[8:].strip())
        elif text.startswith("deepen "):
            out["deepen"] = int(text[7:].strip())
        elif text == "deepen-relative":
            out["deepen_relative"] = True
        elif text == "done":
            out["done"] = True
        elif text.startswith("filter "):
            out["filters"].append(text[7:].strip())
        elif text == "side-band-64k":
            out["sideband"] = True
        elif text == "ofs-delta":
            out["ofs_delta"] = True
        elif text == "thin-pack":
            out["thin"] = True
        elif text == "no-progress":
            out["no_progress"] = True
        elif text == "include-tag":
            out["include_tag"] = True
    return out


def parse_receive_commands(lines: list[bytes]) -> list[tuple[str, str, str]]:
    """receive-pack commands as [(old_oid, new_oid, ref)]."""
    out: list[tuple[str, str, str]] = []
    for line in lines:
        text = line.decode("utf-8", "replace").rstrip("\n")
        if not text:
            continue
        parts = text.split(" ")
        if len(parts) < 3:
            continue
        old, new, ref = parts[0], parts[1], parts[2]
        # The first command may carry capabilities after a NUL.
        if "\x00" in ref:
            ref = ref.split("\x00", 1)[0]
        out.append((old, new, ref))
    return out


def report_status_line(ok: bool, ref: str, reason: str = "") -> bytes:
    """One report-status line: 'ok <ref>' or 'ng <ref> <reason>'."""
    if ok:
        return pkt_text(f"ok {ref}\n")
    return pkt_text(f"ng {ref} {reason}\n")
