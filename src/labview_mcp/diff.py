"""Semantic diff of two block diagrams (vi_dump scan graphs).

Objects keep their LabVIEW UID when a VI is edited and saved, so the two versions are matched by UID:
a node present in both is "the same node" even if its id, position or wiring changed.
"""
from __future__ import annotations

import json
from pathlib import Path

_PARTS = ("tunnel", "shift_register")


def _desc(n: dict) -> str:
    k = n["kind"]
    if k in ("control", "indicator"):
        return f"{k} {n.get('label', '')!r}"
    if k == "function":
        return f"function {n.get('function', '?')}"
    if k == "subvi":
        return f"subVI {Path(n.get('path') or n.get('vi') or '?').name}"
    if k == "constant":
        return f"constant {json.dumps(n.get('value'))[:40]}"
    return k.replace("_", " ")


def _where(n: dict, by_id: dict) -> str:
    c = n.get("in", "")
    if not c:
        return ""
    sid, _, frame = c.partition("#")
    owner = by_id.get(sid)
    name = _desc(owner) if owner else sid
    return f" in {name}" + (f" frame {frame}" if frame else "")


def _end(e: str, by_id: dict) -> tuple | None:
    """A wire end as (uid, terminal) so it can be compared across versions."""
    node, _, term = (e or "").partition(".")
    n = by_id.get(node)
    if n is None or "uid" not in n:
        return None
    return (n["uid"], term)


def _end_text(key: tuple, by_uid: dict) -> str:
    uid, term = key
    n = by_uid.get(uid)
    name = _desc(n) if n else f"uid {uid}"
    return f"{name}.{term}" if term else name


def diff(a: dict, b: dict) -> dict:
    """a = old graph, b = new graph."""
    ua = {n["uid"]: n for n in a["nodes"] if "uid" in n}
    ub = {n["uid"]: n for n in b["nodes"] if "uid" in n}
    ida = {n["id"]: n for n in a["nodes"]}
    idb = {n["id"]: n for n in b["nodes"]}
    out: dict = {"added": [], "removed": [], "changed": [], "wires_added": [], "wires_removed": []}
    for u in sorted(set(ub) - set(ua)):
        n = ub[u]
        if n["kind"] not in _PARTS:
            out["added"].append(f"{_desc(n)}{_where(n, idb)} ({n['id']})")
    for u in sorted(set(ua) - set(ub)):
        n = ua[u]
        if n["kind"] not in _PARTS:
            out["removed"].append(f"{_desc(n)}{_where(n, ida)} ({n['id']})")
    for u in sorted(set(ua) & set(ub)):
        x, y = ua[u], ub[u]
        if x["kind"] in _PARTS:
            continue
        what = f"{_desc(x)} ({y['id']})"
        if x.get("label") != y.get("label") and x["kind"] in ("control", "indicator"):
            out["changed"].append(f"{x['kind']} {x.get('label')!r} renamed to {y.get('label')!r}")
            what = f"{_desc(y)} ({y['id']})"
        if x["kind"] == "constant" and x.get("value") != y.get("value"):
            out["changed"].append(f"constant ({y['id']}){_where(y, idb)}: value "
                                  f"{json.dumps(x.get('value'))[:40]} -> {json.dumps(y.get('value'))[:40]}")
        if x.get("type") is not None and y.get("type") is not None and x.get("type") != y.get("type"):
            out["changed"].append(f"{what}: type {json.dumps(x['type'])[:40]} -> {json.dumps(y['type'])[:40]}")
        if x.get("function") != y.get("function") and x["kind"] == "function":
            out["changed"].append(f"{what}: function {x.get('function')} -> {y.get('function')}")
        if (x.get("path") or x.get("vi")) != (y.get("path") or y.get("vi")) and x["kind"] == "subvi":
            out["changed"].append(f"{what}: calls {y.get('path') or y.get('vi')}")
        if _where(x, ida) != _where(y, idb):
            out["changed"].append(f"{what}: moved from{_where(x, ida) or ' the top level'} to"
                                  f"{_where(y, idb) or ' the top level'}")
        fa, fb = len(x.get("frames", [])), len(y.get("frames", []))
        if fa != fb:
            out["changed"].append(f"{what}: {fa} -> {fb} frames")
    ea = {(s, t) for w in a["wires"] for t in w["to"]
          for s, t in [(_end(w["from"], ida), _end(t, ida))] if s and t}
    eb = {(s, t) for w in b["wires"] for t in w["to"]
          for s, t in [(_end(w["from"], idb), _end(t, idb))] if s and t}
    allu = {**ua, **ub}
    for s, t in sorted(eb - ea):
        out["wires_added"].append(f"{_end_text(s, allu)} -> {_end_text(t, allu)}")
    for s, t in sorted(ea - eb):
        out["wires_removed"].append(f"{_end_text(s, allu)} -> {_end_text(t, allu)}")
    out = {k: v for k, v in out.items() if v}
    out["summary"] = ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in out.items()) or "no differences"
    return out
