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

import json
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


# VI Server class name -> class number in refnum type descriptors (measured on LabVIEW 2026)
VI_CLASSES: dict[str, int] = json.loads(
    (Path(__file__).with_name("vi_classes.json")).read_text(encoding="utf-8"))["classes"]


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
             "enums as {\"enum\": [\"Init\", \"Run\", \"Stop\"]} (values are item names); "
             "arrays by suffix (\"dbl[]\", \"string[][]\"); clusters as "
             "{\"cluster\": [{\"name\": \"a\", \"type\": \"dbl\"}, ...]}.")


@dataclass
class LVType:
    kind: str                     # numeric name, "bool", "string", "path", "array", "cluster"
    elem: "LVType | None" = None  # array element
    dims: int = 0
    fields: "list[tuple[str, LVType]] | None" = None  # cluster elements
    items: "list[str] | None" = None  # enum item names

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
        elif self.kind == "enum":  # U16 enum: item count, then the item names as Pascal strings
            code = 0x16
            names = [_lv_text(x) for x in self.items]
            body = struct.pack(">H", len(names)) + b"".join(bytes([len(b)]) + b for b in names)
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
        if k == "enum":
            return struct.pack(">H", self.enum_index(v))
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

    def enum_index(self, v) -> int:
        if v is None:
            return 0
        if isinstance(v, str):
            if v not in self.items:
                raise SpecError(f"{v!r} is not an item of the enum {self.items}")
            return self.items.index(v)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(self.items):
            return v
        raise SpecError(f"enum value {v!r}: give an item name from {self.items}")


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
    if isinstance(t, dict) and "enum" in t:
        items = t["enum"]
        if (not isinstance(items, list) or not items or len(items) > 1000
                or not all(isinstance(x, str) and x and len(_lv_text(x)) < 256 for x in items)
                or len(set(items)) != len(items)):
            raise SpecError("enum type must be {\"enum\": [\"Init\", \"Run\", \"Stop\"]}: distinct, non-empty names")
        return LVType("enum", items=list(items))
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
# ":" in a name has crashed LabVIEW; everything else is XML-escaped.
_TERM_BAD = re.compile(r"[:\x00-\x1f]")
# In a terminal name a colon is fine ("s? t:f", Select's output) except as "name:<number>", which
# LabVIEW's script parser takes as a terminal index and has crashed on.
_TERM_NAME_BAD = re.compile(r"[\x00-\x1f]|:\s*\d")
_CONTENT_KEYS = ("constants", "functions", "subvis", "structures", "property_nodes", "invoke_nodes")
_NODE_KINDS = ("pnode", "inode")
# growable palette functions: their inputs are wired by position, element[i]
_GROWABLE_FUNCS = {"Concatenate Strings", "Build Array", "Bundle", "Unbundle", "Build Cluster Array",
                   "Index Array", "Array Subset", "Replace Array Subset", "Insert Into Array",
                   "Delete From Array", "Array To Cluster", "Merge Errors", "Compound Arithmetic"}  # Property / Invoke Nodes: configured after they are placed
_STRUCT_TYPES = {"for_loop": "for", "for": "for", "while_loop": "while", "while": "while",
                 "case": "case", "case_structure": "case", "event": "event", "event_structure": "event"}
