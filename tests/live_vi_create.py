"""Live end-to-end tests for vi_create: build each VI, run it, compare outputs.

Needs Windows + LabVIEW. usage: python tests/live_vi_create.py [case ...]
VIs and diagram PNGs are written to a temp folder that is printed at the end.
"""
import json, os, sys, shutil, tempfile
from labview_mcp.lv import LabVIEW

lv = LabVIEW()
D = tempfile.mkdtemp(prefix="lvmcp-test-")

# timestamp type string, as vi_dump gives it in "flat"
TS = "001c0054000600160050000400040003000400030004000300040003"
# a VI that ships with the package (target of the Static VI Reference case)
import labview_mcp
UID_VI = os.path.join(os.path.dirname(labview_mcp.__file__), "vis", "lvmcp_uid.vi")
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
    # front-panel styles keep the label, terminal and wires
    "styled": ({
        "controls": [{"name": "on", "type": "bool", "label": "power", "style": "Vertical Toggle Switch"},
                     {"name": "x", "type": "dbl", "default": 4, "style": "Knob"}],
        "indicators": [{"name": "lamp", "type": "bool", "style": "Round LED"},
                       {"name": "g", "type": "dbl", "label": "level", "style": "Gauge"}],
        "functions": [{"name": "m", "function": "Multiply"}],
        "wires": [["on", "lamp"], ["x", "m.x"], ["x", "m.y"], ["m.x*y", "g"]],
    }, {"power": True}, {"lamp": True, "level": 16.0}),
    # Property Nodes: a linked one writes Visible, a second reads it back (error wires order them)
    "prop_nodes": ({
        "controls": [{"name": "show", "type": "bool", "default": True}, {"name": "x", "type": "dbl"}],
        "indicators": [{"name": "vis", "type": "bool", "label": "visible now"}],
        "property_nodes": [{"name": "w", "control": "x", "properties": ["Visible"], "write": True},
                           {"name": "r", "control": "x", "properties": ["Visible"]}],
        "wires": [["show", "w.Visible"], ["w.error out", "r.error in"], ["r.Visible", "vis"]],
    }, {"show": False}, {"visible now": False}),
    # Invoke Node: Reinitialize To Default on x, then read its Value
    "invoke_node": ({
        "controls": [{"name": "x", "type": "dbl", "default": 5}],
        "indicators": [{"name": "after", "type": "dbl"}],
        "invoke_nodes": [{"name": "i", "control": "x", "method": "Reinit To Dflt"}],
        "property_nodes": [{"name": "r", "control": "x", "properties": ["Value"]}],
        "wires": [["i.error out", "r.error in"], ["r.Value", "after"]],
    }, {"x": 9}, {"after": 5.0}),
    # several properties on one node, one of them an enum ("Disabled": 0 = enabled)
    "prop_multi": ({
        "controls": [{"name": "x", "type": "dbl"}],
        "indicators": [{"name": "dis", "type": "i32", "label": "disabled"},
                       {"name": "vis", "type": "bool", "label": "visible"}],
        "property_nodes": [{"name": "r", "control": "x", "properties": ["Disabled", "Visible"]}],
        "wires": [["r.Disabled", "dis"], ["r.Visible", "vis"]],
    }, {}, {"disabled": 0, "visible": True}),
    # enum control, constant and indicator; values go in and out as item names
    "enum_basic": ({
        "controls": [{"name": "s", "type": {"enum": ["Init", "Run", "Stop"]}, "label": "state", "default": "Run"}],
        "indicators": [{"name": "o", "type": {"enum": ["Init", "Run", "Stop"]}, "label": "same"},
                       {"name": "k", "type": {"enum": ["Init", "Run", "Stop"]}, "label": "const"}],
        "constants": [{"name": "c", "type": {"enum": ["Init", "Run", "Stop"]}, "value": "Stop"}],
        "wires": [["s", "o"], ["c", "k"]],
    }, {"state": "Init"}, {"same": "Init", "const": "Stop"}),
    # a state machine: while loop, enum state in a shift register, one case per state.
    # Init -> Add (n times, accumulating) -> Done (stops the loop)
    "state_machine": ({
        "controls": [{"name": "n", "type": "i32", "default": 3}],
        "indicators": [{"name": "total", "type": "i32"},
                       {"name": "last", "type": {"enum": ["Init", "Add", "Done"]}, "label": "last state"}],
        "constants": [{"name": "start", "type": {"enum": ["Init", "Add", "Done"]}, "value": "Init"},
                      {"name": "zero", "type": "i32", "value": 0}],
        "structures": [{"name": "W", "type": "while_loop", "shift_registers": ["state", "acc"], "structures": [
            {"name": "C", "type": "case", "output_tunnels": ["nx", "ao", "st"], "cases": [
                {"value": "Init",
                 "constants": [{"name": "k_add", "type": {"enum": ["Init", "Add", "Done"]}, "value": "Add"},
                               {"name": "z0", "type": "i32", "value": 0},
                               {"name": "f0", "type": "bool", "value": False}]},
                {"value": "Add",
                 "functions": [{"name": "inc", "function": "Increment"},
                               {"name": "ge", "function": "Greater Or Equal?"},
                               {"name": "sel", "function": "Select"}],
                 "constants": [{"name": "k_done", "type": {"enum": ["Init", "Add", "Done"]}, "value": "Done"},
                               {"name": "k_add2", "type": {"enum": ["Init", "Add", "Done"]}, "value": "Add"},
                               {"name": "f1", "type": "bool", "value": False}]},
                {"value": "Done", "pass": [["acc.left", "ao"]],  # acc passes through unchanged
                 "constants": [{"name": "k_done2", "type": {"enum": ["Init", "Add", "Done"]}, "value": "Done"},
                               {"name": "t1", "type": "bool", "value": True}]}]}]}],
        "wires": [["start", "state.left"], ["zero", "acc.left"], ["state.left", "C.selector"],
                  ["k_add", "nx"], ["z0", "ao"], ["f0", "st"],
                  ["acc.left", "inc.x"], ["inc.x+1", "ao"], ["inc.x+1", "ge.x"], ["n", "ge.y"],
                  ["ge.x >= y?", "sel.s"], ["k_done", "sel.t"], ["k_add2", "sel.f"], ["sel.s? t:f", "nx"],
                  ["f1", "st"],
                  ["k_done2", "nx"], ["t1", "st"],
                  ["nx", "state.right"], ["ao", "acc.right"], ["st", "W.stop"],
                  ["acc.right", "total"], ["state.right", "last"]],
    }, {}, {"total": 3, "last state": "Done"}),
    # producer/consumer: a For Loop enqueues 0..n-1, a While Loop in parallel dequeues n elements
    # and sums them, then the queue is released
    "producer_consumer": ({
        "controls": [{"name": "n", "type": "i32", "default": 5}],
        "indicators": [{"name": "sum", "type": "dbl"}],
        "constants": [{"name": "etype", "type": "dbl", "value": 0}, {"name": "zero", "type": "dbl", "value": 0}],
        "functions": [{"name": "obtain", "function": "Obtain Queue"},
                      {"name": "release", "function": "Release Queue"}],
        "structures": [
            {"name": "P", "type": "for_loop", "functions": [{"name": "enq", "function": "Enqueue Element"}]},
            {"name": "C", "type": "while_loop", "shift_registers": ["acc"],
             "functions": [{"name": "deq", "function": "Dequeue Element"}, {"name": "add", "function": "Add"},
                           {"name": "inc", "function": "Increment"},
                           {"name": "ge", "function": "Greater Or Equal?"}]}],
        "wires": [["etype", "obtain.element data type"],
                  ["obtain.queue out", "enq.queue"], ["n", "P.N"], ["P.i", "enq.element"],
                  ["obtain.queue out", "deq.queue"], ["zero", "acc.left"], ["deq.element", "add.x"],
                  ["acc.left", "add.y"], ["add.x+y", "acc.right"], ["acc.right", "sum"],
                  ["C.i", "inc.x"], ["inc.x+1", "ge.x"], ["n", "ge.y"], ["ge.x >= y?", "C.stop"],
                  ["deq.queue out", "release.queue"]],
    }, {}, {"sum": 10.0}),
    # nested properties ("Label.Text": Label, then Text.Text) next to a plain one, in that order,
    # and a node linked to a restyled control (a slide's own property)
    "prop_nested": ({
        "controls": [{"name": "b", "type": "bool", "label": "my switch"}],
        "indicators": [{"name": "t", "type": "string", "label": "label text"},
                       {"name": "v", "type": "bool", "label": "label shown"},
                       {"name": "c", "type": "dbl", "label": "slide", "style": "Vertical Fill Slide"},
                       {"name": "n", "type": "u32", "label": "fill color"}],
        "property_nodes": [{"name": "p", "control": "b", "properties": ["Label.Text", "Visible", "Label.Visible"]},
                           {"name": "h", "control": "c", "properties": ["FillColor"]}],
        "wires": [["p.Label.Text", "t"], ["p.Label.Visible", "v"], ["h.FillColor", "n"]],
    }, {}, {"label text": "my switch", "label shown": True, "fill color": lambda v: isinstance(v, int)}),
    # an unlinked Property Node of class Application needs no reference
    "app_version": ({
        "indicators": [{"name": "ver", "type": "string", "label": "version"}],
        "property_nodes": [{"name": "a", "class": "Application", "properties": ["App.Version"]}],
        "wires": [["a.App.Version", "ver"]],
    }, {}, {"version": lambda v: isinstance(v, str) and v[:2].isdigit()}),
    # VI Server references as controls and constants (Not a Refnum by default)
    "refnums": ({
        "controls": [{"name": "c", "type": "ref:Control"}, {"name": "vis", "type": "ref:VI[]"}],
        "indicators": [{"name": "v", "type": "bool"}, {"name": "nan", "type": "bool"},
                       {"name": "e", "type": "error"}],  # wired, else the error opens a dialog
        "constants": [{"name": "k", "type": "ref:VI"}],
        "property_nodes": [{"name": "p", "class": "Control", "properties": ["Visible"]}],
        "functions": [{"name": "n", "function": "Not A Number/Path/Refnum?"}],
        "wires": [["c", "p.reference"], ["p.Visible", "v"], ["k", "n.number/path/refnum"],
                  ["n.NaN/Path/Refnum?", "nan"], ["p.error out", "e"]],
    }, {}, {"v": False, "nan": True}),
    # a type copied as a flattened type string ("flat"): a timestamp (0x54, kind 6) constant of 100 s
    # and a timestamp indicator, converted to a number
    "flat_type": ({
        "indicators": [{"name": "o", "type": "dbl", "label": "seconds"},
                       {"name": "ts", "type": {"flat": TS, "data": "00" * 16}, "label": "stamp"}],
        "constants": [{"name": "k", "type": {"flat": TS,
                                             "data": (100).to_bytes(8, "big").hex() + "00" * 8}}],
        "functions": [{"name": "f", "function": "To Double Precision Float"}],
        "wires": [["k", "f.number"], ["f.double precision float", "o"], ["k", "ts"]],
    }, {}, {"seconds": 100.0}),
    # terminal names a script cannot use ("/" in them): wired by terminal reference
    "slash_terms": ({
        "controls": [{"name": "x", "type": "dbl", "default": 9}],
        "indicators": [{"name": "o", "type": "dbl[]", "label": "replaced"},
                       {"name": "d", "type": "dbl[]", "label": "rest"}],
        "constants": [{"name": "k", "type": "dbl[]", "value": [1, 2, 3]},
                      {"name": "i", "type": "i32", "value": 1}],
        "functions": [{"name": "r", "function": "Replace Array Subset"},
                      {"name": "f", "function": "Delete From Array"}],
        "wires": [["k", "r.array"], ["i", "r.index"], ["x", "r.new element/subarray"], ["r.output array", "o"],
                  ["k", "f.array"], ["i", "f.index"], ["f.array w/ subset deleted", "d"]],
    }, {}, {"replaced": [1.0, 9.0, 3.0], "rest": [1.0, 3.0]}),
    # stacked shift register (left[2] = two iterations back): Fibonacci, plus a Property Node
    # wired straight to a shift register
    "stacked_sr": ({
        "controls": [{"name": "n", "type": "i32", "default": 10}, {"name": "x", "type": "dbl", "default": 2.5}],
        "indicators": [{"name": "fib", "type": "i32"}, {"name": "v", "type": "dbl", "label": "value"}],
        "constants": [{"name": "zero", "type": "i32", "value": 0}, {"name": "one", "type": "i32", "value": 1},
                      {"name": "z", "type": "dbl", "value": 0}],
        "structures": [{"name": "L", "type": "for_loop", "shift_registers": ["s", "p"],
                        "functions": [{"name": "a", "function": "Add"}],
                        "property_nodes": [{"name": "pn", "control": "x", "properties": ["Value"]}]}],
        "wires": [["n", "L.N"], ["one", "s.left"], ["zero", "s.left[2]"], ["s.left", "a.x"],
                  ["s.left[2]", "a.y"], ["a.x+y", "s.right"], ["s.right", "fib"],
                  ["z", "p.left"], ["pn.Value", "p.right"], ["p.right", "v"]],
    }, {}, {"fib": 89, "value": 2.5}),
    # functions LabVIEW's scripting cannot place by name: placed by GObject.Replace, wired by reference
    # (Merge Errors grown to 4 inputs)
    "replaced_funcs": ({
        "controls": [{"name": "e1", "type": "error"}, {"name": "e2", "type": "error"},
                     {"name": "e3", "type": "error", "default": [True, 42, "boom"]}, {"name": "e4", "type": "error"}],
        "indicators": [{"name": "eo", "type": "error", "label": "merged"}, {"name": "fc", "type": "bool", "label": "first"}],
        "functions": [{"name": "m", "function": "Merge Errors", "size": 4}, {"name": "f", "function": "First Call?"}],
        "wires": [["e1", "m.error in[1]"], ["e2", "m.error in[2]"], ["e3", "m.error in[3]"], ["e4", "m.error in[4]"],
                  ["m.error out", "eo"], ["f.First Call?: T/F", "fc"]],
    }, {}, {"merged": [True, 42, "boom"], "first": True}),
    # a control used only to pass through one case frame (no input tunnel until the pass makes one)
    "case_pass_outside": ({
        "controls": [{"name": "sel", "type": "bool", "default": True},
                     {"name": "name", "type": "string", "default": "kept"}],
        "indicators": [{"name": "o", "type": "string", "label": "out"}],
        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
            {"value": True, "pass": [["name", "t"]]},
            {"value": False, "constants": [{"name": "k", "type": "string", "value": "other"}]}]}],
        "wires": [["sel", "C.selector"], ["k", "t"], ["t", "o"]],
    }, {}, {"out": "kept"}),
    # Static VI Reference pointed at a VI (strictly typed), its reference wired out of a loop into a
    # Property Node; a second, plain one beside it
    "static_vi_ref": ({
        "indicators": [{"name": "n", "type": "string", "label": "name"},
                       {"name": "n2", "type": "string", "label": "name 2"}],
        "structures": [{"name": "L", "type": "while_loop",
                        "constants": [{"name": "t", "type": "bool", "value": True}],
                        "functions": [{"name": "r", "function": "Static VI Reference", "vi": UID_VI,
                                       "strict": True}]}],
        "functions": [{"name": "r2", "function": "Static VI Reference", "vi": UID_VI}],
        "property_nodes": [{"name": "p", "class": "VI", "properties": ["Name"]},
                           {"name": "p2", "class": "VI", "properties": ["Name"]}],
        "wires": [["t", "L.stop"], ["r.vi reference", "p.reference"], ["p.Name", "n"],
                  ["r2.vi reference", "p2.reference"], ["p2.Name", "n2"]],
    }, {}, {"name": "lvmcp_uid.vi", "name 2": "lvmcp_uid.vi"}),
    # Unbundle By Name / Bundle By Name: elements chosen by name, in any order
    "by_name": ({
        "controls": [{"name": "c", "type": {"cluster": [{"name": "alpha", "type": "dbl"},
                                                        {"name": "beta", "type": "string"},
                                                        {"name": "gamma", "type": "i32"}]},
                      "default": [1.5, "x", 7]},
                     {"name": "s", "type": "string", "default": "new"}],
        "indicators": [{"name": "g", "type": "i32", "label": "gamma out"},
                       {"name": "a", "type": "dbl", "label": "alpha out"},
                       {"name": "o", "type": {"cluster": [{"name": "alpha", "type": "dbl"},
                                                          {"name": "beta", "type": "string"},
                                                          {"name": "gamma", "type": "i32"}]}, "label": "out"}],
        "functions": [{"name": "u", "function": "Unbundle By Name"},
                      {"name": "b", "function": "Bundle By Name"}],
        "wires": [["c", "u.input cluster"], ["u.gamma", "g"], ["u.alpha", "a"],
                  ["c", "b.input cluster"], ["s", "b.beta"], ["b.output cluster", "o"]],
    }, {}, {"gamma out": 7, "alpha out": 1.5, "out": [1.5, "new", 7]}),
    # an unwired error (Not a Refnum into a Property Node) must not open LabVIEW's automatic error
    # dialog, which would block LabVIEW and hang this run
    "no_error_dialog": ({
        "controls": [{"name": "c", "type": "ref:Control"}], "indicators": [{"name": "v", "type": "bool"}],
        "property_nodes": [{"name": "p", "class": "Control", "properties": ["Visible"]}],
        "wires": [["c", "p.reference"], ["p.Visible", "v"]],
    }, {}, {"v": False}),
    # several values and ranges in one case frame (LabVIEW's case-name syntax)
    "case_multi": ({
        "controls": [{"name": "n", "type": "i32", "default": 7}],
        "indicators": [{"name": "o", "type": "string"}],
        "structures": [{"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
            {"value": "1..3", "constants": [{"name": "a", "type": "string", "value": "low"}]},
            {"value": "5, 7, 9..", "constants": [{"name": "b", "type": "string", "value": "odd or big"}]},
            {"default": True, "constants": [{"name": "d", "type": "string", "value": "other"}]}]}],
        "wires": [["n", "C.selector"], ["a", "t"], ["b", "t"], ["d", "t"], ["t", "o"]],
    }, {}, {"o": "odd or big"}),
}

