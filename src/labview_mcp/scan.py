"""Read a block diagram by walking its objects with VI Scripting (no compiler involved).

The helper VI `vis/lvmcp_uids.vi` traverses every GObject on the diagram and returns, per object:
UID, class name, the owner's UID and, for terminals, whether it is a source, the UID of the wire
connected to it and its name. Node labels (the function / subVI name) come from NI's
"Get GObject Label.vi". This module turns those rows into the same graph shape as dfir.parse.

Unlike the DFIR exporter this works for every VI LabVIEW can open, including broken VIs and VIs
with property nodes or event structures. It does not see constant values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Row:
    uid: int
    cls: str
    owner: int
    source: bool
    wire: int
    name: str
    value: object = None  # constants only


_STRUCTS = {"ForLoop": "for_loop", "WhileLoop": "while_loop", "CaseStructure": "case",
            "EventStructure": "event_structure", "FlatSequence": "flat_sequence",
            "StackedSequence": "stacked_sequence", "InPlaceElementStructure": "in_place",
            "DisableStructure": "disable_structure", "ConditionalDisableStructure": "conditional_disable",
            "TimedLoop": "timed_loop", "TypeSpecializationStructure": "type_specialization",
            "MathScriptNode": "mathscript", "FormulaNode": "formula_node"}
_MULTI_FRAME = {"case", "event_structure", "stacked_sequence", "disable_structure",
                "conditional_disable", "flat_sequence", "type_specialization"}
# parts of a node (a Property Node's items, a Formula Node's parameters): their terminals are the node's
_PARTS = {"PropertyItem", "FormulaParameter", "ScriptNodeParameter", "MathScriptNodeParameter"}
_SKIP = {"Wire", "Diagram", "TopLevelDiagram", "Text", "Label", "Decoration", "Joint", "Comment"}


def _slug(s: str) -> str:
    s = re.sub(r"\.vim?$", "", s or "", flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s[:40] or "node"


def _is_terminal(cls: str) -> bool:
    return cls.endswith("Terminal") and cls != "ControlTerminal"


def build(rows: list[Row], labels: dict[int, str]) -> dict:
    by_uid: dict[int, Row] = {}
    for r in rows:
        by_uid.setdefault(r.uid, r)
    rows = list(by_uid.values())

    # containers: every diagram -> "" (top level), "<structure id>" or "<structure id>#<frame>"
    kind_of: dict[int, str] = {}
    for r in rows:
        if r.cls in _STRUCTS:
            kind_of[r.uid] = _STRUCTS[r.cls]
        elif r.cls.endswith("Structure") or r.cls.endswith("Loop") or r.cls.endswith("Sequence"):
            kind_of[r.uid] = _slug(r.cls)
    node_id: dict[int, str] = {}
    for r in rows:
        if r.cls in _SKIP or r.cls in _PARTS or _is_terminal(r.cls) or r.cls.endswith("Tunnel")                 or "ShiftRegister" in r.cls:
            continue
        if r.cls.endswith("Constant") and by_uid.get(r.owner) and by_uid[r.owner].cls.endswith("Constant"):
            continue  # the element of an array/cluster constant, not a node of its own
        if r.cls == "ControlTerminal":
            node_id[r.uid] = f"{_slug(r.name)}_{r.uid}"
        elif r.cls.endswith("Constant"):
            node_id[r.uid] = f"constant_{r.uid}"
        else:
            node_id[r.uid] = f"{_slug(labels.get(r.uid) or r.cls)}_{r.uid}"
    frames: dict[int, list[int]] = {}
    for r in sorted(rows, key=lambda r: r.uid):
        if r.cls == "Diagram" and r.owner in kind_of:
            frames.setdefault(r.owner, []).append(r.uid)
    container: dict[int, str] = {}
    for r in rows:
        if r.cls == "TopLevelDiagram":
            container[r.uid] = ""
        elif r.cls == "Diagram" and r.owner in node_id:
            fs = frames.get(r.owner, [r.uid])
            sid = node_id[r.owner]
            container[r.uid] = f"{sid}#{fs.index(r.uid)}" if kind_of.get(r.owner) in _MULTI_FRAME else sid

    def where(uid: int) -> str:
        """Container of an object: walk owners up to the first diagram."""
        seen = 0
        while uid in by_uid and seen < 50:
            o = by_uid[uid].owner
            if o in container:
                return container[o]
            uid, seen = o, seen + 1
        return ""

    nodes = []
    for r in sorted(rows, key=lambda r: r.uid):
        if r.uid not in node_id:
            continue
        n = {"id": node_id[r.uid], "uid": r.uid, "class": r.cls}
        if r.cls == "ControlTerminal":
            n["kind"] = "control" if r.source else "indicator"
            n["label"] = r.name
        elif r.uid in kind_of:
            n["kind"] = kind_of[r.uid]
            if r.uid in frames and kind_of[r.uid] in _MULTI_FRAME:
                n["frames"] = [{"index": i} for i in range(len(frames[r.uid]))]
        elif r.cls.endswith("Constant"):
            n["kind"] = "constant"
            if r.value is not None:
                n["value"] = r.value
        elif "SubVI" in r.cls or r.cls in ("StaticVIReference", "CallParentNode", "CallByReference"):
            n["kind"] = "subvi"
            n["vi"] = labels.get(r.uid, "")
        elif r.cls in ("Property", "Invoke"):
            n["kind"] = "property_node" if r.cls == "Property" else "invoke_node"
            items = [t.name for p in rows if p.owner == r.uid and p.cls == "PropertyItem"
                     for t in rows if t.owner == p.uid and _is_terminal(t.cls) and t.name]
            if items:
                n["items"] = list(dict.fromkeys(items))
        else:
            n["kind"] = "function"
            n["function"] = labels.get(r.uid, r.cls)
        w = where(r.uid)
        if w:
            n["in"] = w
        nodes.append(n)

    # tunnels and shift registers: name their terminals after the tunnel object
    tunnel_id: dict[int, str] = {}
    for r in rows:
        if r.cls.endswith("Tunnel") or "ShiftRegister" in r.cls:
            tunnel_id[r.uid] = f"{'tunnel' if 'Tunnel' in r.cls else 'shift_register'}_{r.uid}"
            n = {"id": tunnel_id[r.uid], "uid": r.uid, "kind": "tunnel" if "Tunnel" in r.cls else "shift_register",
                 "class": r.cls, "structure": node_id.get(r.owner, "")}
            w = where(r.owner)
            if w:
                n["in"] = w
            nodes.append(n)

    def term_name(t: Row) -> str:
        o = t.owner
        if by_uid.get(o) and by_uid[o].cls in _PARTS:
            o = by_uid[o].owner
        if t.cls == "ControlTerminal":
            return node_id.get(t.uid, str(t.uid))
        if o in tunnel_id:
            side = "outer" if t.cls.startswith("Outer") else "inner" if t.cls.startswith("Inner") else t.name or "term"
            if side == "inner" and kind_of.get(by_uid[o].owner) in _MULTI_FRAME:
                # a case tunnel has one inner terminal per frame
                inners = sorted(x.uid for x in rows if x.owner == o and x.cls.startswith("Inner"))
                if len(inners) > 1:
                    side += f"[frame {inners.index(t.uid)}]"
            return f"{tunnel_id[o]}.{side}"
        if o in node_id:
            nid = node_id[o]
            if by_uid.get(o) and by_uid[o].cls.endswith("Constant"):
                return nid
            return f"{nid}.{t.name}" if t.name else f"{nid}[uid {t.uid}]"
        if o in container:  # loop iteration / count / condition terminals sit on the loop's diagram
            parent = by_uid.get(o)
            sid = node_id.get(parent.owner, "") if parent else ""
            k = kind_of.get(parent.owner) if parent else None
            if k == "while_loop" and not t.source:
                return f"{sid}.stop"
            if k in ("for_loop", "while_loop") and t.source:
                return f"{sid}.i"
            return f"{sid}[uid {t.uid}]"
        return f"uid {t.uid}"

    wires: dict[int, dict] = {}
    unwired = []
    for t in rows:
        if not (_is_terminal(t.cls) or t.cls == "ControlTerminal"):
            continue
        if t.wire:
            e = wires.setdefault(t.wire, {"from": None, "to": [], "uid": t.wire})
            if t.source:
                e["from"] = term_name(t)
            else:
                e["to"].append(term_name(t))
        elif not t.source and t.owner in node_id and node_id[t.owner] and \
                not (by_uid.get(t.owner) and by_uid[t.owner].cls.endswith("Constant")):
            unwired.append(term_name(t))
    # Diagram objects by the key vi_edit uses: "" (top level), "<loop uid>", "<case uid>#<frame>".
    diagram_uids = {}
    for d, c in container.items():
        if c == "":
            diagram_uids[""] = d
        else:
            sid, _, frame = c.partition("#")
            owner = next((u for u, i in node_id.items() if i == sid), None)
            diagram_uids[f"{owner}" + (f"#{frame}" if frame else "")] = d
    return {"nodes": nodes, "wires": [w for w in wires.values() if w["from"] or w["to"]],
            "unwired_inputs": unwired, "diagram_uids": diagram_uids}