# Control events: the value of the "Event Type" enum taken by EventStructure.SetHandledEvents
# (measured on LabVIEW 2026 by registering each value and reading the frame's caption).
CONTROL_EVENTS = {
    "Value Change": 67, "Mouse Down": 38, "Mouse Down?": 39, "Mouse Up": 43, "Mouse Enter": 40,
    "Mouse Leave": 41, "Mouse Move": 42, "Mouse Wheel": 44, "Key Down": 28, "Key Down?": 29,
    "Key Repeat": 30, "Key Repeat?": 31, "Key Up": 32, "Drag Ended": 19, "Drag Enter": 20,
    "Drag Leave": 21, "Drag Over": 22, "Drag Source Update": 23, "Drag Starting": 24,
    "Drag Starting?": 25, "Drop": 26, "Shortcut Menu Activation?": 52,
    "Shortcut Menu Selection (App)": 53, "Shortcut Menu Selection? (App)": 54,
    "Shortcut Menu Selection (User)": 55,
}
# Friendly names for structure terminals -> LabVIEW's names.
_STRUCT_TERMS = {
    "for": {"N": "Loop Count", "count": "Loop Count", "i": "Loop Counter", "iteration": "Loop Counter",
            "Loop Count": "Loop Count", "Loop Counter": "Loop Counter"},
    "while": {"i": "Loop Counter", "iteration": "Loop Counter", "stop": "Loop End Ref",
              "condition": "Loop End Ref", "Loop Counter": "Loop Counter", "Loop End Ref": "Loop End Ref"},
    "case": {"selector": "Case Selector", "Case Selector": "Case Selector"},
}
_LEAF_SIZE = {"constant": (70, 25), "function": (40, 40), "subvi": (40, 40), "pnode": (110, 60), "inode": (110, 60),
              "control": (50, 30), "indicator": (50, 30)}
PAD_X, PAD_TOP, PAD_BOTTOM, GAP_X, GAP_Y = 50, 40, 50, 50, 30

STRUCTURE_HELP = (
    "structures: {\"name\": \"L\", \"type\": \"for_loop\" | \"while_loop\" | \"case\", ...contents}. "
    "Loops hold constants/functions/subvis/structures and optional \"shift_registers\": [\"sr\"]; "
    "while loops take \"stop_if_true\" (default true). A case holds \"cases\": [{\"value\": true | 3 | "
    "\"apple\", \"default\": false, ...contents}] and optional \"output_tunnels\": [\"t\"]. An event "
    "structure holds \"events\": [{\"control\": \"stop\", \"event\": \"Value Change\", ...contents}], one "
    "frame per entry; \"control\" is the name of a control or indicator in the spec.")


NODE_HELP = (
    "property_nodes: {\"name\": \"p\", \"control\": \"level\", \"properties\": [\"Visible\"], \"write\": true} "
    "(linked to a front-panel control) or {\"name\": \"p\", \"class\": \"VI\", \"properties\": [...]} (wire a "
    "reference to \"p.reference\"; class \"Application\" needs none). \"write\": true writes every property, "
    "a list writes those; the rest are read. invoke_nodes: {\"name\": \"m\", \"control\": \"graph\" | "
    "\"class\": \"VI\", \"method\": \"Run VI\"}. Wire them as \"p.Visible\", \"m.<parameter>\", "
    "\"p.reference\", \"p.error in\", \"p.error out\". Names are LabVIEW's short names (\"Visible\", "
    "\"Value\", \"App.Version\").")


def _check_node(pn: dict, kind: str, b) -> None:
    name = pn.get("name")
    extra = set(pn) - {"name", "control", "class", "properties", "write", "method"}
    if extra:
        raise SpecError(f"{name!r}: unknown keys {sorted(extra)}. {NODE_HELP}")
    if bool(pn.get("control")) == bool(pn.get("class")):
        raise SpecError(f"{name!r}: give either \"control\" (a front-panel object) or \"class\". {NODE_HELP}")
    for k in ("control", "class", "method"):
        if pn.get(k) is not None and (not isinstance(pn[k], str) or _TERM_BAD.search(pn[k])):
            raise SpecError(f"{name!r}: bad {k} {pn[k]!r}")
    if pn.get("class") and pn["class"] not in VI_CLASSES:
        import difflib
        near = difflib.get_close_matches(pn["class"], list(VI_CLASSES), 6, 0.5)
        raise SpecError(f"{name!r}: no VI Server class {pn['class']!r}. Close: {near}")
    if kind == "pnode":
        props = pn.get("properties")
        if not isinstance(props, list) or not props or not all(isinstance(x, str) and x for x in props):
            raise SpecError(f"{name!r}: \"properties\" is a list of property names. {NODE_HELP}")
        w = pn.get("write", False)
        if not (isinstance(w, bool) or (isinstance(w, list) and set(w) <= set(props))):
            raise SpecError(f"{name!r}: \"write\" is true, false or a list of its properties")
    elif not isinstance(pn.get("method"), str):
        raise SpecError(f"{name!r}: invoke nodes need \"method\". {NODE_HELP}")


