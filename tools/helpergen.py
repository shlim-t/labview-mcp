"""Generate helper VIs from Python specs, without drawing them by hand.

A helper is built on the blank VI with LabVIEW's diagram scripting:

1. one script places the front-panel controls/indicators and the nodes (Property and Invoke
   Nodes are placed generic, functions by palette name), asking for their references (out="k");
2. lvmcp_propconfig.vi / lvmcp_invokeconfig.vi give each Property/Invoke Node its class and
   properties/method;
3. a second script binds everything by reference (in="k") and draws the wires, then LabVIEW's
   Clean Up Diagram tidies it.

Refnum controls are made from a synthesized type descriptor: 0x70 (refnum), kind 0x0008 (VI
Server), then the class number (VI 0x0002, GObject 0x0024, ...). Terminals of Property/Invoke
Nodes are named by LabVIEW's internal data names (PaletteString, not "Palette String").

usage: python tools/helpergen.py [name ...]      # writes vis_src/<name>.vi
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import quoteattr

from labview_mcp import gen
from labview_mcp.lv import BLANK_VI, LabVIEW, LabVIEWError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "vis_src"
PROPCONFIG = SRC / "lvmcp_propconfig.vi"
INVOKECONFIG = SRC / "lvmcp_invokeconfig.vi"

# VI Server class numbers, as stored in refnum type descriptors (measured; see vi_classes.json).
CLASS_IDS = json.loads((Path(gen.__file__).with_name("vi_classes.json")).read_text())["classes"]


def refnum_td(cls: str) -> bytes:
    return bytes.fromhex("000c00700008") + CLASS_IDS[cls].to_bytes(2, "big") + bytes.fromhex("00040000")


def _type(t) -> tuple[bytes, bytes]:
    if isinstance(t, str) and t.startswith("ref:"):
        if t.endswith("[]"):  # 1-D array of refnums: dims 1, size -1, element; data = 0 elements
            elem = refnum_td(t[4:-2])
            body = bytes.fromhex("0040" "0001" "ffffffff") + elem
            return (len(body) + 2).to_bytes(2, "big") + body, bytes(4)
        return refnum_td(t[4:]), bytes(4)
    lt = t if isinstance(t, gen.LVType) else gen.parse_type(t)
    return lt.td(), lt.data(None)




# what All Supported Properties returns: one cluster per property
_ID_NAMES = gen.LVType("array", dims=1, elem=gen.parse_type({"cluster": [
    {"name": "ID", "type": "string"}, {"name": "Data Name", "type": "string"},
    {"name": "Short Name", "type": "string"}, {"name": "Long Name", "type": "string"}]}))


def _write_mode_node(lv: LabVIEW, t, tmp: Path):
    """A Property Node whose item is in Write mode. Scripting cannot switch an item to Write without
    a Write-mode Property Node, so take the one in lvmcp_propconfig.vi (its class-name item is
    Write): open a throwaway copy, move that node into VI `t`, and return the moved node's ref."""
    src = Path(tempfile.mkdtemp(prefix="lvmcp-wnode-")) / f"lvmcp_wnode_{time.time_ns()}.vi"
    shutil.copyfile(PROPCONFIG, src)
    s = lv._vi(str(src))
    before = set(lv._uid_map(t, "Property", fresh=True))
    node = next(iter(lv._uid_map(s, "Property", fresh=True).values()))
    top = next(iter(lv._uid_map(t, "Diagram", fresh=True).values()))
    m = lv._vi(str(tmp / "lvmcp_move.vi"))  # generated first, next to the helper being built
    m.SetControlValue("reference", node)
    m.SetControlValue("owner", top)
    m.SetControlValue("position", (200, 300))
    m.Run(False)
    st, code, msg = m.GetControlValue("error out")
    if st:
        raise LabVIEWError(f"moving a Write-mode Property Node failed (error {code}): {msg}")
    moved = {u: r for u, r in lv._uid_map(t, "Property", fresh=True).items() if u not in before}
    if len(moved) != 1:
        raise LabVIEWError("could not find the moved Property Node")
    return next(iter(moved.values()))


