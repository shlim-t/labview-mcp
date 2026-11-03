# labview-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI coding agents — Claude Code, Codex, Cursor, and others — **read, render, and run NI LabVIEW VIs**.

LabVIEW source code is a binary, graphical format. Text-based coding agents can't open a `.vi` file. labview-mcp acts as the agent's eyes and hands: it drives your installed LabVIEW over its ActiveX server and returns what the agent needs as text, images, and values.

> Status: **v0.0.1, read & run only.** Programmatic editing (VI Scripting) is on the roadmap.

## What the agent can do

| Tool | What it returns |
|---|---|
| `lv_status` | LabVIEW version and install directory. Starts LabVIEW if it isn't running. |
| `vi_info` | VI type, description, the LabVIEW version it was saved in, broken/idle state, unsaved changes, subVIs. |
| `vi_text` | LabVIEW's own string export: front-panel controls (nested for clusters), descriptions, and block-diagram labels and comments. **Case structures are exported one block per case**, including hidden cases. |
| `vi_snapshot` | PNG images of the block diagram, front panel, or connector pane. `all_frames=true` renders **every hidden case, sequence, and event frame**. |
| `vi_hierarchy` | The subVI call tree. vi.lib, user.lib, and instr.lib are hidden by default. |
| `vi_run` | Sets inputs, runs the VI, and returns all front-panel values. Stops VIs that don't finish within the time limit (UI loops). |

Example prompts:

- "Explain what `Main.vi` does, case by case."
- "Which subVIs write to the `Config` global?"
- "Run `Scale.vi` with gain 2.5 and check `out` matches my Python reference."

## Requirements

- Windows with LabVIEW installed. Tested with **LabVIEW 2026 Q3 (64-bit)**. Community Edition works for personal and non-commercial use.
- In LabVIEW, open **Tools ▸ Options ▸ VI Server** and enable **ActiveX**.
- Python ≥ 3.10.

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
                                                   └─ Set/Run/GetValue → vi_run
```

- All COM calls run on a single dedicated thread.
- `vi_snapshot` temporarily changes LabVIEW's print options to render hidden frames. It always restores them afterwards.
- VI files are never modified or saved.

## Safety

`vi_run` executes LabVIEW code on your machine. That code can drive real instruments, such as DAQ, power supplies, and motion stages. MCP clients ask for confirmation before each tool call, so review what you approve.

## Roadmap

- [ ] `vi_dump`: full block-diagram graph (nodes, wires, constants) as JSON, via a VI Scripting helper VI.
- [ ] `vi_diff`: a semantic diff between two versions of a VI, for code review on binary files.
- [ ] LabVIEWCLI tools: build specs, mass compile, VI Analyzer, unit tests.
- [ ] Scripted edits: duplicate a case, set constants, rename, create globals. Each edit makes a backup first.

## License

MIT. LabVIEW is a trademark of National Instruments (Emerson). This project is not affiliated with or endorsed by NI.