@dataclass
class Script:
    xml: str
    type_strings: list[bytes]
    data_strings: list[bytes]
    labels: dict[str, str]  # node name -> front-panel label for controls/indicators
    defaults: dict[str, Any]  # front-panel label -> default value (set after generation)
    connector: dict[str, str]  # front-panel label -> connector pane slot
    wires: list = None  # (driver, sink) pairs in LabVIEW terminal syntax
    styles: dict = None  # front-panel label -> palette name to replace the control with
    outs: list = None    # node names whose references the script returns (Obj Refs Out[] order)
    # Event structures, in creation order: {"name", "out": index into outs, "frames": [{"control":
    # label, "event": code, "move": [labels of terminals to move in], "bind": [outer names],
    # "script": Script for the frame contents}]}
    events: list = None
    # Property/Invoke Nodes made by this script: {"name", "out", "kind", "class", "items", "write": [index],
    # "control": spec name, "method"}; and the wires to them, drawn after they are configured: `late`
    # (a Script binding its ends) with `late_refs` = ("out", k) | ("in", k) for each bound object.
    pnodes: list = None
    late: "Script | None" = None
    late_refs: list = None

    def has_nodes(self) -> bool:
        return bool(self.pnodes) or any(fr["script"] is not None and fr["script"].has_nodes()
                                        for ev in self.events or [] for fr in ev["frames"])


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
        self.out_index: dict[str, int] = {}  # new objects whose references the script returns
        self.event_order: list[str] = []     # event structures in emit (= creation) order
        self.extra_wires: list = []           # wires the builder adds itself (case "pass")

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
        for kind, key in (("pnode", "property_nodes"), ("inode", "invoke_nodes")):
            for pn in d.get(key, []) or []:
                if not isinstance(pn, dict):
                    raise SpecError(f"{key}: each entry must be an object. {NODE_HELP}")
                self.add(pn.get("name"), kind, container, pn)
                _check_node(pn, kind, self)
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
        elif kind == "event":
            allowed = {"name", "type", "events"}
            events = st.get("events")
            if not isinstance(events, list) or not events:
                raise SpecError(f"event structure {name!r} needs \"events\": [{{\"control\": \"stop\", "
                                "\"event\": \"Value Change\", ...contents}, ...]")
            for i, fr in enumerate(events):
                if not isinstance(fr, dict):
                    raise SpecError(f"event structure {name!r}: each entry of events must be an object")
                extra = set(fr) - {"control", "event", *_CONTENT_KEYS}
                if extra:
                    raise SpecError(f"event structure {name!r} event {i}: unknown keys {sorted(extra)}")
                src = self.nodes.get(fr.get("control"))
                if src is None or src.kind not in ("control", "indicator"):
                    raise SpecError(f"event structure {name!r} event {i}: \"control\" must name a control "
                                    "or indicator of this spec")
                ev = fr.get("event", "Value Change")
                if ev not in CONTROL_EVENTS:
                    raise SpecError(f"event structure {name!r} event {i}: unknown event {ev!r}. Events: "
                                    f"{sorted(CONTROL_EVENTS)}")
                fid = f"{name}#{i}"
                self.owner_of[fid] = name
                self.children[fid] = []
                self.collect(fr, fid)
        else:
            allowed = {"name", "type", "cases", "output_tunnels"}
            cases = st.get("cases")
            if not isinstance(cases, list) or not cases:
                raise SpecError(f"case {name!r} needs \"cases\": [{{\"value\": ..., ...contents}}, ...]")
            for i, fr in enumerate(cases):
                if not isinstance(fr, dict):
                    raise SpecError(f"case {name!r}: each entry of cases must be an object")
                extra = set(fr) - {"value", "default", "pass", *_CONTENT_KEYS}
                if extra:
                    raise SpecError(f"case {name!r} frame {i}: unknown keys {sorted(extra)}")
                if fr.get("value") is None and not fr.get("default"):
                    raise SpecError(f"case {name!r} frame {i}: give a \"value\" or \"default\": true")
                fid = f"{name}#{i}"
                self.owner_of[fid] = name
                self.children[fid] = []
                self.collect(fr, fid)
            tunnels = list(st.get("output_tunnels", []) or [])
            for t in tunnels:
                self.add(t, "tunnel", container, {}, owner=name)
            # "pass": [[source, tunnel]] passes a value through this frame unchanged. A wire from
            # outside straight to an output tunnel would land on the tunnel's outer side, so the
            # value goes through an Always Copy node in the frame.
            for i, fr in enumerate(cases):
                for k, pw in enumerate(fr.get("pass") or []):
                    if not (isinstance(pw, (list, tuple)) and len(pw) == 2 and pw[1] in tunnels
                            and isinstance(pw[0], str)):
                        raise SpecError(f"case {name!r} frame {i}: \"pass\" is a list of [source, output "
                                        f"tunnel] pairs; output tunnels: {tunnels}")
                    cp = f"_pass_{name}_{i}_{k}"
                    self.add(cp, "function", f"{name}#{i}", {"name": cp, "function": "Always Copy"})
                    self.extra_wires += [[pw[0], f"{cp}.Input"], [f"{cp}.Output", pw[1]]]
        extra = set(st) - allowed
        if extra:
            raise SpecError(f"structure {name!r}: unknown keys {sorted(extra)}. {STRUCTURE_HELP}")

    # -- wires
    def resolve(self, end: str) -> str:
        node, _, term = end.partition(".")
        n = self.nodes.get(node)
        if n is None:
            raise SpecError(f"wire end {end!r}: no node named {node!r}")
        if term and _TERM_NAME_BAD.search(term):
            raise SpecError(f"wire end {end!r}: bad terminal name")
        k = n.kind
        if k in _NODE_KINDS:
            if not term:
                raise SpecError(f"wire end {end!r}: give the terminal: a property (\"{node}.Visible\"), a "
                                f"method parameter, \"{node}.reference\", \"{node}.error in\" or \"{node}.error out\"")
            if term == "error in":  # the terminal's real name
                return f"{node}.error in (no error)"
            return end
        if k in ("function", "subvi"):
            if not term:
                raise SpecError(f"wire end {end!r}: give the terminal, e.g. \"{node}.x\"")
            if (k == "function" and term.startswith("element") and not re.fullmatch(r"element\[\d+\]", term)
                    and (n.spec.get("size") is not None or n.spec["function"].strip() in _GROWABLE_FUNCS)):
                # a bare "element" reference on a growable node has crashed LabVIEW
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
        return [f"{name}#{i}" for i in range(len(n.spec["events" if n.kind == "event" else "cases"]))]

    def encloses(self, end: str, fid: str) -> bool:
        """`end` is the stop terminal of a while loop around the event structure of frame `fid`."""
        loop, _, term = end.partition(".")
        if self.nodes.get(loop) is None or self.nodes[loop].kind != "while" or \
                _STRUCT_TERMS["while"].get(term) != "Loop End Ref":
            return False
        c = self.nodes[self.owner_of[fid]].container
        while c:
            if self.owner_of[c] == loop:
                return True
            c = self.nodes[self.owner_of[c]].container
        return False

    def event_frame(self, name: str) -> str | None:
        """The event-structure frame ("E#1") that holds node `name`, if any."""
        n = self.nodes[name]
        c = self.nodes[n.owner].container if n.kind in ("shiftreg", "tunnel") else n.container
        while c:
            owner = self.owner_of[c]
            if self.nodes[owner].kind == "event" and c != owner:
                return c
            c = self.nodes[owner].container
        return None

    _BIND_TAG = {"existing": "Control", "constant": "Constant", "function": "Function",
                 "subvi": "SubVI", "for": "Structure", "while": "Structure", "case": "MultiframeStructure",
                 "pnode": "Function", "inode": "Function"}

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
            if self.nodes[n].kind in ("for", "while", "case", "event"):
                # A loop's contents are positioned relative to the loop; case frames share the
                # coordinates of the diagram that holds the case. (Event frames are generated in
                # their own scripts later; their layout here only sizes the structure.)
                inner = (PAD_X, PAD_TOP) if self.nodes[n].kind in ("for", "while") else (ax + PAD_X, ay + PAD_TOP)
                for b in self.bodies(n):
                    self.place(b, inner, wires, out)

    # -- emit
    def add_type(self, t, value=None) -> int:
        lt = parse_type(t)
        self.types.append((lt.td(), lt.data(value)))
        return len(self.types) - 1

    def out_attr(self, name: str) -> str:
        """Ask LabVIEW for a reference to this new object (returned in Obj Refs Out[])."""
        if name not in self.out_index:
            return ""
        return f" out=\"{self.out_index[name]}\""

    def emit(self, container: str, pos, wires, out: list):
        for name in self.children[container]:
            n = self.nodes[name]
            p = _pos(*pos[name])
            o = self.out_attr(name)
            if n.kind == "constant":
                ti = self.add_type(n.spec.get("type", "dbl"), n.spec.get("value"))
                out.append(f"<Constant name={quoteattr(name)} typeindex=\"{ti}\" position=\"{p}\"{o}></Constant>")
            elif n.kind == "function":
                style = n.spec["function"].strip()
                size = n.spec.get("size", n.spec.get("inputs"))
                if size is None:
                    out.append(f"<Function name={quoteattr(name)} style={quoteattr(style)} position=\"{p}\"{o}></Function>")
                else:
                    tag = "BuildArray" if style.lower() == "build array" else "GrowableFunction"
                    out.append(f"<{tag} name={quoteattr(name)} style={quoteattr(style)} size=\"{int(size)}\" "
                               f"position=\"{p}\"{o}></{tag}>")
            elif n.kind == "subvi":
                out.append(f"<SubVI name={quoteattr(name)} path={quoteattr(n.spec['_path'])} ignoreSearchDialog=\"TRUE\" "
                           f"position=\"{p}\"{o}></SubVI>")
            elif n.kind in _NODE_KINDS:  # placed generic; class and items are set afterwards
                style = "Property Node" if n.kind == "pnode" else "Invoke Node"
                out.append(f"<Function name={quoteattr(name)} style=\"{style}\" position=\"{p}\"{o}></Function>")
            elif n.kind == "event":
                # Created with its one default frame; the frames are added and registered afterwards.
                w, h = self.size(name, wires)
                self.event_order.append(name)
                out.append(f"<MultiframeStructure name={quoteattr(name)} style=\"Event Structure\" "
                           f"position=\"{p}\" width=\"{w}\" height=\"{h}\"{o}></MultiframeStructure>")
            elif n.kind in ("for", "while", "case"):
                w, h = self.size(name, wires)
                side = [m for m in self.nodes.values() if m.owner == name]
                if n.kind == "case":
                    out.append(f"<MultiframeStructure name={quoteattr(name)} style=\"Case Structure\" "
                               f"position=\"{p}\" width=\"{w}\" height=\"{h}\"{o}>")
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
                               f"width=\"{w}\" height=\"{h}\"{cond}{o}>")
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
    labels, defaults, styles = {}, {}, {}
    for c in controls:
        b.add(c.get("name"), "indicator" if c["_ind"] else "control", "", c)
        labels[c["name"]] = str(c.get("label") or c["name"])
        if c.get("style") is not None:
            st = c["style"]
            if not isinstance(st, str) or not st.strip() or _TERM_BAD.search(st):
                raise SpecError(f"{c['name']!r}: \"style\" is the name of a front-panel control as shown "
                                "in the Controls palette, e.g. \"Stop Button\", \"Round LED\", \"Waveform Chart\"")
            styles[labels[c["name"]]] = st.strip()
        if c.get("default") is not None:
            if c["_ind"]:
                raise SpecError(f"indicator {c['name']!r} cannot have a default")
            t = parse_type(c.get("type", "dbl"))
            # an enum's value goes through COM as its item index
            defaults[labels[c["name"]]] = t.enum_index(c["default"]) if t.kind == "enum" else c["default"]
    b.collect(spec, "")
    if not edit and not existing and not any(n.kind not in ("control", "indicator") for n in b.nodes.values()):
        # A diagram holding only terminals is saved by LabVIEW as a file it cannot reopen.
        raise SpecError("the diagram needs at least one constant, function, subVI or structure")

    wires, late_raw = [], []
    frame_wires: dict[str, list] = {}  # event frame id -> wires drawn in that frame, in spec syntax
    vc_frame: dict[str, str] = {}  # control -> its only Value Change frame
    for n in list(b.nodes.values()):
        if n.kind == "event":
            for i, fr in enumerate(n.spec["events"]):
                if fr.get("event", "Value Change") == "Value Change" and b.nodes[fr["control"]].kind == "control":
                    vc_frame[fr["control"]] = None if fr["control"] in vc_frame else f"{n.name}#{i}"
    vc_frame = {k: v for k, v in vc_frame.items() if v}
    for w in list(spec.get("wires", []) or []) + b.extra_wires:
        if isinstance(w, dict):
            w = [w.get("from"), w.get("to")]
        if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
            raise SpecError(f"wire {w!r} must be [\"from\", \"to\"]")
        ends = (b.resolve(w[0]), b.resolve(w[1]))
        frames = {f for f in (b.event_frame(e.partition(".")[0]) for e in w) if f}
        if not frames and w[0] in vc_frame and b.encloses(w[1], vc_frame[w[0]]):
            # [button, loop.stop]: read the button in its own Value Change frame (NI's pattern;
            # a latched button only resets when its terminal is read) and stop the loop from there.
            frames = {vc_frame[w[0]]}
        if not frames and any(b.nodes[e.partition(".")[0]].kind in _NODE_KINDS
                              and b.nodes[e.partition(".")[0]].bind is None for e in w):
            late_raw.append(list(w))  # a Property/Invoke Node has its terminals once it is configured
        elif not frames:
            wires.append(ends)
        elif len(frames) > 1:
            raise SpecError(f"wire {list(w)!r} joins two different event frames")
        else:
            frame_wires.setdefault(frames.pop(), []).append(list(w))
    events = _plan_events(b, frame_wires, wires, labels) if any(
        n.kind == "event" for n in b.nodes.values()) else []
    if events and (edit or existing):
        raise SpecError("event structures can only be made by vi_create for now")
    pnodes, late, late_refs = _plan_nodes(b, late_raw, labels)

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
                   + f" position=\"{_pos(*fp)}\"{b.out_attr(n)}></Control>")
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
        styles=styles,
        outs=sorted(b.out_index, key=b.out_index.get),
        events=[dict(e, out=b.out_index[e["name"]]) for e in
                sorted(events, key=lambda e: b.event_order.index(e["name"]))],
        pnodes=pnodes, late=late, late_refs=late_refs,
    )


