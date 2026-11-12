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
             "VI Server references as \"ref:Control\", \"ref:VI\", \"ref:Boolean[]\"; "
             "arrays by suffix (\"dbl[]\", \"string[][]\"); clusters as "
             "{\"cluster\": [{\"name\": \"a\", \"type\": \"dbl\"}, ...]}; any other type (waveform, "
             "class, typedef, ...) as the \"flat\" entry vi_dump gives a control or constant.")


@dataclass
class LVType:
    kind: str                     # numeric name, "bool", "string", "path", "array", "cluster", "refnum"
    elem: "LVType | None" = None  # array element
    dims: int = 0
    fields: "list[tuple[str, LVType]] | None" = None  # cluster elements
    items: "list[str] | None" = None  # enum item names

    # -- flattened type descriptor
    def td(self, name: str | None = None) -> bytes:
        if self.kind == "flat":
            return self.items[0]
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
        elif self.kind == "refnum":  # VI Server refnum: kind 8, class number (items = [class name])
            code = 0x70
            body = struct.pack(">HHHH", 0x0008, VI_CLASSES[self.items[0]], 0x0004, 0)
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
        if k == "flat":
            if v is not None:
                raise SpecError("a {\"flat\": ...} type keeps the default value it was copied with")
            return self.items[1]
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
        if k == "refnum":  # always Not a Refnum
            return b"\x00" * 4
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
    if isinstance(t, dict) and "flat" in t:  # a type copied from an existing object (vi_dump)
        try:
            ts, ds = bytes.fromhex(t["flat"]), bytes.fromhex(t.get("data") or "")
        except (TypeError, ValueError):
            raise SpecError("{\"flat\": ...} takes the hex type string vi_dump gives, and \"data\": "
                            "its hex default value") from None
        if len(ts) < 4 or struct.unpack_from(">H", ts, 0)[0] != len(ts):
            raise SpecError("{\"flat\": ...}: not a flattened LabVIEW type string")
        return LVType("flat", items=[ts, ds])
    if isinstance(t, dict) and "enum" in t:
        items = t["enum"]
        if (not isinstance(items, list) or not items or len(items) > 1000
                or not all(isinstance(x, str) and x and len(_lv_text(x)) < 256 for x in items)
                or len(set(items)) != len(items)):
            raise SpecError("enum type must be {\"enum\": [\"Init\", \"Run\", \"Stop\"]}: distinct, non-empty names")
        return LVType("enum", items=list(items))
    if isinstance(t, dict) and "array" in t:  # {"array": <element type>, "dims": 1}, e.g. clusters
        dims = t.get("dims", 1)
        if not isinstance(dims, int) or not 1 <= dims <= 8:
            raise SpecError("{\"array\": ..., \"dims\": n} needs dims 1..8")
        return LVType("array", elem=parse_type(t["array"]), dims=dims)
    if isinstance(t, dict):
        fields = t.get("cluster")
        if not isinstance(fields, list) or not fields:
            raise SpecError("cluster type must be {\"cluster\": [{\"name\":..., \"type\":...}, ...]}")
        return LVType("cluster", fields=[(str(f.get("name", "")), parse_type(f.get("type"))) for f in fields])
    if not isinstance(t, str):
        raise SpecError(f"bad type {t!r}. {TYPE_HELP}")
    m = re.fullmatch(r"\s*ref:\s*([A-Za-z][\w ]*?)\s*((?:\[\])*)\s*", t)
    if m:  # a VI Server reference, e.g. "ref:Control", "ref:VI", "ref:Boolean[]"
        cls = next((c for c in VI_CLASSES if c.lower() == m.group(1).replace(" ", "").lower()), None)
        if cls is None:
            raise SpecError(f"unknown VI Server class in {t!r}; e.g. ref:Control, ref:VI, ref:Boolean "
                            f"(classes: lv_class_properties lists any of {len(VI_CLASSES)})")
        base, dims = LVType("refnum", items=[cls]), m.group(2).count("[]")
        return LVType("array", elem=base, dims=dims) if dims else base
    if len(t) > 200:
        raise SpecError(f"bad type {t[:40]!r}... {TYPE_HELP}")
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
# terminal names a script cannot wire ("name:<number>"): wired by terminal reference instead
_TERM_BY_REF = re.compile(r":\s*\d")
_CONTENT_KEYS = ("constants", "functions", "subvis", "structures", "property_nodes", "invoke_nodes")
_NODE_KINDS = ("pnode", "inode")
# growable palette functions: their inputs are wired by position, element[i]
# terminals the diagram shows under one name but Gen Script knows by another
# terminals a script finds under no name (wired by terminal reference)
_SCRIPT_TERM_NONE = {("Insert Into Array", "index"), ("Insert Into Array", "new element/subarray"),
                     ("Replace Array Subset", "new element/subarray"),
                     ("Delete From Array", "array w/ subset deleted")}
# a grown node's repeated input "<name>[k]" is the script's element[k + offset] (measured on
# LabVIEW 2026 by running each: Reshape Array's element[1] is its output array)
_POSITIONAL = {("Compound Arithmetic", "value"): 0, ("Concatenate Strings", "string"): 0,
               ("Index & Bundle Cluster Array", "component array"): 0, ("Reshape Array", "dimension size"): 1,
               ("Build Array", "array"): 0, ("Build Cluster Array", "component element"): 0}
