"""vi_dump graph -> vi_create spec, and a structural comparison of two dumps.

Used by tools/measure.py to rebuild existing VIs and see what is missing. to_spec() never guesses
silently: anything it cannot express is returned in `unsupported` (short, countable reasons such as
"function: PolyVI" or "case: several values in one frame"), and the caller does not build the VI.
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

from . import gen

# dump function names that are LabVIEW class names rather than palette names
FUNCTION_ALIASES = {"ArrayIndexNode": "Index Array"}
# front-panel classes whose look is not given by the data type -> a Controls palette name (vi_create
# "style"); the plain ones (Digital, Boolean, String, Cluster, Array, Enum, Path) need none
CONTROL_STYLES = {"Slide": "Vertical Fill Slide", "XYGraph": "XY Graph", "Ring": "Text Ring",
                  "IntensityGraph": "Intensity Graph", "WaveformGraph": "Waveform Graph",
                  "WaveformChart": "Waveform Chart", "ComboBox": "Combo Box", "Table": "Table",
                  "ListBox": "Listbox", "MulticolumnListbox": "Multicolumn Listbox", "Knob": "Knob",
                  "Scrollbar": "Vertical Scrollbar", "TabControl": "Tab Control", "TreeControl": "Tree"}
_STRUCT = {"for_loop": "for_loop", "while_loop": "while_loop", "case": "case", "event_structure": "event"}
# an event frame's own nodes: they become the frame's "data" / "filter" entries
_EVENT_NODES = {"Event Data Node": "data", "Event Filter Node": "filter"}


class _Unsupported(Exception):
    pass


def _type(t, what: str):
    if t is None:
        raise _Unsupported(f"{what}: type unknown (scan dump)")
    if isinstance(t, str) and t.startswith("type_0x"):
        raise _Unsupported(f"{what}: type {t}")
    try:
        gen.parse_type(t)
    except gen.SpecError:
        raise _Unsupported(f"{what}: type {json.dumps(t)[:40]}") from None
    return t


def _flat_or_type(n: dict, what: str, features: set, need_value: bool = False):
    """The node's type; its exact copied type ("flat") when that cannot be written otherwise or a
    constant's value is unknown."""
    try:
        t = _type(n.get("type"), what)
        if not need_value or "value" in n:
            return t
        if not n.get("flat"):
            raise _Unsupported(f"{what}: value unknown")
    except _Unsupported:
        if not n.get("flat"):
            raise
    features.add("copied type")
    return n["flat"]


def _safe_ids(g: dict) -> dict:
    """vi_dump ids made from labels can start with a digit ("1d_anova_215"); spec names cannot."""
    bad = {n["id"] for n in g["nodes"] if not re.match(r"[A-Za-z_]", n["id"])}
    if not bad:
        return g
    fix = lambda x: f"n_{x}" if x in bad else x  # noqa: E731

    def end(e):
        if not e:
            return e
        node, dot, term = e.partition(".")
        m = re.match(r"(.*?)(\[.*\])$", node)
        if m and m.group(1) in bad:
            return f"n_{node}{dot}{term}"
        return f"{fix(node)}{dot}{term}"
    nodes = []
    for n in g["nodes"]:
        n = dict(n, id=fix(n["id"]))
        for k in ("in", "structure", "pair"):
            if isinstance(n.get(k), str):
                sid, hash_, fr = n[k].partition("#")
                n[k] = fix(sid) + hash_ + fr
        nodes.append(n)
    wires = [dict(w, **{"from": end(w.get("from")), "to": [end(t) for t in w["to"]]}) for w in g["wires"]]
    return {**g, "nodes": nodes, "wires": wires}


def _end(e: str) -> tuple[str, str]:
    node, _, term = e.partition(".")
    return node, term