def _plan_nodes(b: _Builder, late_raw: list, labels: dict):
    """Property/Invoke Nodes of this script (not those in event frames: their frame's script has them)
    and the script that wires them once configured."""
    pnodes = []
    for name, n in b.nodes.items():
        if n.kind not in _NODE_KINDS or n.bind is not None or b.event_frame(name):
            continue
        out = b.out_index.setdefault(name, len(b.out_index))
        s = n.spec
        items = list(s.get("properties") or [])
        w = s.get("write", False)
        write = list(range(len(items))) if w is True else [items.index(x) for x in w or []]
        pnodes.append({"name": name, "out": out, "kind": n.kind, "class": s.get("class"), "items": items,
                       "write": write, "control": s.get("control"), "method": s.get("method")})
    if not late_raw:
        return pnodes, None, None
    ends = sorted({e.partition(".")[0] for w in late_raw for e in w})
    existing, refs = [], []
    for nm in ends:
        n = b.nodes[nm]
        if n.kind in ("shiftreg", "tunnel", "event"):
            raise SpecError(f"a Property/Invoke Node cannot be wired to {nm!r} (a {n.kind}) yet; wire it "
                            "through a function or constant")
        existing.append({"name": nm, "kind": "existing" if n.kind in ("control", "indicator") else n.kind})
        refs.append(("in", n.bind) if n.bind is not None else ("out", b.out_index.setdefault(nm, len(b.out_index))))
    late = build({"wires": late_raw}, existing=existing, edit=True)
    return pnodes, late, refs