_SCRIPT_TERM_ALIASES = {("Index Array", "index"): "index (row)", ("Index Array", "subarray"): "element", ("Replace Array Subset", "index"): "index (row)",
                        ("Delete From Array", "index"): "index (row)"}
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
# Events of the VI itself (no "control"): the Event Type value taken as a Static App/VI Event (same
# enum; measured on LabVIEW 2026 by registering each value and reading EventStructure.EventSelectors).
VI_EVENTS = {
    "Timeout": 1, "Application Instance Close": 4, "Application Instance Close?": 5,
    "Key Down": 28, "Key Down?": 29, "Key Repeat": 30, "Key Repeat?": 31, "Key Up": 32,
    "Menu Selection (App)": 35, "Menu Selection? (App)": 36, "Menu Selection (User)": 37,
    "Mouse Enter": 40, "Mouse Leave": 41, "Panel Close": 47, "Panel Close?": 48, "Panel Resize": 49,
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
    "Loops hold constants/functions/subvis/structures and optional \"shift_registers\": [\"sr\"] "
    "(wired sr.left, sr.right; sr.left[2], sr.left[3]... stack it: values 2, 3... iterations back); "
    "while loops take \"stop_if_true\" (default true). A case holds \"cases\": [{\"value\": true | 3 | "
    "\"apple\", \"default\": false, ...contents}] and optional \"output_tunnels\": [\"t\"]. An event "
    "structure holds \"events\": [{\"control\": \"stop\", \"event\": \"Value Change\", ...contents}], one "
    "frame per entry; \"control\" is the name of a control or indicator in the spec. Events of the VI "
    "itself have no \"control\": {\"event\": \"Timeout\", \"timeout\": 100} (ms), \"Panel Close?\", "
    "\"Key Down\", ... An entry may name its event data node, \"data\": \"d\" (wire \"d.NewVal\", "
    "\"d.OldVal\", \"d.Coords\", ...), and for filter events (ending in \"?\") its filter node, "
    "\"filter\": \"f\" (wire a True constant to \"f.Discard?\"). \"more\": [{\"control\": \"b\"}] adds events to "
    "a frame; a frame's \"wires\": [[\"sr.left\", \"sr.right\"]] pass a value through it unchanged.")


NODE_HELP = (
    "property_nodes: {\"name\": \"p\", \"control\": \"level\", \"properties\": [\"Visible\"], \"write\": true} "
    "(linked to a front-panel control) or {\"name\": \"p\", \"class\": \"VI\", \"properties\": [...]} (wire a "
    "reference to \"p.reference\"; class \"Application\" needs none). \"write\": true writes every property, "
    "a list writes those; the rest are read. invoke_nodes: {\"name\": \"m\", \"control\": \"graph\" | "
    "\"class\": \"VI\", \"method\": \"Run VI\"}. Wire them as \"p.Visible\", \"m.<parameter>\", "
    "\"p.reference\", \"p.error in\", \"p.error out\". Names are LabVIEW's short names (\"Visible\", "
    "\"Value\", \"App.Version\"); nested ones as LabVIEW shows them (\"Label.Text\", \"Plot.Plot Color\").")


STATIC_VI_REF = "Static VI Reference"


