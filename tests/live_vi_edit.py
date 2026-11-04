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

print(f"{'all passed' if not failed else f'{failed} failed'}; files in {D}")
sys.exit(1 if failed else 0)
