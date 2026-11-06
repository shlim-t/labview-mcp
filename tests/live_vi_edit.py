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

# constant values, read both ways (dfir and scan), including non-ASCII text in the ANSI code page
cv = os.path.join(D, "consts.vi")
consts = [("d", "dbl", 2.5), ("i", "i32", -7), ("b", "bool", True), ("s", "string", "hello 안녕"),
          ("a", "dbl[]", [1.5, 2.5]), ("c", {"cluster": [{"name": "g", "type": "dbl"},
                                                        {"name": "t", "type": "string"}]}, [3.0, "x"])]
call(lv.vi_create, cv, {"constants": [{"name": n, "type": t, "value": v} for n, t, v in consts],
                        "indicators": [{"name": "o" + n, "type": t} for n, t, v in consts],
                        "wires": [[n, "o" + n] for n, t, v in consts]}, True, "")
want = sorted(json.dumps(v) for _, _, v in consts)
for method in ("dfir", "scan"):
    g = call(lv.vi_dump, cv, method)
    got = sorted(json.dumps(n.get("value")) for n in g["nodes"] if n["kind"] == "constant")
    check(f"constant values ({method})", got == want, got)

# vi_edit adds a Property Node linked to an existing control (and a style to a new control)
pe = os.path.join(D, "prop_edit.vi")
call(lv.vi_create, pe, {"controls": [{"name": "x", "type": "dbl", "default": 7}],
                        "indicators": [{"name": "y", "type": "dbl"}],
                        "wires": [["x", "y"]], "constants": [{"name": "z", "type": "i32", "value": 0}]}, True, "")
call(lv.vi_edit, pe, {"existing": {"x": "x"},
                      "indicators": [{"name": "d", "type": "dbl", "label": "default of x", "style": "Gauge"}],
                      "property_nodes": [{"name": "p", "control": "x", "properties": ["DefVal"]}],
                      "wires": [["p.DefVal", "d"]]}, False, False)
vals = call(lv.vi_run, pe, {"x": 2}, [], 10)["values"]
check("edit adds a property node", vals.get("default of x") == 7.0 and vals.get("y") == 2.0, vals)

# 3-A basic edits on y = x * k + b:
be = os.path.join(D, "basic_edit.vi")
call(lv.vi_create, be, {"controls": [{"name": "x", "type": "dbl", "default": 2}, {"name": "b", "type": "dbl"}],
                        "indicators": [{"name": "y", "type": "dbl"}, {"name": "x2", "type": "dbl"}],
                        "constants": [{"name": "k", "type": "dbl", "value": 3}],
                        "functions": [{"name": "m", "function": "Multiply"}, {"name": "a", "function": "Add"}],
                        "wires": [["x", "m.x"], ["k", "m.y"], ["m.x*y", "a.x"], ["b", "a.y"], ["a.x+y", "y"],
                                  ["x", "x2"]]}, True, "")
g = call(lv.vi_dump, be)
kid = next(n["id"] for n in g["nodes"] if n["kind"] == "constant")
# rename a control, change a constant's value and a control's default
r = call(lv.vi_edit, be, {"rename": {"x": "input"}, "values": {kid: 10, "b": 0.5}}, False, False)
vals = call(lv.vi_run, be, {}, [], 10)["values"]
check("rename + values", vals.get("y") == 20.5 and "input" in vals and "x" not in vals, (r, vals))
# retype a control and a constant
call(lv.vi_edit, be, {"retype": {"b": "i32", kid: "i32"}}, False, False)
g = call(lv.vi_dump, be)
types = {n.get("label", n["id"]): n.get("type") for n in g["nodes"] if n["kind"] in ("control", "constant")}
check("retype", types.get("b") == "i32" and types.get(kid) == "i32", types)
# disconnect: x2 shows Add's output instead of x; the x -> Multiply branch of that wire stays
mul = next(n["id"] for n in g["nodes"] if n.get("function") == "Multiply")
add = next(n["id"] for n in g["nodes"] if n.get("function") == "Add")
x2 = next(n["id"] for n in g["nodes"] if n.get("label") == "x2")
r = call(lv.vi_edit, be, {"disconnect": [x2], "existing": {"a": {"node": add}, "o": "x2"},
                          "wires": [["a.x+y", "o"]]}, False, False)
