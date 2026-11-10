"""Read a VI's block diagram as a graph.

The helper VI `vis/lvmcp_dfir.vi` calls LabVIEW's private VI method "Compile To DFIR CPP", which
returns the VI's dataflow intermediate representation (DFIR) as C++ test-construction code:
node creation statements, nested diagrams of structures, wires and a connectivity table, plus a
byte blob with type descriptors and constant values. This module turns that text into JSON.
"""
from __future__ import annotations

import re
import struct
from typing import Any

# ---- the data blob ---------------------------------------------------------------------------


def c_string_bytes(s: str) -> bytes:
    """Decode the body of C string literals (octal and simple escapes) to bytes."""
    out = bytearray()
    i, n = 0, len(s)
    esc = {"n": 10, "t": 9, "r": 13, "\\": 92, '"': 34, "'": 39, "?": 63, "a": 7, "b": 8, "f": 12, "v": 11}
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            d = s[i + 1]
            if d in "01234567":
                j = i + 1
                while j < n and j < i + 4 and s[j] in "01234567":
                    j += 1
                out.append(int(s[i + 1:j], 8) & 0xFF)
                i = j
                continue
            out.append(esc.get(d, ord(d) & 0xFF))
            i += 2
            continue
        out.append(ord(c) & 0xFF)
        i += 1
    return bytes(out)


_NUM = {0x01: ("i8", "b", 1), 0x02: ("i16", "h", 2), 0x03: ("i32", "i", 4), 0x04: ("i64", "q", 8),
        0x05: ("u8", "B", 1), 0x06: ("u16", "H", 2), 0x07: ("u32", "I", 4), 0x08: ("u64", "Q", 8),
        0x09: ("sgl", "f", 4), 0x0A: ("dbl", "d", 8)}
_ENUM = {0x15: ("B", 1), 0x16: ("H", 2), 0x17: ("I", 4)}  # U8 / U16 / U32 enums


def _text(raw: bytes) -> str:
    for enc in ("mbcs", "utf-8", "latin-1"):  # LabVIEW strings use the ANSI code page
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