# UI VIs wait for clicks, so they are checked by their structure instead of by running them.
UI_CASES = {
    "ui_events": ({
        "controls": [{"name": "stop", "type": "bool", "label": "STOP", "style": "Stop Button"},
                     {"name": "go", "type": "bool", "label": "Double it", "style": "OK Button"},
                     {"name": "x", "type": "dbl", "default": 21}],
        "indicators": [{"name": "y", "type": "dbl"}, {"name": "hits", "type": "i32"}],
        "structures": [{"name": "W", "type": "while_loop", "structures": [{"name": "E", "type": "event", "events": [
            {"control": "stop"},
            {"control": "go", "functions": [{"name": "m", "function": "Multiply"}],
             "constants": [{"name": "k", "type": "dbl", "value": 2}]},
            {"control": "y", "event": "Mouse Down", "functions": [{"name": "inc", "function": "Increment"}],
             "property_nodes": [{"name": "pv", "control": "x", "properties": ["Visible"], "write": True}],
             "constants": [{"name": "t", "type": "bool", "value": True}]}]}]}],
        "wires": [["stop", "W.stop"], ["x", "m.x"], ["k", "m.y"], ["m.x*y", "y"],
                  ["W.i", "inc.x"], ["inc.x+1", "hits"], ["t", "pv.Visible"]],
    }, {  # where each front-panel terminal must end up: "" = top level, k = event frame k
        "STOP": 0, "Double it": 1, "x": 1, "y": 1, "hits": 2},
        ['[0] "STOP": Value Change', '[1] "Double it": Value Change', '[2] "y": Mouse Down']),
    # Queued Message Handler: the UI loop's events enqueue enum messages; a second loop dequeues
    # them and handles each in a case; "Exit" stops it, then the queue is released
    "qmh": ({
        "controls": [{"name": "stop", "type": "bool", "label": "STOP", "style": "Stop Button"},
                     {"name": "hi", "type": "bool", "label": "Say hi", "style": "OK Button"}],
        "indicators": [{"name": "log", "type": "string"}],
        "constants": [{"name": "etype", "type": {"enum": ["Hello", "Exit"]}, "value": "Hello"}],
        "functions": [{"name": "obtain", "function": "Obtain Queue"},
                      {"name": "release", "function": "Release Queue"}],
        "structures": [
            {"name": "W", "type": "while_loop", "structures": [{"name": "E", "type": "event", "events": [
                {"control": "stop", "functions": [{"name": "q_exit", "function": "Enqueue Element"}],
                 "constants": [{"name": "m_exit", "type": {"enum": ["Hello", "Exit"]}, "value": "Exit"}]},
                {"control": "hi", "functions": [{"name": "q_hi", "function": "Enqueue Element"}],
                 "constants": [{"name": "m_hi", "type": {"enum": ["Hello", "Exit"]}, "value": "Hello"}]}]}]},
            {"name": "C", "type": "while_loop", "functions": [{"name": "deq", "function": "Dequeue Element"}],
             "structures": [{"name": "H", "type": "case", "output_tunnels": ["msg", "done"], "cases": [
                 {"value": "Hello", "constants": [{"name": "s_hi", "type": "string", "value": "hi!"},
                                                  {"name": "f", "type": "bool", "value": False}]},
                 {"value": "Exit", "constants": [{"name": "s_bye", "type": "string", "value": "bye"},
                                                 {"name": "t", "type": "bool", "value": True}]}]}]}],
        "wires": [["etype", "obtain.element data type"],
                  ["obtain.queue out", "q_exit.queue"], ["m_exit", "q_exit.element"],
                  ["obtain.queue out", "q_hi.queue"], ["m_hi", "q_hi.element"], ["stop", "W.stop"],
                  ["obtain.queue out", "deq.queue"], ["deq.element", "H.selector"],
                  ["s_hi", "msg"], ["f", "done"], ["s_bye", "msg"], ["t", "done"],
                  ["msg", "log"], ["done", "C.stop"], ["deq.queue out", "release.queue"]],
    }, {"STOP": 0, "Say hi": 1},
        ['[0] "STOP": Value Change', '[1] "Say hi": Value Change']),
    # events of the VI itself and event data: the new value from the event data node, a Timeout
    # frame that counts while idle, and Panel Close? discarded (the window cannot be closed)
    "vi_events": ({
        "controls": [{"name": "stop", "type": "bool", "label": "STOP", "style": "Stop Button"},
                     {"name": "x", "type": "dbl"}],
        "indicators": [{"name": "last", "type": "dbl"}, {"name": "ticks", "type": "i32"}],
        "structures": [{"name": "W", "type": "while_loop", "structures": [{"name": "E", "type": "event", "events": [
            {"control": "stop"},
            {"control": "x", "data": "d"},
            {"event": "Timeout", "timeout": 50, "functions": [{"name": "inc", "function": "Increment"}]},
            {"event": "Panel Close?", "filter": "f", "constants": [{"name": "t", "type": "bool", "value": True}]}]}]}],
        "wires": [["stop", "W.stop"], ["d.NewVal", "last"], ["W.i", "inc.x"], ["inc.x+1", "ticks"],
                  ["t", "f.Discard?"]],
    }, {"STOP": 0, "x": 1, "last": 1, "ticks": 2},
        ['[0] "STOP": Value Change', '[1] "x": Value Change', '[2] Timeout', '[3] Panel Close?']),
    # state in a shift register through the event structure: one frame handles two buttons ("more")
    # and counts, the STOP frame passes the count on unchanged (frame "wires")
    "event_state": ({
        "controls": [{"name": "stop", "type": "bool", "label": "STOP", "style": "Stop Button"},
                     {"name": "inc", "type": "bool", "label": "Add", "style": "OK Button"},
                     {"name": "inc2", "type": "bool", "label": "Add 2", "style": "OK Button"}],
        "indicators": [{"name": "count", "type": "i32"}],
        "constants": [{"name": "zero", "type": "i32", "value": 0}],
        "structures": [{"name": "W", "type": "while_loop", "shift_registers": ["n"],
                        "structures": [{"name": "E", "type": "event", "events": [
                            {"control": "inc", "more": [{"control": "inc2"}],
                             "functions": [{"name": "a", "function": "Increment"}]},
                            {"control": "stop", "wires": [["n.left", "n.right"]]}]}]}],
        "wires": [["zero", "n.left"], ["n.left", "a.x"], ["a.x+1", "n.right"], ["stop", "W.stop"],
                  ["n.right", "count"]],
    }, {"STOP": 1, "Add": 0},
        ['[0] "Add", "Add 2": Value Change', '[1] "STOP": Value Change']),
}


