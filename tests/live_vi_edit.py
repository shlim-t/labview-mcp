"""Live tests for vi_dump and vi_edit. Needs Windows + LabVIEW.

usage: python tests/live_vi_edit.py
"""
import json
import os
import sys
import tempfile

from labview_mcp.lv import LabVIEW, LabVIEWError

lv = LabVIEW()
D = tempfile.mkdtemp(prefix="lvmcp-edit-")
failed = 0


def check(name, cond, detail=""):
    global failed
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'} {name} {detail}")


def call(fn, *a):
    return lv.call(fn, *a).result()


# A small VI to read and edit: out = Gain + 1
base = os.path.join(D, "base.vi")
call(lv.vi_create, base, {
    "controls": [{"name": "g", "type": "dbl", "label": "Gain", "default": 3}],
    "indicators": [{"name": "out", "type": "dbl"}],
    "functions": [{"name": "inc", "function": "Increment"}],
    "wires": [["g", "inc.x"], ["inc.x+1", "out"]]}, True, "")

# vi_dump reads it back
g = call(lv.vi_dump, base)
labels = {n.get("label") for n in g["nodes"] if n["kind"] in ("control", "indicator")}
check("dump labels", labels == {"Gain", "out"}, labels)
check("dump wires", any(w["to"] == ["increment_4.x"] or any(t.endswith(".x") for t in w["to"]) for w in g["wires"]),
      json.dumps(g["wires"]))

# vi_edit adds y = Gain * 2 next to the existing code
res = call(lv.vi_edit, base, {
    "existing": {"gn": "Gain"},
    "indicators": [{"name": "y", "type": "dbl"}],
    "constants": [{"name": "two", "type": "dbl", "value": 2}],
    "functions": [{"name": "m", "function": "Multiply"}],
    "wires": [["gn", "m.x"], ["two", "m.y"], ["m.x*y", "y"]]}, True, False)
check("edit backup", res["backup"] and os.path.exists(res["backup"]), res["backup"])
vals = call(lv.vi_run, base, {}, None, 10)["values"]
check("edit result", vals.get("out") == 4.0 and vals.get("y") == 6.0, vals)

# an edit that would break the VI is reverted: a second source into the already-wired "out"
try:
    call(lv.vi_edit, base, {
        "existing": {"o": "out"},
        "constants": [{"name": "k", "type": "dbl", "value": 1}],
        "wires": [["k", "o"]]}, False, False)
    check("broken edit rejected", False, "no error raised")
except LabVIEWError as e:
    check("broken edit rejected", "reverted" in str(e), str(e)[:80])
vals = call(lv.vi_run, base, {}, None, 10)["values"]
check("still runs after revert", vals.get("out") == 4.0 and vals.get("y") == 6.0, vals)

# unknown label
try:
    call(lv.vi_edit, base, {"existing": {"q": "Nope"}, "wires": []}, False, False)
    check("unknown label rejected", False)
except LabVIEWError as e:
    check("unknown label rejected", "Gain" in str(e), str(e)[:80])

# replace a node: out = x + 1  ->  out = x * 3
rep = os.path.join(D, "replace.vi")
call(lv.vi_create, rep, {
    "controls": [{"name": "x", "type": "dbl", "default": 3}],
    "indicators": [{"name": "out", "type": "dbl"}],
    "functions": [{"name": "inc", "function": "Increment"}],
    "wires": [["x", "inc.x"], ["inc.x+1", "out"]]}, True, "")
inc_id = next(n["id"] for n in call(lv.vi_dump, rep)["nodes"] if n.get("function") == "Increment")
res = call(lv.vi_edit, rep, {
    "remove": [inc_id],
    "existing": {"x": "x", "o": "out"},
    "constants": [{"name": "k", "type": "dbl", "value": 3}],
    "functions": [{"name": "m", "function": "Multiply"}],
    "wires": [["x", "m.x"], ["k", "m.y"], ["m.x*y", "o"]]}, False, False)
vals = call(lv.vi_run, rep, {}, None, 10)["values"]
funcs = [n.get("function") for n in call(lv.vi_dump, rep)["nodes"] if n["kind"] == "function"]
check("replace node", vals.get("out") == 9.0 and funcs == ["Multiply"], f"{vals} {funcs}")

# add code inside an existing For Loop
loop = os.path.join(D, "loop.vi")
call(lv.vi_create, loop, {
    "controls": [{"name": "n", "type": "i32", "default": 4}],
    "indicators": [{"name": "sq", "type": "i32[]"}],
    "structures": [{"name": "L", "type": "for_loop", "functions": [{"name": "m", "function": "Multiply"}]}],
    "wires": [["n", "L.N"], ["L.i", "m.x"], ["L.i", "m.y"], ["m.x*y", "sq"]]}, True, "")