class TypeTable:
    """Little-endian type-descriptor table: u32 count, the descriptors, u16 root count, u16 roots."""

    def __init__(self, b: bytes):
        self.tds: list[dict] = []
        n = struct.unpack_from("<I", b, 0)[0] if len(b) >= 4 else 0
        p = 4
        for _ in range(n):
            if p + 4 > len(b):
                break
            size, code, flags = struct.unpack_from("<HBB", b, p)
            body = b[p + 4:p + max(size, 4)]
            self.tds.append(self._parse(code, flags, body))
            p += max(size, 4)
        self.roots: list[int] = []
        if p + 2 <= len(b):
            k = struct.unpack_from("<H", b, p)[0]
            self.roots = [struct.unpack_from("<H", b, p + 2 + 2 * i)[0] for i in range(k)
                          if p + 4 + 2 * i <= len(b)]
        self.end = p

    @staticmethod
    def _name(body: bytes, at: int) -> str:
        if at < len(body):
            ln = body[at]
            return body[at + 1:at + 1 + ln].decode("utf-8", "replace")
        return ""

    def _parse(self, code: int, flags: int, body: bytes) -> dict:
        td: dict[str, Any] = {"code": code}
        named = bool(flags & 0x40)
        at = None
        if code in _NUM:
            td["kind"], at = _NUM[code][0], 1
        elif code == 0x21:
            td["kind"], at = "bool", 0
        elif code == 0x30:
            td["kind"], at = "string", 4
        elif code == 0x32:
            td["kind"], at = "path", 4
        elif code == 0x40 and len(body) >= 2:
            dims = struct.unpack_from("<H", body, 0)[0]
            at = 2 + 4 * dims + 2
            td.update(kind="array", dims=dims, elem=struct.unpack_from("<H", body, 2 + 4 * dims)[0]
                      if len(body) >= at else 0)
        elif code == 0x50 and len(body) >= 2:
            k = struct.unpack_from("<H", body, 0)[0]
            at = 2 + 2 * k
            td.update(kind="cluster", fields=[struct.unpack_from("<H", body, 2 + 2 * i)[0] for i in range(k)])
        elif code in _ENUM and len(body) >= 2:  # enum: item count, Pascal-string items
            k = struct.unpack_from("<H", body, 0)[0]
            at, items = 2, []
            for _ in range(k):
                if at >= len(body):
                    break
                ln = body[at]
                items.append(_text(body[at + 1:at + 1 + ln]))
                at += 1 + ln
            if at + 1 < len(body) and body[at] == 0:  # a pad byte before the name
                at += 1
            td.update(kind="enum", items=items)
        else:
            td["kind"] = f"type_0x{code:02x}"
            td["raw"] = body.hex()
        td["name"] = self._name(body, at) if named and at is not None else ""
        return td

    def describe(self, i: int, depth: int = 0) -> Any:
        if i >= len(self.tds) or depth > 20:
            return "?"
        td = self.tds[i]
        k = td["kind"]
        if k == "array":
            if not 1 <= td["dims"] <= 64:  # a misread descriptor
                return "?"
            inner = self.describe(td["elem"], depth + 1)
            return (inner + "[]" * td["dims"]) if isinstance(inner, str) else {"array": inner, "dims": td["dims"]}
        if k == "cluster":
            names = [self.tds[f]["name"] for f in td["fields"] if f < len(self.tds)]
            if names == ["status", "code", "source"]:
                return "error"
            return {"cluster": [{"name": self.tds[f]["name"], "type": self.describe(f, depth + 1)}
                                for f in td["fields"] if f < len(self.tds)]}
        if k == "enum":
            return {"enum": td["items"]}
        if td["code"] == 0x70 and td.get("raw"):
            return refnum_type(bytes.fromhex(td["raw"]), "<")
        return k

    def root(self) -> int | None:
        return self.roots[0] if self.roots else (len(self.tds) - 1 if self.tds else None)

    def unflatten(self, i: int, b: bytes, p: int = 0, depth: int = 0):
        """Native (little-endian) data -> Python value. Returns (value, new position)."""
        td = self.tds[i]
        k = td["kind"]
        if td["code"] in _NUM:
            _, fmt, size = _NUM[td["code"]]
            return struct.unpack_from("<" + fmt, b, p)[0], p + size
        if k == "bool":
            return bool(b[p]), p + 1
        if k == "enum":
            fmt, size = _ENUM[td["code"]]
            v = struct.unpack_from("<" + fmt, b, p)[0]
            return (td["items"][v] if v < len(td["items"]) else v), p + size
        if k == "string":
            n = struct.unpack_from("<i", b, p)[0]
            raw = b[p + 4:p + 4 + n]
            for enc in ("mbcs", "utf-8", "latin-1"):  # LabVIEW strings use the ANSI code page
                try:
                    return raw.decode(enc), p + 4 + n
                except UnicodeDecodeError:
                    continue
        if k == "array" and depth < 20:
            dims = [struct.unpack_from("<i", b, p + 4 * j)[0] for j in range(td["dims"])]
            p += 4 * td["dims"]
            total = 1
            for d in dims:
                total *= d
            flat = []
            for _ in range(total):
                v, p = self.unflatten(td["elem"], b, p, depth + 1)
                flat.append(v)
            for d in reversed(dims[1:]):
                flat = [flat[j:j + d] for j in range(0, len(flat), d)]
            return flat, p
        if k == "cluster" and depth < 20:
            vals = []
            for f in td["fields"]:
                v, p = self.unflatten(f, b, p, depth + 1)
                vals.append(v)
            return vals, p
        raise ValueError(f"cannot decode {k}")


# ---- the C++ text ------------------------------------------------------------------------------

