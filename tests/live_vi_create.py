"""Live end-to-end tests for vi_create: build each VI, run it, compare outputs.

Needs Windows + LabVIEW. usage: python tests/live_vi_create.py [case ...]
VIs and diagram PNGs are written to a temp folder that is printed at the end.
"""
import json, os, sys, shutil, tempfile
from labview_mcp.lv import LabVIEW

lv = LabVIEW()
D = tempfile.mkdtemp(prefix="lvmcp-test-")

CASES = {
    # i*k for i in 0..n-1 -> auto-indexed array
    "for_scale": ({
        "controls": [{"name": "n", "type": "i32", "default": 5}, {"name": "k", "type": "i32", "default": 3}],
        "indicators": [{"name": "out", "type": "i32[]"}],
        "structures": [{"name": "L", "type": "for_loop",
                        "functions": [{"name": "m", "function": "Multiply"}]}],
        "wires": [["n", "L.N"], ["L.i", "m.x"], ["k", "m.y"], ["m.x*y", "out"]],
    }, {}, {"out": [0, 3, 6, 9, 12]}),
    # running sum with a shift register
    "for_sum": ({
        "controls": [{"name": "n", "type": "i32", "default": 5}],
        "indicators": [{"name": "total", "type": "i32"}],
        "constants": [{"name": "zero", "type": "i32", "value": 0}],
        "structures": [{"name": "L", "type": "for_loop", "shift_registers": ["acc"],
                        "functions": [{"name": "a", "function": "Add"}]}],
        "wires": [["n", "L.N"], ["zero", "acc.left"], ["acc.left", "a.x"], ["L.i", "a.y"],
                  ["a.x+y", "acc.right"], ["acc.right", "total"]],
    }, {"n": 101}, {"total": 5050}),
    # while loop until i >= limit
    "while_limit": ({
        "controls": [{"name": "limit", "type": "i32", "default": 7}],
        "indicators": [{"name": "last", "type": "i32", "label": "last i"}],
        "structures": [{"name": "W", "type": "while_loop",
                        "functions": [{"name": "ge", "function": "Greater Or Equal?"}]}],
        "wires": [["W.i", "ge.x"], ["limit", "ge.y"], ["ge.x >= y?", "W.stop"], ["W.i", "last"]],
    }, {}, {"last i": 7}),
    # boolean case
    "case_bool": ({
        "controls": [{"name": "dbl_it", "type": "bool", "label": "double it"}, {"name": "x", "type": "dbl"}],
        "indicators": [{"name": "r", "type": "dbl", "label": "result"}],
        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
            {"value": True, "functions": [{"name": "m", "function": "Multiply"}],
             "constants": [{"name": "two", "type": "dbl", "value": 2}]},
            {"value": False, "functions": [{"name": "inc", "function": "Increment"}]}]}],
        "wires": [["dbl_it", "C.selector"], ["x", "m.x"], ["two", "m.y"], ["m.x*y", "t"],
                  ["x", "inc.x"], ["inc.x+1", "t"], ["t", "r"]],
    }, {"x": 5, "double it": True}, {"result": 10.0}),
    # case inside a for loop, string-free int selector with default
    "nested": ({
        "controls": [{"name": "n", "type": "i32", "default": 5}],
        "indicators": [{"name": "arr", "type": "i32[]"}],
        "structures": [{"name": "L", "type": "for_loop", "structures": [
            {"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
                {"value": 0, "constants": [{"name": "hundred", "type": "i32", "value": 100}]},
                {"default": True, "functions": [{"name": "m", "function": "Multiply"}],
                 "constants": [{"name": "ten", "type": "i32", "value": 10}]}]}]}],
        "wires": [["n", "L.N"], ["L.i", "C.selector"], ["hundred", "t"], ["L.i", "m.x"],
                  ["ten", "m.y"], ["m.x*y", "t"], ["t", "arr"]],
    }, {}, {"arr": [100, 10, 20, 30, 40]}),
    # string case
    "case_string": ({
        "controls": [{"name": "fruit", "type": "string", "default": "pear"}],
        "indicators": [{"name": "price", "type": "dbl"}],
        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
            {"value": "apple", "constants": [{"name": "p1", "type": "dbl", "value": 1.5}]},
            {"value": "pear", "constants": [{"name": "p2", "type": "dbl", "value": 2.25}]},
            {"default": True, "constants": [{"name": "p3", "type": "dbl", "value": -1}]}]}],
        "wires": [["fruit", "C.selector"], ["p1", "t"], ["p2", "t"], ["p3", "t"], ["t", "price"]],
    }, {}, {"price": 2.25}),
    # a subVI with a connector pane (error in/out on the bottom corners) ...
    "sub_scale": ({
        "controls": [{"name": "x", "type": "dbl"}, {"name": "gain", "type": "dbl", "default": 1},
                     {"name": "ei", "type": "error", "label": "error in"}],
        "indicators": [{"name": "y", "type": "dbl"}, {"name": "eo", "type": "error", "label": "error out"}],
        "functions": [{"name": "m", "function": "Multiply"}],
        "wires": [["x", "m.x"], ["gain", "m.y"], ["m.x*y", "y"], ["ei", "eo"]],
    }, {"x": 4, "gain": 2.5}, {"y": 10.0}),
    # ... called in a loop by another generated VI
    "uses_sub": ({
        "controls": [{"name": "xs", "type": "dbl[]", "default": [1, 2, 3]}],
        "indicators": [{"name": "ys", "type": "dbl[]"}],
        "constants": [{"name": "g", "type": "dbl", "value": 10}],
        "structures": [{"name": "L", "type": "for_loop",
                        "subvis": [{"name": "s", "path": "{D}/sub_scale.vi"}]}],
        "wires": [["xs", "s.x"], ["g", "s.gain"], ["s.y", "ys"]],
    }, {}, {"ys": [10.0, 20.0, 30.0]}),
    # growable functions: N inputs element[1..N]
    "growable": ({
        "controls": [{"name": "a", "type": "string", "default": "ab"},
                     {"name": "b", "type": "string", "default": "cd"},
                     {"name": "x", "type": "dbl", "default": 1.5},
                     {"name": "cl", "type": {"cluster": [{"name": "gain", "type": "dbl"},
                                                          {"name": "tag", "type": "string"}]},
                      "default": [2.0, "hello"]}],
        "indicators": [{"name": "s", "type": "string"}, {"name": "arr", "type": "dbl[]"},
                       {"name": "t", "type": "string", "label": "tag"}],
        "constants": [{"name": "dash", "type": "string", "value": "-"}],
        "functions": [{"name": "cat", "function": "Concatenate Strings", "size": 3},
                      {"name": "ba", "function": "Build Array", "size": 2},
                      {"name": "u", "function": "Unbundle", "size": 2}],
        "wires": [["a", "cat.element[1]"], ["dash", "cat.element[2]"], ["b", "cat.element[3]"],
                  ["cat.concatenated string", "s"], ["x", "ba.element[1]"], ["x", "ba.element[2]"],
                  ["ba.appended array", "arr"], ["cl", "u.cluster"], ["u.element[2]", "t"]],
    }, {}, {"s": "ab-cd", "arr": [1.5, 1.5], "tag": "hello"}),
    # mean and max of an array, formatted (Format Into String grown to 2 inputs = size 5)
    "stats": ({
        "controls": [{"name": "data", "type": "dbl[]", "default": [3, 9, 6]}],
        "indicators": [{"name": "report", "type": "string"}],
        "constants": [{"name": "fmt", "type": "string", "value": "mean=%.2f max=%.1f"}],
        "functions": [{"name": "sum", "function": "Add Array Elements"},
                      {"name": "size", "function": "Array Size"},
                      {"name": "div", "function": "Divide"},
                      {"name": "mm", "function": "Array Max & Min"},
                      {"name": "fs", "function": "Format Into String", "size": 5}],
        "wires": [["data", "sum.numeric array"], ["data", "size.array"], ["sum.sum", "div.x"],
                  ["size.size(s)", "div.y"], ["data", "mm.array"], ["fmt", "fs.format string"],
                  ["div.x/y", "fs.input 1"], ["mm.max value", "fs.input 2"],
                  ["fs.resulting string", "report"]],
    }, {}, {"report": "mean=6.00 max=9.0"}),
    # case structure driven by an error cluster
    "error_case": ({
        "controls": [{"name": "ei", "type": "error", "label": "error in"}],
        "indicators": [{"name": "msg", "type": "string"}],
        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
            {"value": "No Error", "constants": [{"name": "ok", "type": "string", "value": "fine"}]},
            {"value": "Error", "constants": [{"name": "bad", "type": "string", "value": "failed"}]}]}],
        "wires": [["ei", "C.selector"], ["ok", "t"], ["bad", "t"], ["t", "msg"]],
    }, {"error in": [True, 42, "boom"]}, {"msg": "failed"}),
}


def _fill(o):
    if isinstance(o, dict):
        return {k: _fill(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_fill(v) for v in o]
    return o.replace("{D}", D) if isinstance(o, str) else o


names = sys.argv[1:] or list(CASES)
failed = 0
for name in names:
    spec, inputs, expect = CASES[name]
    spec = _fill(spec)
    p = os.path.join(D, name + ".vi")
    try:
        lv.call(lv.vi_create, p, spec, True, "").result()
        got = lv.call(lv.vi_run, p, inputs, list(expect), 10).result()["values"]
        ok = all(got.get(k) == v for k, v in expect.items())
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}: {json.dumps(got)}")
    except Exception as e:
        failed += 1
        print(f"ERR  {name}: {str(e)[:300]}")
    try:
        _, imgs = lv.call(lv.vi_snapshot, p, False, True, True, False).result()
        for i in imgs:
            shutil.copy(i, os.path.join(D, f"{name}.{i.name}"))
    except Exception:
        pass
print(f"{len(names) - failed}/{len(names)} passed; files in {D}")
sys.exit(1 if failed else 0)
