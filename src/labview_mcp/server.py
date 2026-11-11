"""labview-mcp: MCP server that lets coding agents read, run and create LabVIEW VIs."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

from . import __version__
from .lv import LabVIEW
from .project import Projects

lv = LabVIEW()
projects = Projects(lv)

mcp = MCPServer(
    "labview-mcp",
    version=__version__,
    instructions=(
        "Tools for reading and running NI LabVIEW VIs on this Windows machine through LabVIEW's "
        "ActiveX server. LabVIEW must be installed; it is started automatically if not running.\n"
        "To understand a VI: start with vi_info, then vi_text (labels, comments, case names, "
        "front-panel controls as text), and use vi_snapshot to SEE the block diagram when wiring "
        "matters. vi_snapshot(all_frames=true) also renders hidden case/sequence frames.\n"
        "To build a new VI: vi_create with a spec of controls, indicators, constants, functions, "
        "subVIs, structures and wires (lv_functions / function_terminals give exact function and "
        "terminal names); check the returned diagram picture, then test with vi_run.\n"
        "To read a diagram as data: vi_dump (nodes, wires, constant values). To change an existing "
        "VI: vi_edit adds code wired to its existing controls/indicators, with a backup.\n"
        "To ship: project_create / project_add put VIs in a .lvproj with an application (EXE) "
        "build spec, project_build builds it, lv_mass_compile recompiles a folder.\n"
        "All paths must be absolute. vi_run executes code on the user's machine and may drive "
        "real instruments; only run VIs the user asked you to run."
    ),
)


async def _run(fn, *args, **kw):
    return await asyncio.wrap_future(lv.call(fn, *args, **kw))


def _image(path: Path, max_px: int) -> Image:
    try:
        from PIL import Image as PILImage
    except ImportError:  # pillow optional
        return Image(path=path)
    with PILImage.open(path) as im:
        if max(im.size) <= max_px:
            return Image(path=path)
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return Image(data=buf.getvalue(), format="png")


@mcp.tool()
async def lv_status() -> dict:
    """Connect to LabVIEW and report its version, install directory and VIs currently in memory."""
    return await _run(lv.status)


def _error_lines(errors: list) -> str:
    """LabVIEW's Error List as text lines: the error, then its explanation."""
    lines = []
    for e in errors:
        lines.append(f"- {e['error']}" + (f" (x{e['count']})" if e.get("count") else ""))
        if e.get("details"):
            lines.append("    " + e["details"].replace("\n", " "))
    return "\n".join(lines)


@mcp.tool()
async def vi_info(path: str) -> dict:
    """Metadata for one VI: type, description, LabVIEW version it was saved in, broken/idle state,
    unsaved changes, subVIs it calls (callees) and callers loaded in memory. A broken VI also gets
    "errors": LabVIEW's Error List (why it is broken), each {"error", "details", "count"}.

    path: absolute path to a .vi/.vit/.ctl/.vim file."""
    return await _run(lv.vi_info, path)


@mcp.tool()
async def vi_text(path: str, include_diagram: bool = True, raw: bool = False) -> str:
    """Text content of a VI as LabVIEW's own string export (XML-like):
    front-panel controls (type + name, nested for clusters), descriptions, captions, and with
    include_diagram=true the block-diagram strings: free labels/comments, structures (case
    structures with one block per case), string constants.
    It does NOT include wires, node types or numeric constants — use vi_snapshot to see those.

    raw=true returns the export unmodified (with font markup)."""
    return await _run(lv.vi_text, path, include_diagram, raw)


