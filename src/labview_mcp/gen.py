"""Build LabVIEW block diagrams from a JSON-friendly spec.

The spec is translated to LabVIEW's internal diagram-scripting XML and executed by the
bundled helper VI `vis/lvmcp_gen_script.vi`, which calls the VI Server method
"Gen Script Diagram" on the target VI's block diagram.

Typed controls/constants are described to LabVIEW as flattened type descriptors plus
flattened data (big-endian), which the helper VI turns into variants with
"Flattened String To Variant". They are passed through COM as byte arrays: passing them as
text would run them through the Windows code page and corrupt bytes >= 0x80.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import quoteattr

try:
    from mcp.server.mcpserver.exceptions import ToolError as _Base
except ImportError:  # pragma: no cover
    _Base = ValueError


class SpecError(_Base):
    pass


# ---- types ------------------------------------------------------------------
_NUM = {  # name: (type code, struct fmt)
    "i8": (0x01, "b"), "i16": (0x02, "h"), "i32": (0x03, "i"), "i64": (0x04, "q"),
    "u8": (0x05, "B"), "u16": (0x06, "H"), "u32": (0x07, "I"), "u64": (0x08, "Q"),
    "sgl": (0x09, "f"), "dbl": (0x0A, "d"),
}
_ALIASES = {"double": "dbl", "float": "dbl", "float64": "dbl", "single": "sgl", "float32": "sgl",
            "int": "i32", "int32": "i32", "int16": "i16", "int8": "i8", "int64": "i64",
            "uint8": "u8", "uint16": "u16", "uint32": "u32", "uint64": "u64",
            "boolean": "bool", "str": "string"}

TYPE_HELP = ("Types: dbl, sgl, i8, i16, i32, i64, u8, u16, u32, u64, bool, string, path; "
             "arrays by suffix (\"dbl[]\", \"string[][]\"); clusters as "
             "{\"cluster\": [{\"name\": \"a\", \"type\": \"dbl\"}, ...]}.")


@dataclass
class LVType:
    kind: str                     # numeric name, "bool", "string", "path", "array", "cluster"
    elem: "LVType | None" = None  # array element
    dims: int = 0
    fields: "list[tuple[str, LVType]] | None" = None  # cluster elements

    # -- flattened type descriptor
    def td(self, name: str | None = None) -> bytes:
        if self.kind in _NUM:
            code, body = _NUM[self.kind][0], b""
        elif self.kind == "bool":
            code, body = 0x21, b""
        elif self.kind == "string":
            code, body = 0x30, b"\xff\xff\xff\xff"
        elif self.kind == "path":
            code, body = 0x32, b"\xff\xff\xff\xff"
        elif self.kind == "array":
            code = 0x40
            body = struct.pack(">H", self.dims) + b"\xff\xff\xff\xff" * self.dims + self.elem.td()
        elif self.kind == "cluster":
            code = 0x50
            body = struct.pack(">H", len(self.fields)) + b"".join(t.td(n) for n, t in self.fields)
        else:  # pragma: no cover
            raise SpecError(f"unsupported type {self.kind}")
        flags = 0
        if name:
            flags = 0x40
            nb = name.encode("utf-8")
            if len(nb) > 255:
                raise SpecError(f"name too long: {name}")
            body += bytes([len(nb)]) + nb
        size = 4 + len(body)
        if size % 2:
            body += b"\x00"
            size += 1
        return struct.pack(">HBB", size, flags, code) + body

    # -- flattened data
    def data(self, v: Any = None) -> bytes:
        k = self.kind
        if k in _NUM:
            fmt = ">" + _NUM[k][1]
            if v is None:
                v = 0
            try:
                v = float(v) if k in ("sgl", "dbl") else int(v)
                return struct.pack(fmt, v)
            except (struct.error, ValueError, TypeError) as e:
                raise SpecError(f"value {v!r} does not fit type {k}: {e}") from None
        if k == "bool":
            return b"\x01" if v else b"\x00"
        if k == "string":
            b = ("" if v is None else str(v)).encode("utf-8")
            return struct.pack(">i", len(b)) + b
        if k == "path":
            return _flatten_path(v)
        if k == "array":
            v = [] if v is None else v
            shape = _shape(v, self.dims)
            flat = _flatten_nested(v, self.dims)
            return b"".join(struct.pack(">i", n) for n in shape) + b"".join(self.elem.data(x) for x in flat)
        if k == "cluster":
            if v is None:
                v = [None] * len(self.fields)
            if isinstance(v, dict):
                v = [v.get(n) for n, _ in self.fields]
            if len(v) != len(self.fields):
                raise SpecError(f"cluster value needs {len(self.fields)} elements, got {len(v)}")
            return b"".join(t.data(x) for (_, t), x in zip(self.fields, v))
        raise SpecError(f"unsupported type {k}")  # pragma: no cover


def _shape(v, dims):
    shape = []
    cur = v
    for _ in range(dims):
        if not isinstance(cur, (list, tuple)):
            raise SpecError(f"array value must be nested {dims} levels deep")
        shape.append(len(cur))
        cur = cur[0] if cur else []
    return shape


def _flatten_nested(v, dims):
    if dims == 1:
        return list(v)
    out = []
    n = len(v[0]) if v else 0
    for row in v:
        if len(row) != n:
            raise SpecError("2D+ array rows must all have the same length")
        out.extend(_flatten_nested(row, dims - 1))
    return out


def _flatten_path(v) -> bytes:
    if v in (None, ""):
        comps, ptype = [], 1          # empty relative path
    else:
        s = str(v).replace("/", "\\")
        ptype = 0 if re.match(r"^[A-Za-z]:\\|^\\\\", s) else 1
        comps = [c for c in s.split("\\") if c]
        if ptype == 0 and re.match(r"^[A-Za-z]:$", comps[0]):
            comps[0] = comps[0][0]    # LabVIEW stores the drive as "C"
    body = struct.pack(">HH", ptype, len(comps))
    for c in comps:
        b = c.encode("utf-8")
        body += bytes([len(b)]) + b
    return b"PTH0" + struct.pack(">i", len(body)) + body


def parse_type(t: Any) -> LVType:
    if isinstance(t, dict):
        fields = t.get("cluster")
        if not isinstance(fields, list) or not fields:
            raise SpecError("cluster type must be {\"cluster\": [{\"name\":..., \"type\":...}, ...]}")
        return LVType("cluster", fields=[(str(f.get("name", "")), parse_type(f.get("type"))) for f in fields])
    if not isinstance(t, str):
        raise SpecError(f"bad type {t!r}. {TYPE_HELP}")
    s = t.strip().lower().replace(" ", "")
    dims = 0
    while s.endswith("[]"):
        s, dims = s[:-2], dims + 1
    s = _ALIASES.get(s, s)
    if s not in _NUM and s not in ("bool", "string", "path"):
        raise SpecError(f"unknown type {t!r}. {TYPE_HELP}")
    base = LVType(s)
    return LVType("array", elem=base, dims=dims) if dims else base


# ---- spec -> XML -------------------------------------------------------------
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TERM_BAD = re.compile(r"[<>\"&:\x00-\x1f]")


@dataclass
class Script:
    xml: str
    type_strings: list[bytes]
    data_strings: list[bytes]
    labels: dict[str, str]  # node name -> front-panel label for controls/indicators
    defaults: dict[str, Any]  # front-panel label -> default value (set after generation)


def _pos(x, y) -> str:
    return f"({int(x)},{int(y)})"


def build(spec: dict) -> Script:
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    unknown = set(spec) - {"controls", "indicators", "constants", "functions", "subvis", "wires"}
    if unknown:
        raise SpecError(f"unknown spec keys: {sorted(unknown)}")

    names: dict[str, str] = {}
    types: list[tuple[bytes, bytes]] = []

    def add_name(n, kind):
        if not isinstance(n, str) or not _NAME.match(n):
            raise SpecError(f"{kind} name {n!r} must match [A-Za-z_][A-Za-z0-9_]* (it is an id, "
                            "use 'label' for display text)")
        if n in names:
            raise SpecError(f"duplicate name {n!r}")
        names[n] = kind

    def add_type(t, value=None) -> int:
        lt = parse_type(t)
        types.append((lt.td(), lt.data(value)))
        return len(types) - 1

    controls = [dict(c, _ind=False) for c in spec.get("controls", [])] + \
               [dict(c, _ind=True) for c in spec.get("indicators", [])]
    labels = {}
    defaults = {}
    for c in controls:
        add_name(c.get("name"), "indicator" if c["_ind"] else "control")
        labels[c["name"]] = str(c.get("label") or c["name"])
        if c.get("default") is not None:
            if c["_ind"]:
                raise SpecError(f"indicator {c['name']!r} cannot have a default")
            defaults[labels[c["name"]]] = c["default"]
    for c in spec.get("constants", []):
        add_name(c.get("name"), "constant")
    for f in spec.get("functions", []):
        add_name(f.get("name"), "function")
        if not isinstance(f.get("function"), str) or not f["function"].strip() or _TERM_BAD.search(f["function"]):
            raise SpecError(f"function {f['name']!r} needs 'function': the palette name, e.g. \"Add\"")
    for s in spec.get("subvis", []):
        add_name(s.get("name"), "subvi")
        if not isinstance(s.get("path"), str) or not s["path"].lower().endswith((".vi", ".vim")):
            raise SpecError(f"subvi {s['name']!r} needs an absolute 'path' to a .vi")

    if not (spec.get("constants") or spec.get("functions") or spec.get("subvis")):
        # A diagram holding only terminals is saved by LabVIEW as a file it cannot reopen.
        raise SpecError("the diagram needs at least one constant, function or subVI")

    wires = []
    for w in spec.get("wires", []):
        if isinstance(w, dict):
            w = [w.get("from"), w.get("to")]
        if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
            raise SpecError(f"wire {w!r} must be [\"from\", \"to\"]")
        for end in w:
            node, _, term = end.partition(".")
            if node not in names:
                raise SpecError(f"wire end {end!r}: no node named {node!r}")
            kind = names[node]
            if kind in ("function", "subvi") and not term:
                raise SpecError(f"wire end {end!r}: give the terminal, e.g. \"{node}.x\"")
            if kind not in ("function", "subvi") and term:
                raise SpecError(f"wire end {end!r}: {kind}s have one terminal, use just \"{node}\"")
            if term and _TERM_BAD.search(term):
                raise SpecError(f"wire end {end!r}: bad terminal name")
        wires.append(w)

    # -- layout: columns by dataflow depth
    depth = {n: 0 for n in names}
    for c in controls:
        if c["_ind"]:
            depth[c["name"]] = 0
    for _ in range(len(names)):
        changed = False
        for a, b in wires:
            na, nb = a.partition(".")[0], b.partition(".")[0]
            if depth[nb] < depth[na] + 1 and depth[na] + 1 < len(names) + 1:
                depth[nb] = depth[na] + 1
                changed = True
        if not changed:
            break
    maxd = max([depth[n] for n in names if names[n] != "indicator"] + [0])
    for c in controls:
        if c["_ind"]:
            depth[c["name"]] = maxd + 1
    rows: dict[int, int] = {}
    pos = {}
    for n in names:
        d = depth[n]
        r = rows.get(d, 0)
        rows[d] = r + 1
        pos[n] = (30 + d * 150, 30 + r * 70)

    out = ["<Script>"]
    fp_rows = {False: 0, True: 0}
    for c in controls:
        ti = add_type(c.get("type", "dbl"), c.get("default"))
        n = c["name"]
        r = fp_rows[c["_ind"]]
        fp_rows[c["_ind"]] += 1
        fp = (300 if c["_ind"] else 20, 20 + r * 60)
        out.append(f"<Control name={quoteattr(n)} typeindex=\"{ti}\""
                   + (" indicator=\"TRUE\"" if c["_ind"] else "")
                   + f" label={quoteattr(labels[n])} position=\"{_pos(*fp)}\"></Control>")
        out.append(f"<ControlTerminal controlname={quoteattr(n)} position=\"{_pos(*pos[n])}\"></ControlTerminal>")
    for c in spec.get("constants", []):
        ti = add_type(c.get("type", "dbl"), c.get("value"))
        out.append(f"<Constant name={quoteattr(c['name'])} typeindex=\"{ti}\" position=\"{_pos(*pos[c['name']])}\"></Constant>")
    for f in spec.get("functions", []):
        out.append(f"<Function name={quoteattr(f['name'])} style={quoteattr(f['function'].strip())} "
                   f"position=\"{_pos(*pos[f['name']])}\"></Function>")
    for s in spec.get("subvis", []):
        out.append(f"<SubVI name={quoteattr(s['name'])} path={quoteattr(s['path'])} "
                   f"position=\"{_pos(*pos[s['name']])}\"></SubVI>")
    # LabVIEW's scripting resolves a wire from its sink: source = where data goes, destination = driver.
    for a, b in wires:
        out.append(f"<Wire source={quoteattr(b)} destination={quoteattr(a)}></Wire>")
    out.append("</Script>")
    return Script(
        xml="\n".join(out),
        type_strings=[t for t, _ in types],
        data_strings=[d for _, d in types],
        labels=labels,
        defaults=defaults,
    )