def to_spec(g: dict, catalog: dict | None = None) -> tuple[dict, list[str], list[str]]:
    """Returns (spec, unsupported reasons, features used)."""
    g = _safe_ids(g)
    nodes = {n["id"]: n for n in g["nodes"]}
    unsupported: list[str] = []
    features: set[str] = set()

    def bad(reason):
        unsupported.append(reason)

    # -- containers: "" top level, a loop id, or "<case id>#<frame index>"
    contents: dict[str, dict] = collections.defaultdict(lambda: collections.defaultdict(list))
    tunnels, srs = {}, {}
    controls, indicators = [], []
    ev_nodes: dict[str, dict] = collections.defaultdict(dict)  # event frame -> {"data": id, "filter": id}
    for n in g["nodes"]:
        k, nid, where = n["kind"], n["id"], n.get("in", "")
        try:
            if k in ("control", "indicator"):
                features.add(k)
                c = {"name": nid, "type": _flat_or_type(n, k, features)}
                if n.get("label"):
                    c["label"] = n["label"]
                if n.get("vi_class") in CONTROL_STYLES:
                    c["style"] = CONTROL_STYLES[n["vi_class"]]
                    features.add("control style")
                if where:
                    features.add("terminal inside a structure")
                (controls if k == "control" else indicators).append(c)
            elif k == "constant":
                features.add("constant")
                t = _flat_or_type(n, "constant", features, need_value=True)
                c = {"name": nid, "type": t}
                if not (isinstance(t, dict) and "flat" in t):
                    c["value"] = n["value"]
                contents[where]["constants"].append(c)
            elif k == "function" and n.get("function") in _EVENT_NODES and "#" in where:
                ev_nodes[where][_EVENT_NODES[n["function"]]] = nid
            elif k == "function":
                fn = FUNCTION_ALIASES.get(n.get("function"), n.get("function"))
                if catalog is not None and fn not in catalog:
                    raise _Unsupported(f"function: {fn}")
                features.add("function")
                f = {"name": nid, "function": fn}
                if fn == "Static VI Reference" and n.get("path"):
                    features.add("static VI reference")
                    f["vi"] = n["path"]
                    if n.get("strict"):
                        f["strict"] = True
                if fn == "Build Array" and n.get("concatenate"):
                    features.add("concatenating Build Array")
                    f["concatenate"] = True
                contents[where]["functions"].append(f)
            elif k == "subvi":
                if not n.get("path"):
                    raise _Unsupported("subVI: path unknown")
                features.add("subVI")
                contents[where]["subvis"].append({"name": nid, "path": n["path"]})
            elif k in _STRUCT:
                features.add(k)
                st = {"name": nid, "type": _STRUCT[k]}
                if k == "while_loop" and n.get("condition") == "ContinueIfTrue":
                    st["stop_if_true"] = False
                if k in ("case", "event_structure"):
                    st["_frames"] = n.get("frames", [])
                contents[where]["structures"].append(st)
            elif k in ("property_node", "invoke_node"):
                contents[where]["property_nodes" if k == "property_node" else "invoke_nodes"].append(
                    _pnode(n, g["nodes"]))
                features.add(k.replace("_", " "))
            elif k == "tunnel":
                tunnels[nid] = n
            elif k in ("shift_register_left", "shift_register_right"):
                features.add("shift register")
                srs[nid] = n
            else:
                raise _Unsupported(f"node: {k}" + (f" ({n['class']})" if n.get("class") else ""))
        except _Unsupported as e:
            bad(str(e))

    # -- a class's private data is unbundled / bundled only by VIs of that class
    fn_of = {n["id"]: n.get("function") for n in g["nodes"] if n["kind"] == "function"}
    for w in g["wires"]:
        if "lvclass" not in json.dumps(w.get("type")):
            continue
        ins = [fn_of.get(_end(t)[0].split("[")[0]) for t in w["to"]]
        out = fn_of.get(_end(w.get("from") or "")[0].split("[")[0])
        if any(f in ("Unbundle", "Unbundle By Name") for f in ins) or out in ("Bundle", "Bundle By Name"):
            bad("class private data (Bundle/Unbundle of a class: the VI must belong to the class)")
            break

    # -- shift registers: one name per left/right pair; a right with several lefts is a stacked one
    sr_name = {}
    lefts_of = collections.Counter(n.get("pair") for n in srs.values() if n["kind"] == "shift_register_right")
    for nid, n in srs.items():
        if n["kind"] == "shift_register_right":
            if not n.get("pair") or n["pair"] not in srs:
                bad("shift register: unpaired")
                continue
            sr_name[nid] = sr_name[n["pair"]] = f"sr_{n['pair']}"
    if any(v > 1 for v in lefts_of.values()):
        bad("shift register: stacked")
    for nid, n in srs.items():
        if n["kind"] == "shift_register_left" and nid not in sr_name:
            bad("shift register: stacked")

    # -- wires as (source end, sink end) pairs, then through tunnels
    edges = [(w["from"], s) for w in g["wires"] for s in w["to"] if w.get("from") and s]
    sel_wire_type = {s[:-len(".selector")]: w.get("type") for w in g["wires"] for s in w["to"]
                     if s.endswith(".selector")}
    if any(not w.get("from") for w in g["wires"]):
        bad("wire: source unknown")
    def direction(tid, t):  # the scan reader gives none: an input tunnel is wired into on the outside
        if t.get("direction"):
            return t["direction"]
        if any(b == f"{tid}.outer" for _, b in edges) or any(a.startswith(f"{tid}.inner") for a, _ in edges):
            return "input"
        return "output"

    case_out, timeouts = {}, {}
    const_value = {n["id"]: n.get("value") for n in g["nodes"] if n["kind"] == "constant"}
    for n in g["nodes"]:  # an integer constant the scan saw only as flattened data
        if n["kind"] == "constant" and n.get("value") is None and n.get("type") in ("i8", "i16", "i32", "i64") \
                and (n.get("flat") or {}).get("data"):
            const_value[n["id"]] = int.from_bytes(bytes.fromhex(n["flat"]["data"]), "big", signed=True)
    for tid, t in list(tunnels.items()):
        st = nodes.get(t.get("structure"), {})
        if st.get("kind") == "event_structure" and t.get("class") == "Tunnel":
            src = [a for a, b in edges if b == f"{tid}.outer"]
            for _ in range(5):  # the constant may sit outside the loop holding the event structure
                if len(src) == 1 and src[0].endswith(".inner") and src[0][:-6] in tunnels:
                    src = [a for a, b in edges if b == f"{src[0][:-6]}.outer"]
            if src and len(src) == 1 and src[0] in const_value and isinstance(const_value[src[0]], int):
                timeouts[st["id"]] = const_value[src[0]]
                edges = [(a, b) for a, b in edges if b != f"{tid}.outer"]
                for d in contents.values():  # its constant is made again from the terminal
                    d["constants"] = [c for c in d.get("constants", []) if c["name"] != src[0]]
            elif src:
                bad("event structure: timeout from something other than a numeric constant")
            if any(a.startswith(f"{tid}.inner") or b.startswith(f"{tid}.inner") for a, b in edges):
                bad("event structure: dynamic event terminal")
            del tunnels[tid]
            continue
        if st.get("kind") == "case" and direction(tid, t) == "output":
            case_out[tid] = f"t_{tid}"
        elif st.get("kind") not in ("for_loop", "while_loop", "case", "event_structure"):
            bad(f"tunnel of a {st.get('kind')}")
        if t.get("mode") and t["mode"] not in ("AutoIndexed", "LastValue", "Indexing", "Normal", ""):
            features.add(f"tunnel mode {t['mode']}")
        if t.get("mode") == "AutoIndexed":
            features.add("auto-indexing")

    def side(tid, t):  # (entry end, exit end) of a tunnel
        return (f"{tid}.outer", f"{tid}.inner") if direction(tid, t) == "input" else (f"{tid}.inner", f"{tid}.outer")

    # a value an event frame passes on unchanged (input tunnel -> output tunnel inside one frame)
    # becomes that frame's "wires" entry, between what feeds the one and what the other feeds
    ev_tun = {tid for tid, t in tunnels.items() if nodes.get(t.get("structure"), {}).get("kind") == "event_structure"}
    tun_frame = re.compile(r"([^.]+)\.inner\[frame (\d+)\]$")
    passes = []
    for a, b in edges:
        ma, mb = tun_frame.match(a), tun_frame.match(b)
        if ma and mb and ma.group(1) in ev_tun and mb.group(1) in ev_tun and ma.group(2) == mb.group(2):
            passes.append((ma.group(1), mb.group(1), tunnels[ma.group(1)]["structure"], int(ma.group(2))))
    edges = [(a, b) for a, b in edges if not any(a == f"{p[0]}.inner[frame {p[3]}]"
                                                 and b == f"{p[1]}.inner[frame {p[3]}]" for p in passes)]
    through = collections.defaultdict(list)  # (event structure, frame) -> [[from, to]]
    via: dict[tuple, set] = {}  # collapsed edge -> the tunnels it passes through
    for stage in ("other tunnels", "event tunnels"):
      for _ in range(20):  # collapse tunnels (nested structures chain them)
        changed = False
        for tid, t in tunnels.items():
            if tid in case_out or (stage == "other tunnels" and tid in ev_tun):
                continue
            entry, exit_ = side(tid, t)
            # an output tunnel of a multi-frame structure has one inner end per frame
            ins = [(a, b) for a, b in edges if b == entry or b.startswith(entry + "[")]
            # a case's input tunnel has one inner end per frame: "tunnel_3.inner[frame 1]"
            outs = [(a, b) for a, b in edges if a == exit_ or a.startswith(exit_ + "[")]
            if ins or outs:
                edges = [(a, b) for a, b in edges if b != entry and not b.startswith(entry + "[")
                         and a != exit_ and not a.startswith(exit_ + "[")]
                for ia in ins:
                    for ob in outs:
                        e = (ia[0], ob[1])
                        edges.append(e)
                        via[e] = via.get(e, set()) | via.get(ia, set()) | via.get(ob, set()) | {tid}
                changed = True
        if not changed:
            break
      if stage == "other tunnels":
        for ta, tb, es, k in passes:
            srcs = [a for a, b in edges if b == f"{ta}.outer"]
            sinks = [b for a, b in edges if a == f"{tb}.outer"]
            if len(srcs) != 1 or not sinks:
                bad("event structure: a value passed through a frame from or to nothing")
                continue
            try:
                for z in sinks:
                    through[(es, k)].append([_map_end(srcs[0], nodes, sr_name, case_out, "source"),
                                             _map_end(z, nodes, sr_name, case_out, "sink")])
            except _Unsupported as e:
                bad(str(e))
    # a case selector's value inside the frames ("<case>.selector (inside)", e.g. an error cluster
    # passed on): wire what feeds the selector into the frame instead (LabVIEW adds a tunnel)
    sel_src = {b: a for a, b in edges if b.endswith(".selector")}
    inside = [(a, b) for a, b in edges if a.endswith(".selector (inside)")]
    edges = [(a, b) for a, b in edges if not a.endswith(".selector (inside)")]
    for a, b in inside:
        src = sel_src.get(a[:-len(" (inside)")])
        if src is None:
            bad("case: selector value used inside, selector unwired")
        else:
            edges.append((src, b))
    def located(x: str) -> str:  # the diagram an end's node sits in ("case_3#1", a loop, or "")
        n = nodes.get(x, {})
        return n["structure"] if n.get("kind", "").startswith("shift_register") else n.get("in", "")

    def inside(loc: str, frame: str) -> bool:
        while loc:
            if loc == frame:
                return True
            loc = nodes.get(loc.split("#")[0], {}).get("in", "")
        return False

    wires = []
    wire_idx: dict[int, dict] = {}  # wires[i] -> {loop: indexing?} where it differs from LabVIEW's default
    case_pass = collections.defaultdict(list)  # (case, frame index) -> [[source, output tunnel]]
    for a, b in edges:
        try:
            src, dst = _map_end(a, nodes, sr_name, case_out, "source"), _map_end(b, nodes, sr_name, case_out, "sink")
        except _Unsupported as e:
            bad(str(e))
            continue
        m = re.match(r"([^.]+)\.inner\[frame (\d+)\]$", b)
        if m and m.group(1) in case_out and not inside(located(_end(a)[0]), f"{tunnels[m.group(1)]['structure']}#{m.group(2)}"):
            # a value from outside the frame, passed on unchanged to the output tunnel
            case_pass[(tunnels[m.group(1)]["structure"], int(m.group(2)))].append([src, dst])
            continue
        idx = _tunnel_indexing(via.get((a, b), ()), tunnels, nodes, direction)
        if idx:
            features.add("loop tunnel indexing set")
            wire_idx[len(wires)] = idx
        wires.append([src, dst])

    # -- growable nodes: size from the highest element[k] wired
    sizes = collections.defaultdict(int)
    grown_index = set()
    for a, b in wires:
        for e in (a, b):
            m = re.match(r"([^.]+)\.(?:element|value|string|error in)\[(\d+)\]$", e)
            if m:
                sizes[m.group(1)] = max(sizes[m.group(1)], int(m.group(2)))
            m = re.match(r"([^.]+)\.(?:element|index)\[(\d+)\]$", e)  # Index Array: size = groups + 2
            if m and fn_of.get(m.group(1)) in ("Index Array", "ArrayIndexNode"):
                grown_index.add(m.group(1))
                sizes[m.group(1)] = max(sizes[m.group(1)], int(m.group(2)) + 2)
                continue
            m = re.match(r"([^.]+)\.input (\d+)$", e)  # Format Into String: size = inputs + 3
            if m and fn_of.get(m.group(1)) == "Format Into String":
                sizes[m.group(1)] = max(sizes[m.group(1)], int(m.group(2)) + 3)
    # a grown Index Array: the dump's element[k] (k-th element output) is terminal 2k-1 of the node,
    # which a script calls element[2k-1]; index[k] stays (vi_create wires it by terminal reference)
    # Bundle: a script numbers its terminals output cluster 0, cluster 1, elements from 2
    def ia_end(e):
        m = re.match(r"([^.]+)\.element\[(\d+)\]$", e)
        if m and m.group(1) in grown_index:
            return f"{m.group(1)}.element[{2 * int(m.group(2)) - 1}]"
        if m and fn_of.get(m.group(1)) == "Bundle":
            return f"{m.group(1)}.element[{int(m.group(2)) + 1}]"
        return e
    wires = [[ia_end(a), ia_end(b)] for a, b in wires]
    for d in contents.values():
        for f in d.get("functions", []):
            if f["name"] in sizes:
                f["size"] = sizes[f["name"]]
                features.add("growable node")

    # front-panel labels -> terminal names, for the controls event frames name (labels must be unique)
    label_count = collections.Counter(c.get("label") for c in controls + indicators)
    label_id = {c["label"]: c["name"] for c in controls + indicators
                if c.get("label") and label_count[c["label"]] == 1}

    # -- assemble the nesting
    def fill(container: str) -> dict:
        d = {k: list(v) for k, v in contents.get(container, {}).items() if k != "structures"}
        sts = []
        for st in contents.get(container, {}).get("structures", []):
            st = dict(st)
            if st["type"] == "case":
                cases = []
                for fr in st.pop("_frames"):
                    entry = fill(f"{st['name']}#{fr['index']}")
                    value = _frame_value(fr, _selector_kind(st["name"], wires, nodes, sel_wire_type))
                    if value is not None:
                        entry["value"] = value
                        if isinstance(value, str) and (", " in value or ".." in value):
                            features.add("case: several values or a range")
                    elif not fr.get("default"):
                        bad("case: frame without a value")
                    if fr.get("default"):
                        entry["default"] = True
                    if case_pass.get((st["name"], fr["index"])):
                        entry["pass"] = case_pass[(st["name"], fr["index"])]
                        features.add("case: value passed through a frame")
                    cases.append(entry)
                st["cases"] = cases
                outs = sorted({n for w in wires + [p for v in case_pass.values() for p in v] for n in w
                               if n.startswith("t_") and nodes.get(n[2:], {}).get("structure") == st["name"]})
                if outs:
                    st["output_tunnels"] = outs
            elif st["type"] == "event":
                st["events"] = [_event_frame(fr, st["name"], fill(f"{st['name']}#{fr['index']}"),
                                             ev_nodes.get(f"{st['name']}#{fr['index']}", {}),
                                             timeouts.get(st["name"]), label_id, bad)
                                for fr in st.pop("_frames")]
                for k, entry in enumerate(st["events"]):
                    if through.get((st["name"], k)):
                        entry["wires"] = through[(st["name"], k)]
                features.add("event structure")
            else:
                st.update(fill(st["name"]))
                names = sorted({v for k, v in sr_name.items() if srs[k]["structure"] == st["name"]})
                if names:
                    st["shift_registers"] = names
            sts.append(st)
        if sts:
            d["structures"] = sts
        return d

    for c in controls + indicators:  # the connector pane is not compared; 12+ terminals would not fit
        c["connector"] = False
    top = fill("")  # (reads the wires as [from, to] pairs)
    spec = {"controls": controls, "indicators": indicators, **top,
            "wires": [{"from": w[0], "to": w[1], "indexing": wire_idx[i]} if i in wire_idx else w
                      for i, w in enumerate(wires)]}
    return spec, sorted(set(unsupported)), sorted(features)