@mcp.tool(structured_output=False)
async def vi_snapshot(
    path: str,
    diagram: bool = True,
    panel: bool = False,
    all_frames: bool = False,
    connector: bool = False,
    max_images: int = 8,
    max_px: int = 2000,
) -> list[Any]:
    """Render a VI to PNG images: block diagram, front panel and/or connector pane.

    all_frames=true also renders every hidden case / sequence / event frame (one image per
    frame, in LabVIEW's order) — big VIs can produce dozens, so raise max_images deliberately.
    Images larger than max_px on their longest side are downscaled."""
    text, imgs = await _run(lv.vi_snapshot, path, panel, diagram, all_frames, connector)
    out: list[Any] = []
    header = f"{Path(path).name}: {len(imgs)} image(s) rendered"
    if len(imgs) > max_images:
        header += f", returning the first {max_images} (raise max_images for more)"
    out.append(header + ("\n" + text if text else ""))
    for p in imgs[:max_images]:
        tag = p.stem[2:]  # files are vi.html -> vip.png, vic.png, vid.png, vid1.png, ...
        if tag == "p":
            kind = "front panel"
        elif tag == "c":
            kind = "connector pane"
        elif tag == "d":
            kind = "block diagram (visible frames)"
        else:
            kind = f"block diagram, hidden frame #{tag[1:]}"
        out.append(f"[{p.name}] {kind}")
        out.append(_image(p, max_px))
    return out


@mcp.tool()
async def vi_hierarchy(path: str, max_depth: int = 3, include_vilib: bool = False) -> dict:
    """SubVI call tree starting at a VI (breadth-first, up to max_depth levels).
    Callees inside the LabVIEW install (vi.lib, user.lib, instr.lib) are hidden unless
    include_vilib=true."""
    return await _run(lv.vi_hierarchy, path, max_depth, include_vilib)


@mcp.tool()
async def vi_run(
    path: str,
    inputs: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    timeout_s: float = 30.0,
    actions: list[Any] | None = None,
) -> dict:
    """Run a VI and return its front-panel values afterwards.

    inputs: {control label: value} set before running (clusters as lists in element order; enums
    by item name).
    outputs: labels to read back; default reads every top-level control and indicator.
    actions: operate the front panel while the VI runs, as a user would, in order:
      {"click": "Go"}            press a button (works on latched buttons such as STOP)
      {"set": {"x": 21}}         change controls; fires their Value Change events
      {"wait": 0.5}              pause, in seconds
      {"read": ["y", "log"]}     record values at this moment
    e.g. [{"set": {"x": 21}}, {"read": ["y"]}, {"click": "Go"}, {"click": "STOP"}] tests a UI
    end to end; the result's "actions" lists what each step did and read.
    A VI still running after timeout_s is aborted (typical for UI loops waiting on a Stop button).
    A modal dialog (a One Button Dialog, or LabVIEW's automatic error dialog for an unwired error
    in a VI that has automatic error handling on) blocks LabVIEW until someone answers it, and this
    call with it. VIs made by vi_create have automatic error handling off; in other VIs wire error
    outputs before running them.
    WARNING: executes code and may drive real hardware — only run VIs the user asked to run."""
    return await _run(lv.vi_run, path, inputs or {}, outputs, timeout_s, actions)