# UI VIs operated while they run (vi_run actions): steps, and a check of the result. Each run must end
# by itself (the STOP click), so "completed" is required too.
UI_RUNS = {
    "ui_events": ([{"set": {"x": 21}}, {"click": "Double it"}, {"read": ["y"]}, {"click": "STOP"}],
                  lambda r: r["actions"][2]["read"]["y"] == 42.0),
    # "log" sits outside the handler loop, so it shows the last message once the loop has ended
    "qmh": ([{"click": "Say hi"}, {"wait": 0.3}, {"click": "STOP"}],
            lambda r: r["values"]["log"] == "bye"),
    # NewVal reaches "last"; the Timeout frame has run several times while waiting
    "vi_events": ([{"set": {"x": 5}}, {"wait": 0.4}, {"read": ["last", "ticks"]}, {"click": "STOP"}],
                  lambda r: r["actions"][2]["read"]["last"] == 5.0 and r["actions"][2]["read"]["ticks"] > 2),
    # two counts (one per button); the STOP frame keeps the count
    "event_state": ([{"click": "Add"}, {"wait": 0.2}, {"click": "Add 2"}, {"wait": 0.2}, {"click": "STOP"}],
                    lambda r: r["values"]["count"] == 2),
}


def _fill(o):
    if isinstance(o, dict):
        return {k: _fill(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_fill(v) for v in o]
    return o.replace("{D}", D) if isinstance(o, str) else o


def ui_case(name):
    spec, where, frames = UI_CASES[name]
    p = os.path.join(D, name + ".vi")
    res = lv.call(lv.vi_create, p, spec, True, "").result()
    problems = []
    if res["exec_state"] != "idle":
        problems.append(f"state {res['exec_state']}")
    if res.get("event_frames", {}).get("E") != frames:
        problems.append(f"frames {res.get('event_frames')}")
    g = lv.call(lv.vi_dump, p, "scan").result()
    ev = next(n for n in g["nodes"] if n["kind"] == "event_structure")
    if len(ev.get("frames", [])) != len(frames):
        problems.append(f"{len(ev.get('frames', []))} frames in the VI")
    term_in = {n["label"]: n.get("in", "") for n in g["nodes"] if n["kind"] in ("control", "indicator")}
    for label, k in where.items():
        want = f"{ev['id']}#{k}" if k is not None else None
        if want and term_in.get(label) != want:
            problems.append(f"{label} is in {term_in.get(label)!r}, expected {want!r}")
    return problems


def broken_errors():
    """A broken VI (Add with one input unwired): vi_create, vi_info and vi_run give LabVIEW's error list."""
    p = os.path.join(D, "broken_errors.vi")
    spec = {"controls": [{"name": "a", "type": "dbl"}], "indicators": [{"name": "s", "type": "dbl"}],
            "functions": [{"name": "add", "function": "Add"}], "wires": [["a", "add.x"], ["add.x+y", "s"]]}
    res = lv.call(lv.vi_create, p, spec, True, "").result()
    problems = []
    if not res["exec_state"].startswith("bad") or not any("Add" in e["error"] for e in res.get("errors", [])):
        problems.append(f"vi_create: {res['exec_state']} {res.get('errors')}")
    info = lv.call(lv.vi_info, p).result()
    if not info.get("errors") or not all(e.get("details") for e in info["errors"]):
        problems.append(f"vi_info: {info.get('errors')}")
    try:
        lv.call(lv.vi_run, p, {}, None, 5).result()
        problems.append("vi_run ran a broken VI")
    except Exception as e:
        if "error list: Add" not in str(e):
            problems.append(f"vi_run: {str(e)[:200]}")
    return problems


CHECKS = {"broken_errors": broken_errors}
names = sys.argv[1:] or list(CASES) + list(UI_CASES) + list(CHECKS)
failed = 0
for name in names:
    if name in CHECKS:
        try:
            problems = CHECKS[name]()
        except Exception as e:
            problems = [f"error: {str(e)[:300]}"]
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'PASS'} {name}: {problems or 'as expected'}")
        continue
    if name in UI_CASES:
        try:
            problems = ui_case(name)
        except Exception as e:
            problems = [f"error: {str(e)[:300]}"]
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'PASS'} {name}: {problems or 'structure as expected'}")
        if name in UI_RUNS and not problems:
            actions, check = UI_RUNS[name]
            try:
                r = lv.call(lv.vi_run, os.path.join(D, name + ".vi"), {}, None, 15, actions).result()
                ok = r["completed"] and check(r)
            except Exception as e:
                ok, r = False, str(e)[:300]
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'} {name} (operated): {json.dumps(r.get('actions') if isinstance(r, dict) else r)}")
        try:
            _, imgs = lv.call(lv.vi_snapshot, os.path.join(D, name + ".vi"), True, True, True, False).result()
            for i in imgs:
                shutil.copy(i, os.path.join(D, f"{name}.{i.name}"))
        except Exception:
            pass
        continue
    spec, inputs, expect = CASES[name]
    spec = _fill(spec)
    p = os.path.join(D, name + ".vi")
    try:
        lv.call(lv.vi_create, p, spec, True, "").result()
        got = lv.call(lv.vi_run, p, inputs, list(expect), 10).result()["values"]
        ok = all(v(got.get(k)) if callable(v) else got.get(k) == v for k, v in expect.items())
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
