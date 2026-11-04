# labview-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI coding agents — Claude Code, Codex, Cursor, and others — **read, render, run, create, and edit NI LabVIEW VIs**.

LabVIEW source code is a binary, graphical format. Text-based coding agents can't open a `.vi` file. labview-mcp acts as the agent's eyes and hands: it drives your installed LabVIEW over its ActiveX server and returns what the agent needs as text, images, and values.

> Status: **v0.7.0.** Reading and running work on any VI. Creating VIs supports controls, constants, functions, subVIs, For/While loops, shift registers, and case structures, nested to any depth. Any existing VI can be read as a graph (`vi_dump`) and changed in place (`vi_edit`: remove, replace, and add code, including inside existing loops and case frames).

## What the agent can do

| Tool | What it returns |
|---|---|
| `lv_status` | LabVIEW version and install directory. Starts LabVIEW if it isn't running. |
| `vi_info` | VI type, description, the LabVIEW version it was saved in, broken/idle state, unsaved changes, subVIs. |
| `vi_text` | LabVIEW's own string export: front-panel controls (nested for clusters), descriptions, and block-diagram labels and comments. **Case structures are exported one block per case**, including hidden cases. |
| `vi_snapshot` | PNG images of the block diagram, front panel, or connector pane. `all_frames=true` renders **every hidden case, sequence, and event frame**. |
| `vi_hierarchy` | The subVI call tree. vi.lib, user.lib, and instr.lib are hidden by default. |
| `vi_run` | Sets inputs, runs the VI, and returns all front-panel values. Stops VIs that don't finish within the time limit (UI loops). |
| `vi_dump` | **The block diagram as a JSON graph**: every node (functions by palette name, constants with their values, subVIs, loops, case frames, tunnels, shift registers, property nodes) and every wire with its source and sinks. Works on any VI, including broken ones. |
| `vi_edit` | **Changes an existing VI in place.** It removes nodes, adds code, and wires to existing controls, nodes, loops, and case frames (by `vi_dump` id). The rest of the diagram keeps its layout. It backs up the VI first, and reverts instead of saving if the result is broken or a wire could not be made. |
| `lv_functions` | Searches the palette functions in your LabVIEW install by name. |
| `function_terminals` | Exact terminal names of a function, **verified against LabVIEW's scripting engine** so the agent wires them right the first time. |
| `vi_create` | **Builds a new VI** from a JSON spec: typed controls and indicators with defaults, constants, palette functions, subVI calls, For/While loops, shift registers, case structures, and wires. LabVIEW's Clean Up Diagram tidies the result, and the tool returns a picture of it. |

Example prompts:

- "Explain what `Main.vi` does, case by case."
- "Which subVIs write to the `Config` global?"
- "Run `Scale.vi` with gain 2.5 and check `out` matches my Python reference."
- "Make a VI that takes an array of readings and returns their sum and mean, then test it."
- "In `Scale.vi`, also output `Gain` squared, then run it."

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
| For Loop | `{"type": "for_loop", "shift_registers": ["acc"], ...}` | `L.N`, `L.i`, `acc.left`, `acc.right` |
| While Loop | `{"type": "while_loop", "stop_if_true": true, ...}` | `W.i`, `W.stop` |
| Case | `{"type": "case", "output_tunnels": ["t"], "cases": [{"value": true, ...}, {"default": true, ...}]}` | `C.selector`, tunnel `t` |

Growable functions take `"size"`. For example, Concatenate Strings with `"size": 3` is wired as `cat.element[1..3]`. `function_terminals(name, size)` shows the terminals a grown node has. A case wired to an `error` cluster gets `"No Error"` and `"Error"` frames.

Wire straight across structure borders and LabVIEW adds the tunnels. For Loops build arrays from values that leave them, and While Loops pass out the last value. Each frame of a case wires its result to an output tunnel.

**Connector pane:** every control and indicator is wired to the standard 4x2x2x4 connector pane, so a generated VI works as a subVI right away. Inputs go on the left, outputs on the right, and `error` clusters on the bottom corners, as in NI's own VIs. To pin a slot, use `"connector": "left3"`; to leave a terminal unwired, use `"connector": false`. The tool reports the resulting layout.

Strings are stored in the Windows ANSI code page, as LabVIEW does, so characters outside it (for example `é` on Korean Windows) become the closest match.

Types: `dbl sgl i8 i16 i32 i64 u8 u16 u32 u64 bool string path error`, arrays (`"dbl[]"`, `"i32[][]"`), and clusters (`{"cluster": [{"name": "gain", "type": "dbl"}, ...]}`). Function terminals use the names shown in LabVIEW's Context Help. SubVI terminals use the subVI's control labels.

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
- [ ] `vi_edit`: rename, move, and retype existing objects; add frames to cases.
- [x] `vi_dump` for any VI (scan fallback), including broken ones and VIs with property nodes and event structures.
- [ ] `vi_diff`: a semantic diff between two versions of a VI, for code review on binary files.
- [ ] LabVIEWCLI tools: build specs, mass compile, VI Analyzer, unit tests.
- [x] `vi_create`: new VIs with controls, constants, functions, subVIs, and wires.
- [x] `vi_create`: For/While loops, shift registers, case structures (boolean, integer, string), nesting.
- [x] `vi_create`: connector pane and error clusters, so generated VIs can call each other.
- [ ] `vi_create`: visible labels, constant widths, event structures, enums.
- [x] Helper VIs saved for LabVIEW 2015 and later.
- [ ] Verified on LabVIEW versions older than 2026 and on localized (non-English) LabVIEW.

## Development

- The helper VIs are edited in LabVIEW from `vis_src/`. `python tools/build_helpers.py` saves them for LabVIEW 2015 into `src/labview_mcp/vis/`, which is what ships.
- `tests/live_vi_create.py`, `tests/live_vi_edit.py`, and `tests/smoke_mcp.py` need Windows and LabVIEW. They build real VIs, run them, and check the results.

## License

MIT. LabVIEW is a trademark of National Instruments (Emerson). This project is not affiliated with or endorsed by NI.