@mcp.tool(structured_output=False)
async def vi_create(
    path: str,
    spec: dict[str, Any],
    overwrite: bool = False,
    description: str = "",
) -> list[Any]:
    """Create a new VI with front-panel controls/indicators and a wired block diagram.
    Returns a summary and a picture of the generated diagram; then use vi_run to test it.

    spec = {
      "controls":   [{"name": "x", "type": "dbl", "label": "x", "default": 1.0}, ...],
      "indicators": [{"name": "result", "type": "dbl"}],
      "constants":  [{"name": "k", "type": "dbl", "value": 2.5}],
      "functions":  [{"name": "add", "function": "Add"}],      # palette names; find them with
                    # lv_functions(query) and their terminals with function_terminals(name)
      "subvis":     [{"name": "s", "path": "C:\\\\abs\\\\Helper.vi"}],
      "wires":      [["x", "add.x"], ["k", "add.y"], ["add.x+y", "result"]]
    }
    names are ids ([A-Za-z_][A-Za-z0-9_]*). Wires go [from, to]. Controls, indicators and
    constants are referenced by name alone; functions and subVIs as "name.Terminal", where the
    terminal is its Context Help label (Add: x, y, x+y; Multiply: x, y, x*y) or, for a subVI,
    the label of its control/indicator.
    Types: dbl, sgl, i8..i64, u8..u64, bool, string, path; arrays "dbl[]", "string[][]";
    clusters {"cluster": [{"name": "a", "type": "dbl"}, ...]}; enums {"enum": ["Init", "Run", "Stop"]}
    (values, defaults and vi_run inputs/outputs are item names; a case on an enum has frames
    {"value": "Init"}, ...). Any other type (waveform, picture, timestamp, class objects, typedefs,
    numbers with units) can be copied from an existing VI: vi_dump gives such a control or constant a
    "flat" entry; use it as the type ({"flat": "...", "data": "..."}, with its default value).

    Structures (top level or nested, in "structures"; they hold constants/functions/subvis/
    structures of their own; all names are global and wires are always listed at the top level):
      {"name": "L", "type": "for_loop", "functions": [...], "shift_registers": ["acc"]}
          terminals L.N (count), L.i; shift register: acc.left (previous value), acc.right (next);
          acc.left[2], acc.left[3], ... stack it (values 2, 3, ... iterations back; wire each an
          initial value from outside like acc.left)
      {"name": "W", "type": "while_loop", "stop_if_true": true, ...}   terminals W.i, W.stop
      {"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
          {"value": true, ...contents}, {"value": 3}, {"value": "apple"}, {"default": true}]}
          terminal C.selector. Several values or ranges in one frame use LabVIEW's case-name text:
          {"value": "1..3, 5"}, {"value": "10.."}, {"value": "\"a\", \"b\""}. Values passing INTO a structure: wire the outer node straight to the
          inner node. Values leaving a loop: wire the inner node to the outer node (For Loops build
          an array, While Loops pass the last value). Values leaving a case: wire each frame's
          result to an output tunnel ("t"), then "t" to the destination.
    Example, sum of 0..n-1:
      {"controls": [{"name": "n", "type": "i32"}], "indicators": [{"name": "total", "type": "i32"}],
       "constants": [{"name": "zero", "type": "i32", "value": 0}],
       "structures": [{"name": "L", "type": "for_loop", "shift_registers": ["acc"],
                       "functions": [{"name": "a", "function": "Add"}]}],
       "wires": [["n", "L.N"], ["zero", "acc.left"], ["acc.left", "a.x"], ["L.i", "a.y"],
                 ["a.x+y", "acc.right"], ["acc.right", "total"]]}
    Functions LabVIEW's scripting has no name for (Merge Errors, First Call?, To Time Stamp, TDMS,
    newer file I/O, Shared Variable) are placed by their palette name and wired by terminal
    reference (not inside event frames); Merge Errors: "size" = inputs, wired m.error in[1], [2], ...
    Static VI Reference: {"name": "r", "function": "Static VI Reference", "vi": "<absolute .vi path>",
    "strict": true}; wire "r.vi reference" (not inside event frames).
    Growable functions take "size" (check it with function_terminals(name, size)):
      {"name": "cat", "function": "Concatenate Strings", "size": 3} -> inputs cat.element[1..3],
      output cat.concatenated string; Build Array likewise ("appended array"); Unbundle (by
      position, size = number of cluster elements) -> u.cluster in, u.element[i] out; Format Into
      String with 2 inputs -> "size": 5, wired as fs.input 1, fs.input 2.
      Build Array with "concatenate": true joins array inputs end to end (Concatenate Inputs).
      Unbundle By Name / Bundle By Name: wire the cluster to "u.input cluster" and wire elements by
      name ("u.gain", "b.offset"); the node shows the elements its wires name, in wire order (or set
      "elements": [...] for another order). Build Waveform ("w.waveform" in, "w.output waveform"
      out) and Get Waveform Components ("g.waveform" in) work the same way with the components
      t0, dt, Y, attributes, error.
    A case wired to an error cluster has frames "No Error" and "Error".
    To pass a value through a case frame unchanged, give that frame "pass": [["acc.left", "t"]]
    (source, output tunnel); a wire from outside straight to an output tunnel does not work.
    Loop tunnels: a For Loop indexes arrays going in and builds arrays coming out, a While Loop
    does neither. To change that, give the wire as {"from": ..., "to": ..., "indexing": false}
    (or true; or {"<loop>": false} for one of several loops it crosses).
    Queues (producer/consumer, queued message handler): "Obtain Queue" (wire a constant of the
    element type to "element data type"; "queue out"), "Enqueue Element" ("queue", "element"),
    "Dequeue Element" ("queue", "element" out, "queue out"), "Release Queue" ("queue"). Loops in
    parallel share the queue wire; release it from the consumer's "queue out".
    State machine: a while loop with shift registers ["state", ...], the enum state wired to
    state.left -> C.selector, one case frame per state, each frame wiring the next state to an
    output tunnel -> state.right, and a boolean tunnel -> W.stop.
    Connector pane: every control and indicator is wired to the 4x2x2x4 connector pane so the VI
    works as a subVI right away (controls on the left then top, indicators on the right then
    bottom, in spec order). Pin a slot with "connector": "left3" (left0-3, top0-1, bottom0-1,
    right0-3) or leave one off with "connector": false. LabVIEW convention: error in at left3,
    error out at right3; type "error" is an error cluster.
    Front-panel look: "style" on a control/indicator replaces it with that Controls-palette object of
    the same data type: bool "Stop Button", "OK Button", "Round LED", "Vertical Toggle Switch"; numeric
    "Knob", "Gauge", "Thermometer", "Vertical Pointer Slide", "Waveform Chart" (dbl indicator).
    User interfaces: an event structure, usually inside a while loop, runs one frame per user event:
      {"name": "E", "type": "event", "events": [
          {"control": "stop"},                                   # "event" defaults to "Value Change"
          {"control": "go", "event": "Value Change", "functions": [...], ...contents}]}
    "control" names a control/indicator of the spec. Other events: "Mouse Down", "Mouse Up",
    "Mouse Enter", "Mouse Leave", "Key Down", ... Events of the VI itself have no "control":
      {"event": "Timeout", "timeout": 100, ...contents}     # runs when no event came for 100 ms
      {"event": "Panel Close?", "filter": "f", ...}         # wire a True constant to "f.Discard?"
      (also "Panel Close", "Panel Resize", "Key Down", "Mouse Enter", ...).
    Event data: "data": "d" names the frame's event data node; wire "d.NewVal", "d.OldVal",
    "d.Coords", "d.Button", "d.CtlRef", ... (the node shows the items that are wired).
    Code in a frame may be wired to code in the same
    frame and to objects outside the event structure, also shift registers and the output tunnels of
    a case around it. One frame can handle more events: "more": [{"control": "b"}, {"event":
    "Panel Close"}]. A value a frame passes on unchanged (state in a shift register) is listed in that
    frame's "wires": {"control": "stop", "wires": [["state.left", "state.right"]]}. A case output
    tunnel left unwired in some frames gives its default there. A front-panel terminal
    used by only one frame is placed in it (read when the event happens), and [button, "W.stop"]
    reads the button in its own frame and stops the loop W, as in NI's templates. Example:
      {"controls": [{"name": "stop", "type": "bool", "style": "Stop Button"},
                    {"name": "go", "type": "bool", "label": "Double it", "style": "OK Button"},
                    {"name": "x", "type": "dbl", "style": "Knob"}],
       "indicators": [{"name": "y", "type": "dbl", "style": "Gauge"}],
       "structures": [{"name": "W", "type": "while_loop", "structures": [{"name": "E", "type": "event",
           "events": [{"control": "stop"}, {"control": "go", "functions": [{"name": "m",
           "function": "Multiply"}], "constants": [{"name": "k", "type": "dbl", "value": 2}]}]}]}],
       "wires": [["stop", "W.stop"], ["x", "m.x"], ["k", "m.y"], ["m.x*y", "y"]]}
    Test a UI with vi_run's "actions" (click buttons, set values, read indicators while it runs).
    The new VI has LabVIEW's automatic error handling off, so an unwired error does not open a
    dialog that would block LabVIEW; "auto_error_handling": true in the spec keeps it on.
    Property and Invoke Nodes (top level, in loops, cases and event frames):
      "property_nodes": [{"name": "p", "control": "chart", "properties": ["Visible"], "write": true},
                         {"name": "a", "class": "Application", "properties": ["App.Version"]}],
      "invoke_nodes":   [{"name": "i", "control": "x", "method": "Reinit To Dflt"}]
    "control" links the node to a front-panel object (it gets that object's class); "class" makes
    an unlinked node (wire a reference into "p.reference"; Application needs none). Properties are
    read unless "write" is true or lists them. Wire "p.Visible", "i.<parameter>", "p.error in",
    "p.error out"; chain error wires to order nodes. Names are LabVIEW's short names ("Visible",
    "Value", "DefVal", "Disabled", "App.Version", method "Reinit To Dflt"). Nested properties are
    written as LabVIEW shows them: "Label.Text", "Plot.Plot Color", "YScale.Multiplier"; wire them
    the same way ("p.Label.Text"). A node linked to a chart, slide, listbox, ... needs that control's
    "style" ("Waveform Chart", "Vertical Fill Slide", "Listbox") so the control has those properties.
    The diagram is tidied with LabVIEW's Clean Up Diagram."""
    res = await _run(lv.vi_create, path, spec, overwrite, description)
    ui = bool(res.get("event_frames") or res.get("styled"))
    text, imgs = await _run(lv.vi_snapshot, res["path"], ui, True, bool(res.get("event_frames")), False)
    out: list[Any] = [
        f"Created {res['path']} (state: {res['exec_state']}).\n"
        f"Front panel: {res['front_panel']}\n"
        f"Connector pane (callers wire these labels): {res['connector_pane']}\n"
        + (f"Event frames: {res['event_frames']}\n" if res.get("event_frames") else "")
        + (f"WARNING: {res['warning']}\n" if res.get("warning") else "")
        + (f"The VI is BROKEN. LabVIEW's error list:\n{_error_lines(res['errors'])}\n" if res.get("errors") else "")
        + "Check the pictures (the front panel, the diagram, and each event frame): a wire that "
        "LabVIEW could not connect is simply missing. Then call vi_run to test."
    ]
    for p in imgs[:8]:
        out.append(_image(p, 2000))
    return out


