"""Flattened LabVIEW type descriptors (big-endian, as Variant To Flattened String returns them)
-> vi_create type notation ("dbl", "i32[]", {"cluster": [...]}, {"enum": [...]}, "error")."""
from __future__ import annotations

import struct

_NUM = {0x01: "i8", 0x02: "i16", 0x03: "i32", 0x04: "i64", 0x05: "u8", 0x06: "u16", 0x07: "u32",
        0x08: "u64", 0x09: "sgl", 0x0A: "dbl", 0x0B: "ext", 0x0C: "csg", 0x0D: "cdb", 0x0E: "cxt"}
_ENUM = {0x15, 0x16, 0x17}


def _pascal(b: bytes, at: int) -> tuple[str, int]:
    if at >= len(b):
        return "", at
    n = b[at]
    return b[at + 1:at + 1 + n].decode("mbcs", "replace"), at + 1 + n


def parse(b: bytes, at: int = 0) -> tuple[object, str, int]:
    """(type, element name, offset after this descriptor)."""
    size, flags, code = struct.unpack_from(">HBB", b, at)
    end = at + size
    body = at + 4
    named = bool(flags & 0x40)
    t: object
    p = body
    if code in _NUM:
        t = _NUM[code]
    elif code == 0x21:
        t = "bool"
    elif code == 0x30:
        t, p = "string", body + 4
    elif code == 0x32:
        t, p = "path", body + 4
    elif code in _ENUM:
        k = struct.unpack_from(">H", b, body)[0]
        p, items = body + 2, []
        for _ in range(k):
            s, p = _pascal(b, p)
            items.append(s)
        t = {"enum": items}
    elif code == 0x40:
        dims = struct.unpack_from(">H", b, body)[0]
        elem, _, p = parse(b, body + 2 + 4 * dims)
        t = (elem + "[]" * dims) if isinstance(elem, str) else {"array": elem, "dims": dims}
    elif code == 0x50:
        k = struct.unpack_from(">H", b, body)[0]
        p, fields = body + 2, []
        for _ in range(k):
            ft, fn, p = parse(b, p)
            fields.append({"name": fn, "type": ft})
        t = "error" if [f["name"] for f in fields] == ["status", "code", "source"] else {"cluster": fields}
    elif code == 0x70:
        from .dfir import refnum_type
        t = refnum_type(b[body:end], ">")
        p = body + 8 if t.__class__ is str else end
    else:
        t = f"type_0x{code:02x}"
    name = ""
    if named:
        if p < end and b[p] == 0 and isinstance(t, dict) and "enum" in t:
            p += 1  # pad byte after the enum items
        if p < end:
            name, _ = _pascal(b, p)
    return t, name, end


def from_flattened(type_string) -> object:
    """type string as LabVIEW returns it through COM (an I16 array) -> vi_create type."""
    b = b"".join(struct.pack(">h", x) for x in type_string) if not isinstance(type_string, bytes) else type_string
    t, _, _ = parse(b)
    return t