def _pnode(n: dict, all_nodes) -> dict:
    """A dumped Property / Invoke Node as a vi_create property_nodes / invoke_nodes entry."""
    d = {"name": n["id"]}
    if n.get("control"):
        term = next((x["id"] for x in all_nodes if x["kind"] in ("control", "indicator")
                     and x.get("label") == n["control"]), None)
        if term is None:
            raise _Unsupported(f"{n['kind']}: linked control has no terminal")
        d["control"] = term
    elif n.get("vi_class"):
        d["class"] = n["vi_class"]
    else:
        raise _Unsupported(f"{n['kind']}: class unknown")
    if n["kind"] == "property_node":
        if not n.get("items"):
            raise _Unsupported("property_node: properties unknown")
        # LabVIEW's short names configure the node, a nested item keeps its dotted terminal name;
        # wires use the terminal names (vi_create maps them)
        short = n.get("properties") or n["items"]
        d["properties"] = [t if t in n.get("nested", []) else p for t, p in zip(n["items"], short)]
        w = [d["properties"][i] for i, x in enumerate(n["items"]) if x in n.get("write", [])
             and i < len(d["properties"])]
        if w:
            d["write"] = True if len(w) == len(d["properties"]) else w
    else:
        if not n.get("method"):
            raise _Unsupported("invoke_node: method unknown")
        d["method"] = n["method"]
    return d