loop_id = next(n["id"] for n in call(lv.vi_dump, loop)["nodes"] if n["kind"] == "for_loop")
call(lv.vi_edit, loop, {
    "existing": {"L": {"node": loop_id, "functions": [{"name": "inc", "function": "Increment"}]}},
    "indicators": [{"name": "p1", "type": "i32[]", "label": "plus one"}],
    "wires": [["L.i", "inc.x"], ["inc.x+1", "p1"]]}, False, False)
vals = call(lv.vi_run, loop, {}, None, 10)["values"]
check("add inside loop", vals.get("sq") == [0, 1, 4, 9] and vals.get("plus one") == [1, 2, 3, 4], vals)

# add code inside one frame of an existing case structure
case = os.path.join(D, "case.vi")
call(lv.vi_create, case, {
    "controls": [{"name": "s", "type": "bool", "default": True}],
    "indicators": [{"name": "r", "type": "dbl"}],
    "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
        {"value": True, "constants": [{"name": "a", "type": "dbl", "value": 1}]},
        {"value": False, "constants": [{"name": "b", "type": "dbl", "value": 2}]}]}],
    "wires": [["s", "C.selector"], ["a", "t"], ["b", "t"], ["t", "r"]]}, True, "")
case_id = next(n["id"] for n in call(lv.vi_dump, case)["nodes"] if n["kind"] == "case")
call(lv.vi_edit, case, {
    "existing": {"C": {"node": case_id, "cases": {"0": {
        "constants": [{"name": "ms", "type": "i32", "value": 1}],
        "functions": [{"name": "w", "function": "Wait (ms)"}]}}}},
    "wires": [["ms", "w.milliseconds to wait"]]}, False, False)
g = call(lv.vi_dump, case)
waits = [n for n in g["nodes"] if n.get("function") == "Wait (ms)"]
vals = call(lv.vi_run, case, {}, None, 10)["values"]
check("add inside case frame", len(waits) == 1 and waits[0]["in"].endswith("#0") and vals.get("r") == 1.0,
      f"{[(w['id'], w['in']) for w in waits]} {vals}")

# new code in a case frame wired to an existing control outside the case (crosses the border)
call(lv.vi_edit, case, {
    "existing": {"sel": "s", "C": {"node": next(n["id"] for n in call(lv.vi_dump, case)["nodes"]
                                                  if n["kind"] == "case"),
                                   "cases": {"1": {"functions": [{"name": "nt", "function": "Not"}]}}}},
    "wires": [["sel", "nt.x"]]}, False, False)
g = call(lv.vi_dump, case)
nots = [n for n in g["nodes"] if n.get("function") == "Not"]
fed = [w for w in g["wires"] if any(t.startswith(nots[0]["id"]) for t in w["to"])] if nots else []
vals = call(lv.vi_run, case, {"s": False}, None, 10)["values"]
check("frame code wired across the border", len(nots) == 1 and nots[0]["in"].endswith("#1") and fed
      and vals.get("r") == 2.0, f"{[(n['id'], n['in']) for n in nots]} {fed} {vals}")

# A VI the compiler exporter refuses (NI example with property nodes): vi_dump falls back to a scan,
# and vi_edit can still remove a node by its id.
import shutil
ex = os.path.join(r"C:\Program Files\National Instruments\LabVIEW 2026\examples",
                  r"Mathematics\Probability and Statistics\Probability Density.vi")
if os.path.exists(ex):
    pd = os.path.join(D, "Probability Density.vi")
    shutil.copyfile(ex, pd)
    g = call(lv.vi_dump, pd)
    waits = [n for n in g["nodes"] if n.get("function") == "Wait (ms)"]
    check("scan fallback", g["source"] == "scan" and len(waits) == 1 and len(g["wires"]) > 40,
          f"{g['source']} {len(g['nodes'])} nodes")
    call(lv.vi_edit, pd, {"remove": [waits[0]["id"]]}, False, True)
    g2 = call(lv.vi_dump, pd)
    left = [n for n in g2["nodes"] if n.get("function") == "Wait (ms)"]
    check("edit a scan-only VI", not left and len(g2["nodes"]) == len(g["nodes"]) - 2,
          f"{len(g['nodes'])} -> {len(g2['nodes'])} nodes")

print(f"{'all passed' if not failed else f'{failed} failed'}; files in {D}")
sys.exit(1 if failed else 0)