@mcp.tool()
async def vi_dump(path: str, method: str = "auto") -> dict:
    """The block diagram as a graph: every node (controls, indicators, constants, functions by
    palette name, subVIs, loops, cases and their frames, tunnels, shift registers, property/invoke
    nodes), which diagram it sits in ("in": a structure id, "case_4#1" = frame 1), and every wire
    with its source and sinks as "node.terminal". Node ids are what vi_edit's "remove" and
    "existing" take; dump again after each edit. Controls carry their front-panel class
    ("vi_class": "WaveformChart", "Slide", ...). Property Nodes carry "vi_class", the linked
    "control" label if any, "items" (terminal names, top to bottom) and "write"; Invoke Nodes
    their "method".

    "source" says how it was read. method="auto" first uses LabVIEW's compiler export ("dfir": adds
    wire data types and case frame values, but refuses VIs with some controls or nodes), then
    walks the diagram objects instead ("scan": works on any VI, including broken ones). Both
    include constant values. method="dfir" or "scan" forces one. Password-protected VIs cannot be read."""
    return await _run(lv.vi_dump, path, method)


@mcp.tool(structured_output=False)
async def vi_edit(
    path: str,
    spec: dict[str, Any],
    backup: bool = True,
    allow_broken: bool = False,
    rewire: bool = False,
) -> list[Any]:
    """Change an existing VI: remove nodes, add code, and wire it to what is already there, including
    inside existing loops and case frames. The rest of the diagram keeps its layout. Call vi_dump
    first to get node ids.

    spec = the vi_create spec plus:
      "remove": ["increment_4", ...]   vi_dump node ids to delete, with their wires
      "existing": {spec name: what}    objects already in the VI that the spec refers to:
          "Gain"                         a front-panel control/indicator by label
          {"node": "multiply_7"}         a vi_dump node (function, subVI, constant, loop, case)
          {"node": "for_loop_3", "functions": [...], ...}       new code inside an existing loop
          {"node": "case_structure_5", "cases": {"1": {...}}}   new code inside case frame 1
          {"node": "for_loop_3", "shift_registers": ["acc"], ...}  new shift registers on a loop
                                       (wire "acc.left" / "acc.right" like in vi_create)
          {"node": "case_structure_5", "remove_cases": [2],
           "add_cases": [{"value": 5, "outputs": {"tunnel_9": "five"}, ...contents},
                         {"value": "7..9", "default": true}]}
                                       remove frames (by index) and add frames (value, range or
                                       string as the selector takes); "outputs" gives the new frame's
                                       value for existing output tunnels (vi_dump tunnel ids)
      "move": {"for_loop_3": [300, 200]}   move block-diagram objects
      "rename": {"Gain": "Gain (dB)"}  relabel front-panel controls/indicators
      "values": {"Gain": 2.5, "constant_6": 10}   a control's default value (by label) or a
                                       constant's value (vi_dump id)
      "retype": {"Count": "i32", "constant_6": "u8"}   change a number's type (control or constant)
      "disconnect": ["out_12", "multiply_7.x"]   cut the wire into these vi_dump wire ends (a
                                       wire's other branches stay); then wire a new source in "wires":
          {"disconnect": ["x2_5"], "existing": {"a": {"node": "add_8"}, "o": "x2"},
           "wires": [["a.x+y", "o"]]}
    Existing nodes are wired like new ones ("m.x", "L.i", "C.selector"). Code put in a case frame
    may only be wired to code in that frame and to existing objects; crossing the border makes a
    tunnel. Example, replace out = x + 1 with out = x * 3:
      {"remove": ["increment_4"], "existing": {"x": "x", "o": "out"},
       "constants": [{"name": "k", "type": "dbl", "value": 3}],
       "functions": [{"name": "m", "function": "Multiply"}],
       "wires": [["x", "m.x"], ["k", "m.y"], ["m.x*y", "o"]]}
    An input that is already wired cannot take a second source (remove its node first). New
    controls stay off the connector pane unless they set "connector". rewire=true deletes with
    LabVIEW's Quick Drop "Remove and Rewire" (joins loose ends of matching type) instead.
    A copy is saved to .lvmcp-backup/ next to the VI first (backup=false skips it). If the edit
    leaves the VI broken, or a requested wire could not be made, the VI is reverted and not saved
    (allow_broken=true saves a broken result anyway)."""
    res = await _run(lv.vi_edit, path, spec, backup, allow_broken, rewire)
    text, imgs = await _run(lv.vi_snapshot, res["path"], False, True, False, False)
    out: list[Any] = [
        f"Edited {res['path']} (state: {res['exec_state']}). Backup: {res['backup']}.\n"
        f"Removed: {res.get('removed', 0)} node(s). New front-panel objects: {res['added_front_panel']}\n"
        + (f"The VI is BROKEN (allow_broken). LabVIEW's error list:\n{_error_lines(res['errors'])}\n"
           if res.get("errors") else "")
        + "Check the picture, then vi_run to test."
    ]
    for p in imgs[:1]:
        out.append(_image(p, 2000))
    return out