def _map_end(e: str, nodes, sr_name, case_out, role: str) -> str:
    node, term = _end(e)
    if node in case_out:
        return case_out[node]
    if node in sr_name:
        left = nodes[node]["kind"] == "shift_register_left"
        m = re.search(r"\[(\d+)\]$", term)  # element k of a stacked shift register
        return f"{sr_name[node]}." + (f"left[{m.group(1)}]" if left and m else "left" if left else "right")
    n = nodes.get(node)
    if n is None:
        raise _Unsupported("wire: unknown end")
    if n["kind"] in ("control", "indicator", "constant"):
        return node
    if n["kind"] in ("property_node", "invoke_node"):
        if not term:
            raise _Unsupported(f"{n['kind']} terminal without a name")
        return f"{node}.{term}"
    if re.search(r"\[\d+\]$", node) or (not term and n["kind"] in ("function", "subvi")):
        raise _Unsupported(f"{n['kind']} terminal without a name")
    if n["kind"] == "subvi" and not term:
        raise _Unsupported("subVI terminal without a name")
    fn = FUNCTION_ALIASES.get(n.get("function"), n.get("function"))
    if n["kind"] == "function" and term == "element" and fn in gen._GROWABLE_FUNCS and fn != "Index Array":
        term = "element[1]"  # the scan reader numbers "element" only when there are several
    return f"{node}.{term}" if term else node