def build_helper(lv: LabVIEW, path: Path, spec: dict) -> dict:
    """spec: {"controls": [(label, type)], "indicators": [(label, type)],
              "nodes": [{"name", "kind": "property"|"invoke"|"function", "class", "items"|"method"|"function"}],
              "wires": [(from, to)]}  ends are a front-panel label or "node.Terminal".
    Types are vi_create types or "ref:<Class>"."""
    shutil.copyfile(BLANK_VI, path)
    t = lv._vi(str(path))
    t._FlagAsMethod("SaveInstrument")
    fps = [(lab, ty, False) for lab, ty in spec.get("controls", [])] + \
          [(lab, ty, True) for lab, ty in spec.get("indicators", [])]
    tds, xml = [], ["<Script>"]
    for k, (lab, ty, ind) in enumerate(fps):
        tds.append(_type(ty))
        xml.append(f"<Control name=\"c{k}\" typeindex=\"{k}\"" + (" indicator=\"TRUE\"" if ind else "")
                   + f" label={quoteattr(lab)} position=\"({300 if ind else 10},{10 + 50 * k})\"></Control>"
                   f"<ControlTerminal controlname=\"c{k}\" position=\"({400 if ind else 10},{10 + 50 * k})\">"
                   "</ControlTerminal>")
    nodes = spec.get("nodes", [])
    placed = [k for k, n in enumerate(nodes) if not n.get("write")]
    for j, k in enumerate(placed):
        n = nodes[k]
        if n["kind"] == "constant":  # {"kind": "constant", "type": ..., "value": ...}
            lt = gen.parse_type(n["type"])
            tds.append((lt.td(), lt.data(n.get("value"))))
            xml.append(f"<Constant name=\"n{k}\" typeindex=\"{len(tds) - 1}\" position=\"(100,{10 + 80 * k})\" "
                       f"out=\"{j}\"></Constant>")
            continue
        style = {"property": "Property Node", "invoke": "Invoke Node"}.get(n["kind"], n.get("function"))
        xml.append(f"<Function name=\"n{k}\" style={quoteattr(style)} position=\"(200,{10 + 80 * k})\" "
                   f"out=\"{j}\"></Function>")
    xml.append("</Script>")
    st, code, src = lv._gen_script(t, "".join(xml), [a for a, _ in tds], [b for _, b in tds], raw=True)
    if st:
        raise LabVIEWError(f"placing the objects failed (error {code}): {src}")
    got = list(lv._gen_out)
    refs = [None] * len(nodes)
    for j, k in enumerate(placed):
        refs[k] = got[j]
    for k, n in enumerate(nodes):
        if n.get("write"):  # a Property Node with one item, in Write mode
            refs[k] = _write_mode_node(lv, t, path.parent)
    for k, n in enumerate(nodes):
        if n["kind"] == "property":
            h = lv._vi(str(PROPCONFIG))
            h.SetControlValue("reference", refs[k])
            h.SetControlValue("Property Node Class Name", "VI Server:" + n["class"])
            h.SetControlValue("ID Strings[]", list(n["items"]))
        elif n["kind"] == "invoke":
            h = lv._vi(str(INVOKECONFIG))
            h.SetControlValue("reference", refs[k])
            h.SetControlValue("Invoke Node Class Name", "VI Server:" + n["class"])
            h.SetControlValue("ID String", n["method"])
        else:
            continue
        h.SetControlValue("Allow Alternate Names? (F)", True)
        h.Run(False)
        st, code, src = h.GetControlValue("error out")
        if st:
            raise LabVIEWError(f"configuring node {n['name']!r} failed (error {code}): {src}")
    fp = lv._fp_refs_by_label(t)
    names = {n["name"]: f"n{k}" for k, n in enumerate(nodes)}
    labels = {lab: f"c{k}" for k, (lab, _, _) in enumerate(fps)}

    def end(e: str) -> str:
        if e in labels:
            return labels[e]
        node, _, term = e.partition(".")
        return f"{names[node]}.{term}" if term else names[node]  # a constant has one terminal
    xml = ["<Script>"]
    xml += [f"<{t_} name=\"n{k}\" in=\"{k}\"></{t_}>"
            for k, t_ in enumerate("Constant" if n["kind"] == "constant" else "Function" for n in nodes)]
    xml += [f"<Control name=\"c{k}\" in=\"{len(nodes) + k}\"></Control>" for k in range(len(fps))]
    # LabVIEW's scripting draws a wire from its sink: source = sink, destination = driver
    # Method return values cannot be wired this way (the name resolves to the wrong terminal).
    # Terminal names are the ones LabVIEW gives the terminal objects ("AllProps2" for the item
    # AllProps[]); node_terminals() lists them. "[...]" in a name would mean a position.
    xml += [f"<Wire source={quoteattr(end(b))} destination={quoteattr(end(a))}></Wire>" for a, b in spec["wires"]]
    xml.append("</Script>")
    st, code, src = lv._gen_script(t, "".join(xml), [], [], refs=refs + [fp[lab] for lab, _, _ in fps])
    if st:
        t.SaveInstrument(str(path), False, False)  # kept for inspection
        raise LabVIEWError(f"wiring failed (error {code}): {src}")
    # Outputs whose terminal name holds "[]" (e.g. Ctrls[]) cannot be wired by name: LabVIEW makes
    # an indicator for them (Terminal.Create Indicator), labelled as LabVIEW chooses.
    made = {}
    for node, term in spec.get("term_indicators", []):
        uid = lv._uid_of(refs[[n["name"] for n in nodes].index(node)])
        by_uid = dict(lv._owned(t, "Terminal", uid))
        tref = next((by_uid[u] for u, nm in lv._owned(t, "Terminal", uid, names=True) if nm == term), None)
        if tref is None:
            raise LabVIEWError(f"{node} has no terminal {term!r}")
        before = set(lv._fp_refs_by_label(t))
        h = lv._helper(path.parent / "lvmcp_termind.vi")  # generated before (ORDER)
        h.SetControlValue("reference", tref)
        lv._run_checked(h, f"making an indicator for {node}.{term}")
        made[term] = sorted(set(lv._fp_refs_by_label(t)) - before)
    t.SaveInstrument(str(path), False, False)
    return {"path": str(path), **({"indicators made": made} if made else {})}