def _static_vi_path(f: dict) -> str | None:
    """The VI a Static VI Reference points at ("vi": absolute path; none = an empty reference)."""
    path, strict = f.get("vi"), f.get("strict", False)
    if not isinstance(strict, bool):
        raise SpecError(f"function {f.get('name')!r}: \"strict\" is true or false")
    if path is None:
        if strict:
            raise SpecError(f"function {f.get('name')!r}: a strictly typed reference needs \"vi\"")
        return None
    if not isinstance(path, str) or not path.lower().endswith((".vi", ".vim", ".vit", ".ctl")):
        raise SpecError(f"function {f.get('name')!r}: \"vi\" is an absolute path to a .vi")
    pp = Path(path)
    if not pp.is_absolute():
        raise SpecError(f"function {f.get('name')!r}: \"vi\" must be absolute: {path}")
    llb = next((q for q in pp.parents if q.suffix.lower() == ".llb"), None)
    if not (pp.is_file() or (llb is not None and llb.is_file())):
        raise SpecError(f"function {f.get('name')!r}: file not found: {path}")
    return str(pp)


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
    # (a Script binding its ends) with `late_refs` = ("out", k, name) | ("in", k, name) for each bound object.
    pnodes: list = None
    # an Unbundle By Name element wired to another By Name node's input cluster: drawn (between
    # terminal references) once the first is configured, before the second is: [(k, element, k2)]
    chains: list = None
    late: "Script | None" = None
    late_refs: list = None
    # wires to terminals a script cannot name ("new element/subarray", "number: 0 to 1"), drawn last
    # between terminal references: [(end, end)], end = ("out", k, terminal) | ("label", label, "")
    # | ("sr", k, element, side): shift register k, element 1.. of its left side (0 = right side)
    ref_wires: list = None
    # stacked shift registers: [(k, elements)], k = index into outs
    stacks: list = None
    # case output tunnels some frames leave unwired: set to "Use Default if Unwired" (indexes into outs)
    use_default: list = None
    # functions a script cannot place by name: placed as a stand-in, then replaced by the palette
    # object of that name (GObject.Replace): [(k, function, size)], k = index into outs
    replaces: list = None
    # Static VI References: [(k, vi path or None, strict)]; pointed at their VI before anything is wired
    # (wires to them are drawn between terminal references: a script cannot name their terminal)
    static_refs: list = None
    # Build Arrays to switch to Concatenate Inputs (indexes into outs), before anything is wired to them
    concats: list = None
    # loop tunnels whose indexing differs from LabVIEW's default, set once everything is wired:
    # [(source end, sink end, [(k, indexing?)])], ends as in ref_wires, k = the loop's index into outs
    index_modes: list = None

    def has_nodes(self) -> bool:
        return bool(self.pnodes) or bool(self.ref_wires) or bool(self.stacks) or bool(self.use_default) \
            or bool(self.replaces) or bool(self.static_refs) or bool(self.concats) or bool(self.index_modes) or any(fr["script"] is not None and fr["script"].has_nodes()
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
        self.sr_stack: dict[str, int] = {}  # stacked shift register -> number of left elements
        self.event_order: list[str] = []     # event structures in emit (= creation) order
        self.extra_wires: list = []           # wires the builder adds itself (case "pass")
        self.case_passes: list = []           # case "pass" entries: (case, frame, source, tunnel)
        self.pass_refs: list = []             # those drawn by terminal reference (no copy node)

    # -- collection
    def add(self, name, kind, container, spec, owner=""):
        if not isinstance(name, str) or not _NAME.match(name):
            raise SpecError(f"{kind} name {name!r} must match [A-Za-z_][A-Za-z0-9_]* (it is an id, "
                            "use 'label' for display text)")
        if name in self.nodes:
            raise SpecError(f"duplicate name {name!r}")
        self.nodes[name] = _Node(name, kind, container, spec, owner)
        if kind not in ("shiftreg", "tunnel", "existing", "evdata"):
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
            for sr in extra.get("shift_registers", []) or []:  # new shift registers on the existing loop
                self.add(sr, "shiftreg", name, {}, owner=name)
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
            if fn.strip() == STATIC_VI_REF:
                f["_vi"] = _static_vi_path(f)
            elif "vi" in f or "strict" in f:
                raise SpecError(f"function {f.get('name')!r}: \"vi\" and \"strict\" belong to a "
                                f"\"{STATIC_VI_REF}\" only")
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
                extra = set(fr) - {"control", "event", "data", "filter", "timeout", "more", "wires",
                                   *_CONTENT_KEYS}
                if extra:
                    raise SpecError(f"event structure {name!r} event {i}: unknown keys {sorted(extra)}")
                ev = fr.get("event", "Value Change")
                where = f"event structure {name!r} event {i}"
                more = fr.get("more", [])
                if not isinstance(more, list) or not all(isinstance(m, dict) and set(m) <= {"control", "event"}
                                                          for m in more):
                    raise SpecError(f"{where}: \"more\" lists further events this frame handles: "
                                    "[{\"control\": \"b\", \"event\": \"Value Change\"}, {\"event\": \"Panel Close\"}]")
                for m in more:
                    me = m.get("event", "Value Change")
                    if m.get("control") is not None:
                        src = self.nodes.get(m["control"])
                        if src is None or src.kind not in ("control", "indicator") or me not in CONTROL_EVENTS:
                            raise SpecError(f"{where}: \"more\" entry {m!r}: needs a control or indicator of "
                                            f"this spec and a control event ({sorted(CONTROL_EVENTS)})")
                    elif me not in VI_EVENTS or me == "Timeout":
                        raise SpecError(f"{where}: \"more\" entry {m!r}: events of the VI itself are "
                                        f"{sorted(e for e in VI_EVENTS if e != 'Timeout')}")
                if fr.get("control") is not None:
                    src = self.nodes.get(fr.get("control"))
                    if src is None or src.kind not in ("control", "indicator"):
                        raise SpecError(f"{where}: \"control\" must name a control or indicator of this spec")
                    if ev not in CONTROL_EVENTS:
                        raise SpecError(f"{where}: unknown control event {ev!r}. Events: {sorted(CONTROL_EVENTS)}"
                                        f"; events of the VI itself (no \"control\"): {sorted(VI_EVENTS)}")
                elif ev not in VI_EVENTS:
                    raise SpecError(f"{where}: {ev!r} needs a \"control\". Events of the VI itself (no "
                                    f"\"control\"): {sorted(VI_EVENTS)}")
                if ev == "Timeout":
                    t = fr.get("timeout")
                    if not isinstance(t, int) or isinstance(t, bool) or t < -1:
                        raise SpecError(f"{where}: a Timeout event needs \"timeout\": milliseconds to wait "
                                        "for other events, e.g. 100 (-1: wait forever)")
                    if sum(e.get("event") == "Timeout" for e in events) > 1:
                        raise SpecError(f"event structure {name!r} has more than one Timeout event")
                elif "timeout" in fr:
                    raise SpecError(f"{where}: \"timeout\" belongs to the Timeout event")
                if "filter" in fr and not ev.endswith("?"):
                    raise SpecError(f"{where}: only filter events (ending in \"?\", e.g. \"Panel Close?\") "
                                    "have a filter node")
                fid = f"{name}#{i}"
                self.owner_of[fid] = name
                self.children[fid] = []
                for role in ("data", "filter"):
                    if fr.get(role) is not None:
                        self.add(fr[role], "evdata", fid, {"role": role})
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
            # "pass": [[source, tunnel]] passes a value through this frame unchanged (see
            # _plan_passes: wired by terminal reference, or through an Always Copy node)
            for i, fr in enumerate(cases):
                for k, pw in enumerate(fr.get("pass") or []):
                    if not (isinstance(pw, (list, tuple)) and len(pw) == 2 and pw[1] in tunnels
                            and isinstance(pw[0], str)):
                        raise SpecError(f"case {name!r} frame {i}: \"pass\" is a list of [source, output "
                                        f"tunnel] pairs; output tunnels: {tunnels}")
                    self.case_passes.append((name, i, pw[0], pw[1]))
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
        if k == "evdata":
            if not term:
                raise SpecError(f"wire end {end!r}: give the item, e.g. \"{node}.NewVal\""
                                if n.spec["role"] == "data" else f"wire end {end!r}: give the item, e.g. "
                                f"\"{node}.Discard?\"")
            return end
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
                    and (n.spec.get("size") is not None or n.spec["function"].strip() in _GROWABLE_FUNCS)
                    and not (term == "element" and n.spec["function"].strip() == "Index Array")):
                # (Index Array has one "element" output per index: a 1-D one has a single one)
                # a bare "element" reference on a growable node has crashed LabVIEW
                raise SpecError(f"wire end {end!r}: wire growable inputs by position as "
                                f"\"{node}.element[1]\", \"{node}.element[2]\", ...")
            m = re.fullmatch(r"(.+?)\s*\[(\d+)\]", term)
            pos = _POSITIONAL.get((str(n.spec.get("function", "")).strip(), m.group(1))) if m else None
            if pos is not None:
                # the diagram names these inputs ("value", "string"); a script knows them by position only
                return f"{node}.element[{int(m.group(2)) + pos}]"
            alias = _SCRIPT_TERM_ALIASES.get((str(n.spec.get("function", "")).strip(), term))
            return f"{node}.{alias}" if alias else end
        if k in _STRUCT_TERMS:
            lv = _STRUCT_TERMS[k].get(term)
            if not lv:
                raise SpecError(f"wire end {end!r}: a {k} structure has terminals "
                                f"{sorted(t for t in _STRUCT_TERMS[k] if ' ' not in t)}. To pass data "
                                "in or out, wire the inner node directly; LabVIEW adds the tunnel")
            return f"{node}.{lv}"
        if k == "shiftreg":
            side = {"left": "Left", "right": "Right", "left[1]": "Left"}.get(term.lower())
            if not side:
                raise SpecError(f"wire end {end!r}: wire shift registers as \"{node}.left\" (the value "
                                f"from the previous iteration), \"{node}.left[2]\" (the one before, a stacked "
                                f"shift register; [3], ... further back) or \"{node}.right\" (the value for the next)")
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
                for k, sr in enumerate(m for m in self.nodes.values() if m.owner == n.name and m.kind == "shiftreg"):
                    out.append(f"<ShiftRegister name={quoteattr(sr.name)} location=\"left\" "
                               f"offset=\"{self.sr_offset(sr)}\"{self.out_attr(sr.name)}></ShiftRegister>")
            elif n.kind == "case":
                for i in n.spec.get("_frames", []):
                    out.append(f"<Frame name={quoteattr(f'{n.name}_frame{i}')} index=\"{i}\">")
                    self.emit(f"{n.name}#{i}", pos, wires, out)
                    out.append("</Frame>")
            out.append(f"</{tag}>")

    def sr_offset(self, sr: "_Node") -> int:
        """Where a shift register sits on its loop's left side, below the (stacked) ones before it."""
        before = 0
        for m in self.nodes.values():
            if m is sr:
                break
            if m.owner == sr.owner and m.kind == "shiftreg":
                before += self.sr_stack.get(m.name, 1)
        return PAD_TOP + 30 * before

    def size(self, name: str, wires) -> tuple[int, int]:
        n = self.nodes[name]
        if n.kind in _LEAF_SIZE:
            return _LEAF_SIZE[n.kind]
        w = h = 0
        for b in self.bodies(name):
            bw, bh, _ = self.layout(b, wires)
            w, h = max(w, bw), max(h, bh)
        side = sum(self.sr_stack.get(m.name, 1) for m in self.nodes.values() if m.owner == name)
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
            elif n.kind == "function" and n.spec["function"].strip() in getattr(self, "replace", ()):
                # a stand-in, replaced by the real palette object once placed
                out.append(f"<Function name={quoteattr(name)} style=\"Add\" position=\"{p}\"{o}></Function>")
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
                                   f"offset=\"{PAD_TOP + 30 * k}\"{self.out_attr(t.name)}></Tunnel>")
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
                                   f"offset=\"{self.sr_offset(sr)}\"{self.out_attr(sr.name)}></ShiftRegister>")
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
          edit: bool = False, refs: tuple = (), replace: frozenset = frozenset()) -> Script:
    """existing: objects already on the diagram that the spec refers to, in ObjRefsIn order; each a
    name (a front-panel terminal) or {"name", "kind", "spec"} (see _Builder.add_existing). Used by
    vi_edit. New controls then stay off the connector pane unless they ask for a slot."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    unknown = set(spec) - {"controls", "indicators", "wires", "existing", "auto_error_handling", "library",
                           *_CONTENT_KEYS}
    lib = spec.get("library")
    if lib is not None and not (isinstance(lib, str) and Path(lib).is_absolute()
                                and Path(lib).suffix.lower() in (".lvlib", ".lvclass")):
        raise SpecError("\"library\" is the absolute path of the .lvlib or .lvclass the VI joins")
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
    b.replace = replace
    replaces = []
    for nm, n in b.nodes.items():
        if n.kind == "function" and n.bind is None and str(n.spec.get("function", "")).strip() in replace:
            if edit or existing or b.event_frame(nm):
                raise SpecError(f"function {nm!r} ({n.spec['function']}) cannot be placed by LabVIEW's "
                                "scripting by name; vi_create places it outside event structures only")
            replaces.append((b.out_index.setdefault(nm, len(b.out_index)), n.spec["function"].strip(),
                             n.spec.get("size")))
    static_refs = []
    for nm, n in b.nodes.items():
        if n.kind == "function" and n.bind is None and str(n.spec.get("function", "")).strip() == STATIC_VI_REF:
            if edit or existing or b.event_frame(nm):
                raise SpecError(f"function {nm!r}: a {STATIC_VI_REF} is placed by vi_create, outside event "
                                "structures, only")
            static_refs.append((b.out_index.setdefault(nm, len(b.out_index)), n.spec.get("_vi"),
                                bool(n.spec.get("strict", False))))
    concats = []  # Build Arrays set to Concatenate Inputs
    for nm, n in b.nodes.items():
        if n.kind == "function" and n.bind is None and "concatenate" in n.spec:
            if str(n.spec.get("function", "")).strip() != "Build Array" or not isinstance(n.spec["concatenate"], bool):
                raise SpecError(f"function {nm!r}: \"concatenate\" (true/false) belongs to a Build Array")
            if n.spec["concatenate"]:
                if edit or existing or b.event_frame(nm):
                    raise SpecError(f"function {nm!r}: a concatenating Build Array is placed by vi_create, "
                                    "outside event structures, only")
                concats.append(b.out_index.setdefault(nm, len(b.out_index)))
    for nm in refs:  # objects whose references the caller wants back (in Script.outs order)
        b.out_index.setdefault(nm, len(b.out_index))
    if not edit and not existing and not any(n.kind not in ("control", "indicator") for n in b.nodes.values()):
        # A diagram holding only terminals is saved by LabVIEW as a file it cannot reopen.
        raise SpecError("the diagram needs at least one constant, function, subVI or structure")

    wires, late_raw, ref_wires = [], [], []
    _plan_passes(b, ref_wires, labels, edit or bool(existing))
    frame_wires: dict[str, list] = {}  # event frame id -> wires drawn in that frame, in spec syntax
    vc_frame: dict[str, str] = {}  # control -> its only Value Change frame
    for n in list(b.nodes.values()):
        if n.kind == "event":
            for i, fr in enumerate(n.spec["events"]):
                if (fr.get("control") and fr.get("event", "Value Change") == "Value Change"
                        and b.nodes[fr["control"]].kind == "control"):
                    vc_frame[fr["control"]] = None if fr["control"] in vc_frame else f"{n.name}#{i}"
    vc_frame = {k: v for k, v in vc_frame.items() if v}
    spec_wires = [list(w) if isinstance(w, (list, tuple)) else w for w in spec.get("wires", []) or []]
    spec_wires += _byname_bridges(b, spec_wires)
    ref_raw = []  # spec wires drawn between terminal references
    index_modes = []  # loop tunnels set to (or from) indexing: see Script.index_modes
    for w in spec_wires + b.extra_wires:
        if isinstance(w, dict):
            idx = w.get("indexing")
            w = [w.get("from"), w.get("to")]
            if idx is not None and all(isinstance(x, str) for x in w):
                index_modes.append(_index_mode(b, w, idx, labels, edit or bool(existing)))
        if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
            raise SpecError(f"wire {w!r} must be [\"from\", \"to\"]")
        if any(_by_ref(b, e) for e in w) or _inode_output(b, w[0]) or (any(_late_end(b, e) for e in w) and any(
                b.nodes.get(e.partition(".")[0]) is not None and b.nodes[e.partition(".")[0]].kind == "shiftreg"
                for e in w)):
            # (a Property/Invoke Node or By Name element to a shift register: the late script cannot
            # bind a shift register, so that wire too is drawn between terminal references)
            ref_wires.append(tuple(_ref_end(b, e, labels, w, edit or bool(existing), i) for i, e in enumerate(w)))
            ref_raw.append(list(w))
            continue
        ends = (b.resolve(w[0]), b.resolve(w[1]))
        frames = {f for f in (b.event_frame(e.partition(".")[0]) for e in w) if f}
        if not frames and w[0] in vc_frame and b.encloses(w[1], vc_frame[w[0]]):
            # [button, loop.stop]: read the button in its own Value Change frame (NI's pattern;
            # a latched button only resets when its terminal is read) and stop the loop from there.
            frames = {vc_frame[w[0]]}
        if not frames and any((b.nodes[e.partition(".")[0]].kind in _NODE_KINDS
                               and b.nodes[e.partition(".")[0]].bind is None) or _byname_elem(b, e) for e in w):
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
    # By Name nodes chained element -> input cluster: the second shows its elements only once its
    # cluster is wired, so that wire is drawn between their configurations
    def byname_in(e):
        node, _, term = e.partition(".")
        n = b.nodes.get(node)
        return (term == "input cluster" and n is not None and n.kind == "function" and n.bind is None
                and str(n.spec.get("function", "")).strip() in _BYNAME and not b.event_frame(node))
    chain_raw = [w for w in late_raw if _byname_elem(b, w[0]) and byname_in(w[1])
                 and _BYNAME[b.nodes[w[0].partition(".")[0]].spec["function"].strip()] == "unbundle"]
    late_raw = [w for w in late_raw if w not in chain_raw]
    pnodes, late, late_refs = _plan_nodes(b, late_raw, labels, list(ref_raw) + chain_raw)
    chains = []
    if chain_raw:
        # configure each By Name node after the one its cluster comes from
        src_of = {w[1].partition(".")[0]: w[0].partition(".")[0] for w in chain_raw}
        def depth(nm, seen=()):
            return 0 if nm not in src_of or nm in seen else 1 + depth(src_of[nm], (*seen, nm))
        pnodes.sort(key=lambda pn: depth(pn["name"]) if pn["kind"] == "byname" else -1)
        chains = [(b.out_index[w[0].partition(".")[0]], w[0].partition(".")[2],
                   b.out_index.setdefault(w[1].partition(".")[0], len(b.out_index))) for w in chain_raw]

    # a case output tunnel wired in only some frames gives its type's default in the others
    use_default = []
    raw = [w if not isinstance(w, dict) else [w.get("from"), w.get("to")]
           for w in list(spec.get("wires", []) or []) + b.extra_wires]
    for t in [m for m in b.nodes.values() if m.kind == "tunnel" and b.nodes[m.owner].kind == "case"
              and b.nodes[m.owner].bind is None]:
        fed = set()
        for a, z in raw:
            if z == t.name and a.partition(".")[0] in b.nodes:
                for body in b.bodies(t.owner):
                    if b.item_in(a.partition(".")[0], body) is not None:
                        fed.add(body)
        fed |= {f"{case}#{i}" for case, i, _, tun in b.pass_refs if tun == t.name}
        if len(fed) < len(b.bodies(t.owner)):
            use_default.append(b.out_index.setdefault(t.name, len(b.out_index)))

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
        pnodes=pnodes, chains=chains, late=late, late_refs=late_refs, ref_wires=ref_wires,
        stacks=sorted((b.out_index[n], k) for n, k in b.sr_stack.items()),
        replaces=replaces,
        static_refs=static_refs,
        concats=concats,
        index_modes=index_modes,
        use_default=use_default,
    )


_SR_ELEMENT = re.compile(r"left\[(\d+)\]")


def _inode_output(b: _Builder, end: str) -> bool:
    """A value out of a new Invoke Node (its method's return value or an in/out parameter): each has
    an input and an output terminal of one name and a script finds the input, so the wire is drawn
    between terminal references from the output one."""
    node, _, term = end.partition(".")
    n = b.nodes.get(node)
    return (n is not None and n.kind == "inode" and n.bind is None and not b.event_frame(node)
            and term not in ("", "reference out", "error out"))


def _by_ref(b: _Builder, end: str) -> bool:
    node, _, term = end.partition(".")
    n = b.nodes.get(node)
    if n is not None and n.kind == "shiftreg":  # element 2.. of a stacked shift register
        m = _SR_ELEMENT.fullmatch(term.lower())
        return bool(m) and int(m.group(1)) >= 2
    if n is None or n.kind not in ("function", "subvi"):
        return False
    if n.kind == "function" and str(n.spec.get("function", "")).strip() in getattr(b, "replace", ()):
        return True
    if n.kind == "function" and str(n.spec.get("function", "")).strip() == STATIC_VI_REF:
        return True  # replaced after the script ran: wired between terminal references
    fn = str(n.spec.get("function", "")).strip()
    # a grown Index Array's index terminals have no name a script accepts
    # (a 2-D one's column index has no name or position a script accepts at all)
    grown_index = fn == "Index Array" and (
        (n.spec.get("size") is not None and re.fullmatch(r"index( \((row|col)\))?(\[\d+\])?", term))
        or re.fullmatch(r"index \(col\)(\[\d+\])?", term))
    return bool(_TERM_BY_REF.search(term)) or (fn, term) in _SCRIPT_TERM_NONE or bool(grown_index)


def _index_mode(b: _Builder, w, idx, labels: dict, edit: bool):
    """A wire's "indexing": true / false (every loop it crosses) or {loop: true / false}."""
    if edit:
        raise SpecError(f"wire {list(w)!r}: \"indexing\" is set by vi_create only for now")
    def within(node: str, loop: str) -> bool:  # node sits inside loop (at any depth)
        if node == loop:  # the loop's own terminals (i, N, stop) are inside it
            return True
        n = b.nodes[node]
        c = b.nodes[n.owner].container if n.kind in ("shiftreg", "tunnel") else n.container
        while c:
            if b.owner_of[c] == loop:
                return True
            c = b.nodes[b.owner_of[c]].container
        return False
    if isinstance(idx, bool):  # every loop the wire crosses: one end inside it, the other not
        ns = [e.partition(".")[0] for e in w]
        if not all(x in b.nodes for x in ns):
            raise SpecError(f"wire {list(w)!r}: no such node")
        idx = {m.name: idx for m in b.nodes.values() if m.kind in ("for", "while")
               and within(ns[0], m.name) != within(ns[1], m.name)}
    if not (isinstance(idx, dict) and all(isinstance(v, bool) for v in idx.values())):
        raise SpecError(f"wire {list(w)!r}: \"indexing\" is true, false or {{\"<loop>\": true/false}}")
    for name in idx:
        if name not in b.nodes or b.nodes[name].kind not in ("for", "while"):
            raise SpecError(f"wire {list(w)!r}: \"indexing\" names {name!r}, which is not a For or While Loop")
    ends = []
    for e in w:
        node, _, term = e.partition(".")
        n = b.nodes.get(node)
        if n is None:
            raise SpecError(f"wire end {e!r}: no node named {node!r}")
        if b.event_frame(node):
            raise SpecError(f"wire {list(w)!r}: \"indexing\" cannot be set inside an event structure yet")
        ends.append(("label", labels[node], "") if n.kind in ("control", "indicator")
                    else ("out", b.out_index.setdefault(node, len(b.out_index)),
                          "" if n.kind in ("shiftreg", "tunnel") else term))
    return (ends[0], ends[1], [(b.out_index.setdefault(k, len(b.out_index)), v) for k, v in idx.items()])


def _ref_end(b: _Builder, end: str, labels: dict, w, edit: bool, at: int = 0):
    """An end of a wire drawn between terminal references (see Script.ref_wires); `at` is 0 for the
    wire's source, 1 for its sink."""
    node, _, term = end.partition(".")
    n = b.nodes.get(node)
    if n is None:
        raise SpecError(f"wire end {end!r}: no node named {node!r}")
    if edit:
        raise SpecError(f"wire {list(w)!r}: terminal {term!r} can only be wired by vi_create for now")
    if n is not None and n.kind == "shiftreg":
        t = term.lower()
        m = _SR_ELEMENT.fullmatch(t)
        k = int(m.group(1)) if m else 1 if t == "left" else 0 if t == "right" else None
        if k is None:
            b.resolve(end)  # raises the usual message
        if b.event_frame(node):
            raise SpecError(f"wire {list(w)!r}: stacked shift registers cannot be wired inside an event "
                            "structure yet")
        b.sr_stack[node] = max(b.sr_stack.get(node, 1), k)
        b.out_index.setdefault(node, len(b.out_index))
        # a left element's value comes from its inner terminal and its initial value goes into the
        # outer one; the right side takes the next value inside and gives the last one outside
        side = ("inner" if at == 0 else "outer") if k else ("outer" if at == 0 else "inner")
        return ("sr", b.out_index[node], k, side)
    if b.event_frame(node):
        raise SpecError(f"wire {list(w)!r}: terminals with names a script cannot use cannot be wired "
                        "inside an event structure yet")
    if n.kind in ("control", "indicator"):
        return ("label", labels[node], "")
    if n.kind in ("function", "subvi", "constant", *_NODE_KINDS):
        if n.kind != "constant" and not term:
            raise SpecError(f"wire end {end!r}: give the terminal, e.g. \"{node}.x\"")
        return ("out", b.out_index.setdefault(node, len(b.out_index)), term)
    raise SpecError(f"wire {list(w)!r}: a terminal whose name a script cannot use can be wired to a "
                    f"function, subVI, constant, control or indicator, not to {node!r} (a {n.kind}) yet")


# nodes whose element terminals are chosen by name (Build Waveform / Get Waveform Components are
# By Name nodes of the waveform's components)
_BYNAME = {"Unbundle By Name": "unbundle", "Bundle By Name": "bundle",
           "Get Waveform Components": "unbundle", "Build Waveform": "bundle"}
_BYNAME_FIXED = {"input cluster", "output cluster", "waveform", "output waveform", ""}


def _plan_passes(b: _Builder, ref_wires: list, labels: dict, edit: bool) -> None:
    """Case "pass" entries. vi_create wires the source straight to the output tunnel's terminal in
    that frame (Terminal.Connect Wire; LabVIEW adds the input tunnel), as LabVIEW users draw it. Where
    that cannot be done (vi_edit, a source a reference cannot name, an event structure around it) the
    value goes through an Always Copy node in the frame instead: a wire from outside straight to an
    output tunnel would land on the tunnel's outer side."""
    for case, i, src, tun in b.case_passes:
        sn = b.nodes.get(src.partition(".")[0])
        direct = (not edit and sn is not None and sn.bind is None and b.nodes[case].bind is None
                  and sn.kind in ("control", "function", "subvi", "constant", "shiftreg")
                  and not b.event_frame(sn.name) and not b.event_frame(case)
                  and b.item_in(sn.name, f"{case}#{i}") is None)
        if direct:
            b.pass_refs.append((case, i, src, tun))
            ref_wires.append((_ref_end(b, src, labels, [src, tun], False, 0),
                              ("tunframe", b.out_index.setdefault(tun, len(b.out_index)),
                               b.out_index.setdefault(case, len(b.out_index)), i)))
            continue
        cp = f"_pass_{case}_{i}_{len(b.extra_wires)}"
        b.add(cp, "function", f"{case}#{i}", {"name": cp, "function": "Always Copy"})
        b.extra_wires += [[src, f"{cp}.Input"], [f"{cp}.Output", tun]]


_GROWN_ELEMENT = re.compile(r"element\[\d+\]")


def _byname_bridges(b: _Builder, wires: list) -> list:
    """A By Name element wired to an element[k] of a Bundle / Build Array / Concatenate... or to a
    tunnel: the late script (which draws By Name element wires) cannot name a grown node's k-th
    element or bind a tunnel, so the value goes through an Always Copy node. Rewrites those wires in `wires` in place
    and returns the wires to add."""
    extra = []
    for j, w in enumerate(wires):
        if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
            continue
        for a, z in ((0, 1), (1, 0)):
            node, _, term = w[z].partition(".")
            n = b.nodes.get(node)
            src = b.nodes.get(w[a].partition(".")[0])
            pnode = (src is not None and src.kind in _NODE_KINDS and src.bind is None
                     and not b.event_frame(src.name))  # a Property/Invoke Node's terminals: late too
            if not (_byname_elem(b, w[a]) or pnode) or n is None:
                continue
            fn = str(n.spec.get("function", "")).strip()
            m = re.fullmatch(r"(.+?)\s*\[\d+\]", term)
            grown = n.kind == "function" and n.bind is None and (
                (_GROWN_ELEMENT.fullmatch(term) and fn in _GROWABLE_FUNCS)
                or (m is not None and (fn, m.group(1)) in _POSITIONAL))
            # nor bind a tunnel: the copy sits next to the By Name node, so the wire to the tunnel
            # leaves from the same diagram
            if grown or n.kind == "tunnel":
                cp = f"_copy_{node}_{len(extra)}"
                where = n.container if grown else b.nodes[w[a].partition(".")[0]].container
                b.add(cp, "function", where, {"name": cp, "function": "Always Copy"})
                if a == 0:  # By Name element -> grown node
                    wires[j], more = [w[0], f"{cp}.Input"], [f"{cp}.Output", w[1]]
                else:
                    wires[j], more = [f"{cp}.Output", w[1]], [w[0], f"{cp}.Input"]
                extra.append(more)
                break
    return extra


def _late_end(b: _Builder, end: str) -> bool:
    """A terminal that exists only once its node is configured (see _plan_nodes)."""
    n = b.nodes.get(end.partition(".")[0])
    return n is not None and ((n.kind in _NODE_KINDS and n.bind is None) or _byname_elem(b, end))


def _byname_elem(b: _Builder, end: str) -> bool:
    """An element terminal of a new Bundle / Unbundle By Name node ("u.gain"): it exists only once the
    node is told which elements to show, after its cluster input is wired."""
    node, _, term = end.partition(".")
    n = b.nodes.get(node)
    return (n is not None and n.kind == "function" and n.bind is None and not b.event_frame(node)
            and str(n.spec.get("function", "")).strip() in _BYNAME and term not in _BYNAME_FIXED)


def _plan_nodes(b: _Builder, late_raw: list, labels: dict, ref_raw: list = ()):
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
    elems: dict[str, list] = {}  # By Name node -> its element names, in wire order
    for w in list(late_raw) + list(ref_raw):  # (By Name elements wired by reference need showing too)
        for e in w:
            if _byname_elem(b, e):
                node, _, term = e.partition(".")
                if term not in elems.setdefault(node, []):
                    elems[node].append(term)
    for name, names in elems.items():
        n = b.nodes[name]
        given = n.spec.get("elements")
        if given is not None:
            if not (isinstance(given, list) and all(isinstance(x, str) for x in given)
                    and set(names) <= set(given)):
                raise SpecError(f"function {name!r}: \"elements\" must list every element it is wired by "
                                f"({names})")
            names = list(given)
        pnodes.append({"name": name, "out": b.out_index.setdefault(name, len(b.out_index)), "kind": "byname",
                       "function": _BYNAME[n.spec["function"].strip()], "items": names, "write": [],
                       # Build Waveform's InputCnt also counts its waveform input
                       "count": len(names) + (n.spec["function"].strip() == "Build Waveform"),
                       "control": None, "class": None, "method": None})
    if not late_raw:
        return pnodes, None, None
    ends = sorted({e.partition(".")[0] for w in late_raw for e in w})
    existing, refs = [], []
    for nm in ends:
        n = b.nodes[nm]
        if n.kind in ("shiftreg", "tunnel", "event"):
            raise SpecError(f"a Property/Invoke Node or a Bundle/Unbundle By Name element cannot be wired to "
                            f"{nm!r} (a {n.kind}) yet; wire it through a function or constant")
        existing.append({"name": nm, "kind": "existing" if n.kind in ("control", "indicator") else n.kind,
                         # a function's name decides how its terminals are named
                         "spec": {"function": n.spec["function"]} if n.spec.get("function") else {}})
        refs.append(("in", n.bind, nm) if n.bind is not None
                    else ("out", b.out_index.setdefault(nm, len(b.out_index)), nm))
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
            inside = {m for m in b.nodes if b.event_frame(m) == fid}
            fw, cross = [], []
            for w in frame_wires.get(fid, []):
                outer = [e for e in w if e.partition(".")[0] not in inside
                         and (b.nodes[e.partition(".")[0]].kind in ("shiftreg", "tunnel")
                              or (b.nodes[e.partition(".")[0]].kind in _NODE_KINDS
                                  and not b.event_frame(e.partition(".")[0])))]
                if not outer:
                    fw.append(w)
                    continue
                if len(outer) == 2:
                    raise SpecError(f"wire {list(w)!r}: neither end is in event frame {fid}")
                # a case's output tunnel: its inner terminal in the case frame holding this structure
                cross.append([("frame", e) if e not in outer else
                              ("tun", b.out_index.setdefault(e, len(b.out_index)),
                               b.out_index.setdefault(name, len(b.out_index)))
                              if b.nodes[e.partition(".")[0]].kind == "tunnel" else
                              _ref_end(b, e, labels, w, False, j) for j, e in enumerate(w)])
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
            ctl = fr.get("control")
            ev_nodes = {m: {"role": b.nodes[m].spec["role"],
                            "items": sorted({e.partition(".")[2] for w in fw for e in w
                                             if e.partition(".")[0] == m})}
                        for m in b.nodes if b.nodes[m].kind == "evdata" and b.nodes[m].container == fid}
            # NI's practice: a front-panel terminal used by one event case sits in that case, so a
            # control is read when the event happens (not once before the loop) and an indicator is
            # written there. A button's own Value Change case reads it even when it is not wired (a
            # latched button resets only when its terminal is read).
            other = outside_wired | {e for f2, w2 in frame_wires.items() if f2 != fid for e in ends(w2)}
            terms = [o for o in bind if b.nodes[o].kind in ("control", "indicator") and o not in other]
            if (ctl and b.nodes[ctl].kind == "control" and fr.get("event", "Value Change") == "Value Change"
                    and ctl not in other and ctl not in terms):
                terms.append(ctl)
            contents = {k: v for k, v in fr.items() if k in _CONTENT_KEYS}
            script = None
            # the frame's ends of the cross wires: its own objects (their references come back from
            # the frame's script), its event data node, or a front-panel terminal
            want = []
            for c in cross:
                for k, e in enumerate(c):
                    if e[0] != "frame":
                        continue
                    nm, _, term = e[1].partition(".")
                    kind = b.nodes[nm].kind
                    if kind == "evdata":
                        ev_nodes.setdefault(nm, {"role": b.nodes[nm].spec["role"], "items": []})
                        if term not in ev_nodes[nm]["items"]:
                            ev_nodes[nm]["items"] = sorted({*ev_nodes[nm]["items"], term})
                        c[k] = ("evdata", nm, term)
                    elif kind in ("control", "indicator"):
                        c[k] = ("label", labels[nm], "")
                    else:
                        if nm not in want:
                            want.append(nm)
                        c[k] = ("fout", nm, term)
            # "wires" of the frame: between objects outside it, drawn through the frame (a value the
            # frame passes on unchanged, e.g. a shift register's state), via the structure's tunnels
            through = []
            for w in fr.get("wires", []) or []:
                if not (isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, str) for x in w)):
                    raise SpecError(f"event {name!r} frame {i}: \"wires\" entries are [\"from\", \"to\"]")
                for e in w:
                    if e.partition(".")[0] not in b.nodes or e.partition(".")[0] in inside:
                        raise SpecError(f"event {name!r} frame {i}: \"wires\" joins objects outside the "
                                        f"frame through it; {e!r} is not one")
                through.append(tuple(_ref_end(b, e, labels, w, False, j) for j, e in enumerate(w)))
            if fw or any(contents.values()) or want:
                script = build({**contents, "wires": fw},
                               existing=[{"name": o, "kind": _FRAME_BIND[b.nodes[o].kind]} for o in bind]
                               + [{"name": m, "kind": "function"} for m in ev_nodes],
                               origin=(40, 40), edit=True, refs=tuple(want))
                for c in cross:
                    for k, e in enumerate(c):
                        if e[0] == "fout":
                            c[k] = ("fout", script.outs.index(e[1]), e[2])
                if script.events:
                    raise SpecError(f"event {name!r} frame {i}: an event structure inside an event "
                                    "frame is not supported")
            ev = fr.get("event", "Value Change")
            frames.append({"control": labels[ctl] if ctl else None,
                           "event": CONTROL_EVENTS[ev] if ctl else VI_EVENTS[ev], "event_name": ev,
                           "more": [(labels[m["control"]], CONTROL_EVENTS[m.get("event", "Value Change")])
                                    if m.get("control") else (None, VI_EVENTS[m["event"]])
                                    for m in fr.get("more", [])],
                           "move": [labels[t] for t in terms], "bind": bind + list(ev_nodes),
                           "evdata": ev_nodes, "script": script, "cross": cross, "through": through})
        entry = {"name": name, "frames": frames}
        tmo = next((fr for fr in n.spec["events"] if fr.get("event") == "Timeout"), None)
        if tmo is not None:  # wired to the structure's Timeout terminal once it exists
            entry["timeout"] = tmo["timeout"]
        events.append(entry)
    return events