def _event_frame(fr: dict, es: str, entry: dict, own: dict, timeout, label_id: dict, bad) -> dict:
    """A dumped event frame (its events read from LabVIEW's caption) as a vi_create events entry."""
    from .gen import CONTROL_EVENTS, VI_EVENTS
    evs = []
    for e in fr.get("events") or []:
        if e.get("dynamic") is not None:
            bad(f"event: dynamic or user event ({e['event']})")
        elif e.get("control") is not None:
            if e["control"] not in label_id:
                bad("event: of an object that is not a control with a unique label")
            elif e["event"] not in CONTROL_EVENTS:
                bad(f"event: {e['event']}")
            else:
                evs.append({"control": label_id[e["control"]], "event": e["event"]})
        elif e["event"] not in VI_EVENTS:
            bad(f"event: {e['event']}")
        else:
            evs.append({"event": e["event"]})
    if not fr.get("events"):
        bad("event structure: frame events unknown")
    if not evs:
        return entry
    tmo = [e for e in evs if e["event"] == "Timeout"]
    filt = [e for e in evs if e["event"].endswith("?")]  # a filter node belongs to a filter event
    first = tmo[0] if tmo else filt[0] if filt and "filter" in own else evs[0]
    entry.update(first)
    more = [e for e in evs if e is not first]
    if tmo and more:
        bad("event structure: Timeout together with other events in one frame")
    elif more:
        entry["more"] = more
    if tmo:
        entry["timeout"] = timeout if timeout is not None else -1
    for role, nid in own.items():
        entry[role] = nid
    return entry