_DECL = re.compile(r"DFIR::(\w+)& (\w+) = (.*?);(?:\s*\w+\.SetSourceID\((\d+)\);)?\s*$")
_CREATE = re.compile(r"DFIR::(\w+)::Create\((\w+)(.*)\)$", re.S)
_TDR = re.compile(r"MakeTDR\(\w+, (\d+), (\d+)\)")
_VALUE = re.compile(r"MakeValue\(\w+, (\d+), (\d+), (\d+)\)")
_CONN = re.compile(r"NodeConnectionSpecification\((?:&(\w+)|null), (\d+), (?:&(\w+)|null), (\d+), "
                   r"DFIR::TestUtil::MakeTDR\(\w+, (\d+), (\d+)\)\)")
_PATH = re.compile(r'PathRef\("((?:[^"\\]|\\.)*)"')

_STRUCT_KIND = {"ForLoop": "for_loop", "WhileLoop": "while_loop", "CaseStructure": "case",
                "FlatSequence": "flat_sequence", "EventStructure": "event_structure",
                "InPlaceElementStructure": "in_place", "DiagramDisableStructure": "disable_structure",
                "ConditionalDisableStructure": "conditional_disable", "TimedLoop": "timed_loop"}
_PART = {"LoopIndex": "i", "LoopMax": "N", "LoopCondition": "stop", "CaseSelector": "selector"}


# Nodes whose DFIR class is not "<Palette Name>Primitive".
_NODE_NAMES = {"ConcatenateNode": "Concatenate Strings", "BuildArrayNode": "Build Array",
               "UnbundleNode": "Unbundle", "BundleNode": "Bundle", "PrintfNode": "Format Into String",
               "UnbundleByNameNode": "Unbundle By Name", "BundleByNameNode": "Bundle By Name",
               "IndexArrayNode": "Index Array", "ScanfNode": "Scan From String",
               "CompoundArithmeticNode": "Compound Arithmetic", "MergeErrorsNode": "Merge Errors",
               "ArrayIndexNode": "Index Array", "DeleteArrayNode": "Delete From Array",
               "ReplaceArrayNode": "Replace Array Subset", "ArraySubsetNode": "Array Subset",
               "InsertIntoArrayNode": "Insert Into Array", "InitArrayNode": "Initialize Array", "RandomNumber0To1Primitive": "Random Number (0-1)",
               "GetDateOrTimeInSecondsPrimitive": "Get Date/Time In Seconds"}
# Array nodes whose DFIR terminals are only numbered: index -> terminal name (1-D forms, measured
# on LabVIEW 2026 by wiring each terminal of a scripted node to its own control or indicator)
_INDEXED_TERMS = {
    "ArrayIndexNode": ["array", "element", "index"],
    "InitArrayNode": ["element", "initialized array", "dimension size"],
    "ArraySubsetNode": ["array", "subarray", "index", "length"],
    "ReplaceArrayNode": ["array", "output array", "index", "new element/subarray"],
    "DeleteArrayNode": ["array", "length", "array w/ subset deleted", "deleted portion", "index"],
    "ReshapeArrayNode": ["array", "output array", "dimension size"],
}
# Growable nodes: DFIR terminal i is what vi_create calls element[i].
_GROWABLE = {"ConcatenateNode", "BuildArrayNode", "UnbundleNode", "BundleNode"}
# DFIR terminal 0 of a growable node (the elements follow; Bundle: cluster, output cluster, elements)
_GROWABLE_T0 = {"ConcatenateNode": "concatenated string", "BuildArrayNode": "appended array",
                "UnbundleNode": "cluster"}
_PRINTF = ["format string", "initial string", "resulting string", "error in", "error out"]


_REF_KINDS = {0x12: "queue", 0x19: "user event", 0x1e: "lvclass", 0x13: "notifier", 0x0e: "occurrence",
              0x04: "file", 0x16: "data value reference", 0x1a: "event registration"}