vals = call(lv.vi_run, be, {"input": 1, "b": 2}, [], 10)["values"]
check("disconnect + rewire", vals.get("x2") == vals.get("y") == 12.0, (r, vals))
try:
    call(lv.vi_edit, be, {"disconnect": [f"{mul}.nope"]}, False, False)
    check("disconnect unknown end refused", False)
except LabVIEWError as e:
    check("disconnect unknown end refused", "no wire ends" in str(e), str(e)[:80])

# 3-B structural edits
cs = os.path.join(D, "cases.vi")
call(lv.vi_create, cs, {"controls": [{"name": "n", "type": "i32"}], "indicators": [{"name": "o", "type": "string"}],
                        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
                            {"value": 0, "default": True, "constants": [{"name": "a", "type": "string", "value": "zero"}]},
                            {"value": 1, "constants": [{"name": "b", "type": "string", "value": "one"}]},
                            {"value": 2, "constants": [{"name": "c", "type": "string", "value": "two"}]}]}],
                        "wires": [["n", "C.selector"], ["a", "t"], ["b", "t"], ["c", "t"], ["t", "o"]]}, True, "")
g = call(lv.vi_dump, cs)
case = next(n["id"] for n in g["nodes"] if n["kind"] == "case")
tun = next(n["id"] for n in g["nodes"] if n["kind"] == "tunnel" and n.get("direction") == "output")
call(lv.vi_edit, cs, {"existing": {"C": {"node": case, "remove_cases": [2],
                                         "add_cases": [{"value": 5, "outputs": {tun: "five"}},
                                                       {"value": "7..9", "outputs": {tun: "many"}}]}}}, False, False)
got = [call(lv.vi_run, cs, {"n": v}, ["o"], 10)["values"]["o"] for v in (0, 1, 2, 5, 8)]
check("add and remove case frames", got == ["zero", "one", "zero", "five", "many"], got)
g = call(lv.vi_dump, cs)
check("case frame values", [f.get("values") for f in next(n for n in g["nodes"] if n["kind"] == "case")["frames"]]
      == [["0"], ["1"], ["5"], ["7, 9"]], g["nodes"][-1])

lp = os.path.join(D, "loop_sr.vi")
call(lv.vi_create, lp, {"controls": [{"name": "n", "type": "i32", "default": 5}],
                        "structures": [{"name": "L", "type": "for_loop"}],
                        "wires": [["n", "L.N"]]}, True, "")
g = call(lv.vi_dump, lp)
loop = next(n["id"] for n in g["nodes"] if n["kind"] == "for_loop")
call(lv.vi_edit, lp, {"existing": {"L": {"node": loop, "shift_registers": ["acc"],
                                         "functions": [{"name": "a", "function": "Add"}]}},
                      "constants": [{"name": "zero", "type": "i32", "value": 0}],
                      "indicators": [{"name": "total", "type": "i32"}],
                      "wires": [["zero", "acc.left"], ["acc.left", "a.x"], ["L.i", "a.y"], ["a.x+y", "acc.right"],
                                ["acc.right", "total"]]}, False, False)
vals = call(lv.vi_run, lp, {"n": 5}, [], 10)["values"]
check("shift register on an existing loop", vals.get("total") == 10, vals)
g = call(lv.vi_dump, lp)
loop = next(n["id"] for n in g["nodes"] if n["kind"] == "for_loop")
r = call(lv.vi_edit, lp, {"move": {loop: [300, 200]}}, False, False)
check("move", r.get("moved") == [loop] and r["exec_state"] == "idle", r)

print(f"{'all passed' if not failed else f'{failed} failed'}; files in {D}")
sys.exit(1 if failed else 0)