def _tunnel_indexing(tids, tunnels: dict, nodes: dict, direction) -> dict:
    """{loop: indexing?} for the loop tunnels a wire passes through whose mode is not the one LabVIEW
    gives a new tunnel: a For Loop indexes arrays in and values out, a While Loop does not."""
    out = {}
    for tid in tids:
        t = tunnels.get(tid) or {}
        loop = nodes.get(t.get("structure"), {})
        mode = t.get("mode")
        if loop.get("kind") == "for_loop" and mode == "LastValue":
            typ = t.get("type")
            arr = isinstance(typ, dict) and "array" in typ or isinstance(typ, str) and typ.endswith("[]")
            if direction(tid, t) == "output" or arr:
                out[t["structure"]] = False
        elif loop.get("kind") == "while_loop" and mode == "AutoIndexed":
            out[t["structure"]] = True
    return out


def _selector_kind(case: str, wires, nodes, wire_types: dict) -> str | None:
    """What a case selector takes: "bool", "error", "string", "enum" or None (a number / unknown).
    From the type of the wire into the selector, else the type of the control feeding it, else a
    source terminal named like LabVIEW's Boolean outputs ("x > y?") -- the DFIR reader cannot name
    the type of some polymorphic outputs (And, Or, Empty Array?)."""
    sel = next((a for a, b in wires if b == f"{case}.selector"), None)
    for t in (wire_types.get(case), nodes.get(sel.partition(".")[0], {}).get("type") if sel else None):
        if isinstance(t, dict) and "enum" in t:
            return "enum"
        if t in ("bool", "error", "string"):
            return t
        if isinstance(t, str) and t not in ("?",) and not t.startswith("type_"):
            return None
    if sel and sel.endswith("?"):
        return "bool"
    return None