def _set_value_helper(prop: str) -> dict:
    return {
        "controls": [("reference", "ref:Control"), ("type string", "string"), ("data string", "string")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "i16", "kind": "constant", "type": "i16[]", "value": []},
                  {"name": "tc", "kind": "function", "function": "Type Cast"},
                  {"name": "fs", "kind": "function", "function": "Flattened String To Variant"},
                  {"name": "s", "kind": "property", "class": "Control", "items": [prop], "write": True}],
        "wires": [("type string", "tc.x"), ("i16", "tc.type"), ("tc.*(type *) &x", "fs.type string"),
                  ("data string", "fs.data string"), ("fs.Variant", f"s.{prop}"), ("reference", "s.reference"),
                  ("fs.error out", "s.error in (no error)"), ("s.error out", "error out")],
    }


HELPERS: dict[str, dict] = {
    # GObject.Replace: swap a front-panel object for another palette object (vi_create "style")
    "lvmcp_replace": {
        "controls": [("reference", "ref:GObject"), ("Style", "u32"), ("Palette String", "string")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "r", "kind": "invoke", "class": "GObject", "method": "Replace"}],
        "wires": [("reference", "r.reference"), ("Style", "r.Style"), ("Palette String", "r.PaletteString"),
                  ("r.error out", "error out")],
    },
    # GObject.Move: move an object into another diagram (also of another VI)
    "lvmcp_move": {
        "controls": [("reference", "ref:GObject"), ("owner", "ref:GObject"),
                     ("position", {"cluster": [{"name": "x", "type": "i32"}, {"name": "y", "type": "i32"}]})],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "m", "kind": "invoke", "class": "GObject", "method": "Move"}],
        "wires": [("reference", "m.reference"), ("owner", "m.owner"), ("position", "m.position"),
                  ("m.error out", "error out")],
    },
    # Property.Property Node Class Name (read): the VI Server class a Property Node has, e.g. after a
    # refnum of unknown class was wired to it
    "lvmcp_classname": {
        "controls": [("reference", "ref:Property")],
        "indicators": [("class name", "string"), ("error out", "error")],
        "nodes": [{"name": "c", "kind": "property", "class": "Property", "items": ["PropClassName"]}],
        "wires": [("reference", "c.reference"), ("c.ClassName", "class name"), ("c.error out", "error out")],
    },
    # Property.All Supported Properties: the properties of the class a Property Node has
    "lvmcp_allprops": {
        "controls": [("reference", "ref:Property")],
        "indicators": [("properties", _ID_NAMES), ("error out", "error")],
        "nodes": [{"name": "a", "kind": "property", "class": "Property", "items": ["AllProps[]"]}],
        "wires": [("reference", "a.reference"), ("a.AllProps2", "properties"), ("a.error out", "error out")],
    },
    # Control.Value (Signaling): set a control of a running VI as if the user did (fires Value Change).
    # The value arrives flattened (type string + data string) and becomes a variant here.
    "lvmcp_signal": _set_value_helper("Val(Sgnl)"),
    # Control.Value -> Control.Value (Signaling): fire Value Change with the control's current value
    # (after vi_run set it through COM, which knows its type)
    "lvmcp_fire": {
        "controls": [("reference", "ref:Control")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "r", "kind": "property", "class": "Control", "items": ["Value"]},
                  {"name": "w", "kind": "property", "class": "Control", "items": ["Val(Sgnl)"], "write": True}],
        "wires": [("reference", "r.reference"), ("r.reference out", "w.reference"), ("r.Value", "w.Val(Sgnl)"),
                  ("r.error out", "w.error in (no error)"), ("w.error out", "error out")],
    },
    # Property.Link To Control: make a Property Node an implicit one of a front-panel control
    "lvmcp_proplink": {
        "controls": [("reference", "ref:Property"), ("control", "ref:Control")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "k", "kind": "invoke", "class": "Property", "method": "LinkToControl"}],
        "wires": [("reference", "k.reference"), ("control", "k.Reference"), ("k.error out", "error out")],
    },
    # Invoke.Link To Control: make an Invoke Node an implicit one of a front-panel control
    "lvmcp_invokelink": {
        "controls": [("reference", "ref:Invoke"), ("control", "ref:Control")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "k", "kind": "invoke", "class": "Invoke", "method": "LinkToControl"}],
        "wires": [("reference", "k.reference"), ("control", "k.Reference"), ("k.error out", "error out")],
    },
    # GObject.UID: the object's UID (matches lvmcp_uids.vi and vi_dump)
    "lvmcp_uid": {
        "controls": [("reference", "ref:GObject")],
        "indicators": [("UID", "i32"), ("error out", "error")],
        "nodes": [{"name": "u", "kind": "property", "class": "GObject", "items": ["UID"]}],
        "wires": [("reference", "u.reference"), ("u.UID", "UID"), ("u.error out", "error out")],
    },
    # PropertyItem.IsWrite: make a Property Node item Write (T) or Read (F)
    "lvmcp_propwrite": {
        "controls": [("item", "ref:PropertyItem"), ("write", "bool")],
        "indicators": [("error out", "error")],
        "nodes": [{"name": "w", "kind": "property", "class": "PropertyItem", "items": ["IsWrite"], "write": True}],
        "wires": [("item", "w.reference"), ("write", "w.IsWrite"), ("w.error out", "error out")],
    },
}
# Generated in this order: lvmcp_propwrite needs lvmcp_move. lvmcp_replace is still the hand-built one
# in vis_src (this spec reproduces it; see tests).
# Terminal.Create Constant: a constant wired to a terminal (e.g. an event structure's Timeout,
# which has no name a script could wire), with the value given flattened as for lvmcp_signal
HELPERS["lvmcp_termconst"] = {
    "controls": [("reference", "ref:Terminal"), ("type string", "string"), ("data string", "string")],
    "indicators": [("error out", "error")],
    "nodes": [{"name": "i16", "kind": "constant", "type": "i16[]", "value": []},
              {"name": "tc", "kind": "function", "function": "Type Cast"},
              {"name": "fs", "kind": "function", "function": "Flattened String To Variant"},
              {"name": "k", "kind": "invoke", "class": "Terminal", "method": "Create Constant"}],
    "wires": [("type string", "tc.x"), ("i16", "tc.type"), ("tc.*(type *) &x", "fs.type string"),
              ("data string", "fs.data string"), ("fs.Variant", "k.Value"), ("reference", "k.reference"),
              ("fs.error out", "k.error in (no error)"), ("k.error out", "error out")],
}
# NamedUnbundler.OutputCnt (write): how many items an event data node shows. A data node made by
# scripting shows only Source, Type and Time; more items (NewVal, Coords, ...) appear in order.
HELPERS["lvmcp_outcnt"] = {
    "controls": [("reference", "ref:NamedUnbundler"), ("count", "i32")],
    "indicators": [("error out", "error")],
    "nodes": [{"name": "w", "kind": "property", "class": "NamedUnbundler", "items": ["OutputCnt"], "write": True}],
    "wires": [("reference", "w.reference"), ("count", "w.OutputCnt"), ("w.error out", "error out")],
}
# Constant.Value / Control.Value as flattened type + data: the type of a constant or front-panel
# control (vi_dump's scan reader has no other way to learn it)
def _typeof_helper(cls: str) -> dict:
    return {
        "controls": [("reference", f"ref:{cls}")],
        "indicators": [("type string", "i16[]"), ("data string", "string"), ("error out", "error")],
        "nodes": [{"name": "p", "kind": "property", "class": cls, "items": ["Value"]},
                  {"name": "v", "kind": "function", "function": "Variant To Flattened String"}],
        "wires": [("reference", "p.reference"), ("p.Value", "v.Variant"), ("v.type string", "type string"),
                  ("v.data string", "data string"), ("p.error out", "error out")],
    }


