"""Rebuild the shipped helper VIs from their sources.

The helper VIs are edited by hand in vis_src/ (current LabVIEW). The package ships
copies saved for an older LabVIEW in src/labview_mcp/vis/, so they open in that version and every
later one. Run this after changing a helper:

    python tools/build_helpers.py [version]        # default 15.0 (LabVIEW 2015)

Needs Windows + LabVIEW with ActiveX enabled.
"""
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "labview_mcp"
SRC, OUT = ROOT.parent.parent / "vis_src", ROOT / "vis"
version = sys.argv[1] if len(sys.argv) > 1 else "15.0"

from labview_mcp.lv import LabVIEW  # noqa: E402

lv = LabVIEW()
tmp = Path(tempfile.mkdtemp(prefix="lvmcp-build-"))


def go():
    done = []
    for f in sorted(SRC.glob("*.vi")):
        vi = lv._vi(str(f))
        vi._FlagAsMethod("SaveForPrevious")
        vi.SaveForPrevious(str(tmp / f.name), None, version)
        done.append(f.name)
    return done


names = lv.call(go).result()
major = int(version.split(".")[0])
stamp = struct.pack(">I", int(f"{major:02d}008000", 16))  # e.g. 15.0 -> 0x15008000
for n in names:
    data = (tmp / n).read_bytes()
    if stamp not in data:
        sys.exit(f"{n}: not saved as {version}")
    shutil.copyfile(tmp / n, OUT / n)
    print(f"{n}: saved for LabVIEW {version}")
shutil.rmtree(tmp, ignore_errors=True)
