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

lv = LabVIEW()

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
        "subVIs and wires; check the returned diagram picture, then test with vi_run.
"
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


@mcp.tool()
async def vi_info(path: str) -> dict:
    """Metadata for one VI: type, description, LabVIEW version it was saved in, broken/idle state,
    unsaved changes, subVIs it calls (callees) and callers loaded in memory.

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
) -> dict:
    """Run a VI and return its front-panel values afterwards.

    inputs: {control label: value} set before running (clusters as lists in element order).
    outputs: labels to read back; default reads every top-level control and indicator.
    A VI still running after timeout_s is aborted (typical for UI loops waiting on a Stop button).
    WARNING: executes code and may drive real hardware — only run VIs the user asked to run."""
    return await _run(lv.vi_run, path, inputs or {}, outputs, timeout_s)


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
      "functions":  [{"name": "add", "function": "Add"}],      # palette names: Add, Multiply,
                    # Subtract, Divide, Concatenate Strings, Build Array, Index Array, ...
      "subvis":     [{"name": "s", "path": "C:\\\\abs\\\\Helper.vi"}],
      "wires":      [["x", "add.x"], ["k", "add.y"], ["add.x+y", "result"]]
    }
    names are ids ([A-Za-z_][A-Za-z0-9_]*). Wires go [from, to]. Controls, indicators and
    constants are referenced by name alone; functions and subVIs as "name.Terminal", where the
    terminal is its Context Help label (Add: x, y, x+y; Multiply: x, y, x*y) or, for a subVI,
    the label of its control/indicator.
    Types: dbl, sgl, i8..i64, u8..u64, bool, string, path; arrays "dbl[]", "string[][]";
    clusters {"cluster": [{"name": "a", "type": "dbl"}, ...]}.

    Structures (top level or nested, in "structures"; they hold constants/functions/subvis/
    structures of their own; all names are global and wires are always listed at the top level):
      {"name": "L", "type": "for_loop", "functions": [...], "shift_registers": ["acc"]}
          terminals L.N (count), L.i; shift register: acc.left (previous value), acc.right (next)
      {"name": "W", "type": "while_loop", "stop_if_true": true, ...}   terminals W.i, W.stop
      {"name": "C", "type": "case", "output_tunnels": ["t"], "cases": [
          {"value": true, ...contents}, {"value": 3}, {"value": "apple"}, {"default": true}]}
          terminal C.selector. Values passing INTO a structure: wire the outer node straight to the
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
    Connector pane: every control and indicator is wired to the 4x2x2x4 connector pane so the VI
    works as a subVI right away (controls on the left then top, indicators on the right then
    bottom, in spec order). Pin a slot with "connector": "left3" (left0-3, top0-1, bottom0-1,
    right0-3) or leave one off with "connector": false. LabVIEW convention: error in at left3,
    error out at right3; type "error" is an error cluster.
    The diagram is tidied with LabVIEW's Clean Up Diagram."""
    res = await _run(lv.vi_create, path, spec, overwrite, description)
    text, imgs = await _run(lv.vi_snapshot, res["path"], False, True, False, False)
    out: list[Any] = [
        f"Created {res['path']} (state: {res['exec_state']}).\n"
        f"Front panel: {res['front_panel']}\n"
        f"Connector pane (callers wire these labels): {res['connector_pane']}\n"
        "Check the diagram picture: a wire that LabVIEW could not connect is simply missing. "
        "Then call vi_run to test."
    ]
    for p in imgs[:1]:
        out.append(_image(p, 2000))
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