@mcp.tool()
async def vi_test(path: str, cases: list[dict], timeout_s: float = 30.0, tol: float = 1e-9) -> dict:
    """Test a VI against many input sets at once: run it once per case and compare the outputs.
    cases: [{"name": "zero gain", "inputs": {"Gain": 0, "x": 5}, "expect": {"y": 0}},
            {"inputs": {"x": [1, 2]}, "expect": {"sum": 3.0, "mean": 1.5}, "tol": 1e-6}, ...]
    Numbers match within tol (relative to the expected value, at least absolute tol), arrays and
    clusters (lists) element by element, strings and booleans exactly. Returns "summary"
    ("9/10 cases passed") and per case "passed" with the mismatches (expected vs got) or the error.
    Same rules as vi_run: it executes code; only test VIs the user asked to run."""
    return await _run(lv.vi_test, path, cases, timeout_s, tol)


@mcp.tool()
async def vi_diff(path_a: str, path_b: str | None = None) -> dict:
    """What changed between two versions of a VI, as a list of semantic changes: nodes added and
    removed, constants with new values, controls renamed or retyped, objects moved into another
    structure or frame, and wires added and removed. path_a is the old version, path_b the new one.
    Without path_b it compares the newest vi_edit backup of path_a (.lvmcp-backup/) with path_a, so
    after vi_edit it shows exactly what the edit did. Objects are matched by their LabVIEW UID,
    which survives edits. Neither file is changed."""
    return await _run(lv.vi_diff, path_a, path_b)