def _frame_value(fr: dict, kind: str | None):
    """A case frame's selector value(s) in vi_create form: a number / bool / string for one value,
    else LabVIEW's case-name text ("1..3, 5", '"a", "b"'). From the scan reader's frame "name"
    (LabVIEW's own text) or the DFIR reader's "values" ("lo, hi" is a range)."""
    if fr.get("name") is not None:
        parts = [x.strip() for x in fr["name"].split(",") if x.strip() and x.strip() != "Default"]
        text = ", ".join(parts)
        if not parts:
            return None
        if len(parts) == 1 and ".." not in parts[0]:
            return _case_value(parts[0].strip('"'), kind)
        return text
    vals = fr.get("values", [])
    if not vals:
        return None
    is_str = kind == "string"
    if len(vals) == 1 and "," not in vals[0]:
        return _case_value(vals[0], kind)
    out = []
    for v in vals:
        lo, sep, hi = v.partition(", ")
        if not sep:
            out.append(f'"{v}"' if is_str else v)
            continue
        q = (lambda x: f'"{x}"') if is_str else (lambda x: x)
        if "NegInfinity" in hi:  # "x, NegInfinity" = everything up to x
            out.append(f"..{q(lo)}")
        elif "Infinity" in hi:  # x and up
            out.append(f"{q(lo)}..")
        else:
            out.append(f"{q(lo)}..{q(hi)}")
    return ", ".join(out)


