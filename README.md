# labview-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI coding agents — Claude Code, Codex, Cursor, and others — **read, render, run, and create NI LabVIEW VIs**.

LabVIEW source code is a binary, graphical format. Text-based coding agents can't open a `.vi` file. labview-mcp acts as the agent's eyes and hands: it drives your installed LabVIEW over its ActiveX server and returns what the agent needs as text, images, and values.

> Status: **v0.1.0.** Reading and running work on any VI. Creating VIs works for straight-line diagrams (controls, constants, functions, subVIs, wires); loops and case structures are next.

## What the agent can do

| Tool | What it returns |
|---|---|
| `lv_status` | LabVIEW version and install directory. Starts LabVIEW if it isn't running. |
| `vi_info` | VI type, description, the LabVIEW version it was saved in, broken/idle state, unsaved changes, subVIs. |
| `vi_text` | LabVIEW's own string export: front-panel controls (nested for clusters), descriptions, and block-diagram labels and comments. **Case structures are exported one block per case**, including hidden cases. |
| `vi_snapshot` | PNG images of the block diagram, front panel, or connector pane. `all_frames=true` renders **every hidden case, sequence, and event frame**. |
| `vi_hierarchy` | The subVI call tree. vi.lib, user.lib, and instr.lib are hidden by default. |
| `vi_run` | Sets inputs, runs the VI, and returns all front-panel values. Stops VIs that don't finish within the time limit (UI loops). |
| `vi_create` | **Builds a new VI** from a JSON spec: typed controls and indicators with defaults, constants, palette functions, subVI calls, and wires. Returns a picture of the generated diagram. |

Example prompts:

- "Explain what `Main.vi` does, case by case."
- "Which subVIs write to the `Config` global?"
- "Run `Scale.vi` with gain 2.5 and check `out` matches my Python reference."
- "Make a VI that takes an array of readings and returns their sum and mean, then test it."

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

Types: `dbl sgl i8 i16 i32 i64 u8 u16 u32 u64 bool string path`, arrays (`"dbl[]"`, `"i32[][]"`), and clusters (`{"cluster": [{"name": "gain", "type": "dbl"}, ...]}`). Function terminals use the names shown in LabVIEW's Context Help. SubVI terminals use the subVI's control labels.

## Requirements

- Windows with LabVIEW installed. Tested with **LabVIEW 2026 Q3 (64-bit)**. Community Edition works for personal and non-commercial use.
- In LabVIEW, open **Tools ▸ Options ▸ VI Server** and enable **ActiveX**.
- Python ≥ 3.10.
- `vi_create` uses helper VIs saved in **LabVIEW 2026**. Older versions are on the roadmap.

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
                                                   └─ helper VI        → vi_create
```

- All COM calls run on a single dedicated thread.
- `vi_snapshot` temporarily changes LabVIEW's print options to render hidden frames. It always restores them afterwards.
- Read tools never modify or save VI files. `vi_create` only writes the new file you name, and it refuses to overwrite an existing file unless you pass `overwrite=true`.
- `vi_create` translates the spec into LabVIEW's internal diagram-scripting XML. A small helper VI shipped in the package (`vis/lvmcp_gen_script.vi`) runs that script on a blank VI. The spec is validated in Python first, so malformed input never reaches LabVIEW.

## Safety

`vi_run` executes LabVIEW code on your machine. That code can drive real instruments, such as DAQ, power supplies, and motion stages. MCP clients ask for confirmation before each tool call, so review what you approve.

## Roadmap

- [ ] `vi_dump`: full block-diagram graph (nodes, wires, constants) as JSON, via a VI Scripting helper VI.
- [ ] `vi_diff`: a semantic diff between two versions of a VI, for code review on binary files.
- [ ] LabVIEWCLI tools: build specs, mass compile, VI Analyzer, unit tests.
- [x] `vi_create`: new VIs with controls, constants, functions, subVIs, and wires.
- [ ] `vi_create`: For/While loops, case structures, shift registers; visible labels; connector pane.
- [ ] Helper VIs saved for older LabVIEW versions.
- [ ] Scripted edits: duplicate a case, set constants, rename, create globals. Each edit makes a backup first.

## License

MIT. LabVIEW is a trademark of National Instruments (Emerson). This project is not affiliated with or endorsed by NI.