@mcp.tool()
async def lv_functions(query: str) -> dict:
    """Search LabVIEW's palette functions by name (words match in any order), e.g. "array",
    "string length", "file". Use the exact name as "function" in vi_create, and call
    function_terminals(name) to learn its terminal names."""
    return await _run(lv.lv_functions, query)


@mcp.tool()
async def lv_class_properties(class_name: str, query: str = "") -> dict:
    """Properties of a VI Server class as LabVIEW lists them (All Supported Properties), filtered by
    `query` (words in the name or long name, e.g. "visible", "plot color"). Use "name" in a property
    node's "properties" in vi_create / vi_edit. class_name is a class like "Boolean", "Digital",
    "WaveformChart", "Control", "VI", "Application"; a node linked to a front-panel object has that
    object's class (vi_dump / vi_text show the control type; its parent classes also apply)."""
    return await _run(lv.class_properties, class_name, query)


@mcp.tool()
async def function_terminals(name: str, size: int | None = None) -> dict:
    """Exact terminal names of one palette function, verified against LabVIEW's scripting engine
    (Context Help labels sometimes differ, e.g. "Variant" vs "variant"). Wire them in vi_create
    as "<node>.<terminal>". "help_labels" lists the Context Help text; inputs usually carry a
    default in parentheses there. Growable nodes are wired as element[1..N] instead."""
    return await _run(lv.function_terminals, name)