def refnum_type(body: bytes, endian: str):
    """A refnum type descriptor body -> "ref:<class>" for VI Server references, else a description
    such as {"refnum": "lvclass", "class": "Person.lvclass"} (not creatable yet)."""
    import struct as _st
    if len(body) < 2:
        return "type_0x70"
    kind = _st.unpack_from(endian + "H", body, 0)[0]
    if kind == 0x08 and len(body) >= 4:
        cid = _st.unpack_from(endian + "H", body, 2)[0]
        from .gen import VI_CLASSES
        name = next((c for c, i in VI_CLASSES.items() if i == cid), None)
        return f"ref:{name}" if name else {"refnum": "VI Server", "class_id": cid}
    out = {"refnum": _REF_KINDS.get(kind, f"0x{kind:02x}")}
    if kind == 0x1e:
        names = [x.decode("utf-8", "replace") for x in re.findall(rb"[\x20-\x7e]{3,}?\.lvclass", body)]
        if names:
            out["class"] = names[0].lstrip(" \x00")
    return out


def _short(ref: str) -> str:
    """controlRef_2 -> control_2, addPrimitiveRef_12 -> add_12."""
    m = re.match(r"(\w+?)(?:Primitive)?Ref_(\d+)$", ref)
    if not m:
        return ref
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", m.group(1)).lower()
    return f"{base}_{m.group(2)}"


def _palette_name(cls: str, catalog: dict[str, list[str]]) -> str | None:
    base = cls[:-len("Primitive")] if cls.endswith("Primitive") else cls
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[a-z])(?=[0-9])", " ", base)
    cands = [words, words.replace(" Or ", "/"), words.replace("Fixed Point", "Fixed-Point")]
    if words.startswith("Is "):
        cands.append(words[3:] + "?")
    cands += [c.replace(" And ", " & ") for c in list(cands)]
    low = {k.lower(): k for k in catalog}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    # ignoring spaces and punctuation: TDMSRead -> "TDMS Read", Transpose2DArray -> "Transpose 2D Array"
    flat = {re.sub(r"[^a-z0-9]", "", k.lower()): k for k in catalog}
    for c in (base, base[2:] if base.startswith("Is") else base):
        k = flat.get(re.sub(r"[^a-z0-9]", "", c.lower()))
        if k:
            return k
    for c in cands:  # palette names with a unit suffix, e.g. WaitPrimitive -> "Wait (ms)"
        hits = sorted(k for k in catalog if k.lower().startswith(c.lower() + " ("))
        if len(hits) == 1:
            return hits[0]
    return None


