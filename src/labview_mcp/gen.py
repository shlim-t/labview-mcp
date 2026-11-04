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
from pathlib import Path
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

TYPE_HELP = ("Types: dbl, sgl, i8, i16, i32, i64, u8, u16, u32, u64, bool, string, path, error; "
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
            b = _lv_text(v)
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


def _lv_text(v) -> bytes:
    """LabVIEW strings hold bytes in the Windows ANSI code page (e.g. CP949 on Korean Windows), not
    UTF-8. Characters the code page cannot represent become '?'."""
    s = "" if v is None else str(v)
    try:
        return s.encode("mbcs", "replace")
    except LookupError:  # not Windows
        return s.encode("utf-8")


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
        b = _lv_text(c)
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
    if s in ("error", "errorcluster"):
        # Same layout and names as LabVIEW's error cluster, so it wires to error in/out terminals.
        base = LVType("cluster", fields=[("status", LVType("bool")), ("code", LVType("i32")),
                                         ("source", LVType("string"))])
        return LVType("array", elem=base, dims=dims) if dims else base
    if s not in _NUM and s not in ("bool", "string", "path"):
        raise SpecError(f"unknown type {t!r}. {TYPE_HELP}")
    base = LVType(s)
    return LVType("array", elem=base, dims=dims) if dims else base




# ---- spec -> XML -------------------------------------------------------------
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# ":" in a terminal reference has crashed LabVIEW; everything else is XML-escaped.
_TERM_BAD = re.compile(r"[:\x00-\x1f]")
_CONTENT_KEYS = ("constants", "functions", "subvis", "structures")
_STRUCT_TYPES = {"for_loop": "for", "for": "for", "while_loop": "while", "while": "while",
                 "case": "case", "case_structure": "case"}
# Friendly names for structure terminals -> LabVIEW's names.
_STRUCT_TERMS = {
    "for": {"N": "Loop Count", "count": "Loop Count", "i": "Loop Counter", "iteration": "Loop Counter",
            "Loop Count": "Loop Count", "Loop Counter": "Loop Counter"},
    "while": {"i": "Loop Counter", "iteration": "Loop Counter", "stop": "Loop End Ref",
              "condition": "Loop End Ref", "Loop Counter": "Loop Counter", "Loop End Ref": "Loop End Ref"},
    "case": {"selector": "Case Selector", "Case Selector": "Case Selector"},
}
_LEAF_SIZE = {"constant": (70, 25), "function": (40, 40), "subvi": (40, 40),
              "control": (50, 30), "indicator": (50, 30)}
PAD_X, PAD_TOP, PAD_BOTTOM, GAP_X, GAP_Y = 50, 40, 50, 50, 30

STRUCTURE_HELP = (
    "structures: {\"name\": \"L\", \"type\": \"for_loop\" | \"while_loop\" | \"case\", ...contents}. "
    "Loops hold constants/functions/subvis/structures and optional \"shift_registers\": [\"sr\"]; "
    "while loops take \"stop_if_true\" (default true). A case holds \"cases\": [{\"value\": true | 3 | "
    "\"apple\", \"default\": false, ...contents}] and optional \"output_tunnels\": [\"t\"].")


@dataclass
class Script:
    xml: str
    type_strings: list[bytes]
    data_strings: list[bytes]
    labels: dict[str, str]  # node name -> front-panel label for controls/indicators
    defaults: dict[str, Any]  # front-panel label -> default value (set after generation)
    connector: dict[str, str]  # front-panel label -> connector pane slot
    wires: list = None  # (driver, sink) pairs in LabVIEW terminal syntax


@dataclass
class _Node:
    name: str
    kind: str        # control indicator constant function subvi for while case shiftreg tunnel
    container: str   # id of the diagram/body/frame holding it ("" = top-level diagram)
    spec: dict
    owner: str = ""  # structure a shift register / tunnel belongs to
    bind: int | None = None  # index into ObjRefsIn for an object that already exists (vi_edit)


def _pos(x, y) -> str:
    return f"({int(x)},{int(y)})"


class _Builder:
    def __init__(self):
        self.nodes: dict[str, _Node] = {}
        self.owner_of: dict[str, str] = {"": ""}        # container id -> owning structure name
        self.children: dict[str, list[str]] = {"": []}  # container id -> node names, spec order
        self.types: list[tuple[bytes, bytes]] = []

    # -- collection
    def add(self, name, kind, container, spec, owner=""):
        if not isinstance(name, str) or not _NAME.match(name):
            raise SpecError(f"{kind} name {name!r} must match [A-Za-z_][A-Za-z0-9_]* (it is an id, "
                            "use 'label' for display text)")
        if name in self.nodes:
            raise SpecError(f"duplicate name {name!r}")
        self.nodes[name] = _Node(name, kind, container, spec, owner)
        if kind not in ("shiftreg", "tunnel", "existing"):
            self.children[container].append(name)

    def add_existing(self, name: str, kind: str, bind: int, extra: dict):
        """An object already on the diagram, bound to ObjRefsIn[bind]. kind: "existing" (a front-panel
        terminal), constant, function, subvi, for, while or case. Loops may get new contents; cases
        get new contents per frame index via {"cases": {"1": {...contents}}}."""
        self.add(name, kind, "", extra or {})
        n = self.nodes[name]
        n.bind = bind
        self.children[""].remove(name) if name in self.children[""] else None
        extra = extra or {}
        if kind in ("for", "while"):
            self.owner_of[name] = name
            self.children[name] = []
            self.collect(extra, name)
        elif kind == "case":
            frames = extra.get("cases") or {}
            if not isinstance(frames, dict):
                raise SpecError(f"existing case {name!r}: \"cases\" maps a frame index to contents, "
                                "e.g. {\"1\": {\"functions\": [...]}}")
            n.spec["_frames"] = []
            for k, contents in frames.items():
                try:
                    idx = int(k)
                except ValueError:
                    raise SpecError(f"existing case {name!r}: frame index {k!r} is not a number") from None
                fid = f"{name}#{idx}"
                self.owner_of[fid] = name
                self.children[fid] = []
                n.spec["_frames"].append(idx)
                self.collect(contents or {}, fid)
        elif any(k in extra for k in _CONTENT_KEYS):
            raise SpecError(f"existing {kind} {name!r} cannot hold new contents")

    def collect(self, d: dict, container: str):
        for c in d.get("constants", []) or []:
            self.add(c.get("name"), "constant", container, c)
        for f in d.get("functions", []) or []:
            self.add(f.get("name"), "function", container, f)
            fn = f.get("function")
            if not isinstance(fn, str) or not fn.strip() or _TERM_BAD.search(fn):
                raise SpecError(f"function {f.get('name')!r} needs 'function': the palette name, e.g. \"Add\"")
            size = f.get("size", f.get("inputs"))
            if size is not None and not (isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 100):
                raise SpecError(f"function {f.get('name')!r}: \"size\" must be an integer 1..100")
        for s in d.get("subvis", []) or []:
            self.add(s.get("name"), "subvi", container, s)
            path = s.get("path")
            if not isinstance(path, str) or not path.lower().endswith((".vi", ".vim")):
                raise SpecError(f"subvi {s.get('name')!r} needs an absolute 'path' to a .vi")
            pp = Path(path)
            if not pp.is_absolute():
                raise SpecError(f"subvi {s.get('name')!r}: path must be absolute: {path}")
            # Inside an .llb the file itself is the library; otherwise the VI file must exist,
            # or LabVIEW opens a modal "Find the VI" dialog and blocks.
            llb = next((q for q in pp.parents if q.suffix.lower() == ".llb"), None)
            if not (pp.is_file() or (llb is not None and llb.is_file())):
                raise SpecError(f"subvi {s.get('name')!r}: file not found: {path}")
            s["_path"] = str(pp)
        for st in d.get("structures", []) or []:
            self.collect_structure(st, container)

    def collect_structure(self, st: dict, container: str):
        if not isinstance(st, dict):
            raise SpecError(f"bad structure {st!r}. {STRUCTURE_HELP}")
        kind = _STRUCT_TYPES.get(str(st.get("type", "")).lower())
        name = st.get("name")
        if not kind:
            raise SpecError(f"structure {name!r}: unknown type {st.get('type')!r}. {STRUCTURE_HELP}")
        self.add(name, kind, container, st)
        if kind in ("for", "while"):
            allowed = {"name", "type", "shift_registers", "stop_if_true", *_CONTENT_KEYS}
            self.owner_of[name] = name
            self.children[name] = []
            self.collect(st, name)
            for sr in st.get("shift_registers", []) or []:
                self.add(sr, "shiftreg", container, {}, owner=name)
        else:
            allowed = {"name", "type", "cases", "output_tunnels"}
            cases = st.get("cases")
            if not isinstance(cases, list) or not cases:
                raise SpecError(f"case {name!r} needs \"cases\": [{{\"value\": ..., ...contents}}, ...]")
            for i, fr in enumerate(cases):
                if not isinstance(fr, dict):
                    raise SpecError(f"case {name!r}: each entry of cases must be an object")
                extra = set(fr) - {"value", "default", *_CONTENT_KEYS}
                if extra:
                    raise SpecError(f"case {name!r} frame {i}: unknown keys {sorted(extra)}")
                if fr.get("value") is None and not fr.get("default"):
                    raise SpecError(f"case {name!r} frame {i}: give a \"value\" or \"default\": true")
                fid = f"{name}#{i}"
                self.owner_of[fid] = name
                self.children[fid] = []
                self.collect(fr, fid)
            for t in st.get("output_tunnels", []) or []:
                self.add(t, "tunnel", container, {}, owner=name)
        extra = set(st) - allowed
        if extra:
            raise SpecError(f"structure {name!r}: unknown keys {sorted(extra)}. {STRUCTURE_HELP}")

    # -- wires
    def resolve(self, end: str) -> str:
        node, _, term = end.partition(".")
        n = self.nodes.get(node)
        if n is None:
            raise SpecError(f"wire end {end!r}: no node named {node!r}")
        if term and _TERM_BAD.search(term):
            raise SpecError(f"wire end {end!r}: bad terminal name")
        k = n.kind
        if k in ("function", "subvi"):
            if not term:
                raise SpecError(f"wire end {end!r}: give the terminal, e.g. \"{node}.x\"")
            if k == "function" and term.startswith("element") and not re.fullmatch(r"element\[\d+\]", term):
                # a bare "element" reference has crashed LabVIEW
                raise SpecError(f"wire end {end!r}: wire growable inputs by position as "
                                f"\"{node}.element[1]\", \"{node}.element[2]\", ...")
            return end
        if k in _STRUCT_TERMS:
            lv = _STRUCT_TERMS[k].get(term)
            if not lv:
                raise SpecError(f"wire end {end!r}: a {k} structure has terminals "
                                f"{sorted(t for t in _STRUCT_TERMS[k] if ' ' not in t)}. To pass data "
                                "in or out, wire the inner node directly; LabVIEW adds the tunnel")
            return f"{node}.{lv}"
        if k == "shiftreg":
            side = {"left": "Left", "right": "Right"}.get(term.lower())
            if not side:
                raise SpecError(f"wire end {end!r}: wire shift registers as \"{node}.left\" (the value "
                                f"from the previous iteration) or \"{node}.right\" (the value for the next)")
            return f"{node}.{side}"
        if term:
            raise SpecError(f"wire end {end!r}: {k}s have one terminal, use just \"{node}\"")
        return node

    # -- layout
    def item_in(self, name: str, container: str) -> str | None:
        """The direct child of `container` that is or holds node `name`."""
        n = self.nodes[name]
        x = n.owner if n.kind in ("shiftreg", "tunnel") else name
        c = self.nodes[x].container
        while c != container:
            if c == "":
                return None
            x = self.owner_of[c]
            c = self.nodes[x].container
        return x

    def layout(self, container: str, wires):
        items = self.children[container]
        depth = {n: 0 for n in items}
        edges = set()
        for a, z in wires:
            ia = self.item_in(a.partition(".")[0], container)
            iz = self.item_in(z.partition(".")[0], container)
            if ia in depth and iz in depth and ia != iz:
                edges.add((ia, iz))
        for _ in range(len(items)):
            changed = False
            for ia, iz in edges:
                if depth[iz] < depth[ia] + 1 <= len(items):
                    depth[iz], changed = depth[ia] + 1, True
            if not changed:
                break
        if container == "":
            for n in items:
                k = self.nodes[n].kind
                if k == "control":
                    depth[n] = 0
                elif k != "indicator":
                    depth[n] += 1
            last = max([depth[n] for n in items if self.nodes[n].kind != "indicator"] + [0]) + 1
            for n in items:
                if self.nodes[n].kind == "indicator":
                    depth[n] = last
        sizes = {n: self.size(n, wires) for n in items}
        cols: dict[int, list[str]] = {}
        for n in items:
            cols.setdefault(depth[n], []).append(n)
        rel, x, total_h = {}, 0, 0
        for d in sorted(cols):
            y = 0
            for n in cols[d]:
                rel[n] = (x, y)
                y += sizes[n][1] + GAP_Y
            total_h = max(total_h, y - GAP_Y)
            x += max(sizes[n][0] for n in cols[d]) + GAP_X
        return max(x - GAP_X, 0), total_h, rel

    def bodies(self, name: str) -> list[str]:
        n = self.nodes[name]
        if n.kind in ("for", "while"):
            return [name]
        if n.bind is not None:
            return [f"{name}#{i}" for i in n.spec.get("_frames", [])]
        return [f"{name}#{i}" for i in range(len(n.spec["cases"]))]

    _BIND_TAG = {"existing": "Control", "constant": "Constant", "function": "Function",
                 "subvi": "SubVI", "for": "Structure", "while": "Structure", "case": "MultiframeStructure"}

    def emit_bindings(self, pos, wires, out: list):
        """Name the existing objects (ObjRefsIn order) and add new contents inside existing structures."""
        for n in sorted((m for m in self.nodes.values() if m.bind is not None), key=lambda m: m.bind):
            tag = self._BIND_TAG[n.kind]
            out.append(f"<{tag} name={quoteattr(n.name)} in=\"{n.bind}\">")
            if n.kind in ("for", "while"):
                self.emit(n.name, pos, wires, out)
            elif n.kind == "case":
                for i in n.spec.get("_frames", []):
                    out.append(f"<Frame name={quoteattr(f'{n.name}_frame{i}')} index=\"{i}\">")
                    self.emit(f"{n.name}#{i}", pos, wires, out)
                    out.append("</Frame>")
            out.append(f"</{tag}>")

    def size(self, name: str, wires) -> tuple[int, int]:
        n = self.nodes[name]
        if n.kind in _LEAF_SIZE:
            return _LEAF_SIZE[n.kind]
        w = h = 0
        for b in self.bodies(name):
            bw, bh, _ = self.layout(b, wires)
            w, h = max(w, bw), max(h, bh)
        side = sum(1 for m in self.nodes.values() if m.owner == name)
        return (max(w + 2 * PAD_X, 160),
                max(h + PAD_TOP + PAD_BOTTOM, 110, PAD_TOP + 30 * side + PAD_BOTTOM))

    def place(self, container: str, origin, wires, out: dict):
        """Rough positions; the helper VI runs LabVIEW's Clean Up Diagram afterwards."""
        _, _, rel = self.layout(container, wires)
        for n, (rx, ry) in rel.items():
            ax, ay = origin[0] + rx, origin[1] + ry
            out[n] = (ax, ay)
            if self.nodes[n].kind in ("for", "while", "case"):
                # A loop's contents are positioned relative to the loop; case frames share the
                # coordinates of the diagram that holds the case.
                inner = (PAD_X, PAD_TOP) if self.nodes[n].kind in ("for", "while") else (ax + PAD_X, ay + PAD_TOP)
                for b in self.bodies(n):
                    self.place(b, inner, wires, out)

    # -- emit
    def add_type(self, t, value=None) -> int:
        lt = parse_type(t)
        self.types.append((lt.td(), lt.data(value)))
        return len(self.types) - 1

    def emit(self, container: str, pos, wires, out: list):
        for name in self.children[container]:
            n = self.nodes[name]
            p = _pos(*pos[name])
            if n.kind == "constant":
                ti = self.add_type(n.spec.get("type", "dbl"), n.spec.get("value"))
                out.append(f"<Constant name={quoteattr(name)} typeindex=\"{ti}\" position=\"{p}\"></Constant>")
            elif n.kind == "function":
                style = n.spec["function"].strip()
                size = n.spec.get("size", n.spec.get("inputs"))
                if size is None:
                    out.append(f"<Function name={quoteattr(name)} style={quoteattr(style)} position=\"{p}\"></Function>")
                else:
                    tag = "BuildArray" if style.lower() == "build array" else "GrowableFunction"
                    out.append(f"<{tag} name={quoteattr(name)} style={quoteattr(style)} size=\"{int(size)}\" "
                               f"position=\"{p}\"></{tag}>")
            elif n.kind == "subvi":
                out.append(f"<SubVI name={quoteattr(name)} path={quoteattr(n.spec['_path'])} ignoreSearchDialog=\"TRUE\" "
                           f"position=\"{p}\"></SubVI>")
            elif n.kind in ("for", "while", "case"):
                w, h = self.size(name, wires)
                side = [m for m in self.nodes.values() if m.owner == name]
                if n.kind == "case":
                    out.append(f"<MultiframeStructure name={quoteattr(name)} style=\"Case Structure\" "
                               f"position=\"{p}\" width=\"{w}\" height=\"{h}\">")
                    for i, fr in enumerate(n.spec["cases"]):
                        val = fr.get("value")
                        if isinstance(val, bool):
                            val = "True" if val else "False"
                        val = "Default" if val is None else str(val)
                        out.append(f"<Frame name={quoteattr(f'{name}_frame{i}')} value={quoteattr(val)} "
                                   f"index=\"{i}\"" + (" default=\"TRUE\"" if fr.get("default") else "") + ">")
                        self.emit(f"{name}#{i}", pos, wires, out)
                        out.append("</Frame>")
                    for k, t in enumerate(side):
                        out.append(f"<Tunnel name={quoteattr(t.name)} location=\"right\" "
                                   f"offset=\"{PAD_TOP + 30 * k}\"></Tunnel>")
                    out.append("</MultiframeStructure>")
                else:
                    style = "For Loop" if n.kind == "for" else "While Loop"
                    cond = ""
                    if n.kind == "while":
                        cond = " loopCondition=\"" + ("Stop if True" if n.spec.get("stop_if_true", True)
                                                     else "Continue if True") + "\""
                    out.append(f"<Structure name={quoteattr(name)} style=\"{style}\" position=\"{p}\" "
                               f"width=\"{w}\" height=\"{h}\"{cond}>")
                    self.emit(name, pos, wires, out)
                    for k, sr in enumerate(side):
                        out.append(f"<ShiftRegister name={quoteattr(sr.name)} location=\"left\" "
                                   f"offset=\"{PAD_TOP + 30 * k}\"></ShiftRegister>")
                    out.append("</Structure>")


# Connector pane: the helper's blank VI uses the 4x2x2x4 pattern. conpaneidx numbers its terminals
# counter-clockwise from the bottom right (measured):
#          top0=7  top1=5
#   left0=11                right0=3
#   left1=10                right1=2
#   left2=9                 right2=1
#   left3=8                 right3=0
#          bottom0=6 bottom1=4
CONPANE_SLOTS = {"left0": 11, "left1": 10, "left2": 9, "left3": 8, "top0": 7, "top1": 5,
                 "bottom0": 6, "bottom1": 4, "right0": 3, "right1": 2, "right2": 1, "right3": 0}
_AUTO_ORDER = {False: ["left0", "left1", "left2", "top0", "top1", "left3"],
               True: ["right0", "right1", "right2", "bottom0", "bottom1", "right3"]}


def _fp_height(t: LVType) -> int:
    """Rough height of a front-panel control of this type, used to stack controls."""
    if t.kind == "cluster":
        return 30 + sum(_fp_height(f) for _, f in t.fields)
    if t.kind == "array":
        return _fp_height(t.elem) + 10
    return 45


def _assign_connector(controls: list[dict], default="auto") -> dict[str, str]:
    """Front-panel name -> slot. Controls fill the left side then the top, indicators the right side
    then the bottom, in spec order; "connector": "left3" pins a slot, false leaves it unwired."""
    taken: dict[str, str] = {}
    controls = [dict(c, connector=c.get("connector", default)) for c in controls]
    for c in controls:
        slot = c.get("connector", "auto")
        if slot in (False, None, "none"):
            continue
        if slot != "auto":
            if slot not in CONPANE_SLOTS:
                raise SpecError(f"{c['name']!r}: connector must be one of {sorted(CONPANE_SLOTS)}, "
                                "\"auto\" or false")
            if slot in taken.values():
                raise SpecError(f"{c['name']!r}: connector slot {slot!r} is used twice")
            taken[c["name"]] = slot
    for c in controls:  # error clusters go to the bottom corners, as in NI's VIs
        if c.get("connector", "auto") == "auto" and str(c.get("type", "")).lower() in ("error", "errorcluster"):
            slot = "right3" if c["_ind"] else "left3"
            if slot not in taken.values():
                taken[c["name"]] = slot
    for c in controls:
        if c.get("connector", "auto") != "auto" or c["name"] in taken:
            continue
        free = [s for s in _AUTO_ORDER[c["_ind"]] if s not in taken.values()]
        if not free:
            free = [s for s in CONPANE_SLOTS if s not in taken.values()]
        if not free:
            raise SpecError("more than 12 controls and indicators; set \"connector\": false on some")
        taken[c["name"]] = free[0]
    return taken


def build(spec: dict, existing: list | None = None, origin=(30, 30), fp_origin=(30, 30),
          edit: bool = False) -> Script:
    """existing: objects already on the diagram that the spec refers to, in ObjRefsIn order; each a
    name (a front-panel terminal) or {"name", "kind", "spec"} (see _Builder.add_existing). Used by
    vi_edit. New controls then stay off the connector pane unless they ask for a slot."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    unknown = set(spec) - {"controls", "indicators", "wires", "existing", *_CONTENT_KEYS}
    if unknown:
        raise SpecError(f"unknown spec keys: {sorted(unknown)}")
    b = _Builder()
    existing = list(existing or [])
    for i, e in enumerate(existing):
        if isinstance(e, str):
            e = {"name": e, "kind": "existing"}
        b.add_existing(e["name"], e.get("kind", "existing"), i, e.get("spec") or {})
    controls = [dict(c, _ind=False) for c in spec.get("controls", []) or []] + \
               [dict(c, _ind=True) for c in spec.get("indicators", []) or []]
    labels, defaults = {}, {}
    for c in controls:
        b.add(c.get("name"), "indicator" if c["_ind"] else "control", "", c)
        labels[c["name"]] = str(c.get("label") or c["name"])
        if c.get("default") is not None:
            if c["_ind"]:
                raise SpecError(f"indicator {c['name']!r} cannot have a default")
            defaults[labels[c["name"]]] = c["default"]
    b.collect(spec, "")
    if not edit and not existing and not any(n.kind not in ("control", "indicator") for n in b.nodes.values()):
        # A diagram holding only terminals is saved by LabVIEW as a file it cannot reopen.
        raise SpecError("the diagram needs at least one constant, function, subVI or structure")

    wires = []
    for w in spec.get("wires", []) or []:
        if isinstance(w, dict):
            w = [w.get("from"), w.get("to")]
        if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
            raise SpecError(f"wire {w!r} must be [\"from\", \"to\"]")
        wires.append((b.resolve(w[0]), b.resolve(w[1])))

    pos: dict[str, tuple[int, int]] = {}
    b.place("", origin, wires, pos)
    for n in b.nodes.values():  # new contents of existing structures, relative to the structure
        if n.bind is not None and n.kind in ("for", "while", "case"):
            for body in b.bodies(n.name):
                b.place(body, (PAD_X, PAD_TOP + 60), wires, pos)

    conpane = _assign_connector(controls, default=False if existing else "auto")
    out = ["<Script>"]
    b.emit_bindings(pos, wires, out)
    fp_y = {False: fp_origin[1], True: fp_origin[1]}  # labels sit above the control
    for c in controls:
        ti = b.add_type(c.get("type", "dbl"), c.get("default"))
        n = c["name"]
        fp = (fp_origin[0] + (320 if c["_ind"] else 0), fp_y[c["_ind"]])
        fp_y[c["_ind"]] += _fp_height(parse_type(c.get("type", "dbl"))) + 25
        out.append(f"<Control name={quoteattr(n)} typeindex=\"{ti}\""
                   + (" indicator=\"TRUE\"" if c["_ind"] else "")
                   + f" label={quoteattr(labels[n])}"
                   + (f" conpaneidx=\"{CONPANE_SLOTS[conpane[n]]}\"" if n in conpane else "")
                   + f" position=\"{_pos(*fp)}\"></Control>")
        out.append(f"<ControlTerminal controlname={quoteattr(n)} position=\"{_pos(*pos[n])}\"></ControlTerminal>")
    b.emit("", pos, wires, out)
    # LabVIEW's scripting resolves a wire from its sink: source = where data goes, destination = driver.
    for a, z in wires:
        out.append(f"<Wire source={quoteattr(z)} destination={quoteattr(a)}></Wire>")
    out.append("</Script>")
    return Script(
        xml="\n".join(out),
        type_strings=[t for t, _ in b.types],
        data_strings=[d for _, d in b.types],
        labels=labels,
        defaults=defaults,
        connector={labels[n]: slot for n, slot in conpane.items()},
        wires=wires,
    )
