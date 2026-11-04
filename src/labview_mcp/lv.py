"""Thin wrapper around the LabVIEW ActiveX server.

All COM calls run on one dedicated thread (COM objects are apartment-bound),
so callers use `LabVIEW.call(fn, *args)` which returns a concurrent Future.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pythoncom
import pywintypes
import win32com.client

try:  # surface our errors to the agent as normal tool errors, not hidden crashes
    from mcp.server.mcpserver.exceptions import ToolError as _Base
except ImportError:  # pragma: no cover
    _Base = RuntimeError

VI_SUFFIXES = {".vi", ".vit", ".ctl", ".ctt", ".vim"}

VI_TYPES = {
    1: "VI", 2: "Control", 3: "Global", 4: "Polymorphic", 5: "Config",
    6: "SubSystem", 7: "Facade", 8: "Method", 9: "StateChart",
}
EXEC_STATES = {0: "bad (broken)", 1: "idle", 2: "running top-level", 3: "running as subVI"}

# Print-setup properties that vi_snapshot touches; saved and restored around each export
# because they are global LabVIEW options the user may rely on.
_PRINT_PROPS = (
    "PrintSetupCustomPanel", "PrintSetupCustomDiagram", "PrintSetupCustomDiagramHidden",
    "PrintSetupCustomConnector", "PrintSetupCustomDescription", "PrintSetupCustomControls",
    "PrintSetupCustomHierarchy", "PrintSetupCustomSubVIs", "PrintSetupCustomHistory",
    "PrintSetupCustomDiagramRepeat",
)


class LabVIEWError(_Base):
    pass


def _com_msg(e: Exception) -> str:
    if isinstance(e, pywintypes.com_error) and e.excepinfo and e.excepinfo[2]:
        return str(e.excepinfo[2]).strip()
    return str(e)


def _jsonable(v: Any) -> Any:
    """Convert COM variant results (tuples, pywintypes.datetime, bytes...) to JSON-safe values."""
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return str(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def check_vi_path(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise LabVIEWError(f"Use an absolute path: {path}")
    if p.suffix.lower() not in VI_SUFFIXES:
        raise LabVIEWError(f"Not a LabVIEW VI/control file ({', '.join(sorted(VI_SUFFIXES))}): {path}")
    if p.is_file():
        return str(p.resolve())
    # VIs inside an LLB (LabVIEW library file): C:\dir\lib.llb\name.vi
    llb = next((q for q in p.parents if q.suffix.lower() == ".llb"), None)
    if llb is not None and llb.is_file():
        return str(p)
    raise LabVIEWError(f"File not found: {path}")


class LabVIEW:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lv-com",
                                        initializer=pythoncom.CoInitialize)
        self._app = None
        self._tmp = Path(tempfile.mkdtemp(prefix="labview-mcp-"))

    # ---- plumbing -------------------------------------------------------
    def call(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Future:
        return self._pool.submit(self._guarded, fn, *args, **kw)

    def _guarded(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except pywintypes.com_error as e:
            # LabVIEW may have been closed since we connected: reconnect once and retry.
            if self._app is not None and e.hresult in (-2147023174, -2147023170, -2147417848):
                self._app = None
                try:
                    return fn(*args, **kw)
                except pywintypes.com_error as e2:
                    raise LabVIEWError(_com_msg(e2)) from None
            raise LabVIEWError(_com_msg(e)) from None

    @property
    def app(self):
        if self._app is None:
            try:
                self._app = win32com.client.dynamic.Dispatch("LabVIEW.Application")
            except pywintypes.com_error as e:
                raise LabVIEWError(
                    "Cannot connect to LabVIEW over ActiveX. Is LabVIEW installed and is "
                    "Tools > Options > VI Server > ActiveX enabled? (" + _com_msg(e) + ")"
                ) from None
            for m in ("GetVIReference", "GetVIVersion", "MassCompile"):
                self._app._FlagAsMethod(m)
        return self._app

    def _vi(self, path: str):
        vi = self.app.GetVIReference(check_vi_path(path))
        for m in ("PrintVIToHTML", "ExportVIStringsUTF8", "SetControlValue", "GetControlValue",
                  "Run", "Abort", "GetPanelImage"):
            vi._FlagAsMethod(m)
        return vi

    def _scratch(self, name: str) -> Path:
        d = self._tmp / f"{name}-{time.time_ns()}"
        d.mkdir(parents=True)
        return d

    # ---- tools ----------------------------------------------------------
    def status(self) -> dict:
        a = self.app
        res = {
            "connected": True,
            "version": a.Version,
            "version_name": _try(lambda: a.VersionDisplayName),
            "app_dir": a.ApplicationDirectory,
        }
        # Listing VIs in memory is denied by LabVIEW's default VI Server access settings.
        in_mem = _try(lambda: list(a.AllVIsInMemory or ()))
        if in_mem is not None:
            res["vis_in_memory"] = in_mem[:50]
        return res

    def vi_info(self, path: str) -> dict:
        vi = self._vi(path)
        callees = list(vi._CalleesPaths or ())
        return _jsonable_dict({
            "name": vi.Name,
            "path": vi.Path,
            "type": VI_TYPES.get(vi.VIType, vi.VIType),
            "exec_state": EXEC_STATES.get(vi.ExecState, vi.ExecState),
            "description": vi.Description,
            "saved_in_lv": _header_version(self._export_strings(vi, diagram=False)),
            "library": _try(lambda: vi.Library.Name),
            "reentrant": _try(lambda: bool(vi.IsReentrant)),
            "revision": _try(lambda: vi.RevisionNumber),
            "unsaved_changes": {
                "front_panel": _try(lambda: bool(vi.FPModificationBitSet)),
                "block_diagram": _try(lambda: bool(vi.BDModificationBitSet)),
            },
            "callees": callees,
            "callers_in_memory": list(vi.Callers or ()),
        })

    def _export_strings(self, vi, diagram: bool) -> str:
        f = self._scratch("strings") / "strings.txt"
        vi.ExportVIStringsUTF8(str(f), False, "", False, bool(diagram))
        text = f.read_text(encoding="utf-8", errors="replace")
        shutil.rmtree(f.parent, ignore_errors=True)
        return text

    def vi_text(self, path: str, include_diagram: bool = True, raw: bool = False) -> str:
        text = self._export_strings(self._vi(path), include_diagram)
        return text if raw else _clean_strings(text)

    def vi_snapshot(self, path: str, panel: bool, diagram: bool, all_frames: bool,
                    connector: bool) -> tuple[str, list[Path]]:
        vi = self._vi(path)
        a = self.app
        saved = {p: _try(lambda p=p: getattr(a, p)) for p in _PRINT_PROPS}
        out = self._scratch("snap")
        try:
            want = {
                "PrintSetupCustomPanel": panel, "PrintSetupCustomDiagram": diagram,
                "PrintSetupCustomDiagramHidden": diagram and all_frames,
                "PrintSetupCustomConnector": connector, "PrintSetupCustomDescription": True,
                "PrintSetupCustomControls": False, "PrintSetupCustomHierarchy": False,
                "PrintSetupCustomSubVIs": False, "PrintSetupCustomHistory": False,
                "PrintSetupCustomDiagramRepeat": False,
            }
            for k, v in want.items():
                _try(lambda k=k, v=v: setattr(a, k, v))
            # format 0 = custom (uses the PrintSetupCustom* options), image 0 = PNG, 24-bit
            vi.PrintVIToHTML(str(out / "vi.html"), False, 0, 0, 24, str(out))
        finally:
            for k, v in saved.items():
                if v is not None:
                    _try(lambda k=k, v=v: setattr(a, k, v))
        html = (out / "vi.html").read_text(encoding="utf-8", errors="replace") if (out / "vi.html").exists() else ""
        imgs = [out / m for m in re.findall(r'<IMG SRC="([^"]+)"', html, flags=re.I)]
        return _strip_html(html), [p for p in imgs if p.exists()]

    def vi_hierarchy(self, path: str, max_depth: int, include_vilib: bool) -> dict:
        root = check_vi_path(path)
        vilib = str(Path(self.app.ApplicationDirectory)).lower()
        seen: dict[str, list[str]] = {}
        skipped_vilib = set()
        frontier = [(root, 0)]
        while frontier:
            p, depth = frontier.pop(0)
            if p in seen:
                continue
            try:
                vi = self.app.GetVIReference(p)
                callees = list(vi._CalleesPaths or ())
            except pywintypes.com_error as e:
                seen[p] = [f"<error: {_com_msg(e)}>"]
                continue
            kept = []
            for c in callees:
                if not include_vilib and c.lower().startswith(vilib):
                    skipped_vilib.add(c)
                    continue
                kept.append(c)
                if depth + 1 < max_depth and Path(c).suffix.lower() in VI_SUFFIXES:
                    frontier.append((c, depth + 1))
            seen[p] = kept
        return {"root": root, "calls": seen, "vilib_callees_hidden": len(skipped_vilib)}

    def vi_run(self, path: str, inputs: dict[str, Any], outputs: list[str] | None,
               timeout_s: float) -> dict:
        vi = self._vi(path)
        if vi.ExecState == 0:
            raise LabVIEWError("VI is broken (has errors) and cannot run. Open it in LabVIEW to see the error list.")
        if vi.ExecState in (2, 3):
            raise LabVIEWError("VI is already running.")
        for name, value in (inputs or {}).items():
            try:
                vi.SetControlValue(name, value)
            except pywintypes.com_error as e:
                known = _top_level_controls(self._export_strings(vi, diagram=False))
                raise LabVIEWError(
                    f"Cannot set control '{name}': {_com_msg(e)}. "
                    f"Front-panel labels on this VI: {known}") from None
        t0 = time.monotonic()
        vi.Run(True)  # async; we poll so a VI that never stops can be aborted
        timed_out = False
        while vi.ExecState in (2, 3):
            if time.monotonic() - t0 > timeout_s:
                vi.Abort()
                timed_out = True
                break
            time.sleep(0.05)
        elapsed = round(time.monotonic() - t0, 3)
        names = outputs if outputs else _top_level_controls(self._export_strings(vi, diagram=False))
        values, errors = {}, {}
        for n in names:
            try:
                values[n] = _jsonable(vi.GetControlValue(n))
            except pywintypes.com_error as e:
                errors[n] = _com_msg(e)
        res = {"completed": not timed_out, "elapsed_s": elapsed, "values": values}
        if timed_out:
            res["note"] = (f"Aborted after {timeout_s}s. The VI probably waits for user input "
                           "or runs a loop until a Stop button is pressed.")
        if errors:
            res["unreadable"] = errors
        return res

    def vi_create(self, path: str, spec: dict, overwrite: bool, description: str) -> dict:
        from . import gen

        script = gen.build(spec)  # validates before LabVIEW is touched
        p = Path(path).expanduser()
        if not p.is_absolute() or p.suffix.lower() != ".vi":
            raise LabVIEWError(f"path must be an absolute path ending in .vi: {path}")
        if not p.parent.is_dir():
            raise LabVIEWError(f"folder does not exist: {p.parent}")
        if p.exists() and not overwrite:
            raise LabVIEWError(f"{p} already exists (pass overwrite=true to replace it)")
        if not BOOTSTRAP_VI.exists() or not BLANK_VI.exists():
            raise LabVIEWError(f"helper VIs missing from the package: {VIS_DIR}")
        shutil.copyfile(BLANK_VI, p)
        target = self._vi(str(p))
        if Path(target.Path).resolve() != p.resolve():
            raise LabVIEWError(f"a different VI named {p.name} is already in LabVIEW's memory "
                               f"({target.Path}); pick another file name")
        target._FlagAsMethod("SaveInstrument")
        boot = self._vi(str(BOOTSTRAP_VI))
        boot.SetControlValue("reference", target)
        boot.SetControlValue("XML Script Commands", script.xml)
        boot.SetControlValue("Type Strings", _byte_strings(script.type_strings))
        boot.SetControlValue("Data Strings", _byte_strings(script.data_strings))
        boot.Run(False)
        status, code, source = boot.GetControlValue("error out")
        if status:
            hint = _GEN_ERRORS.get(code, "")
            raise LabVIEWError(
                f"LabVIEW rejected the diagram (error {code}){': ' + hint if hint else ''}. "
                f"The file {p} was left as an empty VI.\nLabVIEW said: {_strip_html(str(source))}")
        if script.defaults:
            # LabVIEW's scripting ignores the data for controls, so set the values and
            # make them the defaults.
            for label, value in script.defaults.items():
                try:
                    target.SetControlValue(label, value)
                except pywintypes.com_error as e:
                    raise LabVIEWError(f"cannot set default of '{label}' to {value!r}: {_com_msg(e)}") from None
            target._FlagAsMethod("MakeCurValueDefault")
            target.MakeCurValueDefault()
        _try(lambda: setattr(target, "FPWinTitle", p.stem))  # blank template carries its own title
        if description:
            target.Description = description
        target.SaveInstrument(str(p), False, False)
        return {
            "path": str(p),
            "exec_state": EXEC_STATES.get(target.ExecState, target.ExecState),
            "front_panel": script.labels,
            "script_xml": script.xml,
        }


VIS_DIR = Path(__file__).parent / "vis"
BOOTSTRAP_VI = VIS_DIR / "lvmcp_gen_script.vi"
BLANK_VI = VIS_DIR / "lvmcp_blank.vi"
_GEN_ERRORS = {
    1063: "a wire names a terminal that does not exist on that node (terminal names are the "
          "labels shown in Context Help, or the subVI's control/indicator labels)",
    1374: "unknown function name or bad attribute in the generated script",
    1055: "invalid object reference",
}


# ---- helpers --------------------------------------------------------------
def _byte_strings(items: list[bytes]):
    """A LabVIEW string array whose elements carry raw bytes. Each element is sent as a COM byte
    array (VT_UI1) so it bypasses the code-page conversion applied to text."""
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
                   [VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, b) for b in items])


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _jsonable_dict(d: dict) -> dict:
    return {k: (_jsonable_dict(v) if isinstance(v, dict) else _jsonable(v)) for k, v in d.items()}


def _header_version(strings: str) -> str | None:
    m = re.search(r"LVversion=(\d+)", strings)
    if not m:
        return None
    v = m.group(1).rjust(8, "0")  # BCD-ish: MMmbSbbb, e.g. 26008000 -> 26.0.0
    return f"{int(v[:2])}.{int(v[2])}.{int(v[3])}"


_TAG = re.compile(r"<FONT[^>]*>|</?(?:STEXT|B)>|<<\/?B>>", re.I)


def _clean_strings(text: str) -> str:
    """Make ExportVIStrings output easier for a model to read: drop font markup and empty tags."""
    t = _TAG.sub("", text)
    t = t.replace("<LF>", " ")
    t = re.sub(r"<(DESC|TIP|PARTS|HELP_PATH|HELP_TAG|GROUPER)></\1>\n?", "", t)
    t = re.sub(r"\n\s*<(DESC|TIP)></\1>", "", t)
    t = re.sub(r"\n\s*<PARTS>\s*</PARTS>", "", t)
    t = re.sub(r"\n\s*<GROUPER>\s*</GROUPER>", "", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _top_level_controls(strings: str) -> list[str]:
    """Names of front-panel controls at the top level of <CONTENT> (not cluster elements)."""
    names = []
    depth = 0
    for line in strings.splitlines():
        s = line.strip()
        if s.startswith("<CONTENT>"):
            depth += 1
        elif s.startswith("</CONTENT>"):
            depth -= 1
        elif depth == 1:
            m = re.match(r'<CONTROL ID=\d+ type="[^"]*" name="([^"]*)"', s)
            if m and m.group(1):
                names.append(m.group(1))
    return list(dict.fromkeys(names))


def _strip_html(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    t = re.sub(r"<IMG[^>]*>", "", t, flags=re.I)
    t = re.sub(r"<br\s*/?>|</P>|</H\d>|<HR>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()
