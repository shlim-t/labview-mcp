# labview-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI coding agents — Claude Code, Codex, Cursor, and others — **read, render, run, create, and edit NI LabVIEW VIs**.

LabVIEW source code is a binary, graphical format. Text-based coding agents can't open a `.vi` file. labview-mcp acts as the agent's eyes and hands: it drives your installed LabVIEW over its ActiveX server and returns what the agent needs as text, images, and values.

> Status: **v0.37.0.** Reading and running work on any VI. Creating VIs supports controls, constants, functions, subVIs, For/While loops, shift registers (also stacked), and case structures, nested to any depth, and **user interfaces**: front-panel styles (buttons, LEDs, knobs, gauges, charts), event structures (control and VI events, Timeout, event data), and Property and Invoke Nodes. Any existing VI can be read as a graph (`vi_dump`) and changed in place (`vi_edit`: remove, replace, and add code, including inside existing loops and case frames). **Projects and builds**: the agent can put VIs in a project, add application (EXE), packed-library, and source-distribution build specs, build them, and mass compile folders.

## What the agent can do

| Tool | What it returns |
|---|---|
| `lv_status` | LabVIEW version and install directory. Starts LabVIEW if it isn't running. |
| `vi_info` | VI type, description, the LabVIEW version it was saved in, broken/idle state, unsaved changes, subVIs. **For a broken VI, LabVIEW's error list**: each error with LabVIEW's explanation ("Add: Contains unwired or bad terminal", "You have connected two terminals of different types", with the source and sink types). |
| `vi_text` | LabVIEW's own string export: front-panel controls (nested for clusters), descriptions, and block-diagram labels and comments. **Case structures are exported one block per case**, including hidden cases. |
| `vi_snapshot` | PNG images of the block diagram, front panel, or connector pane. `all_frames=true` renders **every hidden case, sequence, and event frame**. |
| `vi_hierarchy` | The subVI call tree. vi.lib, user.lib, and instr.lib are hidden by default. |
| `vi_run` | Sets inputs, runs the VI, and returns all front-panel values. **While the VI runs, it can operate the front panel as a user would**: click buttons, change values, and read indicators. A UI VI can therefore be tested end to end. It stops VIs that don't finish within the time limit. |
| `vi_test` | **Tests a VI against many input sets at once** and returns a pass/fail table: numbers within a tolerance, arrays and clusters element by element, and for each failure the expected and actual values. |
| `vi_dump` | **The block diagram as a JSON graph**: every node (functions by palette name, constants with their values, subVIs, loops, case frames, tunnels, shift registers, property nodes) and every wire with its source and sinks. Works on any VI, including broken ones. |
| `vi_edit` | **Changes an existing VI in place.** It removes nodes, adds code, and wires to existing controls, nodes, loops, and case frames (by `vi_dump` id). The rest of the diagram keeps its layout. It backs up the VI first, and reverts instead of saving if the result is broken (naming LabVIEW's errors) or a wire could not be made. |
| `vi_diff` | **What changed between two versions of a VI**: nodes added and removed, constant values, renamed or retyped controls, objects moved between structures, and wires added and removed. Objects are matched by their LabVIEW UID. Without a second path it compares the VI with its newest `vi_edit` backup, which shows exactly what an edit did. |
| `lv_functions` | Searches the palette functions in your LabVIEW install by name. |
| `lv_class_properties` | The properties of a VI Server class, such as Boolean, WaveformChart, or VI, **as LabVIEW itself lists them**. You can filter them by a word. Use the names in Property Nodes. |
| `function_terminals` | Exact terminal names of a function, **read from a node LabVIEW places**, with inputs and outputs, so the agent wires them right the first time. Names that cannot be wired by script are flagged. |
| `vi_create` | **Builds a new VI** from a JSON spec: typed controls and indicators with defaults and front-panel styles, constants, palette functions, subVI calls, For/While loops, shift registers, case structures, event structures, and wires. LabVIEW's Clean Up Diagram tidies the result, and the tool returns pictures of it. Automatic error handling is off in the new VI, so an unwired error cannot open a dialog that blocks LabVIEW. |
| `project_create` / `project_add` | **Creates or extends a LabVIEW project** (`.lvproj`): adds files, and adds build specs for an application (EXE), a packed library (`.lvlibp`), or a source distribution. LabVIEW creates the project and its items itself. |
| `project_info` | A project's targets, items, and build specs (startup VIs, output folder, version), read from the file. |
| `project_build` | **Builds a build spec with NI's Application Builder** and returns the generated files. A broken VI fails the build with LabVIEW's list of broken VIs and their reasons. |
| `lv_mass_compile` | Mass compiles a folder and lists the VIs that were broken or failed to load. |

Example prompts:

- "Explain what `Main.vi` does, case by case."
- "Which subVIs write to the `Config` global?"
- "Run `Scale.vi` with gain 2.5 and check `out` matches my Python reference."
- "Test `Stats.vi` with an empty array, one value, and 1000 random values."
- "Make a VI that takes an array of readings and returns their sum and mean, then test it."
- "In `Scale.vi`, also output `Gain` squared, then run it."
- "Make a panel with a knob, a gauge, a Double button and a STOP button."
- "Put `Main.vi` in a project, build it as `Logger.exe`, and check the EXE runs."

### `vi_dump` output (excerpt)

```json
{"nodes": [{"id": "control_2", "kind": "control", "label": "data", "type": "dbl[]", "conpane_index": 11},
           {"id": "divide_7", "kind": "function", "function": "Divide"},
           {"id": "case_structure_4", "kind": "case", "frames": [{"index": 0, "values": ["apple"]},
                                                                  {"index": 2, "values": [], "default": true}]}],
 "wires": [{"from": "add_array_elements_5.sum", "to": ["divide_7.x"], "type": "dbl"},
           {"from": "tunnel_9.outer", "to": ["indicator_3"], "type": "dbl"}]}
```

`vi_dump` reads a diagram in one of two ways and reports which one it used in `"source"`:

- `"dfir"` uses LabVIEW's compiler export. It includes constant values, data types, and case values, but LabVIEW refuses it for some VIs, for example ones with certain front-panel controls.
- `"scan"` walks every diagram object with VI Scripting. It works on any VI that opens, including broken VIs and VIs with property nodes or event structures.

Both ways report constant values, including arrays and clusters.

On a random sample of 50 NI example VIs, every VI was read. Password-protected VIs are refused, by design.

### `vi_edit` spec

It takes the `vi_create` spec plus two keys:

- `"remove"` lists `vi_dump` node ids to delete, together with their wires.
- `"existing"` names objects already in the VI: a front-panel label, `{"node": id}` for any node, `{"node": loop_id, "functions": [...]}` to add code inside a loop, or `{"node": case_id, "cases": {"1": {...}}}` to add code inside a case frame.

This spec replaces `out = x + 1` with `out = x * 3`:

```json
{"remove":    ["increment_4"],
 "existing":  {"x": "x", "o": "out"},
 "constants": [{"name": "k", "type": "dbl", "value": 3}],
 "functions": [{"name": "m", "function": "Multiply"}],
 "wires":     [["x", "m.x"], ["k", "m.y"], ["m.x*y", "o"]]}
```

Small changes need no new code:

| Key | Example | Effect |
|---|---|---|
| `"rename"` | `{"Gain": "Gain (dB)"}` | Relabels front-panel controls and indicators. |
| `"values"` | `{"Gain": 2.5, "constant_6": 10}` | Sets a control's default value (by label) or a constant's value (by `vi_dump` id). |
| `"retype"` | `{"Count": "i32", "constant_6": "u8"}` | Changes the numeric type of a control or a constant. |
| `"disconnect"` | `["x2_5", "multiply_7.x"]` | Cuts the wire into these `vi_dump` wire ends. The wire's other branches stay. Wire a new source in `"wires"`. |
| `"move"` | `{"for_loop_3": [300, 200]}` | Moves block-diagram objects. |

Structural edits go in `"existing"`:

- `{"node": "for_loop_3", "shift_registers": ["acc"]}` adds shift registers to a loop. Wire `acc.left` and `acc.right` as in `vi_create`.
- `{"node": "case_structure_5", "remove_cases": [2], "add_cases": [...]}` removes and adds case frames. Each new frame has a `"value"` (a number, a range like `"7..9"`, or a string, as the selector takes), optional `"default": true`, its contents, and `"outputs"` that give its value for existing output tunnels by `vi_dump` tunnel id: `{"value": 5, "outputs": {"tunnel_9": "five"}}`.

Objects are matched by their LabVIEW UID, which `vi_dump` reports. Edits therefore hit exactly the node you named, even in a VI with many identical functions.

### `vi_create` spec

```json
{
  "controls":   [{"name": "x", "type": "dbl", "default": 1.5},
                 {"name": "readings", "type": "dbl[]"}],
  "indicators": [{"name": "y", "type": "dbl"}, {"name": "total", "type": "dbl"}],
  "constants":  [{"name": "k", "type": "dbl", "value": 2.5}],
  "functions":  [{"name": "add", "function": "Add"},
                 {"name": "sum", "function": "Add Array Elements"}],
  "subvis":     [],
  "wires":      [["x", "add.x"], ["k", "add.y"], ["add.x+y", "y"],
                 ["readings", "sum.numeric array"], ["sum.sum", "total"]]
}
```

Loops and case structures go in `"structures"` and can hold their own constants, functions, subVIs, and structures. This spec sums 0..n-1 with a shift register:

```json
{
  "controls":   [{"name": "n", "type": "i32", "default": 5}],
  "indicators": [{"name": "total", "type": "i32"}],
  "constants":  [{"name": "zero", "type": "i32", "value": 0}],
  "structures": [{"name": "L", "type": "for_loop", "shift_registers": ["acc"],
                  "functions": [{"name": "a", "function": "Add"}]}],
  "wires": [["n", "L.N"], ["zero", "acc.left"], ["acc.left", "a.x"], ["L.i", "a.y"],
            ["a.x+y", "acc.right"], ["acc.right", "total"]]
}
```

| Structure | Spec | Terminals |
|---|---|---|
| For Loop | `{"type": "for_loop", "shift_registers": ["acc"], ...}` | `L.N`, `L.i`, `acc.left`, `acc.right`; stacked: `acc.left[2]`, `acc.left[3]`, ... |
| While Loop | `{"type": "while_loop", "stop_if_true": true, ...}` | `W.i`, `W.stop` |
| Case | `{"type": "case", "output_tunnels": ["t"], "cases": [{"value": true, ...}, {"default": true, ...}]}` | `C.selector`, tunnel `t` |

A shift register is stacked by wiring its older values: `acc.left[2]` is the value from two iterations back, `acc.left[3]` from three, and so on. Wire an initial value into each one from outside the loop, as for `acc.left`.

Some palette functions have no name in LabVIEW's scripting, such as Merge Errors, First Call?, To Time Stamp, the TDMS and newer file functions, and Shared Variable functions. `vi_create` places them by their palette name instead (a stand-in node replaced with GObject.Replace) and wires them by terminal reference, outside event structures. Merge Errors takes `"size"`, the number of inputs (`m.error in[1]`, `m.error in[2]`, ...).

A **Static VI Reference** points at a VI: `{"name": "r", "function": "Static VI Reference", "vi": "C:\\path\\Callee.vi", "strict": true}`. `"strict": true` makes it a strictly typed reference (for Call By Reference, Start Asynchronous Call); without `"vi"` it is an empty reference. Wire it as `r.vi reference`. `vi_dump` gives the VI it points at as `"path"` and `"strict"`.

Growable functions take `"size"`. For example, Concatenate Strings with `"size": 3` is wired as `cat.element[1..3]`. `function_terminals(name, size)` shows the terminals a grown node has. A case wired to an `error` cluster gets `"No Error"` and `"Error"` frames.

Wire straight across structure borders and LabVIEW adds the tunnels. For Loops build arrays from values that leave them, and While Loops pass out the last value. To change a tunnel's indexing, give the wire as `{"from": ..., "to": ..., "indexing": false}` (or `true`, or `{"<loop>": false}` for one of several loops it crosses). Each frame of a case wires its result to an output tunnel.

**Connector pane:** every control and indicator is wired to the standard 4x2x2x4 connector pane, so a generated VI works as a subVI right away. Inputs go on the left, outputs on the right, and `error` clusters on the bottom corners, as in NI's own VIs. To pin a slot, use `"connector": "left3"`; to leave a terminal unwired, use `"connector": false`. The tool reports the resulting layout.

### Enums and state machines

`{"enum": ["Init", "Add", "Done"]}` is an enum type. Values, defaults, and `vi_run` inputs and outputs use the item names. A case on an enum has one frame per item, such as `{"value": "Init", ...}`.

A state machine is built from parts that already exist in the spec:

- a while loop with a shift register that holds the state;
- a case structure on the state, with one frame per state;
- in each frame, the next state wired to an output tunnel that feeds the shift register.

To pass a value through a frame unchanged, give the frame `"pass": [["acc.left", "t"]]`, where the pair is a source and an output tunnel. `tests/live_vi_create.py` (`state_machine`) has a complete example that runs Init → Add ×n → Done.

### Queues: producer/consumer and queued message handlers

Queue functions are ordinary palette functions:

| Function | Terminals |
|---|---|
| `Obtain Queue` | Wire a constant of the element type to `element data type`. The output is `queue out`. |
| `Enqueue Element` | `queue`, `element` |
| `Dequeue Element` | `queue`, `element`, `queue out` |
| `Release Queue` | `queue` |

Loops placed side by side run in parallel and share the queue wire. `tests/live_vi_create.py` has two complete examples:

- **`producer_consumer`:** a For Loop enqueues numbers and a While Loop sums them.
- **`qmh`:** a queued message handler. Button events in a UI loop enqueue enum messages, and a second loop handles each message in a case until `Exit`.

### User interfaces

`"style"` on a control or indicator replaces it with the Controls-palette object of that name, keeping its label, terminal and wires: `"Stop Button"`, `"OK Button"`, `"Round LED"`, `"Vertical Toggle Switch"` for booleans, `"Knob"`, `"Gauge"`, `"Thermometer"`, `"Waveform Chart"` for numbers.

An event structure runs one frame per user event. This spec is a panel whose **Double it** button sets `y = 2x` and whose **STOP** button ends the loop:

```json
{
  "controls":   [{"name": "stop", "type": "bool", "label": "STOP", "style": "Stop Button"},
                 {"name": "go", "type": "bool", "label": "Double it", "style": "OK Button"},
                 {"name": "x", "type": "dbl", "style": "Knob"}],
  "indicators": [{"name": "y", "type": "dbl", "style": "Gauge"}],
  "structures": [{"name": "W", "type": "while_loop", "structures": [
                   {"name": "E", "type": "event", "events": [
                     {"control": "stop"},
                     {"control": "go", "functions": [{"name": "m", "function": "Multiply"}],
                      "constants": [{"name": "k", "type": "dbl", "value": 2}]}]}]}],
  "wires": [["stop", "W.stop"], ["x", "m.x"], ["k", "m.y"], ["m.x*y", "y"]]
}
```

The result follows NI's event-handler pattern:

- The STOP button is read in its own Value Change frame, which stops the loop.
- `x` and `y` are used only by the Double it frame, so their terminals are placed in that frame. The knob is therefore read when the button is pressed, not once before the loop.

To test a UI, give `vi_run` the steps a user would take:

```json
"actions": [{"set": {"x": 21}}, {"click": "Double it"}, {"read": ["y"]}, {"click": "STOP"}]
```

- **`click`** presses a button, including a latched one such as STOP.
- **`set`** changes controls.
- Both fire the controls' Value Change events, as a real click or edit would.
- **`read`** records values while the VI runs.
- **`wait`** pauses for the given number of seconds.

The result lists each step. Here it reads `y = 42` and then the VI stops by itself.

`"event"` defaults to `"Value Change"`. Other control events include `"Mouse Down"`, `"Mouse Up"`, `"Mouse Enter"`, `"Mouse Leave"`, and `"Key Down"`. Code in a frame can be wired to its own frame and to anything outside the event structure, including shift registers and a surrounding case's output tunnels. One frame can handle several events: `"more": [{"control": "b"}, {"event": "Panel Close"}]`. A value a frame passes on unchanged, such as a state kept in a shift register, goes in that frame's `"wires"`: `{"control": "stop", "wires": [["state.left", "state.right"]]}`. A case output tunnel that some frames leave unwired gives its default value there ("Use Default if Unwired"). Event structures are made by `vi_create` only, for now.

Events of the VI itself have no `"control"`, and frames can use their event data:

```json
{"name": "E", "type": "event", "events": [
  {"control": "stop"},
  {"control": "x", "data": "d"},
  {"event": "Timeout", "timeout": 50, "functions": [{"name": "inc", "function": "Increment"}]},
  {"event": "Panel Close?", "filter": "f", "constants": [{"name": "t", "type": "bool", "value": true}]}]}
```

with wires `["d.NewVal", "last"]`, `["W.i", "inc.x"]`, `["inc.x+1", "ticks"]`, `["t", "f.Discard?"]`.

- `"data"` names the frame's event data node. Wire its items by name, such as `NewVal`, `OldVal`, `CtlRef`, `Coords`, and `Button`. The node shows the items that are wired.
- `"Timeout"` runs when no other event arrives within `"timeout"` milliseconds. The constant is wired to the structure's hourglass terminal.
- Filter events end in `?`. `"filter"` names their filter node, such as `Discard?` for `"Panel Close?"`, which keeps the window open.
- Other VI events: `"Panel Close"`, `"Panel Resize"`, `"Key Down"`, `"Mouse Enter"`, and more.


### Property and Invoke Nodes

```json
"property_nodes": [{"name": "p", "control": "chart", "properties": ["Visible"], "write": true},
                   {"name": "a", "class": "Application", "properties": ["App.Version"]}],
"invoke_nodes":   [{"name": "i", "control": "x", "method": "Reinit To Dflt"}]
```

- **Linked:** `"control"` links a node to a front-panel object, like right-clicking it and choosing Create ▸ Property Node.
- **Unlinked:** `"class"` makes an unlinked node. Wire a reference to `"p.reference"`; Application needs none.
- **Read or write:** properties are read unless `"write"` is `true` or lists them.
- **Wiring:** wire nodes as `"p.Visible"`, `"i.<parameter>"`, `"p.error in"`, and `"p.error out"`. Chain the error wires to set the order of nodes.
- **Names:** use LabVIEW's short names, such as `Visible`, `Value`, `DefVal`, `Disabled`, and `App.Version`. `lv_class_properties` lists them for a class. If LabVIEW names a property's terminal differently, the wire is redirected to the right terminal.
- **Nested properties:** write them as LabVIEW shows them, such as `Label.Text`, `Plot.Plot Color`, or `YScale.Multiplier`, and wire them the same way (`"p.Label.Text"`). LabVIEW's scripting call refuses nested names, so they are set through the item's own (deprecated) Property setting, as NI suggests on its forum.
- **Charts and other UI controls:** a node linked to a chart, slide, or listbox needs that control's `"style"` (`"Waveform Chart"`, `"Vertical Fill Slide"`, `"Listbox"`), so the control has those properties.
- **Placement:** nodes work at the top level, in loops and cases, inside event frames, and in `vi_edit`.

Strings are stored in the Windows ANSI code page, as LabVIEW does, so characters outside it (for example `é` on Korean Windows) become the closest match.

Types: `dbl sgl i8 i16 i32 i64 u8 u16 u32 u64 bool string path error`, VI Server references (`"ref:Control"`, `"ref:VI"`, `"ref:Boolean[]"`), enums (`{"enum": [...]}`), arrays (`"dbl[]"`, `"i32[][]"`, or `{"array": <type>, "dims": 1}` for arrays of clusters), and clusters (`{"cluster": [{"name": "gain", "type": "dbl"}, ...]}`). Other types (waveform, picture, timestamp, class objects, typedefs, numbers with units) are copied from an existing VI: `vi_dump` gives such a control or constant a `"flat"` entry, and that entry (`{"flat": "...", "data": "..."}`) works as a type, with the original default value. Function terminals use the names shown in LabVIEW's Context Help; Unbundle By Name and Bundle By Name are wired by element name (`"u.gain"`), and so are Build Waveform and Get Waveform Components (components `t0`, `dt`, `Y`, `attributes`, `error`). Build Array takes `"concatenate": true` to join its array inputs end to end. SubVI terminals use the subVI's control labels.

### Projects and builds

```json
{"path": "C:/work/Demo/Demo.lvproj",
 "files": ["C:/work/Demo/Main.vi"],
 "build_specs": [
   {"name": "Demo App", "type": "exe", "startup": ["C:/work/Demo/Main.vi"],
    "include": ["C:/work/Demo/Plugin.vi"], "file_name": "Demo.exe", "version": "1.2.0.0"},
   {"name": "Lib", "type": "ppl", "library": "C:/work/Demo/Tools.lvlib"},
   {"name": "Src", "type": "source", "include": ["C:/work/Demo/Main.vi"]}
 ]}
```

`project_create` takes this. `project_build` with `build_spec="Demo App"` then produces `builds/Demo App/Demo.exe` next to the project. LabVIEW creates the project, its items, and the empty build specs through its ActiveX Project interface. The build settings are the properties that LabVIEW itself writes for each spec type, filled into the saved project file. `project_build` calls NI's Application Builder API (`AB_API_Simple/Build (path).vi`), the same VI that LabVIEWCLI's ExecuteBuildSpec operation uses.

- Application Builder is included in LabVIEW Community Edition and in the Professional edition. A built EXE needs the matching LabVIEW Run-Time Engine on the machine that runs it.
- `lv_mass_compile` saves the VIs it compiles. It refuses folders inside the LabVIEW installation. Don't mass compile a copy of a library while LabVIEW holds the original: the two get cross-linked, and LabVIEW re-saves the original.

## Requirements

- Windows with LabVIEW installed. Community Edition works for personal and non-commercial use.
- In LabVIEW, open **Tools ▸ Options ▸ VI Server** and enable **ActiveX**.
- Python ≥ 3.10.
- `vi_create`, `vi_dump`, and `vi_edit` use small helper VIs shipped in the package and saved in **LabVIEW 2015** format, so they open in LabVIEW 2015 and later. They call scripting methods that an older LabVIEW might lack. In that case the tool reports which helper cannot run, and `vi_dump` falls back to its scan reader where it can.

Tested on **LabVIEW 2026 Q3 (64-bit, English)** under Korean Windows. Reports from other versions and languages are welcome.

## Install

```bash
pip install git+https://github.com/shlim-t/labview-mcp
claude mcp add labview -s user -- python -m labview_mcp
```

Other MCP clients use the same command: `python -m labview_mcp` over stdio.

## How it works

```
agent ──MCP/stdio──▶ labview-mcp ──ActiveX (COM)──▶ LabVIEW.exe
                                                   ├─ ExportVIStrings  → vi_text
                                                   ├─ PrintVIToHTML    → vi_snapshot
                                                   ├─ CalleesPaths     → vi_hierarchy
                                                   ├─ Set/Run/GetValue → vi_run
                                                   ├─ helper VIs       → vi_create, vi_edit
                                                   ├─ Project + AB API → project_*, lv_mass_compile
                                                   └─ helper VI        → vi_dump
```

- All COM calls run on a single dedicated thread.
- `vi_snapshot` temporarily changes LabVIEW's print options to render hidden frames. It always restores them afterwards.
- Read tools never modify or save VI files. `vi_create` only writes the new file you name, and it refuses to overwrite an existing file unless you pass `overwrite=true`.
- `vi_edit` copies the VI to `.lvmcp-backup/` next to it before editing. It refuses to touch a VI with unsaved changes in LabVIEW. It reverts instead of saving if the edit breaks the VI or LabVIEW silently drops a requested wire.
- `vi_create` and `vi_edit` translate the spec into LabVIEW's internal diagram-scripting XML. Small helper VIs shipped in the package (`vis/`) run that script. The spec is validated in Python first, so malformed input never reaches LabVIEW.
- `vi_dump` runs LabVIEW's dataflow (DFIR) exporter through a helper VI and parses its output.

## Safety

`vi_run` executes LabVIEW code on your machine. That code can drive real instruments, such as DAQ, power supplies, and motion stages. MCP clients ask for confirmation before each tool call, so review what you approve.

## Roadmap

- [x] `vi_dump`: block-diagram graph (nodes, wires, constants) as JSON.
- [x] `vi_edit`: add code wired to an existing VI's controls and indicators, with backup and revert.
- [x] `vi_edit`: remove and replace nodes; add code inside existing loops and case frames.
- [x] `vi_edit`: rename controls, set constant and default values, retype numbers, cut and re-make wires.
- [x] `vi_edit`: move objects, add and remove case frames, add shift registers to existing loops.
- [ ] `vi_edit`: wrap existing code in a loop or structure.
- [x] `vi_dump` for any VI (scan fallback), including broken ones and VIs with property nodes and event structures.
- [x] `vi_diff`: a semantic diff between two versions of a VI, for code review on binary files.
- [x] Projects: create, add files and build specs (EXE, packed library, source distribution), build, mass compile.
- [ ] Installers, shared libraries (DLL), VI Analyzer, unit tests; creating libraries and classes.
- [x] `vi_create`: new VIs with controls, constants, functions, subVIs, and wires.
- [x] `vi_create`: For/While loops, shift registers, case structures (boolean, integer, string), nesting.
- [x] `vi_create`: connector pane and error clusters, so generated VIs can call each other.
- [x] `vi_create`: front-panel styles and event structures (control events) for user interfaces.
- [x] Property and Invoke Nodes, linked to controls or of any VI Server class.
- [x] `lv_class_properties`: a class's properties by name. Methods are not listed yet.
- [x] Enums, enum cases, and state machines.
- [x] Queues: producer/consumer and queued message handlers.
- [x] Event structures: Timeout, VI events (Panel Close?), event data (NewVal, Coords) and filter nodes (Discard?).
- [ ] Event structures: editing with `vi_edit`; dynamic (user) events.
- [x] `vi_run` operates a running VI's front panel: click, set, read.
- [x] Helper VIs saved for LabVIEW 2015 and later.
- [ ] Verified on LabVIEW versions older than 2026 and on localized (non-English) LabVIEW.

## Development

- **Hand-built helpers** are edited in LabVIEW from `vis_src/`.
- **Generated helpers** are built by `python tools/helpergen.py` from Python specs into `vis_src/`. Start from a freshly started LabVIEW so that no VI with the same name is already in memory. The generator uses diagram scripting together with the two hand-built configuration helpers: `lvmcp_propconfig` and `lvmcp_invokeconfig`.
- **Shipping:** `python tools/build_helpers.py` saves every helper for LabVIEW 2015 into `src/labview_mcp/vis/`, which is what ships.
- **Class table:** `src/labview_mcp/vi_classes.json` maps VI Server class names to the class numbers used in refnum type descriptors. It was measured by wiring a refnum of each class number into a Property Node and reading the node's class.
- `tests/live_vi_create.py`, `tests/live_vi_edit.py`, `tests/live_project.py`, and `tests/smoke_mcp.py` need Windows and LabVIEW.
- **Measurement:** `python tools/measure.py run` reads every software example VI shipped with LabVIEW (about 960), converts each to a `vi_create` spec, rebuilds it in a scratch folder, and compares the rebuilt VI with the original. `python tools/measure.py report` ranks the features that block rebuilding by how many examples use them. Originals are only read, never saved. The first baseline: 962/964 examples read, 23 rebuilt identically. After v0.17.0's reader fixes, examples blocked by a read gap fell from 742 to 374, and 27 are rebuilt identically; with v0.23.0's real terminal names, 32; with v0.25.0's Property/Invoke Node rebuild, 35, and no example is blocked by a Property Node any more (180 before); with v0.26.0's copied types (waveform, picture, timestamp, class objects, typedefs, units), 77, and examples stopped by a feature with no spec fell from 834 to 616; with v0.27.0's Bundle/Unbundle By Name, Format Into String sizes and exact connector-pane matching, 84, and failed rebuilds fell from 123 to 84; with v0.28.0's polymorphic VI paths, array-node terminal maps, wiring by terminal reference and consistent growable-node numbering, 123, and examples blocked by an unnamed wire end fell from 304 to 208; with v0.29.0's shift registers in the scan reader (paired sides, stacked elements) and stacked shift registers in `vi_create`, no example is blocked by a shift register any more (100 before), and 124 are rebuilt identically; with v0.30.0's event structure rebuild (frame events read from LabVIEW, several events per frame, event data and filter nodes, frames wired to shift registers and case tunnels, values passed through a frame) and the case-selector and frame-order fixes in the scan reader, no example is blocked by an event structure any more (64 before), and 130 are rebuilt identically; with v0.31.0's functions placed by palette name (Merge Errors, First Call?, To Time Stamp, TDMS, file I/O, Shared Variable), rebuilds LabVIEW refused for an unknown function fell from 33 to 3, and 138 are rebuilt identically; with v0.32.0's Static VI References (read with the VI they point at, rebuilt pointed at it), examples blocked by an unnamed wire end fell from 191 to 153 and 146 are rebuilt identically. With v0.33.0's error list the measurement ranks why rebuilt VIs are broken; with v0.34.0's case pass-throughs drawn as LabVIEW users draw them (a value from outside a frame wired straight to the output tunnel's terminal in that frame), rebuilt VIs broken by an undirected tunnel fell from 53 to 10, failed rebuilds from 141 to 115, and 152 are rebuilt identically; with v0.35.0's case values read by selector type (DFIR numbers Boolean and error-cluster frames 0/1), the selector value used inside its frames, and Bundle/Unbundle By Name recognized in DFIR's element lists, rebuilt VIs broken by a case-selector error fell from 31 to 4, broken rebuilds from 140 to 124, and 165 are rebuilt identically; with v0.36.0's DFIR data blob read as LabVIEW writes it (backslash bytes unescaped), waveform components (Build Waveform / Get Waveform Components) and concatenating Build Arrays, rebuilt VIs broken by a type conflict fell from 81 to 66, broken rebuilds from 124 to 107, and 180 are rebuilt identically; with v0.37.0's loop tunnel indexing (a wire's `"indexing"`, read from DFIR's tunnel modes) and Format Into String inputs kept by name, type conflicts fell from 66 to 50, broken rebuilds from 107 to 85, and 195 are rebuilt identically. They build real VIs, run them, and check the results.

## License

MIT. LabVIEW is a trademark of National Instruments (Emerson). This project is not affiliated with or endorsed by NI.
