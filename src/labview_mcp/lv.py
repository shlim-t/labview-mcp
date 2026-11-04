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
from xml.sax.saxutils import quoteattr

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
        status, code, source = self._gen_script(target, script.xml, script.type_strings, script.data_strings)
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
        res = {
            "path": str(p),
            "exec_state": EXEC_STATES.get(target.ExecState, target.ExecState),
            "front_panel": script.labels,
            "connector_pane": script.connector,
            "script_xml": script.xml,
        }
        made = self._sink_count(str(p))
        if made is not None and made < len(script.wires):
            res["warning"] = (f"only {made} of {len(script.wires)} wires were connected; LabVIEW silently "
                              "drops a wire it cannot make. Compare with vi_dump and fix the spec.")
        return res

    def _gen_script(self, target, xml: str, type_strings: list[bytes], data_strings: list[bytes],
                    refs=(), raw: bool = False):
        """raw=True uses the helper without Clean Up Diagram (edits keep the user's layout)."""
        boot = self._vi(str(RAW_GEN_VI if raw else BOOTSTRAP_VI))
        boot.SetControlValue("reference", target)
        boot.SetControlValue("XML Script Commands", xml)
        boot.SetControlValue("Type Strings", _byte_strings(type_strings))
        boot.SetControlValue("Data Strings", _byte_strings(data_strings))
        boot.SetControlValue("Obj Refs In[]", list(refs))
        try:
            boot.Run(False)
            return boot.GetControlValue("error out")
        finally:
            if refs:
                _try(lambda: boot.SetControlValue("Obj Refs In[]", []))

    def _probe_vi(self):
        """A scratch VI kept in memory (never saved) for probing terminal names."""
        if getattr(self, "_probe", None) is None:
            p = self._scratch("probe") / "lvmcp_probe.vi"
            shutil.copyfile(BLANK_VI, p)
            self._probe = self._vi(str(p))
            self._probe_n = 0
        return self._probe

    def lv_functions(self, query: str) -> dict:
        from . import catalog
        names = catalog.search(self.app.ApplicationDirectory, query)
        return {"query": query, "functions": names,
                "note": "Names come from LabVIEW's Context Help. Use function_terminals(name) to get "
                        "the exact terminal names for wiring."}

    def function_terminals(self, name: str, size: int | None = None) -> dict:
        from . import catalog
        labels = catalog.load(self.app.ApplicationDirectory).get(name)
        if labels is None:
            hits = catalog.search(self.app.ApplicationDirectory, name, 10)
            raise LabVIEWError(f"no function named {name!r} in LabVIEW's help catalog. Close matches: {hits}")
        if size is not None and not (isinstance(size, int) and 1 <= size <= 100):
            raise LabVIEWError("size must be an integer 1..100")
        cache = self.__dict__.setdefault("_term_cache", {})
        if (name, size) in cache:
            return cache[(name, size)]
        vi = self._probe_vi()
        if size is None:
            node = lambda k: f"<Function name=\"f{k}\" style={quoteattr(name)} position=\"(0,0)\"></Function>"
        else:
            tag = "BuildArray" if name.lower() == "build array" else "GrowableFunction"
            node = lambda k: (f"<{tag} name=\"f{k}\" style={quoteattr(name)} size=\"{size}\" "
                              f"position=\"(0,0)\"></{tag}>")

        def probe(term: str) -> int:
            self._probe_n += 1
            k = self._probe_n
            xml = (f"<Script>{node(k)}"
                   f"<Constant name=\"c{k}\" style=\"Numeric Constant\" value=\"0\" position=\"(0,0)\"></Constant>"
                   f"<Wire source={quoteattr(f'f{k}.{term}')} destination=\"c{k}\"></Wire></Script>")
            status, code, _ = self._gen_script(vi, xml, [], [])
            return code if status else 0

        cands = catalog.candidates(labels, size)
        verified: list[str] = []
        for i, cand in enumerate(cands):
            code = probe(cand)
            if code == 1374 and i == 0 and not verified:
                raise LabVIEWError(
                    f"{name!r} cannot be placed by name with LabVIEW's scripting (some newer palette "
                    "functions, e.g. Write to Text File, are not reachable). Use an equivalent subVI "
                    "from vi.lib through \"subvis\" (for files: vi.lib/Utility/file.llb, e.g. "
                    "Write Delimited Spreadsheet.vi), or an older primitive such as Open File / "
                    "Read File / Write File / Close File.")
            if code == 0 and cand.lower() not in {v.lower() for v in verified}:
                verified.append(cand)
        res = {"function": name, "size": size, "terminals": verified, "help_labels": labels}
        if size is None and catalog.is_growable(labels):
            res["growable"] = (
                "this node grows. size is LabVIEW's internal count and means different things per function "
                "(Concatenate Strings / Build Array / Bundle / Unbundle: number of inputs or elements; "
                "Format Into String: 3 + number of inputs). Call function_terminals(name, size) to see "
                "which terminals a grown node has, pick the size that gives exactly the inputs you need "
                "(unwired extra inputs can break the VI), and give that \"size\" in vi_create. "
                "element[i] addresses terminals by position (for Concatenate Strings / Build Array, "
                "element[1..N] are the inputs).")
        cache[(name, size)] = res
        return res

    def _fp_refs_by_label(self, vi) -> dict:
        """Front-panel controls and indicators of an open VI, label -> GObject refnum."""
        tref = Path(self.app.ApplicationDirectory) / _VILIB_TREF
        tr = self._vi(str(tref / "TRef Traverse for References.vi"))
        tr.SetControlValue("VI Refnum", vi)
        tr.SetControlValue("Class Name", "Control")
        tr.SetControlValue("Traverse Target", 0)  # front panel
        tr.Run(False)
        lab = self._vi(str(tref / "Get GObject Label.vi"))
        out: dict = {}
        for r in tr.GetControlValue("References") or ():
            lab.SetControlValue("GObject", r)
            lab.Run(False)
            out.setdefault(lab.GetControlValue("Object Label"), r)
        return out

    def _content_size(self, path: str) -> tuple[tuple[int, int], tuple[int, int]]:
        """Pixel size of the front panel and block diagram pictures, to place new code below."""
        try:
            from PIL import Image as PILImage
            _, imgs = self.vi_snapshot(path, True, True, False, False)
            sizes = {p.stem[2:]: PILImage.open(p).size for p in imgs}
            return sizes.get("p", (0, 0)), sizes.get("d", (0, 0))
        except Exception:  # noqa: BLE001 - placement is cosmetic
            return (0, 300), (0, 400)

    def vi_edit(self, path: str, spec: dict, backup: bool, allow_broken: bool) -> dict:
        from . import gen

        path = check_vi_path(path)
        if not RAW_GEN_VI.exists():
            raise LabVIEWError(f"helper VI missing from the package: {RAW_GEN_VI}")
        existing_map = spec.get("existing") or {}
        if isinstance(existing_map, list):
            existing_map = {n: n for n in existing_map}
        if not isinstance(existing_map, dict):
            raise LabVIEWError("\"existing\" maps spec names to front-panel labels, e.g. {\"x\": \"Gain\"}")
        names = list(existing_map)
        target = self._vi(path)
        target._FlagAsMethod("SaveInstrument")
        target._FlagAsMethod("Revert")
        target._FlagAsMethod("GetLockState")
        if _try(lambda: bool(target.FPModificationBitSet or target.BDModificationBitSet)):
            raise LabVIEWError(f"{Path(path).name} has unsaved changes in LabVIEW. Save or revert them first.")
        (fpw, fph), (bdw, bdh) = self._content_size(path)
        script = gen.build({k: v for k, v in spec.items() if k != "existing"}, existing=names,
                           origin=(30, bdh + 80), fp_origin=(30, fph + 60))
        refs = []
        if names:
            found = self._fp_refs_by_label(target)
            for n in names:
                label = existing_map[n]
                if label not in found:
                    raise LabVIEWError(f"no control or indicator labelled {label!r} on {Path(path).name}. "
                                       f"Labels: {sorted(k for k in found if k)}")
                refs.append(found[label])
        bak = None
        if backup:
            bdir = Path(path).parent / ".lvmcp-backup"
            bdir.mkdir(exist_ok=True)
            bak = bdir / f"{Path(path).stem}.{time.strftime('%Y%m%d-%H%M%S')}{Path(path).suffix}"
            shutil.copy2(path, bak)
        before = self._sink_count(path)
        status, code, source = self._gen_script(target, script.xml, script.type_strings,
                                                script.data_strings, refs=refs, raw=True)
        if status:
            _try(lambda: target.Revert())
            hint = _GEN_ERRORS.get(code, "")
            raise LabVIEWError(f"LabVIEW rejected the edit (error {code}){': ' + hint if hint else ''}. "
                               f"The VI was reverted and not saved.\nLabVIEW said: {_strip_html(str(source))}")
        broken = target.ExecState == 0
        after = self._sink_count(path)
        if not broken and before is not None and after is not None and after - before < len(script.wires):
            # LabVIEW drops a wire it cannot make (e.g. a second source into a wired indicator)
            # without an error; the dangling remains would be saved.
            _try(lambda: target.Revert())
            raise LabVIEWError(
                f"LabVIEW connected only {after - before} of the {len(script.wires)} requested wires "
                "(a wire into an input that is already wired, or between incompatible terminals, is "
                "silently dropped). The VI was reverted and not saved. Check the spec with vi_dump.")
        if broken and not allow_broken:
            _try(lambda: target.Revert())
            raise LabVIEWError(
                "The edit made the VI broken (for example two sources wired to one indicator, an unwired "
                "required input, or mismatched types), so it was reverted and not saved. Fix the spec, or "
                "pass allow_broken=true to save it anyway and inspect it with vi_snapshot.")
        for label, value in script.defaults.items():
            target.SetControlValue(label, value)
        if script.defaults:
            target._FlagAsMethod("MakeCurValueDefault")
            target.MakeCurValueDefault()
        target.SaveInstrument(path, False, False)
        return {"path": path, "backup": str(bak) if bak else None,
                "exec_state": EXEC_STATES.get(target.ExecState, target.ExecState),
                "added_front_panel": script.labels}

    def _dfir_text(self, path: str) -> str:
        if not DFIR_VI.exists():
            raise LabVIEWError(f"helper VI missing from the package: {DFIR_VI}")
        target = self._vi(path)
        h = self._vi(str(DFIR_VI))
        h.SetControlValue("reference", target)
        h.SetControlValue("VI Variable Name", "vi")
        h.SetControlValue("Indentation Level", 0)
        h.Run(False)
        status, code, source = h.GetControlValue("error out")
        if status:
            if code == 1040:
                raise LabVIEWError(f"{Path(path).name} is password-protected; its block diagram cannot be read.")
            raise LabVIEWError(f"LabVIEW could not compile {Path(path).name} for reading (error {code}). "
                               f"A broken VI cannot be read; fix it first. {_strip_html(str(source))}")
        return h.GetControlValue("CPP Output") or ""

    def _sink_count(self, path: str) -> int | None:
        """Number of wire ends that land on a terminal, or None when the diagram cannot be read."""
        from . import dfir
        try:
            g = dfir.parse(self._dfir_text(path))
        except (LabVIEWError, ValueError):
            return None
        return sum(len(w["to"]) for w in g["wires"])

    def _connector_labels(self, path: str) -> list[str] | None:
        """A VI's connector-pane labels in the order DFIR numbers a subVI node's terminals."""
        from . import dfir
        cache = self.__dict__.setdefault("_conpane_cache", {})
        if path not in cache:
            try:
                g = dfir.parse(self._dfir_text(path))
                terms = [(n["conpane_index"], n.get("label", "")) for n in g["nodes"]
                         if n["kind"] in ("control", "indicator") and n.get("conpane_index", -1) >= 0
                         and "in" not in n]
                cache[path] = [lab for _, lab in sorted(terms)]
            except LabVIEWError:
                cache[path] = None
        return cache[path]

    def vi_dump(self, path: str) -> dict:
        from . import catalog, dfir
        text = self._dfir_text(check_vi_path(path))
        g = dfir.parse(text, catalog.load(self.app.ApplicationDirectory), self._connector_labels)
        return {"vi": str(path), **g}


VIS_DIR = Path(__file__).parent / "vis"
BOOTSTRAP_VI = VIS_DIR / "lvmcp_gen_script.vi"
BLANK_VI = VIS_DIR / "lvmcp_blank.vi"
DFIR_VI = VIS_DIR / "lvmcp_dfir.vi"
RAW_GEN_VI = VIS_DIR / "lvmcp_gen_script_raw.vi"
_VILIB_TREF = Path("vi.lib") / "Utility" / "traverseref.llb"
_GEN_ERRORS = {
    1063: "a wire names a terminal that does not exist on that node. Call function_terminals(name) "
          "for a function's exact terminal names; a subVI's terminals are its control/indicator labels "
          "(see vi_text)",
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