@mcp.tool()
async def project_info(path: str) -> dict:
    """Contents of a LabVIEW project (.lvproj): targets, items (with project path like
    "/My Computer/Main.vi" and file path) and build specifications (type, startup and always-included
    VIs, output folder and primary output, version). Reads the file; nothing is opened or changed."""
    return await _run(projects.info, path)


@mcp.tool()
async def project_create(path: str, files: list[str] | None = None,
                         build_specs: list[dict] | None = None, overwrite: bool = False) -> dict:
    """Create a LabVIEW project (.lvproj) under My Computer holding `files` (absolute paths of VIs,
    controls, libraries, other files), optionally with build specs. LabVIEW makes the project and
    adds the items itself.

    A build spec: {"name": "My App", "type": "exe" | "ppl" | "source", "version": "1.0.0.0",
    "description": "...", "output_dir": "C:/.../builds/My App" (default <project folder>/builds/
    <name>), "file_name": "MyApp.exe" (exe/ppl; default <name>.exe / <name>.lvlibp), plus
      exe (application): "startup": ["C:/.../Main.vi"] (opens when the EXE runs), "include":
        [always-included VIs, e.g. ones called dynamically];
      ppl (packed library): "library": "C:/.../My.lvlib", "include": [...];
      source (source distribution, copies the VIs and their non-vi.lib subVIs): "include": [...]}.
    Source files are added to the project if missing. Build it with project_build. Returns
    project_info of the result."""
    return await _run(projects.create, path, files or [], build_specs or [], overwrite)


@mcp.tool()
async def project_add(path: str, files: list[str] | None = None,
                      build_specs: list[dict] | None = None) -> dict:
    """Add files and/or new build specs (same format as project_create) to an existing project.
    Files already in the project are skipped; a build spec name already in use is refused. LabVIEW
    opens, saves and closes the project, so close it in the LabVIEW IDE first if it has unsaved
    changes there."""
    return await _run(projects.add, path, files or [], build_specs or [])


@mcp.tool()
async def project_build(path: str, build_spec: str = "", target: str = "My Computer") -> dict:
    """Build a project's build specification with NI's Application Builder (empty build_spec =
    build all of the target's specs). Returns the generated files (the .exe first for an
    application). A broken VI fails the build with LabVIEW's list of broken VIs and reasons; fix
    them (vi_snapshot / vi_edit) and build again. The built EXE needs the LabVIEW Run-Time Engine
    of this LabVIEW version on the machine that runs it."""
    return await _run(projects.build, path, build_spec, target)


@mcp.tool()
async def lv_mass_compile(directory: str) -> dict:
    """Mass compile every VI under a folder in this LabVIEW version (recompiles and saves them; use
    after upgrading LabVIEW or moving code). "problems" lists the log lines about VIs that failed
    to load, were broken or could not be saved; an empty list means all compiled cleanly."""
    return await _run(projects.mass_compile, directory)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