HELPERS["lvmcp_consttype"] = _typeof_helper("Constant")
HELPERS["lvmcp_ctltype"] = _typeof_helper("Control")
# Terminal.Create Indicator: an indicator wired to a terminal (for outputs named "...[]")
HELPERS["lvmcp_termind"] = {
    "controls": [("reference", "ref:Terminal")],
    "indicators": [("error out", "error")],
    "nodes": [{"name": "k", "kind": "invoke", "class": "Terminal", "method": "Create Indicator"}],
    "wires": [("reference", "k.reference"), ("k.error out", "error out")],
}
# VI.Connector Pane -> ConnectorPane.Controls[]: the front-panel controls in connector-terminal order
# (unassigned terminals give Not a Refnum); readable for password-protected VIs too
HELPERS["lvmcp_conpane"] = {
    "controls": [("reference", "ref:VI")],
    "indicators": [("error out", "error")],
    "nodes": [{"name": "a", "kind": "property", "class": "VI", "items": ["ConPane.Ref"]},
              {"name": "b", "kind": "property", "class": "ConnectorPane", "items": ["Ctrls[]"]}],
    "wires": [("reference", "a.reference"), ("a.ConPane", "b.reference"),
              ("a.error out", "b.error in (no error)"), ("b.error out", "error out")],
    "term_indicators": [("b", "Ctrls[]")],
}
# SubVI.VI Path: the VI a subVI node calls (also polymorphic, dynamic-dispatch and Call Parent nodes)
HELPERS["lvmcp_subvipath"] = {
    "controls": [("reference", "ref:SubVI")],
    "indicators": [("path", "path"), ("error out", "error")],
    "nodes": [{"name": "p", "kind": "property", "class": "SubVI", "items": ["VIPath"]}],
    "wires": [("reference", "p.reference"), ("p.VIPath", "path"), ("p.error out", "error out")],
}
# EventStructure.Timeout Terminal: the terminal to wire the timeout (ms) to
HELPERS["lvmcp_timeoutterm"] = {
    "controls": [("reference", "ref:EventStructure")],
    "indicators": [("terminal", "ref:GObject"), ("error out", "error")],
    "nodes": [{"name": "p", "kind": "property", "class": "EventStructure", "items": ["TimeoutTerminal"]}],
    "wires": [("reference", "p.reference"), ("p.TimeoutTerminal", "terminal"), ("p.error out", "error out")],
}
# EventStructure.Event Selectors: each frame's caption, e.g. ' "stop": Value Change ' or ' Timeout '
HELPERS["lvmcp_eventsel"] = {
    "controls": [("reference", "ref:EventStructure")],
    "indicators": [("selectors", "string[]"), ("error out", "error")],
    "nodes": [{"name": "p", "kind": "property", "class": "EventStructure", "items": ["EventSelectors"]}],
    "wires": [("reference", "p.reference"), ("p.EventSelectors", "selectors"), ("p.error out", "error out")],
}

ORDER = ["lvmcp_move", "lvmcp_propwrite", "lvmcp_proplink", "lvmcp_invokelink", "lvmcp_uid", "lvmcp_classname",
         "lvmcp_allprops", "lvmcp_signal", "lvmcp_fire", "lvmcp_timeoutterm", "lvmcp_eventsel", "lvmcp_termconst", "lvmcp_outcnt", "lvmcp_consttype", "lvmcp_ctltype", "lvmcp_termind", "lvmcp_conpane", "lvmcp_subvipath"]


def main(args: list[str]) -> None:
    out = SRC
    if args[:1] == ["--out"]:
        out, args = Path(args[1]), args[2:]
    lv = LabVIEW()
    for name in args or ORDER:
        res = lv.call(build_helper, lv, out / f"{name}.vi", HELPERS[name]).result()
        print(f"{name}: {res['path']}")


if __name__ == "__main__":
    main(sys.argv[1:])