# vi_dump-style kinds of objects that code in an event frame can be wired to -> how they are bound
_FRAME_BIND = {"control": "existing", "indicator": "existing", "constant": "constant",
               "function": "function", "subvi": "subvi", "for": "for", "while": "while", "case": "case"}


def _plan_events(b: _Builder, frame_wires: dict, main_wires: list, labels: dict) -> list:
    """Each event frame becomes its own script, run on the frame's diagram after the frame exists.
    Objects outside the frame that its wires reach are bound by reference (Obj Refs Out of the
    main script -> Obj Refs In of the frame's script)."""
    def ends(ws):
        return {str(e).partition(".")[0] for w in ws for e in w}

    outside_wired = ends([(a.partition(".")[0], z.partition(".")[0]) for a, z in main_wires])
    events = []
    for name, n in b.nodes.items():
        if n.kind != "event":
            continue
        b.out_index.setdefault(name, len(b.out_index))
        frames = []
        for i, fr in enumerate(n.spec["events"]):
            fid = f"{name}#{i}"
            fw = frame_wires.get(fid, [])
            inside = {m for m in b.nodes if b.event_frame(m) == fid}
            bind = sorted(ends(fw) - inside)
            for o in bind:
                k = b.nodes[o].kind
                if k not in _FRAME_BIND or b.event_frame(o):
                    raise SpecError(
                        f"event {name!r} frame {i}: code in an event frame cannot be wired to {o!r} (a "
                        f"{k}). It can be wired to controls, indicators, constants, functions, subVIs and "
                        "loop or case structures outside the event structure, but not to shift registers "
                        "or to code in another event frame")
                b.out_index.setdefault(o, len(b.out_index))
            ctl = fr["control"]
            # NI's practice: a front-panel terminal used by one event case sits in that case, so a
            # control is read when the event happens (not once before the loop) and an indicator is
            # written there. A button's own Value Change case reads it even when it is not wired (a
            # latched button resets only when its terminal is read).
            other = outside_wired | {e for f2, w2 in frame_wires.items() if f2 != fid for e in ends(w2)}
            terms = [o for o in bind if b.nodes[o].kind in ("control", "indicator") and o not in other]
            if (b.nodes[ctl].kind == "control" and fr.get("event", "Value Change") == "Value Change"
                    and ctl not in other and ctl not in terms):
                terms.append(ctl)
            contents = {k: v for k, v in fr.items() if k in _CONTENT_KEYS}
            script = None
            if fw or any(contents.values()):
                script = build({**contents, "wires": fw},
                               existing=[{"name": o, "kind": _FRAME_BIND[b.nodes[o].kind]} for o in bind],
                               origin=(40, 40), edit=True)
                if script.events:
                    raise SpecError(f"event {name!r} frame {i}: an event structure inside an event "
                                    "frame is not supported")
            frames.append({"control": labels[ctl], "event": CONTROL_EVENTS[fr.get("event", "Value Change")],
                           "move": [labels[t] for t in terms], "bind": bind, "script": script})
        events.append({"name": name, "frames": frames})
    return events