def _case_value(v: str, kind: str | None):
    """One selector value. The DFIR reader numbers Boolean frames 0 (False) / 1 (True) and error
    cluster frames 0 (No Error) / 1 (Error)."""
    if kind == "bool":
        return v not in ("0", "False", "false")
    if kind == "error" and v in ("0", "1"):
        return "Error" if v == "1" else "No Error"
    if re.fullmatch(r"-?\d+", v) and kind not in ("string", "enum"):
        return int(v)
    return v


# ---- compare ------------------------------------------------------------------------------------------
def signature(g: dict) -> dict:
    """What a rebuilt VI must share with the original: node kinds and functions/subVIs, structures,
    and the number of wire connections between them (tunnels collapsed)."""
    kinds = collections.Counter()
    for n in g["nodes"]:
        k = n["kind"]
        if k in ("tunnel",):
            continue
        if k == "function":
            k = f"function:{FUNCTION_ALIASES.get(n.get('function'), n.get('function'))}"
            if n.get("path"):  # Static VI Reference: the VI it points at
                k += f":{Path(n['path']).name}{' (strict)' if n.get('strict') else ''}"
        elif k == "subvi":
            k = f"subvi:{Path(n.get('path', '')).name}"
        elif k == "constant":
            k = f"constant:{json.dumps(n.get('value'))[:40]}"
        elif k in ("property_node", "invoke_node"):
            k = f"{k}:{n.get('control') or n.get('vi_class')}:{n.get('method') or ','.join(n.get('items', []))}"
        kinds[k] += 1
    tun = {n["id"] for n in g["nodes"] if n["kind"] == "tunnel"}
    conn = sum(1 for w in g["wires"] for s in w["to"]
               if (w["from"] or "").partition(".")[0] not in tun and s.partition(".")[0] not in tun)
    return {"nodes": kinds, "connections": conn}


def compare(a: dict, b: dict) -> list[str]:
    sa, sb = signature(a), signature(b)
    diffs = []
    for k in sorted(set(sa["nodes"]) | set(sb["nodes"])):
        if sa["nodes"][k] != sb["nodes"][k]:
            diffs.append(f"{k}: {sa['nodes'][k]} vs {sb['nodes'][k]}")
    return diffs


def measure(lv, g: dict, out: Path) -> dict:
    """Convert a dump, rebuild it at `out`, and compare. Returns the fields of a measure result."""
    from . import catalog as cat
    res: dict = {}
    spec, unsupported, features = to_spec(g, cat.load(lv.app.ApplicationDirectory))
    res["features"] = features
    if unsupported:
        res.update(stage="unsupported", unsupported=unsupported)
        return res
    if not spec.get("functions") and not spec.get("subvis") and not spec.get("structures") \
            and not spec.get("constants"):
        res.update(stage="unsupported", unsupported=["empty diagram"])
        return res
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = lv.vi_create(str(out), spec, True, "")
    except Exception as e:
        res.update(stage="create failed", error=str(e)[:300])
        return res
    if r["exec_state"].startswith("bad"):
        res["stage"] = "broken"
        res["errors"] = [e["error"] for e in r.get("errors", [])]
    try:
        g2 = lv.vi_dump(str(out), g["source"])
    except Exception as e:
        res.update(stage=res.get("stage", "create failed"), error=f"dump of rebuilt VI: {str(e)[:200]}")
        return res
    diffs = compare(g, g2)
    res["diffs"] = diffs[:20]
    sa, sb = signature(g)["connections"], signature(g2)["connections"]
    res["connections"] = [sa, sb]
    if "stage" not in res:
        res["stage"] = "same" if not diffs and sa == sb else "different"
    return res