def parse(text: str, catalog: dict[str, list[str]] | None = None, subvi_terms=None) -> dict:
    """subvi_terms(path) -> the subVI's connector-pane labels in DFIR terminal order, or None."""
    catalog = catalog or {}
    m = re.search(r"const char \w+\[\] =\s*(.*?\");\s", text, re.S)
    blob = c_string_bytes("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))) if m else b""

    def tdr(o, ln):
        try:
            t = TypeTable(blob[int(o):int(o) + int(ln)])
            r = t.root()
            return t, r
        except (struct.error, IndexError):
            return None, None

    def type_of(o, ln):
        t, r = tdr(o, ln)
        return t.describe(r) if t is not None and r is not None else "?"

    body = text[text.find("DFIR::VirtualInstrument&"):] if "DFIR::VirtualInstrument&" in text else text
    # Join statements that span lines (SubVI::Create has a multi-line LinkIdentity).
    stmts, buf = [], ""
    for line in body.splitlines():
        s = line.strip()
        if not s or s in ("{", "}"):
            continue
        buf = (buf + " " + s).strip() if buf else s
        if buf.count("(") <= buf.count(")") and buf.endswith(";"):
            stmts.append(buf)
            buf = ""

    diagrams: dict[str, str] = {}       # diagramRef -> container id ("" = top level)
    nodes: dict[str, dict] = {}         # ref -> node
    order: list[str] = []
    structures: dict[str, dict] = {}
    wires: dict[str, dict] = {}
    conns = []
    case_ranges: dict[str, list] = {}   # case ref -> [(value, diagram index)]
    case_default: dict[str, int] = {}
    selector_owner: dict[str, str] = {}
    border: dict[str, list[str]] = {}    # structure ref -> tunnels / shift registers, creation order

    for st in stmts:
        cm = _CONN.search(st)
        if cm:
            conns.append(cm.groups())
            continue
        d = _DECL.match(st)
        if d:
            cls, ref, expr, _sid = d.groups()
            gm = re.match(r"(\w+)\.GetDiagrams\(\)\[(\d+)\]", expr)
            if cls == "Diagram" and gm:
                owner, idx = gm.groups()
                diagrams[ref] = "" if owner not in nodes else f"{_short(owner)}#{idx}" \
                    if nodes[owner]["kind"] == "case" else _short(owner)
                continue
            cr = _CREATE.match(expr)
            if cr:
                ccls, diag, rest = cr.groups()
                if ccls == "VirtualInstrument":
                    continue
                node = {"id": _short(ref), "class": ccls, "in": diagrams.get(diag, "")}
                if _sid:
                    # The object's UID in LabVIEW (for constants, the UID of its data object).
                    node["uid"] = int(_sid)
                if ccls == "Wire":
                    wires[ref] = {"in": node["in"], "uid": node.get("uid")}
                    continue
                if ccls in ("Control", "Indicator"):
                    node["kind"] = ccls.lower()
                    vm = _VALUE.search(rest)
                    if vm:
                        t, r = tdr(vm.group(1), vm.group(2))
                        if t is not None and r is not None:
                            node["type"] = t.describe(r)
                            node["label"] = t.tds[r]["name"]
                    nums = re.findall(r",\s*(-?\d+)", rest.split(")", 1)[-1])
                    if len(nums) >= 2:
                        node["conpane_index"] = int(nums[1])
                elif ccls == "Constant":
                    node["kind"] = "constant"
                    vm = _VALUE.search(rest)
                    if vm:
                        o, ln, dl = (int(x) for x in vm.groups())
                        t, r = tdr(o, ln)
                        if t is not None and r is not None:
                            node["type"] = t.describe(r)
                            try:
                                node["value"] = t.unflatten(r, blob[o + ln:o + ln + dl])[0]
                            except (ValueError, struct.error, IndexError):
                                pass
                elif ccls in _STRUCT_KIND:
                    node["kind"] = _STRUCT_KIND[ccls]
                    if "LoopCondition::" in rest:
                        node["condition"] = re.search(r"LoopCondition::(\w+)", rest).group(1)
                    structures[ref] = node
                elif ccls == "SubVI" or "SubVI" in ccls or ccls == "PolyVI":
                    node["kind"] = "subvi"
                    paths = [m.replace("\\\\", "\\") for m in _PATH.findall(rest)]
                    if paths:
                        node["path"] = paths[0]  # a polymorphic VI's node: the instance it calls
                    if ccls == "PolyVI" and len(paths) > 1:
                        node["polymorphic"] = paths[1]
                else:
                    node["kind"] = "function"
                    name = _NODE_NAMES.get(ccls) or _palette_name(ccls, catalog)
                    node["function"] = name or ccls
                    if name:
                        node["_labels"] = [x for x in catalog.get(name, []) if x != "..."]
                nodes[ref] = node
                order.append(ref)
                continue
            # attached parts: loop index / count / condition, case selector, tunnels, shift registers
            om = re.match(r"(\w+)\.(\w+)\((.*)\)$", expr)
            if om:
                owner, meth, args = om.groups()
                own = nodes.get(owner) or nodes.get(selector_owner.get(owner, ""), {})
                base = {"in": own.get("in", ""), "structure": own.get("id", owner)}
                if meth.startswith("Get") and meth[3:] in _PART:
                    nodes[ref] = {"id": f"{own.get('id', owner)}.{_PART[meth[3:]]}", "kind": "structure_terminal", **base}
                    if meth == "GetCaseSelector":
                        selector_owner[ref] = owner
                elif meth == "CreateTunnel":
                    dm = re.search(r"Terminal::(\w+), DFIR::Tunnel::Mode::(\w+)", args)
                    tt = _TDR.findall(args)
                    border.setdefault(owner, []).append(ref)
                    nodes[ref] = {"id": _short(ref), "kind": "tunnel", **base,
                                  "direction": dm.group(1).lower() if dm else "?",
                                  "mode": dm.group(2) if dm else "?",
                                  "type": type_of(*tt[0]) if tt else "?"}
                    order.append(ref)
                elif meth == "CreateShiftRegister":
                    tt = _TDR.findall(args)
                    # a stacked shift register is one left node with N elements: its terminals are the
                    # N outer (initial value) ones, then the N inner ones; the loop has one per element
                    cm = re.search(r"\),\s*(\d+)\s*$", args)
                    count = int(cm.group(1)) if cm else 1
                    for k in range(count):
                        border.setdefault(owner, []).append(ref if k == 0 else f"{ref}#{k + 1}")
                    nodes[ref] = {"id": _short(ref), "kind": "shift_register_left", "_owner": owner, **base,
                                  "type": type_of(*tt[0]) if tt else "?"}
                    if count > 1:
                        nodes[ref]["elements"] = count
                    order.append(ref)
                elif meth == "GetRightShiftRegister":
                    left = nodes.get(owner, {})
                    border.setdefault(left.get("_owner", ""), []).append(ref)
                    nodes[ref] = {"id": _short(ref), "kind": "shift_register_right", "in": left.get("in", ""),
                                  "structure": left.get("structure", ""), "pair": left.get("id", "")}
                    order.append(ref)
            continue
        # bare calls on the case selector
        rm = re.match(r"(\w+)\.CreateRange\((.*), (\d+)\);$", st)
        if rm:
            case_ranges.setdefault(selector_owner.get(rm.group(1), rm.group(1)), []).append(
                (rm.group(2), int(rm.group(3))))
            continue
        dm = re.match(r"(\w+)\.SetDefaultDiagramIndex\((\d+)\);$", st)
        if dm:
            case_default[selector_owner.get(dm.group(1), dm.group(1))] = int(dm.group(2))

    # case frames
    for ref, node in structures.items():
        if node["kind"] == "case":
            frames: dict[int, dict] = {}
            for val, idx in case_ranges.get(ref, []):
                frames.setdefault(idx, {"index": idx, "values": []})["values"].append(val)
            if ref in case_default:
                frames.setdefault(case_default[ref], {"index": case_default[ref], "values": []})["default"] = True
            n_frames = max([i for i in frames] + [case_default.get(ref, -1), 0]) + 1
            node["frames"] = [frames.get(i, {"index": i, "values": []}) for i in range(n_frames)]

    # wires: each wire gets one driver and its sinks
    sub_cache: dict[str, Any] = {}

    def term_name(ref, t):
        n = nodes.get(ref)
        if n is None:
            return f"{_short(ref)}[{t}]"
        k = n["kind"]
        labels = n.get("_labels")
        if labels and n.get("class", "").endswith("Primitive") and t < len(labels):
            return f"{n['id']}.{labels[t]}"
        if n.get("class") in _GROWABLE:
            # vi_dump's element[k] is the k-th element terminal (1-based), as the scan reader names it
            if n["class"] == "BundleNode":
                return f"{n['id']}." + ("cluster" if t == 0 else "output cluster" if t == 1 else f"element[{t - 1}]")
            if t == 0:
                return f"{n['id']}." + _GROWABLE_T0[n["class"]]
            return f"{n['id']}.element[{t}]"
        if n.get("class") == "ArrayIndexNode" and n.get("_nterms", 0) > 3:
            # a grown Index Array (1-D): array, then element k and index k in turn
            return f"{n['id']}." + ("array" if t == 0 else f"element[{(t + 1) // 2}]" if t % 2 else f"index[{t // 2}]")
        cls = n.get("class")
        if cls in _INDEXED_TERMS and t < len(_INDEXED_TERMS[cls]) and n.get("_nterms", 0) <= len(_INDEXED_TERMS[cls]):
            return f"{n['id']}.{_INDEXED_TERMS[n['class']][t]}"
        if n.get("class") == "MergeErrorsNode":  # error out, then error in 1..n
            return f"{n['id']}." + ("error out" if t == 0 else f"error in[{t}]")
        if n.get("class") == "PrintfNode":
            return f"{n['id']}." + (_PRINTF[t] if t < len(_PRINTF) else f"input {t - len(_PRINTF) + 1}")
        if k in ("control", "indicator", "constant", "structure_terminal"):
            return n["id"]
        if ref in structures:
            # A structure's own terminals are the outer sides of its tunnels / shift registers, in
            # creation order, after one leading terminal on For Loops (N) and cases (selector).
            lead = {"for_loop": "N", "case": "selector"}.get(k)
            if lead and t == 0:
                return f"{n['id']}.{lead}"
            b = border.get(ref, [])
            j = t - (1 if lead else 0)
            if 0 <= j < len(b):
                r, _, k = b[j].partition("#")
                return f"{nodes[r]['id']}.outer" + (f"[{k}]" if k else "")
        if k == "tunnel":
            if t == 0:
                return f"{n['id']}.outer"
            frames = len(structures.get(_ref_of(n["structure"]), {}).get("frames", []))
            return f"{n['id']}.inner" + (f"[frame {t - 1}]" if frames > 1 else "")
        if k == "shift_register_left" and n.get("elements", 1) > 1:
            e = n["elements"]  # element 1 is "outer" / "inner", element k "outer[k]" / "inner[k]"
            side, j = ("outer", t + 1) if t < e else ("inner", t - e + 1)
            return f"{n['id']}.{side}" + (f"[{j}]" if j > 1 else "")
        if k in ("shift_register_left", "shift_register_right"):
            return f"{n['id']}." + ("outer" if t == 0 else "inner")
        if k == "subvi" and subvi_terms and n.get("path"):
            if n["path"] not in sub_cache:
                try:
                    sub_cache[n["path"]] = subvi_terms(n["path"])
                except Exception:  # noqa: BLE001 - names are a convenience
                    sub_cache[n["path"]] = None
            names = sub_cache[n["path"]]
            if names and t < len(names):
                return f"{n['id']}.{names[t]}"
        return f"{n['id']}[{t}]"

    def _ref_of(node_id):
        return next((r for r, v in nodes.items() if v.get("id") == node_id), "")

    for src, st_, dst, dt, o, ln in conns:  # highest terminal index wired per node (a grown node has more)
        for ref, t in ((src, st_), (dst, dt)):
            if ref in nodes:
                nodes[ref]["_nterms"] = max(nodes[ref].get("_nterms", 0), int(t) + 1)
    edges: dict[str, dict] = {}
    unwired = []
    for src, st_, dst, dt, o, ln in conns:
        st_, dt = int(st_), int(dt)
        if src in wires and dst:
            edges.setdefault(src, {"from": None, "to": [], "type": None, "uid": wires[src]["uid"]})["to"].append(
                term_name(dst, dt))
        elif dst in wires and src:
            e = edges.setdefault(dst, {"from": None, "to": [], "type": None, "uid": wires[dst]["uid"]})
            e["from"] = term_name(src, st_)
            e["type"] = type_of(o, ln)
        elif src and not dst:
            pass  # unwired output
        elif dst and not src:
            unwired.append(term_name(dst, dt))

    out_nodes = []
    for ref in order:
        n = dict(nodes[ref])
        n.pop("_labels", None)
        n.pop("_nterms", None)
        n.pop("_owner", None)
        n.pop("class", None) if n.get("kind") in ("control", "indicator", "constant") else None
        if not n.get("in"):
            n.pop("in", None)
        out_nodes.append(n)
    # A case driven by an enum: show frame values as item names, not item numbers.
    for n in out_nodes:
        if n.get("kind") != "case":
            continue
        sel = next((e["type"] for e in edges.values()
                    if f"{n['id']}.selector" in e["to"] and isinstance(e["type"], dict) and "enum" in e["type"]), None)
        if sel:
            items = sel["enum"]
            for fr in n.get("frames", []):
                fr["values"] = [items[int(v)] if v.lstrip("-").isdigit() and 0 <= int(v) < len(items) else v
                                for v in fr["values"]]
    return {
        "nodes": out_nodes,
        "wires": [e for e in edges.values() if e["from"] or e["to"]],
        "unwired_inputs": unwired,
    }
