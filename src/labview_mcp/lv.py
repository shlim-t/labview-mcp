"""Thin wrapper around the LabVIEW ActiveX server.

All COM calls run on one dedicated thread (COM objects are apartment-bound),
so callers use `LabVIEW.call(fn, *args)` which returns a concurrent Future.
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import re
import shutil
import struct
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

    def _helper(self, path: Path):
        """A bundled helper VI, checked to be runnable. The helpers are saved for LabVIEW 2015 and use
        private scripting methods; an older or different LabVIEW may lack one, which breaks the VI."""
        if not Path(path).exists():
            raise LabVIEWError(f"helper VI missing from the package: {path}")
        vi = self._vi(str(path))
        if vi.ExecState == 0:
            raise LabVIEWError(
                f"the helper {Path(path).name} cannot run in LabVIEW {self.app.Version}: it uses a "
                "scripting method this LabVIEW does not have. This tool is not available here.")
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
        }) | ({"errors": self.vi_errors(vi)} if vi.ExecState == 0 else {})

    def vi_errors(self, vi) -> list[dict]:
        """Why a VI is broken: LabVIEW's Error List entries (VI.Get Errors), the same ones repeated
        counted once: [{"error": short text, "details": LabVIEW's explanation, "count": n}]."""
        h = _try(lambda: self._helper(GETERRORS_VI))
        if h is None:
            return []
        h.SetControlValue("reference", vi)
        h.Run(False)
        if h.GetControlValue("error out")[0]:
            return []
        errs = list(h.GetControlValue("Errors") or ())
        dets = list(h.GetControlValue("Details") or ())
        out: dict[tuple, dict] = {}
        for i, e in enumerate(errs):
            d = re.sub(r"\r\n?|\n", "\n", str(dets[i] if i < len(dets) else "")).strip()
            k = (str(e).strip(), d)
            if k in out:
                out[k]["count"] += 1
            else:
                out[k] = {"error": k[0], "details": d, "count": 1}
        return [x if x["count"] > 1 else {k: v for k, v in x.items() if k != "count"} for x in out.values()]

    def _errors_text(self, vi, limit: int = 8) -> str:
        """The Error List in one line, for error messages."""
        errs = _try(lambda: self.vi_errors(vi)) or []
        if not errs:
            return ""
        parts = [e["error"] + (f" (x{e['count']})" if e.get("count") else "") for e in errs[:limit]]
        more = f"; and {len(errs) - limit} more" if len(errs) > limit else ""
        return " LabVIEW's error list: " + "; ".join(parts) + more + "."

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

    def _run_actions(self, vi, actions: list, enums: dict, t0: float, timeout_s: float) -> list:
        """Operate the front panel of a running VI as a user would. Steps: {"click": label} presses a
        button; {"set": {label: value}} changes controls; both fire Value Change events. {"wait": s}
        pauses, {"read": [labels]} records values. Returns what each step did."""
        from win32com.client import VARIANT
        from . import gen
        out = []
        time.sleep(0.3)  # let the VI reach its event structure
        fp = None
        for step in actions:
            if vi.ExecState not in (2, 3):
                out.append({"step": step, "skipped": "the VI is no longer running"})
                continue
            if time.monotonic() - t0 > timeout_s:
                out.append({"step": step, "skipped": "time limit reached"})
                continue
            if not isinstance(step, dict) or len(step) != 1:
                raise LabVIEWError(f"bad action {step!r}: use {{\"click\": label}}, {{\"set\": {{label: value}}}}, "
                                   "{\"wait\": seconds} or {\"read\": [labels]}")
            (kind, arg), = step.items()
            if kind in ("click", "set"):
                fp = fp if fp is not None else self._fp_refs_by_label(vi)
            if kind == "click":
                if arg not in fp:
                    raise LabVIEWError(f"click: no control {arg!r}. Labels: {sorted(k for k in fp if k)}")
                lt = gen.parse_type("bool")
                h = self._helper(SIGNAL_VI)
                h.SetControlValue("reference", fp[arg])
                h.SetControlValue("type string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, lt.td()))
                h.SetControlValue("data string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, lt.data(True)))
                self._run_checked(h, f"clicking {arg!r}")
                out.append({"clicked": arg})
                time.sleep(0.2)
            elif kind == "set":
                if not isinstance(arg, dict):
                    raise LabVIEWError("set takes {label: value}")
                for label, value in arg.items():
                    if label not in fp:
                        raise LabVIEWError(f"set: no control {label!r}. Labels: {sorted(k for k in fp if k)}")
                    if label in enums and isinstance(value, str):
                        value = enums[label].index(value)
                    try:
                        vi.SetControlValue(label, value)
                    except pywintypes.com_error as e:
                        raise LabVIEWError(f"set {label!r}: {_com_msg(e)}") from None
                    h = self._helper(FIRE_VI)
                    h.SetControlValue("reference", fp[label])
                    self._run_checked(h, f"signalling the change of {label!r}")
                out.append({"set": arg})
                time.sleep(0.2)
            elif kind == "wait":
                time.sleep(max(0.0, min(float(arg), timeout_s)))
                out.append({"waited": arg})
            elif kind == "read":
                got = {}
                for label in ([arg] if isinstance(arg, str) else arg):
                    v = _jsonable(_try(lambda label=label: vi.GetControlValue(label)))
                    got[label] = enums[label][v] if label in enums and isinstance(v, int) and \
                        0 <= v < len(enums[label]) else v
                out.append({"read": got})
            else:
                raise LabVIEWError(f"unknown action {kind!r}")
        return out

    def vi_run(self, path: str, inputs: dict[str, Any], outputs: list[str] | None,
               timeout_s: float, actions: list | None = None) -> dict:
        vi = self._vi(path)
        if vi.ExecState == 0:
            raise LabVIEWError("VI is broken (has errors) and cannot run." + (
                self._errors_text(vi) or " Open it in LabVIEW to see the error list.")
                + " vi_info gives each error with LabVIEW's details.")
        if vi.ExecState in (2, 3):
            raise LabVIEWError("VI is already running.")
        enums = self._enum_items(path)  # label -> item names; enums go through COM as numbers
        for name, value in (inputs or {}).items():
            if name in enums and isinstance(value, str):
                if value not in enums[name]:
                    raise LabVIEWError(f"'{name}' is an enum with items {enums[name]}, not {value!r}")
                value = enums[name].index(value)
            try:
                vi.SetControlValue(name, value)
            except pywintypes.com_error as e:
                known = _top_level_controls(self._export_strings(vi, diagram=False))
                raise LabVIEWError(
                    f"Cannot set control '{name}': {_com_msg(e)}. "
                    f"Front-panel labels on this VI: {known}") from None
        t0 = time.monotonic()
        vi.Run(True)  # async; we poll so a VI that never stops can be aborted
        trace = self._run_actions(vi, actions or [], enums, t0, timeout_s) if actions else None
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
                if n in enums and isinstance(values[n], int) and 0 <= values[n] < len(enums[n]):
                    values[n] = enums[n][values[n]]
            except pywintypes.com_error as e:
                errors[n] = _com_msg(e)
        res = {"completed": not timed_out, "elapsed_s": elapsed, "values": values}
        if trace is not None:
            res["actions"] = trace
        if timed_out:
            res["note"] = (f"Aborted after {timeout_s}s. The VI probably waits for user input "
                           "or runs a loop until a Stop button is pressed.")
        if errors:
            res["unreadable"] = errors
        return res

    def _enum_items(self, path: str) -> dict[str, list[str]]:
        """Front-panel enums of a VI (label -> item names), read from the compiler export. Empty when
        LabVIEW will not export the VI."""
        from . import dfir
        cache = self.__dict__.setdefault("_enum_cache", {})
        key = (str(path), _try(lambda: os.path.getmtime(path)))
        if key not in cache:
            try:
                g = dfir.parse(self._dfir_text(path))
                cache[key] = {n["label"]: n["type"]["enum"] for n in g["nodes"]
                              if n["kind"] in ("control", "indicator") and "in" not in n and n.get("label")
                              and isinstance(n.get("type"), dict) and "enum" in n["type"]}
            except (LabVIEWError, ValueError):
                cache[key] = {}
        return cache[key]

    def vi_create(self, path: str, spec: dict, overwrite: bool, description: str,
                  save_library: bool = True) -> dict:
        """save_library=False leaves a "library" the VI joined changed only in memory (the
        measurement rebuilds class members into LabVIEW's own example classes, never saving them)."""
        from . import gen

        script = gen.build(spec)  # validates before LabVIEW is touched
        # functions LabVIEW's scripting cannot place by name (Merge Errors, First Call?, TDMS, ...)
        unplaceable = {f for f in _spec_functions(spec) if self.node_terminals(f) is None}
        # some functions placed by name come as an older node lacking terminals the palette's has
        # (Unflatten From String without "byte order"): placed like the unplaceable ones when wired so
        for f, terms in _spec_wired_terms(spec).items():
            if f in gen._BYNAME:  # (their element terminals are the cluster's, shown once configured)
                continue
            have = {n for n, _ in self.node_terminals(f) or ()}
            missing = {t for t in terms if t not in have and not re.search(r"\[\d+\]$", t)}
            if f not in unplaceable and have and missing:
                full = {n for n, _ in self.replaced_terminals(f) or ()}
                if missing & full - have:
                    unplaceable.add(f)
        unplaceable = frozenset(unplaceable)
        if unplaceable:
            script = gen.build(spec, replace=unplaceable)
        # copied class types ({"flat": ..., "from": VI}) need their classes in memory: keep the VIs
        # they were copied from open while building
        keep = [_try(lambda: self._vi(f)) for f in sorted(_flat_sources(spec)) if Path(f).is_file()]  # noqa: F841
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
        # tidy the diagram once everything is in place
        later = bool(script.events or script.styles or script.has_nodes())
        status, code, source = self._gen_script(target, script.xml, script.type_strings, script.data_strings,
                                                raw=later)
        if status:
            hint = _GEN_ERRORS.get(code, "")
            raise LabVIEWError(
                f"LabVIEW rejected the diagram (error {code}){': ' + hint if hint else ''}. "
                f"The file {p} was left as an empty VI.\nLabVIEW said: {_strip_html(str(source))}")
        try:
            out_refs = list(self._gen_out)
            if len(out_refs) != len(script.outs):
                raise LabVIEWError("LabVIEW did not return the references of the new objects")
            labels = script.labels
            # a Property Node linked to a chart needs the chart's properties: restyle those controls
            # first; the others last (a restyled control is a new object, so event frames bound to it
            # would lose it)
            linked = {labels.get(pn["control"], pn["control"]) for pn in script.pnodes or [] if pn.get("control")}
            early = {k: v for k, v in (script.styles or {}).items() if k in linked}
            if early:
                self._apply_styles(target, early)
            self._finish_nodes(target, script, out_refs, [], labels,
                               lambda s, refs: self._gen_script(target, s.xml, s.type_strings, s.data_strings,
                                                                refs=refs, raw=True))
            if script.replaces:
                self._replace_functions(target, script.replaces, out_refs)
            if script.static_refs:
                self._point_static_refs(target, script.static_refs, out_refs)
            if script.concats:
                self._concatenate(target, script.concats, out_refs)
            srs = self._stack_shift_registers(target, script.stacks, out_refs) if script.stacks else {}
            if script.ref_wires:
                self._connect_by_ref(target, script.ref_wires, out_refs, srs)
            if script.events:
                self._build_events(target, script.events, dict(zip(script.outs, out_refs)), labels,
                                   out_refs, srs)
            if script.use_default:
                ud = self._helper(USEDEFAULT_VI)
                owners = self._owners(target)
                sel = self._all_of(target, "SelectorTunnel")
                for k in script.use_default:  # Gen Script returned the tunnel's outer terminal
                    tun = owners.get(self._uid_of(out_refs[k]))
                    if tun not in sel:
                        raise LabVIEWError("setting a case tunnel to Use Default if Unwired: tunnel not found")
                    ud.SetControlValue("reference", sel[tun])
                    self._run_checked(ud, "setting a case tunnel to Use Default if Unwired")
            if script.widen:  # numeric constants sized to their value (a script draws them one digit wide)
                from win32com.client import VARIANT
                h = self._helper(CONSTVALUE_VI)
                arr = pythoncom.VT_ARRAY | pythoncom.VT_UI1
                for k, ts, ds in script.widen:
                    h.SetControlValue("reference", out_refs[k])
                    h.SetControlValue("type string", VARIANT(arr, ts))
                    h.SetControlValue("data string", VARIANT(arr, ds))
                    self._run_checked(h, "sizing a constant to its value")
            if script.index_modes:
                self._set_index_modes(target, script.index_modes, out_refs)
            rest = {k: v for k, v in (script.styles or {}).items() if k not in early}
            if rest:
                self._apply_styles(target, rest)
            if script.styles:  # restyled objects (a chart, a knob) are bigger: stack them by real size
                _try(lambda: self._restack_panel(target, script.panel or []))
            if later:
                status, code, source = self._gen_script(target, "<Script>\n</Script>", [], [])  # Clean Up
        except LabVIEWError as e:
            target.SaveInstrument(str(p), False, False)
            raise LabVIEWError(f"{e} The VI was saved as far as it got: {p}") from None
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
        if script.relabel:  # non-ASCII labels: stand-ins until now (see gen.build)
            self._relabel(target, script.relabel)
        _try(lambda: setattr(target, "FPWinTitle", p.stem))  # blank template carries its own title
        # LabVIEW's automatic error handling turns an unwired error into a modal dialog, which blocks
        # LabVIEW (and vi_run) until someone clicks it: off unless the spec asks for it
        if not spec.get("auto_error_handling", False):
            h = _try(lambda: self._helper(AUTOERR_VI))
            if h is not None:
                h.SetControlValue("reference", target)
                h.SetControlValue("enabled", False)
                _try(lambda: h.Run(False))
        if description:
            target.Description = description
        target.SaveInstrument(str(p), False, False)
        if spec.get("library"):
            self._join_library(p, spec["library"], save_library)
            target.SaveInstrument(str(p), False, False)  # now saved as a member
        res = {
            "path": str(p),
            "exec_state": EXEC_STATES.get(target.ExecState, target.ExecState),
            **({"errors": self.vi_errors(target)} if target.ExecState == 0 else {}),
            "front_panel": {k: (script.relabel or {}).get(v, v) for k, v in script.labels.items()},
            "connector_pane": {(script.relabel or {}).get(k, k): v for k, v in (script.connector or {}).items()},
            "script_xml": script.xml,
        }
        if script.events:
            # the frames as LabVIEW shows them (its Event Selectors), e.g. '[0] "STOP": Value Change'
            sel = _try(lambda: self.event_selectors(target)) or []
            res["event_frames"] = {}
            for j, ev in enumerate(script.events):
                caps = sel[j] if j < len(sel) else [
                    (f"\"{fr['control']}\": " if fr["control"] else "") + fr["event_name"] for fr in ev["frames"]]
                res["event_frames"][ev["name"]] = [f"[{i}] {c}" for i, c in enumerate(caps)]
        if script.styles:
            res["styled"] = script.styles
        made = self._sink_count(str(p))
        if made is not None and made < len(script.wires):
            res["warning"] = (f"only {made} of {len(script.wires)} wires were connected; LabVIEW silently "
                              "drops a wire it cannot make. Compare with vi_dump and fix the spec.")
        return res

    def _gen_script(self, target, xml: str, type_strings: list[bytes], data_strings: list[bytes],
                    refs=(), raw: bool = False):
        """raw=True uses the helper without Clean Up Diagram (edits keep the user's layout)."""
        if not xml.isascii():
            # LabVIEW reads the script as UTF-8 but keeps text in the Windows code page: such text
            # comes out garbled, or the whole script is dropped without an error
            odd = sorted({c for c in xml if not c.isascii()})
            raise LabVIEWError(f"text with characters outside ASCII ({''.join(odd[:10])}) is supported in "
                               "control and indicator labels only for now (use ASCII for names, case "
                               "values, ...; string constants may hold any text)")
        boot = self._helper(RAW_GEN_VI if raw else BOOTSTRAP_VI)
        boot.SetControlValue("reference", target)
        boot.SetControlValue("XML Script Commands", xml)
        boot.SetControlValue("Type Strings", _byte_strings(type_strings))
        boot.SetControlValue("Data Strings", _byte_strings(data_strings))
        boot.SetControlValue("Obj Refs In[]", list(refs))
        try:
            boot.Run(False)
            # references to the objects created with an out="k" attribute
            self._gen_out = list(_try(lambda: boot.GetControlValue("Obj Refs Out[]"), ()) or ())
            return boot.GetControlValue("error out")
        finally:
            if refs:
                _try(lambda: boot.SetControlValue("Obj Refs In[]", []))

    def _finish_nodes(self, target, script, outs: list, ins: list, labels: dict, run_late) -> None:
        """Give the Property/Invoke Nodes a script made their class and properties or method, link them
        to their front-panel control, switch Write items, then draw the wires to them (run_late)."""
        from . import gen
        if not script.pnodes and script.late is None:
            return
        fp = None
        node_terms: dict[str, list] = {}
        rename: dict[str, str] = {}
        for pn in script.pnodes:
            ref = outs[pn["out"]]
            if pn["kind"] == "byname":  # Bundle / Unbundle By Name: which elements it shows
                h = self._helper(UNBNAMES_VI if pn["function"] == "unbundle" else BUNNAMES_VI)
                h.SetControlValue("reference", ref)
                h.SetControlValue("count", pn.get("count", len(pn["items"])))
                h.SetControlValue("names", list(pn["items"]))
                self._run_checked(h, f"choosing the elements {pn['items']} of {pn['name']!r} (are they names "
                                     "of the wired cluster's elements?)")
                node_terms[pn["name"]] = [n for _, n in sorted(self._owned(target, "Terminal", self._uid_of(ref),
                                                                          names=True))]
                for k, elem, k2 in script.chains or ():
                    if k == pn["out"]:  # its element into the next By Name node's cluster, before that one is configured
                        c = self._helper(CONNECT_VI)
                        c.SetControlValue("reference", self._named_terminal(target, outs[k2], "input cluster"))
                        c.SetControlValue("other", self._named_terminal(target, ref, elem))
                        self._run_checked(c, f"wiring {pn['name']}.{elem} to the next By Name node")
                continue
            what = f"{'property' if pn['kind'] == 'pnode' else 'invoke'} node {pn['name']!r}"
            if pn["control"]:
                fp = fp if fp is not None else self._fp_refs_by_label(target)
                label = labels.get(pn["control"], pn["control"])
                if label not in fp:
                    raise LabVIEWError(f"{what}: no front-panel object {label!r}")
                # the control's own class, so its own properties (e.g. a chart's) are available
                pn = dict(pn, **{"class": self._control_class(target, fp[label])})
            cls = "VI Server:" + pn["class"]
            nested = []
            if pn["kind"] == "pnode":
                if any("." in x for x in pn["items"]):
                    # Set Properties[] refuses nested items ("Label.Text": Label, then Text.Text) and a
                    # refused call leaves the node half-configured, so tell them apart first: a dotted
                    # name the class lists ("App.Version") is a plain one.
                    known = {q for p_ in _try(lambda: self.class_properties(pn["class"])["properties"], [])
                             for q in (p_["name"], p_["long_name"], p_["data_name"])}
                    nested = [i for i, x in enumerate(pn["items"]) if "." in x and x not in known]
                h = self._helper(PROPCONFIG_VI)
                h.SetControlValue("reference", ref)
                h.SetControlValue("Property Node Class Name", cls)
                # nested items: the first part now, the full name below (PropertyItem.Property (Deprecated))
                h.SetControlValue("ID Strings[]", [x.split(".")[0] if i in nested else x
                                                   for i, x in enumerate(pn["items"])])
            else:
                h = self._helper(INVOKECONFIG_VI)
                h.SetControlValue("reference", ref)
                h.SetControlValue("Invoke Node Class Name", cls)
                h.SetControlValue("ID String", pn["method"])
            h.SetControlValue("Allow Alternate Names? (F)", True)
            h.Run(False)
            st, code, src = h.GetControlValue("error out")
            if st:
                raise LabVIEWError(_node_error(what, pn, code))
            if pn["control"]:
                link = self._helper(PROPLINK_VI if pn["kind"] == "pnode" else INVOKELINK_VI)
                link.SetControlValue("reference", ref)
                link.SetControlValue("control", fp[label])
                self._run_checked(link, f"linking {what} to {label!r}")
            uid = self._uid_of(ref)
            if nested or pn["write"]:
                items = self._prop_items(ref)  # in node order (UIDs are not: the first item is the oldest)
            if nested:
                ps = self._helper(PROPITEMSET_VI)
                for i in nested:
                    ps.SetControlValue("item", items[i])
                    ps.SetControlValue("name", pn["items"][i])
                    ps.Run(False)
                    if ps.GetControlValue("error out")[0]:
                        raise LabVIEWError(_node_error(what, dict(pn, items=[pn["items"][i]]), 1077))
            if pn["write"]:
                items = self._prop_items(ref)
                w = self._helper(PROPWRITE_VI)
                for k in pn["write"]:
                    w.SetControlValue("item", items[k])
                    w.SetControlValue("write", True)
                    self._run_checked(w, f"making {pn['items'][k]!r} of {what} writable")
            # A terminal is named by LabVIEW, not always as the property was asked for (the item
            # "AllProps[]" has the terminal "AllProps2"); item terminals follow the item order.
            terms = [n for _, n in sorted(self._owned(target, "Terminal", uid, names=True))]
            node_terms[pn["name"]] = terms
            item_terms = self._item_terminals(target, ref, uid) if pn["kind"] == "pnode" else []
            if pn["kind"] == "pnode" and len(item_terms) == len(pn["items"]):
                for asked, real in zip(pn["items"], item_terms):
                    if asked != real:
                        rename[f"\"{pn['name']}.{asked}\""] = f"\"{pn['name']}.{real}\""
        if script.late is not None:
            refs = [outs[k] if src == "out" else ins[k] for src, k, _ in script.late_refs]
            if script.styles:  # a restyled control is a new object: bind it by its front-panel refnum
                fp = self._fp_refs_by_label(target)
                for i, (_, _, nm) in enumerate(script.late_refs):
                    if labels.get(nm) in script.styles and labels[nm] in fp:
                        refs[i] = fp[labels[nm]]
            late = script.late
            # Functions already wired in the first script may have renamed terminals: a 1-D Index
            # Array's "index (row)" is "index" once its array is wired. Use the names they have now.
            for ref, (src, _, nm) in zip(refs, script.late_refs):
                if src != "out" or nm in node_terms or f'"{nm}.' not in late.xml:
                    continue
                real = {n for _, n in self._owned(target, "Terminal", self._uid_of(ref), names=True)}
                for term in set(re.findall(rf'"{re.escape(nm)}\.([^"]+)"', late.xml)) - real:
                    base = term.split(" (")[0]
                    if base in real:
                        rename[f'"{nm}.{term}"'] = f'"{nm}.{base}"'
            if rename:
                xml = late.xml
                for a, b in rename.items():
                    xml = xml.replace(a, b)
                late = gen.Script(**{**late.__dict__, "xml": xml})
            st, code, msg = run_late(late, refs)
            if st:
                hint = ""
                if code == 1063:
                    hint = (" A wire names a terminal the node does not have. Terminals: "
                            + "; ".join(f"{n}: {[t for t in ts if t != 'error in (no error)'] + ['error in']}"
                                        for n, ts in node_terms.items()) + ".")
                raise LabVIEWError(f"wiring the Property/Invoke Nodes failed (error {code}).{hint} "
                                   f"{_strip_html(str(msg))}")

    def _prop_items(self, ref) -> list:
        """A Property Node's items (PropertyItem refnums), top to bottom."""
        h = self._helper(PROPITEMS_VI)
        h.SetControlValue("reference", ref)
        self._run_checked(h, "reading the items of a Property Node")
        return list(h.GetControlValue("Property Items[]") or ())

    def _item_terminals(self, target, ref, uid: int) -> list[str]:
        """The terminal names of a Property Node's items, top to bottom."""
        names = dict(self._owned(target, "Terminal", uid, names=True))
        h = self._helper(ITEMTERM_VI)
        out = []
        for it in self._prop_items(ref):
            h.SetControlValue("reference", it)
            self._run_checked(h, "reading a Property Node item's terminal")
            out.append(str(names.get(h.GetControlValue("UID"), "")))
        return out

    def _uid_of(self, ref) -> int:
        u = self._helper(UID_VI)
        u.SetControlValue("reference", ref)
        self._run_checked(u, "reading an object's UID")
        return u.GetControlValue("UID")

    def _control_class(self, target, ref) -> str:
        """VI Server class name of a front-panel object, e.g. "WaveformChart"."""
        uid = self._uid_of(ref)
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 0)  # front panel
        h.SetControlValue("Class Name", "Control")
        h.Run(False)
        for u, c in zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Classes") or ()):
            if u == uid:
                return str(c)
        return "Control"

    def _replace_functions(self, target, replaces: list, outs: list) -> None:
        """Replace stand-in nodes by the palette functions named (GObject.Replace with the palette
        name); outs[k] then refers to the new node."""
        rep, cnt = self._helper(REPLACE_VI), None
        for k, fn, size in replaces:
            before = self._all_of(target, "Node")
            rep.SetControlValue("reference", outs[k])
            rep.SetControlValue("Style", 4294967295)
            rep.SetControlValue("Palette String", fn)
            self._run_checked(rep, f"placing {fn!r} (it has no scripting name)")
            after = self._all_of(target, "Node")
            new = [u for u in after if u not in before]
            if len(new) != 1:
                raise LabVIEWError(f"placing {fn!r}: the new node was not found")
            outs[k] = after[new[0]]
            if size:
                bund = self._all_of(target, "Bundler")
                if new[0] in bund:  # Merge Errors: as many inputs as wired
                    cnt = cnt or self._helper(INPUTCNT_VI)
                    cnt.SetControlValue("reference", bund[new[0]])
                    cnt.SetControlValue("count", int(size))
                    self._run_checked(cnt, f"growing {fn!r} to {size} inputs")


    def _point_static_refs(self, target, static_refs: list, outs: list) -> None:
        """Point placed Static VI References at their VI (VI Path, then Is Strict)."""
        if not any(p for _, p, _ in static_refs):
            return
        svr, all_refs = self._helper(STATICVI_VI), self._all_of(target, "StaticVIReference")
        for k, path, strict in static_refs:
            if not path:
                continue
            ref = all_refs.get(self._uid_of(outs[k]))
            if ref is None:
                raise LabVIEWError("a Static VI Reference was not found on the diagram")
            svr.SetControlValue("reference", ref)
            svr.SetControlValue("path", path)
            svr.SetControlValue("strict", bool(strict))
            self._run_checked(svr, f"pointing a Static VI Reference at {path}")

    def _set_index_modes(self, target, modes: list, outs: list) -> None:
        """Set the loop tunnels a wire passes through to indexing / last value (LoopTunnel.Index
        Mode 1 / 0). The tunnels are found by following the wire from its source to its sink through
        tunnels: a terminal's wire UID joins it to the other terminals on that wire."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "GObject")
        h.Run(False)
        cols = [list(h.GetControlValue(c) or ()) for c in ("UIDs", "Classes", "Owner UIDs", "Wire UIDs", "Names")]
        cls_of, owner_of = {}, {}
        on_wire: dict[int, list] = {}   # wire uid -> [(object uid, terminal name)]
        wires_of: dict[int, list] = {}  # object uid -> [(wire uid, terminal name)]
        for u, c, o, wu, nm in zip(*cols):
            cls_of.setdefault(u, c)
            owner_of.setdefault(u, o)
        for u, c, o, wu, nm in zip(*cols):
            if wu and c != "Wire":
                obj = u if c == "ControlTerminal" else o  # a terminal belongs to its node
                on_wire.setdefault(wu, []).append((obj, nm))
                wires_of.setdefault(obj, []).append((wu, nm))
        labels = {nm: u for u, c, o, wu, nm in zip(*cols) if c == "ControlTerminal"}

        def obj_of(end):
            if end[0] == "label":
                return labels.get(end[1])
            u = self._uid_of(outs[end[1]])
            return owner_of.get(u, u) if cls_of.get(u, "").endswith("Terminal") else u

        tunnels = self._all_of(target, "LoopTunnel")
        h = self._helper(INDEXMODE_VI)
        for a, z, loops in modes:
            src, dst = obj_of(a), obj_of(z)
            if cls_of.get(src) in ("ForLoop", "WhileLoop"):
                # its iteration terminal sits on its inner diagram (unnamed): start from every
                # terminal there; only the wire that reaches the sink counts
                inner = {u for u, o in owner_of.items() if o == src and cls_of.get(u) == "Diagram"}
                start = [wu for u, c, o, wu, nm in zip(*cols) if c == "Terminal" and o in inner and wu]
            else:
                start = [wu for wu, nm in wires_of.get(src, []) if not a[2] or nm == a[2]] or                     [wu for wu, _ in wires_of.get(src, [])]
            # breadth-first from the source's wires, passing through tunnels only
            prev, seen, todo, hit = {}, set(start), list(start), None
            while todo and hit is None:
                wu = todo.pop(0)
                for obj, nm in on_wire.get(wu, []):
                    if obj == dst:  # (any terminal of it: names a script uses may differ)
                        hit = wu
                        break
                    if obj != src and cls_of.get(obj, "").endswith("Tunnel") and ("t", obj) not in prev:
                        prev[("t", obj)] = wu
                        for w2, _ in wires_of.get(obj, []):
                            if w2 not in seen:
                                seen.add(w2)
                                prev[("w", w2)] = obj
                                todo.append(w2)
            if hit is None:
                raise LabVIEWError("setting a loop tunnel's indexing: the wire was not found")
            path, wu = [], hit
            while ("w", wu) in prev:
                path.append(prev[("w", wu)])
                wu = prev[("t", prev[("w", wu)])]
            want = {self._uid_of(outs[k]): v for k, v in loops}
            for t in path:
                if owner_of.get(t) in want and t in tunnels:
                    h.SetControlValue("reference", tunnels[t])
                    h.SetControlValue("mode", 1 if want[owner_of[t]] else 0)
                    self._run_checked(h, "setting a loop tunnel's indexing")

    def _join_library(self, vi_path: Path, library: str, save: bool) -> None:
        """Make the VI a member of a library or class (Library.Add Item), then save the library."""
        lib = Path(library)
        if not lib.is_file():
            raise LabVIEWError(f"library not found: {lib}")
        h = self._helper(LIBADD_VI)
        h.SetControlValue("library", str(lib))
        h.SetControlValue("item", str(vi_path))
        h.SetControlValue("Name", "")
        h.SetControlValue("Type", "VI")
        self._run_checked(h, f"adding the VI to {lib.name}")
        if save:
            h = self._helper(LIBSAVE_VI)
            h.SetControlValue("library", str(lib))
            self._run_checked(h, f"saving {lib.name}")

    def _concatenate(self, target, concats: list, outs: list) -> None:
        """Switch placed Build Arrays to Concatenate Inputs."""
        h, all_refs = self._helper(CONCAT_VI), self._all_of(target, "BuildArray")
        for k in concats:
            ref = all_refs.get(self._uid_of(outs[k]))
            if ref is None:
                raise LabVIEWError("a Build Array was not found on the diagram")
            h.SetControlValue("reference", ref)
            h.SetControlValue("concatenate", True)
            self._run_checked(h, "setting a Build Array to Concatenate Inputs")

    def _owners(self, target) -> dict:
        """{uid: owner uid} of every block-diagram object."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "GObject")
        h.Run(False)
        return dict(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or ()))

    def _all_of(self, target, cls: str) -> dict:
        """{uid: refnum} of the block-diagram objects of a class."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", cls)
        h.Run(False)
        return dict(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("References") or ()))

    def _stack_shift_registers(self, target, stacks: list, outs: list) -> dict:
        """Add the left elements of stacked shift registers (LeftShiftRegister.Add Element; each one is
        a LeftShiftRegister of its own). Returns {(k, element): uid}, element 0 = the right side."""
        add, right = self._helper(SRADD_VI), self._helper(SRRIGHT_VI)
        found = {}
        for k, n in stacks:
            lefts = self._all_of(target, "LeftShiftRegister")
            rid = self._uid_of(outs[k])  # Gen Script returns the right side
            cur = None
            for uid, ref in lefts.items():
                right.SetControlValue("reference", ref)
                self._run_checked(right, "reading a shift register's right side")
                if right.GetControlValue("UID") == rid:
                    cur = uid
                    break
            if cur is None:
                raise LabVIEWError("stacking a shift register: its left side was not found")
            found[(k, 0)], found[(k, 1)] = rid, cur
            for e in range(2, n + 1):
                add.SetControlValue("reference", lefts[cur])
                self._run_checked(add, "adding a shift register element")
                now = self._all_of(target, "LeftShiftRegister")
                new = set(now) - set(lefts)
                if len(new) != 1:
                    raise LabVIEWError("stacking a shift register: the new element was not found")
                lefts, cur = now, new.pop()
                found[(k, e)] = cur
        return found

    def _tunnel_inner_by(self, target, tunnel, structure, rows):
        """The inner terminal of a case tunnel (given by its outer terminal) on the frame that holds
        `structure`."""
        tu, su = self._uid_of(tunnel), self._uid_of(structure)
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "GObject")
        h.Run(False)
        owners = dict(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or ()))
        owner = owners.get(su)
        tu = owners.get(tu, tu)  # Gen Script returns a tunnel's outer terminal
        diag = self._helper(TERMDIAG_VI)
        for r in rows:
            if r[1] == tu and r[3] == "InnerTerminal":
                diag.SetControlValue("reference", r[4])
                self._run_checked(diag, "reading a terminal's diagram")
                if diag.GetControlValue("UID") == owner:
                    return r[4]
        raise LabVIEWError("wiring to a case tunnel from an event frame: its terminal was not found")

    def _tunnel_inner_in_frame(self, target, tunnel, case, i: int, rows):
        """The inner terminal of a case tunnel (given by its outer terminal) in frame i of the case."""
        h = self._helper(FRAMEDIAGS_VI)
        h.SetControlValue("reference", case)
        self._run_checked(h, "reading a case structure's frames")
        diags = [int(self._uid_of(d)) for d in h.GetControlValue("Diagrams[]") or ()]
        if i >= len(diags):
            raise LabVIEWError(f"passing a value through case frame {i}: the case has {len(diags)} frames")
        tu = self._owners(target).get(self._uid_of(tunnel))  # Gen Script returns the outer terminal
        diag = self._helper(TERMDIAG_VI)
        for r in rows:
            if r[1] == tu and r[3] == "InnerTerminal":
                diag.SetControlValue("reference", r[4])
                self._run_checked(diag, "reading a terminal's diagram")
                if diag.GetControlValue("UID") == diags[i]:
                    return r[4]
        raise LabVIEWError(f"passing a value through case frame {i}: the tunnel has no terminal there")

    def _connect_through(self, target, es, j: int, i: int, a, z, outs: list, srs: dict) -> None:
        """Wire a -> z through frame i of event structure j (reference es): from the inner terminal of
        the input tunnel fed by a to the inner terminal of the output tunnel feeding z, in that frame."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "Terminal")
        h.Run(False)
        rows = list(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
                        h.GetControlValue("Names") or (), h.GetControlValue("Classes") or (),
                        h.GetControlValue("References") or (), h.GetControlValue("Wire UIDs") or ()))
        es_uid = self._uid_of(es)
        h.SetControlValue("Class Name", "Tunnel")
        h.Run(False)
        tunnels = {u for u, o in zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or ())
                   if o == es_uid}
        frame = self._event_frame_diagrams(target, uids=True)[j][i]
        diag = self._helper(TERMDIAG_VI)

        def wire_of(end):
            if end[0] == "sr":
                _, k, e, side = end
                cls = "InnerTerminal" if side == "inner" else "OuterTerminal"
                hit = [w for _, o, _, c, _, w in rows if o == srs.get((k, e)) and c == cls]
            elif end[0] == "label":
                hit = [w for _, _, n, c, _, w in rows if c == "ControlTerminal" and n == end[1]]
            else:
                uid = self._uid_of(outs[end[1]])
                hit = [w for _, o, n, _, _, w in rows if o == uid and (n == end[2] or not end[2])]
            if len(hit) != 1 or not hit[0]:
                raise LabVIEWError("wiring through an event frame: an end is not wired to the event structure")
            return hit[0]

        def inner(end):
            w = wire_of(end)
            tun = [o for _, o, _, c, _, ww in rows if c == "OuterTerminal" and ww == w and o in tunnels]
            if len(tun) != 1:
                raise LabVIEWError("wiring through an event frame: no tunnel of the event structure carries "
                                   "that value (wire it in another frame too)")
            for _, o, _, c, r, _ in rows:
                if o == tun[0] and c == "InnerTerminal":
                    diag.SetControlValue("reference", r)
                    self._run_checked(diag, "reading a terminal's diagram")
                    if diag.GetControlValue("UID") == frame:
                        return r
            raise LabVIEWError("wiring through an event frame: the tunnel has no terminal in that frame")
        src, dst = inner(a), inner(z)
        c = self._helper(CONNECT_VI)
        c.SetControlValue("reference", dst)
        c.SetControlValue("other", src)
        self._run_checked(c, f"wiring through frame {i} of an event structure")

    def _connect_by_ref(self, target, ref_wires: list, outs: list, srs: dict | None = None) -> None:
        """Draw wires between terminal references (Terminal.Connect Wire): for terminals whose names a
        script cannot use ("new element/subarray", "number: 0 to 1") and stacked shift registers."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "Terminal")
        h.Run(False)
        rows = list(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
                        h.GetControlValue("Names") or (), h.GetControlValue("Classes") or (),
                        h.GetControlValue("References") or ()))

        def term(end, source=False):
            if end[0] == "tun":  # ("tun", k, j): tunnel k's inner terminal where structure j sits
                return self._tunnel_inner_by(target, outs[end[1]], outs[end[2]], rows)
            if end[0] == "tunframe":  # ("tunframe", k, j, i): tunnel k's terminal in frame i of case j
                return self._tunnel_inner_in_frame(target, outs[end[1]], outs[end[2]], end[3], rows)
            if end[0] == "sr":  # ("sr", k, element, "inner" | "outer")
                _, k, e, side = end
                cls = "InnerTerminal" if side == "inner" else "OuterTerminal"
                hits = [r for _, o, _, c, r in rows if o == (srs or {}).get((k, e)) and c == cls]
                if len(hits) != 1:
                    raise LabVIEWError(f"wiring a stacked shift register: cannot find element {e}'s {side} terminal")
                return hits[0]
            kind, k, name = end
            if kind == "label":  # a front-panel object's block-diagram terminal
                hits = [r for _, _, n, c, r in rows if c == "ControlTerminal" and n == k]
                what = f"the terminal of {k!r}"
            else:
                uid = self._uid_of(outs[k])
                own = [(n, r) for _, o, n, _, r in sorted(rows, key=lambda x: x[0]) if o == uid]
                hits = [r for n, r in own if n == name or not name]
                if len(hits) == 2 and name:
                    # an Invoke Node parameter (or return value) has an input and an output
                    # terminal of one name, the output made second
                    hits = hits[1:] if source else hits[:1]
                if not hits:  # names differ in case between LabVIEW versions ("time stamp")
                    hits = [r for n, r in own if str(n).lower() == name.lower()]
                if not hits and name == "subarray":  # an Index Array's output, as a script names it
                    hits = [r for n, r in own if n == "element"]
                m = re.fullmatch(r"(.*)\[(\d+)\]", name)
                if not hits and m:  # "index[2]": the 2nd terminal named "index", in node order
                    same = [r for n, r in own if n == m.group(1)]
                    hits = same[int(m.group(2)) - 1:int(m.group(2))] if int(m.group(2)) >= 1 else []
                if not hits and len(own) == 1 and not own[0][0]:
                    hits = [own[0][1]]  # a node's only, unnamed terminal (Static VI Reference)
                what = f"terminal {name!r}" + (f" (the node has {[n for n, _ in own]})" if own else "")
            if len(hits) != 1:
                raise LabVIEWError(f"wiring by terminal reference: cannot find {what}")
            return hits[0]
        c = self._helper(CONNECT_VI)
        for a, z in ref_wires:
            c.SetControlValue("reference", term(z))
            c.SetControlValue("other", term(a, source=True))
            self._run_checked(c, f"wiring {_end_text(a)} to {_end_text(z)}")

    def _named_terminal(self, target, node_ref, name: str):
        """The one terminal of a node with this name."""
        uid = self._uid_of(node_ref)
        names = dict(self._owned(target, "Terminal", uid, names=True))
        hits = [r for u, r in self._owned(target, "Terminal", uid) if names.get(u) == name]
        if len(hits) != 1:
            raise LabVIEWError(f"no single terminal {name!r} on the node (it has {sorted(map(str, names.values()))})")
        return hits[0]

    def _owned(self, target, cls: str, owner: int, names: bool = False) -> list:
        """(uid, refnum) — or (uid, name) — of the objects of a class whose owner has UID `owner`."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", cls)
        h.Run(False)
        col = h.GetControlValue("Names" if names else "References") or ()
        return [(u, r) for u, o, r in zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
                                          col) if o == owner]

    def class_properties(self, cls: str, query: str = "") -> dict:
        """Properties of a VI Server class, from LabVIEW itself (All Supported Properties)."""
        from . import gen
        cache = self.__dict__.setdefault("_props_cache", {})
        if cls not in cache:
            vi = self._probe_vi()
            self._probe_n += 1
            st, code, _ = self._gen_script(
                vi, f"<Script><Function name=\"pp{self._probe_n}\" style=\"Property Node\" position=\"(0,0)\" "
                    "out=\"0\"></Function></Script>", [], [], raw=True)
            if st:
                raise LabVIEWError(f"could not place a Property Node (error {code})")
            ref = self._gen_out[0]
            c = self._helper(PROPCONFIG_VI)
            c.SetControlValue("reference", ref)
            c.SetControlValue("Property Node Class Name", "VI Server:" + cls)
            c.SetControlValue("ID Strings[]", [])
            c.SetControlValue("Allow Alternate Names? (F)", True)
            c.Run(False)
            st, code, _ = c.GetControlValue("error out")
            if st:
                raise LabVIEWError(_node_error(f"class {cls!r}", {"class": cls, "kind": "pnode", "items": []}, code)
                                   + f" Classes: {sorted(gen.VI_CLASSES)}")
            h = self._helper(ALLPROPS_VI)
            h.SetControlValue("reference", ref)
            self._run_checked(h, f"listing the properties of {cls}")
            cache[cls] = [{"name": short, "long_name": long_, "data_name": data}
                          for _, data, short, long_ in h.GetControlValue("properties") or ()]
        props = cache[cls]
        if query:
            q = query.lower()
            props = [p for p in props if q in (p["name"] + " " + p["long_name"]).lower()]
        return {"class": cls, "count": len(props), "properties": props,
                "use": "put \"name\" in a property node's \"properties\" and wire it as \"node.<name>\""}

    def _run_checked(self, h, what: str) -> None:
        h.Run(False)
        status, code, source = h.GetControlValue("error out")
        if status:
            raise LabVIEWError(f"{what} failed (error {code}). {_strip_html(str(source))}")

    def _apply_styles(self, target, styles: dict) -> None:
        """Replace front-panel controls with the palette object named, e.g. "Stop Button". The label,
        terminal and wires stay."""
        h = self._helper(REPLACE_VI)
        fp = self._fp_refs_by_label(target)
        for label, style in styles.items():
            h.SetControlValue("reference", fp[label])
            h.SetControlValue("Style", _ANY_STYLE)
            h.SetControlValue("Palette String", style)
            h.Run(False)
            status, code, _ = h.GetControlValue("error out")
            if status:
                raise LabVIEWError(
                    f"cannot make {label!r} a {style!r} (error {code}). \"style\" is the name shown in "
                    "LabVIEW's Controls palette for an object of the same data type, e.g. \"Stop Button\", "
                    "\"OK Button\", \"Round LED\", \"Vertical Toggle Switch\" for booleans; \"Knob\", "
                    "\"Gauge\", \"Thermometer\", \"Waveform Chart\" for numbers.")

    def _relabel(self, target, relabel: dict) -> None:
        """Front-panel objects renamed from stand-in labels to their real ones (through COM, which
        hands LabVIEW the text in its own code page)."""
        fp = self._fp_refs_by_label(target)
        h = self._helper(RELABEL_VI)
        for stand_in, real in relabel.items():
            if stand_in in fp:
                h.SetControlValue("reference", fp[stand_in])
                h.SetControlValue("text", real)
                self._run_checked(h, f"labelling {real!r}")

    def _restack_panel(self, target, panel: list) -> None:
        """Controls in a left column and indicators in a right one, in spec order, each placed below
        the previous by its real size (label included)."""
        fp = self._fp_refs_by_label(target)
        h, mv = self._helper(BOUNDS_VI), self._helper(SETPOS_VI)
        size = {}
        for label, _ in panel:
            if label in fp:
                h.SetControlValue("reference", fp[label])
                self._run_checked(h, f"reading the size of {label!r}")
                size[label] = h.GetControlValue("bounds")
        left = max([size[lb][0] for lb, ind in panel if lb in size and not ind] or [0])
        for ind in (False, True):
            x, y = (30 + left + 60 if ind and left else 30), 30
            for label, i in panel:
                if i != ind or label not in size:
                    continue
                mv.SetControlValue("reference", fp[label])
                mv.SetControlValue("position", (x, y + 20))  # (the label sits above the position)
                self._run_checked(mv, f"placing {label!r}")
                y += size[label][1] + 25

    def _event_frame_diagrams(self, target, uids: bool = False) -> list[list]:
        """Diagram refnums of every event frame: one list per event structure in creation order,
        frames in creation order (UIDs grow as objects are created)."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "EventStructure")
        h.Run(False)
        evs = sorted(h.GetControlValue("UIDs") or ())
        h.SetControlValue("Class Name", "Diagram")
        h.Run(False)
        rows = sorted(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
                          h.GetControlValue("References") or ()))
        return [[(u if uids else r) for u, o, r in rows if o == ev] for ev in evs]

    def _build_events(self, target, events: list, outs: dict, labels: dict, out_list=(), srs=None) -> None:
        """Give each new event structure its frames: register the events, then generate each frame's
        code on the frame's own diagram."""
        from win32com.client import VARIANT
        arr = pythoncom.VT_ARRAY | pythoncom.VT_VARIANT
        add, reg = self._helper(ADDFRAME_VI), self._helper(EVENTS_VI)
        fp = self._fp_refs_by_label(target)
        for ev in events:
            es = outs[ev["name"]]
            for i, fr in enumerate(ev["frames"]):
                if i:
                    add.SetControlValue("reference", es)
                    add.SetControlValue("Reference Frame Index", i - 1)
                    add.SetControlValue("After? (T)", True)
                    self._run_checked(add, f"adding frame {i} to event structure {ev['name']!r}")
                # Frame 0 is the Timeout frame LabVIEW makes; registering an event replaces it.
                reg.SetControlValue("reference", es)
                reg.SetControlValue("Frame Index", i)
                every = [(fr["control"], fr["event"])] + list(fr.get("more", []))
                # control events: Event Type, Reference, Limit (-1 = none), Lock Panel?; events of
                # this VI: Event Type, Limit, Lock Panel? (no reference)
                reg.SetControlValue("Static Control Events", VARIANT(
                    arr, [VARIANT(arr, [e, fp[c], -1, False]) for c, e in every if c is not None]))
                reg.SetControlValue("Static App/VI Events", VARIANT(
                    arr, [VARIANT(arr, [e, -1, False]) for c, e in every if c is None]))
                self._run_checked(reg, f"registering the event of frame {i} of {ev['name']!r}")
        _try(lambda: reg.SetControlValue("Static Control Events", VARIANT(arr, [])))
        _try(lambda: reg.SetControlValue("Static App/VI Events", VARIANT(arr, [])))
        for ev in events:
            if ev.get("timeout") is not None and ev["timeout"] >= 0:
                self._set_timeout(outs[ev["name"]], ev["timeout"], ev["name"])
        n_frames = [len(ev["frames"]) for ev in events]
        if [len(d) for d in self._event_frame_diagrams(target)] != n_frames:
            raise LabVIEWError("could not find the frames of the new event structures")
        for j, ev in enumerate(events):
            for i, fr in enumerate(ev["frames"]):
                passes = []
                if fr["move"]:  # front-panel terminals used only here go into the frame
                    xml = "".join(f"<Control name=\"t{k}\" in=\"{k}\"></Control><ControlTerminal "
                                  f"controlname=\"t{k}\" position=\"(20,{20 + 40 * k})\"></ControlTerminal>"
                                  for k in range(len(fr["move"])))
                    passes.append((f"<Script>{xml}</Script>", [], [], [fp[t] for t in fr["move"]]))
                if fr["script"] is not None:
                    s = fr["script"]
                    ednodes = self._event_data_nodes(target, j, i) if fr["evdata"] else {}
                    refs = []
                    for n in fr["bind"]:
                        if n in fr["evdata"]:
                            role = fr["evdata"][n]["role"]
                            if role not in ednodes:
                                raise LabVIEWError(f"event frame {i} of {ev['name']!r} ({fr['event_name']}) "
                                                   f"has no event {role} node")
                            if role == "data":
                                self._show_event_data(target, ednodes["data"], fr["evdata"][n]["items"],
                                                      f"{n!r} ({fr['event_name']})")
                            refs.append(ednodes[role][1])
                        else:
                            refs.append(outs[n])
                    passes.append((s.xml, s.type_strings, s.data_strings, refs))

                def on_frame(xml, ts, ds, refs, j=j, i=i):
                    # the helper closes the diagram reference it is given, so look it up each time
                    return self._gen_on_diagram(self._event_frame_diagrams(target)[j][i], xml, ts, ds, refs)
                for xml, ts, ds, refs in passes:
                    status, code, source = on_frame(xml, ts, ds, refs)
                    if status:
                        hint = _GEN_ERRORS.get(code, "")
                        raise LabVIEWError(
                            f"LabVIEW rejected the code of event frame {i} of {ev['name']!r} (error {code})"
                            f"{': ' + hint if hint else ''}. {_strip_html(str(source))}")
                if fr["script"] is not None:
                    frame_outs = list(self._gen_out)
                    self._finish_nodes(target, fr["script"], frame_outs, passes[-1][3], labels,
                                       lambda s, refs, f=on_frame: f(s.xml, s.type_strings, s.data_strings, refs))
                else:
                    frame_outs = []
                if fr.get("cross"):
                    # wires from this frame to shift registers / nodes outside, between terminal
                    # references: frame objects and event data nodes go after the main script's refs
                    refs = list(out_list)
                    ednodes = self._event_data_nodes(target, j, i) if any(
                        e[0] == "evdata" for c in fr["cross"] for e in c) else {}

                    def end(e):
                        if e[0] == "fout":
                            refs.append(frame_outs[e[1]])
                            return ("out", len(refs) - 1, e[2])
                        if e[0] == "evdata":
                            role = fr["evdata"][e[1]]["role"]
                            if role == "data":
                                self._show_event_data(target, ednodes["data"], fr["evdata"][e[1]]["items"],
                                                      f"{e[1]!r} ({fr['event_name']})")
                            refs.append(ednodes[role][1])
                            return ("out", len(refs) - 1, e[2])
                        return e
                    wires = [tuple(end(e) for e in c) for c in fr["cross"]]
                    self._connect_by_ref(target, wires, refs, srs or {})
        # values a frame passes on unchanged: join the inner terminals of the tunnels in that frame
        # (the other frames' wires made the tunnels)
        for j, ev in enumerate(events):
            for i, fr in enumerate(ev["frames"]):
                for a, z in fr.get("through") or []:
                    self._connect_through(target, outs[ev["name"]], j, i, a, z, list(out_list), srs or {})

    def _event_data_nodes(self, target, j: int, i: int) -> dict:
        """The event data node ("data", an unbundler on the left of the frame) and, for filter events,
        the event filter node ("filter", a bundler on the right) of frame i of event structure j."""
        uid = self._event_frame_diagrams(target, uids=True)[j][i]
        found = {}
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "Node")
        h.Run(False)
        for u, c, o, r in zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Classes") or (),
                              h.GetControlValue("Owner UIDs") or (), h.GetControlValue("References") or ()):
            if o == uid and c in ("NamedUnbundler", "NamedBundler"):
                found.setdefault("data" if c == "NamedUnbundler" else "filter", (u, r))
        return found

    def _show_event_data(self, target, node, wanted: list, what: str) -> None:
        """A scripted event data node shows only Source, Type and Time. Show more items, in LabVIEW's
        order, until the wired ones are there (OutputCnt repeats the last item past the end)."""
        uid, ref = node
        h = self._helper(OUTCNT_VI)

        def names():
            return [n for _, n in self._owned(target, "Terminal", uid, names=True)][1:]  # minus the input
        have = names()
        while not set(wanted) <= set(have):
            h.SetControlValue("reference", ref)
            h.SetControlValue("count", len(have) + 1)
            self._run_checked(h, "showing event data items")
            more = names()
            if len(more) <= len(have) or more[-1] in more[:-1]:
                h.SetControlValue("count", len(have))
                self._run_checked(h, "showing event data items")
                raise LabVIEWError(f"event data {what} has no item {sorted(set(wanted) - set(have))}; "
                                   f"its items are {have}")
            have = more

    def _set_timeout(self, es, ms: int, name: str) -> None:
        """Wire a constant of `ms` to the event structure's Timeout terminal. That terminal has no name
        a script can wire, so the constant is made from the terminal itself (Terminal.Create Constant)."""
        from win32com.client import VARIANT
        from . import gen
        h = self._helper(TIMEOUTTERM_VI)
        h.SetControlValue("reference", es)
        self._run_checked(h, f"finding the Timeout terminal of {name!r}")
        lt = gen.parse_type("i32")
        c = self._helper(TERMCONST_VI)
        c.SetControlValue("reference", h.GetControlValue("terminal"))
        c.SetControlValue("type string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, lt.td()))
        c.SetControlValue("data string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, lt.data(int(ms))))
        self._run_checked(c, f"wiring the timeout of event structure {name!r}")

    def event_selectors(self, target) -> list[list[str]]:
        """Frame captions of each event structure (creation order), e.g. '"stop": Value Change'."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "EventStructure")
        h.Run(False)
        sel = self._helper(EVENTSEL_VI)
        out = []
        for _, r in sorted(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("References") or ())):
            sel.SetControlValue("reference", r)
            self._run_checked(sel, "reading event selectors")
            out.append([" ".join(str(x).split()) for x in sel.GetControlValue("selectors") or ()])
        return out

    def node_terminals(self, name: str, size: int | None = None) -> list[tuple[str, bool]] | None:
        """The terminals LabVIEW gives a palette function when it is placed: (name, is_output), in
        the node's order. None if the function cannot be placed by scripting. Cached per process."""
        cache = self.__dict__.setdefault("_node_terms", {})
        key = (name, size)
        if key in cache:
            return cache[key]
        vi = self._probe_vi()
        self._probe_n += 1
        k = self._probe_n
        if size is None:
            xml = f"<Function name=\"n{k}\" style={quoteattr(name)} position=\"(0,0)\" out=\"0\"></Function>"
        else:
            tag = "BuildArray" if name.lower() == "build array" else "GrowableFunction"
            xml = (f"<{tag} name=\"n{k}\" style={quoteattr(name)} size=\"{size}\" position=\"(0,0)\" "
                   f"out=\"0\"></{tag}>")
        status, _, _ = self._gen_script(vi, f"<Script>{xml}</Script>", [], [])
        out = list(self._gen_out) if not status else []
        if status or not out:
            cache[key] = None
            return None
        node = out[0]
        uid = self._uid_of(node)
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", vi)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "Terminal")
        h.Run(False)
        terms = sorted((u, n, bool(src)) for u, o, n, src in zip(
            h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
            h.GetControlValue("Names") or (), h.GetControlValue("Is Source") or ()) if o == uid)
        _try(lambda: self._delete(vi, [node]))
        cache[key] = [(n, src) for _, n, src in terms]
        return cache[key]

    def replaced_terminals(self, name: str, size: int | None = None) -> list[tuple[str, bool]] | None:
        """Terminals of a palette function placed by GObject.Replace (no scripting name), in node order;
        repeated names numbered "name[k]". None if it cannot be placed that way either."""
        cache = self.__dict__.setdefault("_repl_terms", {})
        if (name, size) in cache:
            return cache[(name, size)]
        vi = self._probe_vi()
        self._probe_n += 1
        status, _, _ = self._gen_script(vi, f"<Script><Function name=\"r{self._probe_n}\" style=\"Add\" "
                                            "position=\"(0,0)\" out=\"0\"></Function></Script>", [], [])
        out = list(self._gen_out) if not status else []
        if not out:
            return None
        outs = [out[0]]
        try:
            self._replace_functions(vi, [(0, name, size)], outs)
        except LabVIEWError:
            _try(lambda: self._delete(vi, [out[0]]))
            cache[(name, size)] = None
            return None
        uid = self._uid_of(outs[0])
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", vi)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "Terminal")
        h.Run(False)
        terms = sorted((u, n, bool(src)) for u, o, n, src in zip(
            h.GetControlValue("UIDs") or (), h.GetControlValue("Owner UIDs") or (),
            h.GetControlValue("Names") or (), h.GetControlValue("Is Source") or ()) if o == uid)
        _try(lambda: self._delete(vi, [outs[0]]))
        count = collections.Counter(n for _, n, _ in terms)
        seen = collections.Counter()
        res = []
        for _, n, src in terms:
            seen[n] += 1
            res.append((f"{n}[{seen[n]}]" if count[n] > 1 else n, src))
        cache[(name, size)] = res
        return res

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
        if self.node_terminals(name) is None:
            # no scripting name: vi_create places it by its palette name instead
            real = self.replaced_terminals(name, size)
            if real is not None:
                res = {"function": name, "size": size, "terminals": [n for n, _ in real],
                       "inputs": [n for n, out in real if not out], "outputs": [n for n, out in real if out],
                       "help_labels": labels,
                       "note": "LabVIEW's scripting has no name for this function; vi_create places it by its "
                               "palette name and wires it by terminal reference. Repeated names are numbered "
                               "(\"error in[2]\")." + (" It grows: give \"size\" = number of inputs."
                                                        if any(n.endswith("[2]") for n, _ in real) else "")}
                cache[(name, size)] = res
                return res
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
        real = _try(lambda: self.node_terminals(name, size))
        if real:
            res["inputs"] = [n for n, out in real if not out]
            res["outputs"] = [n for n, out in real if out]
            for n, _ in real:
                if n not in verified and not _TERM_UNSCRIPTABLE.search(n):
                    verified.append(n)
            bad = [n for n, _ in real if _TERM_UNSCRIPTABLE.search(n)]
            if bad:
                res["unwireable"] = (f"{bad}: LabVIEW's script parser reads \":<number>\" in a terminal "
                                     "name as a position, so these cannot be wired by name")
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

    def vi_edit(self, path: str, spec: dict, backup: bool, allow_broken: bool, rewire: bool = False) -> dict:
        path = check_vi_path(path)
        existing_map = spec.get("existing") or {}
        ops = {k: v for k, v in existing_map.items() if isinstance(existing_map, dict) and isinstance(v, dict)
               and ("add_cases" in v or "remove_cases" in v)}
        if not ops:
            return self._vi_edit(path, spec, backup, allow_broken, rewire)
        bak = self._backup(path) if backup else None
        pristine = self._scratch("edit") / Path(path).name
        shutil.copy2(path, pristine)
        try:
            spec = self._edit_case_frames(path, spec, ops)
            res = self._vi_edit(path, spec, False, allow_broken, rewire, pristine=pristine)
        except Exception:
            shutil.copy2(pristine, path)
            vi = self._vi(path)
            vi._FlagAsMethod("Revert")
            _try(lambda: vi.Revert())
            raise
        res["backup"] = str(bak) if bak else None
        return res

    def _backup(self, path: str) -> Path:
        bdir = Path(path).parent / ".lvmcp-backup"
        bdir.mkdir(exist_ok=True)
        bak = bdir / f"{Path(path).stem}.{time.strftime('%Y%m%d-%H%M%S')}{Path(path).suffix}"
        shutil.copy2(path, bak)
        return bak

    def _edit_case_frames(self, path: str, spec: dict, ops: dict) -> dict:
        """existing case entries with "add_cases": [{"value": 5, "default": false, ...contents}] and/or
        "remove_cases": [frame index, ...]: change the frames and save; return the spec with the new
        frames' contents moved to "cases" under their frame index."""
        import gc
        from win32com.client import VARIANT
        g = self.vi_dump(path)
        by_id = {n["id"]: n for n in g["nodes"]}
        target = self._vi(path)
        target._FlagAsMethod("SaveInstrument")
        rd, wr = self._helper(CASENAMES_VI), self._helper(SETCASENAMES_VI)
        add, rm = self._helper(ADDFRAME_VI), self._helper(REMOVEFRAME_VI)
        spec = dict(spec)
        spec["existing"] = existing = dict(spec["existing"])
        for name, ref in ops.items():
            n = by_id.get(ref.get("node"))
            if n is None or n["kind"] != "case":
                raise LabVIEWError(f"existing {name!r}: add_cases / remove_cases need {{\"node\": <case "
                                   "structure id from vi_dump>}")
            cs = self._ref_for_uid(target, n["uid"], "case")
            rd.SetControlValue("reference", cs)
            self._run_checked(rd, "reading the case names")
            names = [x.strip() for x in rd.GetControlValue("names") or ()]
            removes = sorted({int(i) for i in ref.get("remove_cases", [])}, reverse=True)
            if any(i < 0 or i >= len(names) for i in removes) or len(removes) >= len(names):
                raise LabVIEWError(f"existing {name!r}: remove_cases {sorted(removes)} must be frame indices "
                                   f"0..{len(names) - 1} and leave at least one frame (frames: {names})")
            for i in removes:
                rm.SetControlValue("reference", cs)
                rm.SetControlValue("Frame Index", i)
                self._run_checked(rm, f"removing case frame {i}")
                del names[i]
            quoted = any(x.startswith('"') for x in names)
            contents = dict(ref.get("cases") or {})
            # the case's tunnels: vi_dump ids (DFIR numbers them in creation order, the scan reader
            # by UID) -> the tunnel objects (creation order = UID order) and their types
            mine = [t for t in g["nodes"] if t["kind"] == "tunnel" and t.get("structure") == n["id"]]
            mine.sort(key=lambda t: int(re.sub(r"\D", "", t["id"]) or 0))
            objs = self._uid_rows(target)
            tun_objs = sorted(u for u, c, o, _ in objs if c == "SelectorTunnel" and o == n["uid"])
            for k, c in enumerate(ref.get("add_cases", []) or []):
                if not isinstance(c, dict) or "value" not in c:
                    raise LabVIEWError(f"existing {name!r}: each add_cases entry needs a \"value\", e.g. "
                                       "{\"value\": 5, \"functions\": [...]}")
                v = c["value"]
                label = ("True" if v else "False") if isinstance(v, bool) else \
                    f'"{v}"' if quoted and isinstance(v, str) else str(v)
                if c.get("default"):
                    names = [x.replace(", Default", "").replace("Default", "").strip(", ") or x for x in names]
                    label += ", Default"
                outs = c.get("outputs") or {}
                before = {u for u, cl, _, _ in self._uid_rows(target) if cl == "InnerTerminal"} if outs else set()
                add.SetControlValue("reference", cs)
                add.SetControlValue("Reference Frame Index", len(names) - 1)
                add.SetControlValue("After? (T)", True)
                self._run_checked(add, "adding a case frame")
                names.append(label)
                if outs:
                    if len(mine) != len(tun_objs):
                        raise LabVIEWError(f"existing {name!r}: could not match the case's tunnels "
                                           f"({len(mine)} in vi_dump, {len(tun_objs)} on the diagram)")
                    rows = self._uid_rows(target, refs=True)
                    for tid, value in outs.items():
                        k3 = next((i for i, t in enumerate(mine) if t["id"] == tid), None)
                        if k3 is None:
                            raise LabVIEWError(f"existing {name!r}: outputs: {tid!r} is not a tunnel of this "
                                               f"case; its tunnels: {[t['id'] for t in mine]}")
                        # the tunnel's terminal in the new frame: the one the new frame added
                        inner = [r for u, cl, o, r in rows
                                 if cl == "InnerTerminal" and o == tun_objs[k3] and u not in before]
                        if len(inner) != 1:
                            raise LabVIEWError(f"could not find {tid}'s terminal in the new case {label!r}")
                        self._terminal_constant(inner[0], mine[k3].get("type"), value,
                                                f"output {tid} of the new case {label!r}")
                body = {k2: v2 for k2, v2 in c.items() if k2 not in ("value", "default", "outputs")}
                if body:
                    contents[str(len(names) - 1)] = body
            wr.SetControlValue("reference", cs)
            wr.SetControlValue("names", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, names))
            self._run_checked(wr, f"naming the cases {names}")
            rd.Run(False)
            got = [x.strip() for x in rd.GetControlValue("names") or ()]
            if len(got) != len(names):
                raise LabVIEWError(f"the case structure has frames {got}, expected {names}")
            # by UID: once frames changed, vi_dump may read the VI another way and number nodes differently
            existing[name] = {k2: v2 for k2, v2 in ref.items()
                              if k2 not in ("add_cases", "remove_cases", "cases", "node")}
            existing[name]["uid"] = n["uid"]
            if contents:
                existing[name]["cases"] = contents
        target.SaveInstrument(path, False, False)
        target = cs = None
        self._uid_refs = {}
        gc.collect()
        return spec

    def _uid_rows(self, target, refs: bool = False) -> list:
        """(uid, class, owner uid, name or reference) of every object on the block diagram."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 1)
        h.SetControlValue("Class Name", "GObject")
        h.Run(False)
        return list(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Classes") or (),
                        h.GetControlValue("Owner UIDs") or (),
                        h.GetControlValue("References" if refs else "Names") or ()))

    def _terminal_constant(self, term, typ, value, what: str) -> None:
        """A constant made from a terminal (Terminal.Create Constant) and set to `value`."""
        from win32com.client import VARIANT
        from . import gen
        if typ is None or (isinstance(typ, str) and typ.startswith("type_")):
            raise LabVIEWError(f"{what}: its type is unknown, so the value cannot be set")
        try:
            lt = gen.parse_type(typ)
            ts, ds = lt.td(), lt.data(lt.enum_index(value) if lt.kind == "enum" else value)
        except (gen.SpecError, TypeError, ValueError) as e:
            raise LabVIEWError(f"{what}: {value!r} is not a {typ}: {e}") from None
        h = self._helper(TERMCONST_VI)
        h.SetControlValue("reference", term)
        h.SetControlValue("type string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, ts))
        h.SetControlValue("data string", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, ds))
        self._run_checked(h, what)

    def _vi_edit(self, path: str, spec: dict, backup: bool, allow_broken: bool, rewire: bool = False,
                 pristine: Path | None = None) -> dict:
        from . import gen

        path = check_vi_path(path)
        if not RAW_GEN_VI.exists():
            raise LabVIEWError(f"helper VI missing from the package: {RAW_GEN_VI}")
        existing_map = spec.get("existing") or {}
        if isinstance(existing_map, list):
            existing_map = {n: n for n in existing_map}
        if not isinstance(existing_map, dict):
            raise LabVIEWError("\"existing\" maps spec names to objects already in the VI, e.g. "
                               "{\"g\": \"Gain\", \"L\": {\"node\": \"for_loop_4\"}}")
        remove = spec.get("remove") or []
        if not isinstance(remove, list) or not all(isinstance(r, str) for r in remove):
            raise LabVIEWError("\"remove\" is a list of vi_dump node ids, e.g. [\"multiply_18\"]")
        rename, values, retype, move = (spec.get(k) or {} for k in ("rename", "values", "retype", "move"))
        disconnect = spec.get("disconnect") or []
        for k, v in (("rename", rename), ("values", values), ("retype", retype), ("move", move)):
            if not isinstance(v, dict):
                raise LabVIEWError(f"\"{k}\" maps a front-panel label (or a vi_dump constant id) to "
                                   + {"rename": "its new label", "values": "a value",
                                      "retype": "a numeric type, e.g. \"i32\"",
                                      "move": "a diagram position [x, y]"}[k])
        for t in retype.values():
            if t not in _REPRESENTATION:
                raise LabVIEWError(f"retype: {t!r} is not a numeric type; one of {sorted(_REPRESENTATION)}")
        if not isinstance(disconnect, list) or not all(isinstance(e, str) for e in disconnect):
            raise LabVIEWError("\"disconnect\" lists wire ends from vi_dump, e.g. [\"multiply_7.x\", \"out_12\"]")
        self._uid_refs = {}
        target = self._vi(path)
        target._FlagAsMethod("SaveInstrument")
        target._FlagAsMethod("Revert")
        if _try(lambda: bool(target.FPModificationBitSet or target.BDModificationBitSet)):
            raise LabVIEWError(f"{Path(path).name} has unsaved changes in LabVIEW. Save or revert them first.")

        # Resolve every reference to the VI's current objects before touching anything.
        graph = None
        if remove or disconnect or any(isinstance(v, dict) and ("node" in v or "uid" in v)
                                       for v in existing_map.values()) or \
                bool(move) or any(not self._is_label(k, target) for k in [*values, *retype]):
            graph = self.vi_dump(path)
        by_id = {n["id"]: n for n in (graph or {}).get("nodes", [])}
        cut_wires, cut_sinks = [], 0  # wires deleted by "disconnect" and the ends they had
        if disconnect:
            spec = dict(spec)
            existing_map = dict(existing_map)
            spec["wires"] = list(spec.get("wires") or [])
            for e in disconnect:
                w = next((w for w in graph["wires"] if e in w["to"]), None) or \
                    next((w for w in graph["wires"] if w["from"] == e), None)
                if w is None:
                    raise LabVIEWError(f"disconnect: no wire ends at {e!r}. Wire ends in vi_dump: "
                                       f"{sorted({x for w in graph['wires'] for x in w['to']})[:40]}")
                if w.get("uid") is None or w["uid"] in cut_wires:
                    continue
                cut_wires.append(w["uid"])
                cut_sinks += len(w["to"])
                # a wire is one object with all its branches: re-make the branches that stay
                keep = [t for t in w["to"] if t not in disconnect] if e in w["to"] else []
                for t in keep:
                    spec["wires"].append([self._bind_end(w["from"], by_id, existing_map),
                                          self._bind_end(t, by_id, existing_map)])

        def node_of(key, where):
            if key not in by_id:
                raise LabVIEWError(f"{where}: no node {key!r} in vi_dump of {Path(path).name}. "
                                   f"Node ids: {sorted(by_id)}")
            return by_id[key]

        bindings = []  # (spec name, kind for gen, how to find the ref, extra contents)
        for name, ref in existing_map.items():
            extra = {}
            if isinstance(ref, dict):
                extra = {k: v for k, v in ref.items() if k not in ("label", "node", "uid")}
                if "label" in ref:
                    ref = ref["label"]
                elif "node" in ref or "uid" in ref:
                    n = node_of(ref["node"], f"existing {name!r}") if "node" in ref else \
                        next((m for m in by_id.values() if m.get("uid") == ref["uid"]), None)
                    if n is None:
                        raise LabVIEWError(f"existing {name!r}: no node with uid {ref['uid']}")
                    if n["kind"] in ("control", "indicator"):
                        ref = n.get("label", "")
                    else:
                        kind = _EDIT_KINDS.get(n["kind"])
                        if not kind or "uid" not in n:
                            raise LabVIEWError(f"existing {name!r}: a {n['kind']} cannot be bound; bind "
                                               "the node it belongs to instead")
                        bindings.append((name, kind, ("uid", n["uid"], n["kind"]), extra))
                        continue
            if not isinstance(ref, str):
                raise LabVIEWError(f"existing {name!r}: give a front-panel label, {{\"node\": vi_dump id}} "
                                   "or {\"uid\": n}")
            if extra:
                raise LabVIEWError(f"existing {name!r}: only loops and cases can hold new contents")
            bindings.append((name, "existing", ("label", ref), {}))
        remove_nodes = [node_of(r, "remove") for r in remove]
        for n in remove_nodes:
            if "uid" not in n or n["kind"] not in _EDIT_KINDS:
                raise LabVIEWError(f"remove: {n['id']} ({n['kind']}) cannot be removed on its own; remove "
                                   "the node or structure it belongs to")

        # New code for case frames is generated directly on each frame's diagram, in its own script.
        frame_jobs = []  # (case spec name, case uid, frame index, contents)
        for name, kind, how, extra in bindings:
            if kind == "case" and extra.get("cases"):
                frames = extra.pop("cases")
                if not isinstance(frames, dict):
                    raise LabVIEWError(f"existing case {name!r}: \"cases\" maps a frame index to contents, "
                                       "e.g. {\"1\": {\"functions\": [...]}}")
                for k, contents in frames.items():
                    try:
                        frame_jobs.append((name, how[1], int(k), contents or {}))
                    except ValueError:
                        raise LabVIEWError(f"existing case {name!r}: frame index {k!r} is not a number") from None
        bound_names = {b[0] for b in bindings}
        frame_names = [_spec_names(job[3]) for job in frame_jobs]
        main_wires, frame_wires = [], [[] for _ in frame_jobs]
        for w in spec.get("wires", []) or []:
            ends = [w.get("from"), w.get("to")] if isinstance(w, dict) else list(w) if isinstance(w, (list, tuple)) else []
            nodes = [str(e).split(".")[0] for e in ends]
            home = [i for i, names in enumerate(frame_names) if any(n in names for n in nodes)]
            if not home:
                main_wires.append(w)
                continue
            i = home[0]
            for n in nodes:
                if n not in frame_names[i] and n not in bound_names:
                    raise LabVIEWError(
                        f"wire {w!r}: code added to a case frame can only be wired to code in the same frame "
                        "or to existing objects (\"existing\"), not to other new code")
            frame_wires[i].append(w)
        diagram_uid = {}
        if frame_jobs:
            diagram_uid = self._diagram_uids(path, target)
            # Revert reloads the VI, so take a fresh reference.
            target = self._vi(path)
            target._FlagAsMethod("SaveInstrument")
            target._FlagAsMethod("Revert")
        for _, cuid, k, _ in frame_jobs:
            if f"{cuid}#{k}" not in diagram_uid:
                raise LabVIEWError(f"the case structure has no frame {k} (frames are numbered from 0 as in "
                                   "vi_dump's \"frames\")")

        (fpw, fph), (bdw, bdh) = self._content_size(path)
        main_spec = {k: v for k, v in spec.items()
                     if k not in ("existing", "remove", "rename", "values", "retype", "disconnect", "move")}
        main_spec["wires"] = main_wires
        script = gen.build(main_spec,
                           existing=[{"name": b[0], "kind": b[1], "spec": b[3]} for b in bindings],
                           origin=(30, bdh + 80), fp_origin=(30, fph + 60), edit=True)
        frame_scripts = []
        for (cname, cuid, k, contents), fw in zip(frame_jobs, frame_wires):
            used = {str(e).split(".")[0] for w in fw for e in
                    ([w.get("from"), w.get("to")] if isinstance(w, dict) else w)} & bound_names
            sub_existing = [{"name": b[0], "kind": b[1], "spec": {}} for b in bindings if b[0] in used]
            frame_scripts.append((cuid, k, [b[0] for b in bindings if b[0] in used],
                                  gen.build({**contents, "wires": fw}, existing=sub_existing, origin=(40, 40))))

        bak = None
        if backup:
            bdir = Path(path).parent / ".lvmcp-backup"
            bdir.mkdir(exist_ok=True)
            bak = bdir / f"{Path(path).stem}.{time.strftime('%Y%m%d-%H%M%S')}{Path(path).suffix}"
            shutil.copy2(path, bak)
        # the file as it was, to put back if the edit only shows as broken once saved
        if pristine is None:
            pristine = self._scratch("edit") / Path(path).name
            shutil.copy2(path, pristine)

        removed = 0
        if cut_wires:
            try:
                wires = self._uid_map(target, "Wire")
                self._delete(target, [wires[u] for u in cut_wires if u in wires])
            except LabVIEWError as e:
                _try(lambda: target.Revert())
                raise LabVIEWError(f"{e} The VI was reverted.") from None
            self._uid_refs = {}
        if remove_nodes:
            gone = {n["id"] for n in remove_nodes}

            def node_part(end):
                return end.split(".")[0].split("[")[0]
            # Constants that only fed removed nodes go too (as Quick Drop's remover does).
            for n in list(by_id.values()):
                if n["kind"] != "constant" or n["id"] in gone or "uid" not in n:
                    continue
                outs = [w for w in graph["wires"] if w["from"] and node_part(w["from"]) == n["id"]]
                if outs and all(node_part(t) in gone for w in outs for t in w["to"]):
                    remove_nodes.append(n)
                    gone.add(n["id"])
            items = [self._ref_for_uid(target, n["uid"], n["kind"]) for n in remove_nodes]
            # Wires that touched the removed nodes (by vi_dump id prefix of their ends).
            touching = {w["uid"] for w in graph["wires"] if w.get("uid") is not None and any(
                end.split(".")[0].split("[")[0] in gone for end in [w["from"] or ""] + w["to"])}
            try:
                if rewire:
                    # NI's Quick Drop remover: deletes and joins loose ends of matching type.
                    self._remove(target, items)
                else:
                    # Plain delete: the node's wires first (else they would dangle), then the node.
                    wires = self._uid_map(target, "Wire")
                    self._delete(target, [r for u, r in wires.items() if u in touching])
                    self._delete(target, items)
            except LabVIEWError as e:
                _try(lambda: target.Revert())
                raise LabVIEWError(f"{e} The VI was reverted.") from None
            self._uid_refs = {}
            removed = len(items)

        refs = []
        fp = None
        for name, kind, how, _ in bindings:
            if how[0] == "label":
                fp = fp if fp is not None else self._fp_refs_by_label(target)
                if how[1] not in fp:
                    _try(lambda: target.Revert())
                    raise LabVIEWError(f"no control or indicator labelled {how[1]!r} on {Path(path).name}. "
                                       f"Labels: {sorted(k for k in fp if k)}")
                refs.append(fp[how[1]])
            else:
                refs.append(self._ref_for_uid(target, how[1], how[2]))

        before = self._sink_count(path)
        if script.xml.strip() == "<Script>\n</Script>":  # only removals or value changes
            status, code, source = False, 0, ""
            self._gen_out = []
        else:
            status, code, source = self._gen_script(target, script.xml, script.type_strings,
                                                    script.data_strings, refs=refs, raw=True)
        ref_of = {b[0]: r for b, r in zip(bindings, refs)}
        try:
            if not status:
                self._finish_nodes(target, script, list(self._gen_out), refs, {},
                                   lambda s, r: self._gen_script(target, s.xml, s.type_strings, s.data_strings,
                                                                 refs=r, raw=True))
            if not status and frame_scripts:
                for cuid, k, used, sub in frame_scripts:
                    def on_frame(s, r, key=f"{cuid}#{k}"):
                        # the helper closes the diagram reference it is given, so look it up each time
                        diag = self._uid_map(target, "Diagram", fresh=True)[diagram_uid[key]]
                        return self._gen_on_diagram(diag, s.xml, s.type_strings, s.data_strings, refs=r)
                    frame_refs = [ref_of[n] for n in used]
                    status, code, source = on_frame(sub, frame_refs)
                    if status:
                        break
                    self._finish_nodes(target, sub, list(self._gen_out), frame_refs, {}, on_frame)
        except LabVIEWError as e:
            _try(lambda: target.Revert())
            raise LabVIEWError(f"{e} The VI was reverted and not saved.") from None
        if status:
            _try(lambda: target.Revert())
            hint = _GEN_ERRORS.get(code, "")
            raise LabVIEWError(f"LabVIEW rejected the edit (error {code}){': ' + hint if hint else ''}. "
                               f"The VI was reverted and not saved.\nLabVIEW said: {_strip_html(str(source))}")
        if script.styles:
            try:
                self._apply_styles(target, script.styles)
            except LabVIEWError as e:
                _try(lambda: target.Revert())
                raise LabVIEWError(f"{e} The VI was reverted and not saved.") from None
        try:
            changed = self._edit_objects(target, by_id, rename, values, retype, move)
        except LabVIEWError as e:
            _try(lambda: target.Revert())
            raise LabVIEWError(f"{e} The VI was reverted and not saved.") from None
        after = self._sink_count(path)  # compiles the diagram, which also refreshes ExecState
        broken = self._still_broken(target)
        errors = []
        n_wires = len(script.wires) + sum(len(fs[3].wires) for fs in frame_scripts) - cut_sinks
        if not broken and before is not None and after is not None and after - before < n_wires:
            # LabVIEW drops a wire it cannot make (e.g. a second source into a wired indicator)
            # without an error; the dangling remains would be saved.
            _try(lambda: target.Revert())
            raise LabVIEWError(
                f"LabVIEW connected only {after - before} of the {n_wires} requested wires "
                "(a wire into an input that is already wired, or between incompatible terminals, is "
                "silently dropped). The VI was reverted and not saved. Check the spec with vi_dump.")
        if broken:
            # After objects were deleted (here or by a case-frame change) LabVIEW keeps reporting
            # "broken" until the references to them are released; save, release, judge again (and
            # put the original file back if it really is broken).
            target.SaveInstrument(path, False, False)
            # LabVIEW rechecks the VI only once the references this edit holds are released
            target = refs = ref_of = items = None
            self._uid_refs = {}
            import gc
            gc.collect()
            target = self._vi(path)
            target._FlagAsMethod("SaveInstrument")
            target._FlagAsMethod("Revert")
            broken = self._still_broken(target, 1.0)
            errors = self.vi_errors(target) if broken else []
            if broken and not allow_broken:
                shutil.copy2(pristine, path)
                _try(lambda: target.Revert())
        if broken and not allow_broken:
            _try(lambda: target.Revert())
            listed = "; ".join(e["error"] + (f" (x{e['count']})" if e.get("count") else "") for e in errors[:8])
            raise LabVIEWError(
                "The edit made the VI broken (for example two sources wired to one indicator, an unwired "
                "required input, or mismatched types), so it was reverted and not saved."
                + (f" LabVIEW's error list: {listed}." if listed else "")
                + " Fix the spec, or pass allow_broken=true to save it anyway and inspect it with vi_info "
                "(each error with LabVIEW's details) and vi_snapshot.")
        for label, value in script.defaults.items():
            target.SetControlValue(label, value)
        if script.defaults:
            target._FlagAsMethod("MakeCurValueDefault")
            target.MakeCurValueDefault()
        if script.relabel:
            self._relabel(target, script.relabel)
        target.SaveInstrument(path, False, False)
        return {"path": path, "backup": str(bak) if bak else None,
                "exec_state": EXEC_STATES.get(target.ExecState, target.ExecState),
                **({"errors": errors} if broken else {}),
                "removed": removed,
                "added_front_panel": {k: (script.relabel or {}).get(v, v) for k, v in script.labels.items()},
                **({"disconnected": len(cut_wires)} if cut_wires else {}), **changed}

    @staticmethod
    def _still_broken(vi, wait_s: float = 3.0) -> bool:
        """ExecState can read "broken" for a moment after an edit (deleted wires, then new code)
        until LabVIEW has rechecked the diagram, so a broken result is confirmed by waiting."""
        t0 = time.monotonic()
        while vi.ExecState == 0:
            if time.monotonic() - t0 > wait_s:
                return True
            time.sleep(0.2)
        return False

    def _is_label(self, key: str, target) -> bool:
        """A front-panel label of the VI (else a vi_dump node id)."""
        cache = self.__dict__.setdefault("_label_cache", {})
        k = id(target)
        if k not in cache:
            cache.clear()
            cache[k] = set(self._fp_refs_by_label(target))
        return key in cache[k]

    @staticmethod
    def _bind_end(end: str, by_id: dict, existing_map: dict) -> str:
        """A vi_dump wire end as a vi_edit spec end, binding its node under a generated name."""
        node, _, term = end.partition(".")
        n = by_id.get(node)
        if n is None or n["kind"] in ("tunnel", "shift_register", "shift_register_left",
                                      "shift_register_right") or "[" in end:
            raise LabVIEWError(f"disconnect: the wire also goes to {end!r}, which vi_edit cannot re-wire "
                               "(a tunnel or shift register). Remove and re-make that code instead.")
        name = "_w_" + "".join(ch if ch.isalnum() else "_" for ch in node)
        existing_map.setdefault(name, n["label"] if n["kind"] in ("control", "indicator") else {"node": node})
        return f"{name}.{term}" if term else name

    def _edit_objects(self, target, by_id: dict, rename: dict, values: dict, retype: dict,
                      move: dict | None = None) -> dict:
        """vi_edit "rename" / "values" / "retype" on front-panel objects (by label) and constants
        (by vi_dump id)."""
        from win32com.client import VARIANT
        from . import gen
        done: dict = {}
        move = move or {}
        if not (rename or values or retype or move):
            return done
        for key, xy in move.items():  # block-diagram objects by vi_dump id
            n = by_id.get(key)
            if n is None or "uid" not in n or not (isinstance(xy, (list, tuple)) and len(xy) == 2):
                raise LabVIEWError(f"move: give a vi_dump node id and [x, y]; got {key!r}: {xy!r}")
            h = self._helper(SETPOS_VI)
            h.SetControlValue("reference", self._ref_for_uid(target, n["uid"], n["kind"]))
            h.SetControlValue("position", (int(xy[0]), int(xy[1])))
            self._run_checked(h, f"moving {key!r}")
        fp = self._fp_refs_by_label(target)
        arr = pythoncom.VT_ARRAY | pythoncom.VT_UI1

        def const_ref(key, what):
            n = by_id.get(key)
            if n is None or n["kind"] != "constant" or "uid" not in n:
                raise LabVIEWError(f"{what}: {key!r} is neither a front-panel label ({sorted(k for k in fp if k)}) "
                                   "nor a vi_dump constant id")
            return n, self._ref_for_uid(target, n["uid"], "constant")
        for key, t in retype.items():  # first, so "values" can use the new type
            if key in fp:
                h, ref = self._helper(REP_VI), fp[key]
            else:
                h, ref = self._helper(CONSTREP_VI), const_ref(key, "retype")[1]
                by_id[key] = dict(by_id[key], type=t)
            h.SetControlValue("reference", ref)
            h.SetControlValue("representation", _REPRESENTATION[t])
            self._run_checked(h, f"retype {key!r} (only numbers can be retyped)")
        for key, v in values.items():
            if key in fp:
                target.SetControlValue(key, v)
                target._FlagAsMethod("MakeCurValueDefault")
                target.MakeCurValueDefault()
                continue
            n, ref = const_ref(key, "values")
            try:
                lt = gen.parse_type(n.get("type"))
                ts, ds = lt.td(), lt.data(lt.enum_index(v) if lt.kind == "enum" else v)
            except (gen.SpecError, TypeError, ValueError) as e:
                raise LabVIEWError(f"values: cannot set {key!r} (type {n.get('type')!r}) to {v!r}: {e}") from None
            h = self._helper(CONSTVALUE_VI)
            h.SetControlValue("reference", ref)
            h.SetControlValue("type string", VARIANT(arr, ts))
            h.SetControlValue("data string", VARIANT(arr, ds))
            self._run_checked(h, f"setting the value of {key!r}")
        for old, new in rename.items():  # last: the other edits find objects by their old labels
            if old not in fp:
                raise LabVIEWError(f"rename: no control or indicator labelled {old!r}; labels: "
                                   f"{sorted(k for k in fp if k)}")
            h = self._helper(RELABEL_VI)
            h.SetControlValue("reference", fp[old])
            h.SetControlValue("text", str(new))
            self._run_checked(h, f"renaming {old!r}")
        for k, v in (("renamed", rename), ("values_set", values), ("retyped", retype), ("moved", move)):
            if v:
                done[k] = sorted(v)
        return done

    def _uid_map(self, vi, cls: str, fresh: bool = False) -> dict:
        """uid -> GObject refnum for every block-diagram object of a VI Server class (also works on
        broken VIs, unlike vi_dump)."""
        cache = self.__dict__.setdefault("_uid_refs", {})
        if fresh or cls not in cache:
            h = self._helper(UIDS_VI)
            h.SetControlValue("VI Refnum", vi)
            h.SetControlValue("Class Name", cls)
            h.SetControlValue("Traverse Target", 1)
            h.Run(False)
            uids = list(h.GetControlValue("UIDs") or ())
            owners = list(h.GetControlValue("Owner UIDs") or ()) or [0] * len(uids)
            refs = list(h.GetControlValue("References") or ())
            # element constants inside array / cluster constants are not objects of their own
            cache[cls] = {u: r for u, o, r in zip(uids, owners, refs)
                          if not (cls == "Constant" and o in uids)}
        return cache[cls]

    def _remove(self, vi, items: list) -> None:
        core = self._vi(str(Path(self.app.ApplicationDirectory) / _REMOVE_CORE))
        core.SetControlValue("Items to Remove", items)
        core.SetControlValue("Clean Wires (T)", True)
        core.Run(False)
        st, code, src = core.GetControlValue("error out")
        if st:
            raise LabVIEWError(f"removing failed (error {code}). {_strip_html(str(src))}")

    def _gen_on_diagram(self, diagram_ref, xml: str, type_strings, data_strings, refs=()):
        """Run a diagram script on one specific (sub)diagram, e.g. a case frame."""
        h = self._helper(DIAG_GEN_VI)
        h.SetControlValue("diagram", diagram_ref)
        h.SetControlValue("XML Script Commands", xml)
        h.SetControlValue("Type Strings", _byte_strings(type_strings))
        h.SetControlValue("Data Strings", _byte_strings(data_strings))
        h.SetControlValue("Obj Refs In[]", list(refs))
        try:
            h.Run(False)
            self._gen_out = list(_try(lambda: h.GetControlValue("Obj Refs Out[]"), ()) or ())
            return h.GetControlValue("error out")
        finally:
            if refs:
                _try(lambda: h.SetControlValue("Obj Refs In[]", []))

    def _diagram_uids(self, path: str, target) -> dict[str, int]:
        """vi_dump container id ("" for the top level, "case_4#1" for a frame, "for_loop_3") -> the
        UID of that LabVIEW Diagram object. Found by dropping a marker constant into every diagram,
        reading back where vi_dump sees it, and reverting the VI."""
        from . import gen
        try:
            self._dfir_text(path)
        except LabVIEWError:
            # No compiler export (e.g. property nodes, or a broken VI): take the frames from a scan,
            # numbered in the order the frames were created.
            return self._scan(path)["diagram_uids"]
        diags = self._uid_map(target, "Diagram", fresh=True)
        ty = gen.parse_type("i32")
        base = 1_900_000_000
        try:
            for uid, ref in diags.items():
                xml = (f"<Script><Constant name=\"lvmcp_marker\" typeindex=\"0\" position=\"(0,0)\">"
                       f"</Constant></Script>")
                self._gen_on_diagram(ref, xml, [ty.td()], [ty.data(base + uid)])
            g = self.vi_dump(path)
            # The markers shift vi_dump's numbering, so key by the structure's UID, which is stable:
            # "" (top level), "<uid>" (a loop body) or "<uid>#<frame>" (a case frame).
            uid_of = {n["id"]: n.get("uid") for n in g["nodes"]}
            found = {}
            for n in g["nodes"]:
                v = n.get("value")
                if n["kind"] == "constant" and isinstance(v, int) and base <= v < base + 1_000_000:
                    where = n.get("in", "")
                    sid, _, frame = where.partition("#")
                    key = "" if not where else f"{uid_of.get(sid)}" + (f"#{frame}" if frame else "")
                    found[key] = v - base
            return found
        finally:
            target._FlagAsMethod("Revert")
            _try(lambda: target.Revert())
            self._uid_refs = {}

    def _delete(self, vi, items: list) -> None:
        """GObject.Delete on each item (no rewiring)."""
        if not items:
            return
        h = self._helper(DELETE_VI)
        h.SetControlValue("References", items)
        h.Run(False)

    def _ref_for_uid(self, vi, uid: int, kind: str):
        """GObject refnum for a vi_dump node. DFIR reports a constant by its data object's UID,
        which is a few above the constant node's own UID."""
        cls = "Constant" if kind == "constant" else "Node"
        refs = self._uid_map(vi, cls)
        if cls == "Constant":
            below = [u for u in refs if u <= uid]
            if below:
                return refs[max(below)]
        elif uid in refs:
            return refs[uid]
        raise LabVIEWError(f"object with uid {uid} not found on the diagram")

    def _dfir_text(self, path: str) -> str:
        if not DFIR_VI.exists():
            raise LabVIEWError(f"helper VI missing from the package: {DFIR_VI}")
        target = self._vi(path)
        h = self._helper(DFIR_VI)
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
            names = _try(lambda: self._conpane_labels(path))
            if names is not None:
                cache[path] = names
                return names
            try:
                g = dfir.parse(self._dfir_text(path))
                terms = [(n["conpane_index"], n.get("label", "")) for n in g["nodes"]
                         if n["kind"] in ("control", "indicator") and n.get("conpane_index", -1) >= 0
                         and "in" not in n]
                cache[path] = [lab for _, lab in sorted(terms)]
            except LabVIEWError:
                cache[path] = None
        return cache[path]

    def vi_test(self, path: str, cases: list, timeout_s: float = 30.0, tol: float = 1e-9) -> dict:
        """Run a VI once per case ({"name", "inputs", "expect", "tol"}) and compare the outputs."""
        if not isinstance(cases, list) or not cases:
            raise LabVIEWError("cases is a list of {\"inputs\": {...}, \"expect\": {label: value}}")
        rows, passed = [], 0
        for i, c in enumerate(cases):
            if not isinstance(c, dict) or not isinstance(c.get("expect"), dict) or not c["expect"]:
                raise LabVIEWError(f"case {i}: needs \"expect\": {{indicator label: value, ...}}")
            name = c.get("name") or f"case {i + 1}"
            row: dict = {"case": name}
            try:
                r = self.vi_run(path, c.get("inputs") or {}, list(c["expect"]), timeout_s)
            except LabVIEWError as e:
                row.update(passed=False, error=str(e)[:300])
                rows.append(row)
                continue
            got = r.get("values", {})
            bad = {}
            for label, want in c["expect"].items():
                if not _matches(got.get(label), want, c.get("tol", tol)):
                    bad[label] = {"expected": want, "got": got.get(label)}
            if not r.get("completed", True):
                bad["(run)"] = {"expected": "the VI finishes", "got": f"aborted after {timeout_s} s"}
            row["passed"] = not bad
            if bad:
                row["mismatches"] = bad
            passed += not bad
            rows.append(row)
        return {"vi": path, "passed": passed, "failed": len(rows) - passed,
                "summary": f"{passed}/{len(rows)} cases passed", "cases": rows}

    def vi_diff(self, path_a: str, path_b: str | None = None) -> dict:
        """What changed from VI a (old) to VI b (new). Without b: from the newest vi_edit backup
        (.lvmcp-backup/) of a to a itself."""
        from . import diff
        if path_b is None:
            cur = Path(check_vi_path(path_a))
            baks = sorted((cur.parent / ".lvmcp-backup").glob(f"{cur.stem}.*{cur.suffix}"),
                          key=lambda p: p.stat().st_mtime)
            if not baks:
                raise LabVIEWError(f"no backup of {cur.name} in {cur.parent / '.lvmcp-backup'}; give path_b")
            old, new = baks[-1], cur
        else:
            old, new = Path(check_vi_path(path_a)), Path(check_vi_path(path_b))
        # LabVIEW holds one VI per name: read the old version from a copy with its own name, next to
        # the new one so relative subVI paths still resolve.
        tmp = new.parent / f"{new.stem}.lvmcp-diff-{time.time_ns()}{old.suffix}"
        shutil.copy2(old, tmp)
        try:
            ga = self.vi_dump(str(tmp), "scan")
        finally:
            _try(lambda: tmp.unlink())
        gb = self.vi_dump(str(new), "scan")
        res = diff.diff(ga, gb)
        return {"old": str(old), "new": str(new), **res}

    def _conpane_labels(self, path: str) -> list[str]:
        """Labels of the controls on a VI's connector pane, in terminal order, skipping unassigned
        terminals (the order DFIR numbers a subVI node's terminals). Works for locked VIs too: it
        reads the front panel (VI.Connector Pane -> Controls[])."""
        h = self._helper(CONPANE_VI)
        vi = self._vi(path)  # kept alive until the helper has run
        h.SetControlValue("reference", vi)
        self._run_checked(h, "reading the connector pane")
        lab = self._vi(str(Path(self.app.ApplicationDirectory) / _VILIB_TREF / "Get GObject Label.vi"))
        out = []
        for r in h.GetControlValue("Controls[]") or ():
            if r:
                lab.SetControlValue("GObject", r)
                lab.Run(False)
                out.append(lab.GetControlValue("Object Label"))
        return out

    def _control_classes(self, target, g: dict) -> None:
        """The VI Server class of each front-panel control ("WaveformChart", "Slide", "Listbox"):
        its data type alone does not say it is a chart or a listbox."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 0)  # front panel
        h.SetControlValue("Class Name", "Control")
        h.Run(False)
        fp = self._fp_refs_by_label(target)
        cls_of = dict(zip(h.GetControlValue("UIDs") or (), h.GetControlValue("Classes") or ()))
        for n in g["nodes"]:
            if n["kind"] in ("control", "indicator") and n.get("label") in fp:
                c = cls_of.get(_try(lambda: self._uid_of(fp[n["label"]])))
                if c:
                    n["vi_class"] = str(c)

    def vi_dump(self, path: str, method: str = "auto") -> dict:
        """method: "dfir" (compiler export: constant values, but refuses some VIs), "scan" (walk the
        objects with VI Scripting: any VI, including broken ones, no constant values) or "auto"
        (dfir, falling back to scan)."""
        from . import catalog, dfir
        path = check_vi_path(path)
        if method not in ("auto", "dfir", "scan"):
            raise LabVIEWError("method must be auto, dfir or scan")
        if method != "scan":
            try:
                text = self._dfir_text(path)
                g = dfir.parse(text, catalog.load(self.app.ApplicationDirectory), self._connector_labels)
                if any(n["kind"] == "constant" and "[]" in json.dumps(n.get("type", "")) for n in g["nodes"]):
                    self._fill_array_constants(path, g)
                _try(lambda: self._real_terminal_names(g))
                _try(lambda: self._dfir_flat_types(str(path), g))  # first: it fills in missing labels
                _try(lambda: self._control_classes(self._vi(str(path)), g))
                _try(lambda: self._static_vi_targets(self._vi(str(path)), g))
                return {"vi": str(path), "source": "dfir", **_owning_library(path), **g}
            except LabVIEWError as e:
                if method == "dfir" or "password-protected" in str(e):
                    raise
        return {"vi": str(path), "source": "scan", **_owning_library(path),
                "note": "read by walking the diagram objects: constant values are not included, and "
                        "case frames are numbered in creation order",
                **self._scan(path)}

    def _real_terminal_names(self, g: dict) -> None:
        """DFIR names a function's terminals from LabVIEW's Context Help ("number (0 to 1)"); the
        terminals themselves may be named differently ("number: 0 to 1"). Rename wire ends to the
        real names when they match ignoring spaces and punctuation."""
        def norm(x):
            return re.sub(r"[^a-z0-9]", "", x.lower())
        from . import catalog
        labels_of = catalog.load(self.app.ApplicationDirectory)
        funcs = {n["id"]: n for n in g["nodes"] if n["kind"] == "function" and n.get("function")
                 and n["function"] not in ("Unbundle By Name", "Bundle By Name",
                                       "Build Waveform", "Get Waveform Components")}  # element names are the cluster's
        used: dict[str, dict[str, bool]] = {}  # node -> wired label -> is an output
        for w in g["wires"]:
            for e, is_out in [(w.get("from"), True)] + [(t, False) for t in w["to"]]:
                node, _, term = (e or "").partition(".")
                # (Format Into String's "input 2"...: a default-size node has only "input 1", so a
                # later input must keep its name, not be paired with a leftover terminal)
                if node in funcs and term and not term.startswith("element[") \
                        and not re.fullmatch(r"input \d+", term):
                    used.setdefault(node, {})[term] = is_out
        fixes: dict[str, dict[str, str]] = {}
        for node, terms in used.items():
            f = funcs[node]
            real = self.node_terminals(f["function"], f.get("size"))
            if not real:
                continue
            names = {n for n, _ in real}
            if any(t not in names for t in terms):
                # the node placed by name may be an older one lacking terminals (Unflatten From
                # String): vi_create places the full one when a wire names its terminals, so keep them
                full = {n for n, _ in self.replaced_terminals(f["function"], f.get("size")) or ()}
                if full - names and all(t in full for t in terms):
                    continue
            fx = {}
            def base(x):
                return norm(x.split("(")[0])
            for term in terms:
                if term in names:
                    continue
                for same in (lambda n: norm(n) == norm(term),                 # punctuation only
                             lambda n: norm(n).startswith(norm(term)),        # "error in (no error)"
                             lambda n: base(n) == base(term) and base(term)):  # "default (0L)"
                    hits = [n for n, o in real if same(n) and o == terms[term]]
                    if len(hits) == 1:
                        fx[term] = hits[0]
                        break
            # Still unmatched (help text differs: "element data type" / "notification data type"):
            # pair them when exactly one wired label and one real terminal of that direction remain.
            known = {norm(x) for x in labels_of.get(f["function"], [])}
            for out in (False, True):
                left = [t for t, o in terms.items() if o == out and t not in names and t not in fx]
                free = [n for n, o in real if o == out and norm(n) not in known
                        and n not in fx.values() and n not in terms]
                if len(left) == 1 and len(free) == 1:
                    fx[left[0]] = free[0]
            if fx:
                fixes[node] = fx

        def fix(e):
            node, dot, term = (e or "").partition(".")
            return f"{node}.{fixes[node][term]}" if node in fixes and term in fixes[node] else e
        if fixes:
            for w in g["wires"]:
                w["from"] = fix(w.get("from"))
                w["to"] = [fix(t) for t in w["to"]]
            g["unwired_inputs"] = [fix(t) for t in g.get("unwired_inputs", [])]

    def _fill_array_constants(self, path: str, g: dict) -> None:
        """The DFIR export leaves array constants empty; read constant values with VI Scripting.
        DFIR reports a constant by its data object's UID, a few above the constant's own UID."""
        target = self._vi(path)  # keep the reference alive while the helper runs
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Class Name", "Constant")
        h.SetControlValue("Traverse Target", 1)
        h.Run(False)
        uids = list(h.GetControlValue("UIDs") or ())
        owners = list(h.GetControlValue("Owner UIDs") or ())
        vals = _try(lambda: list(h.GetControlValue("Values") or ()), []) or []
        # skip the element constants inside array / cluster constants
        by_uid = {u: v for u, o, v in zip(uids, owners, vals) if o not in uids}
        for n in g["nodes"]:
            if n["kind"] == "constant" and "uid" in n and "[]" in json.dumps(n.get("type", "")):
                below = [u for u in by_uid if u <= n["uid"]]
                if below:
                    n["value"] = _jsonable(by_uid[max(below)])

    def _conpane_by_index(self, target) -> dict:
        """Connector-pane terminal index -> (label, refnum) of the control assigned to it."""
        h = self._helper(CONPANE_VI)
        h.SetControlValue("reference", target)
        self._run_checked(h, "reading the connector pane")
        lab = self._vi(str(Path(self.app.ApplicationDirectory) / _VILIB_TREF / "Get GObject Label.vi"))
        out = {}
        for i, r in enumerate(h.GetControlValue("Controls[]") or ()):
            if r:
                lab.SetControlValue("GObject", r)
                lab.Run(False)
                out[i] = (lab.GetControlValue("Object Label") or "", r)
        return out

    def _top_controls(self, target) -> list:
        """(label, refnum) of the top-level front-panel controls and indicators."""
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Traverse Target", 0)  # front panel
        h.SetControlValue("Class Name", "Control")
        h.Run(False)
        uids = list(h.GetControlValue("UIDs") or ())
        lab = self._vi(str(Path(self.app.ApplicationDirectory) / _VILIB_TREF / "Get GObject Label.vi"))
        out = []
        for o, r in zip(h.GetControlValue("Owner UIDs") or (), h.GetControlValue("References") or ()):
            if o in uids:
                continue  # an element of a cluster or array
            lab.SetControlValue("GObject", r)
            lab.Run(False)
            out.append((lab.GetControlValue("Object Label") or "", r))
        return out

    def _dfir_flat_types(self, path: str, g: dict) -> None:
        """_flat_types for a DFIR dump. DFIR names a constant by its data object's UID, a few above
        the constant's own UID."""
        target = self._vi(path)
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Class Name", "Constant")
        h.SetControlValue("Traverse Target", 1)
        h.Run(False)
        uids = list(h.GetControlValue("UIDs") or ())
        owners = list(h.GetControlValue("Owner UIDs") or ())
        ref_of = {u: r for u, o, r in zip(uids, owners, h.GetControlValue("References") or ()) if o not in uids}

        def const_ref(n):
            below = [u for u in ref_of if "uid" in n and u <= n["uid"]]
            return ref_of[max(below)] if below else None
        self._flat_types(target, g, const_ref)

    def _scan(self, path: str) -> dict:
        from . import scan
        target = self._vi(path)
        target._FlagAsMethod("GetLockState")
        if _try(lambda: target.GetLockState()) == 3:
            raise LabVIEWError(f"{Path(path).name} is password-protected; its block diagram cannot be read.")
        h = self._helper(UIDS_VI)
        h.SetControlValue("VI Refnum", target)
        h.SetControlValue("Class Name", "GObject")
        h.SetControlValue("Traverse Target", 1)
        h.Run(True)  # asynchronous so a stuck helper can be aborted instead of hanging the server
        t0 = time.monotonic()
        while h.ExecState in (2, 3):
            if time.monotonic() - t0 > 120:
                h.Abort()
                raise LabVIEWError("reading the diagram objects timed out")
            time.sleep(0.05)
        cols = [list(h.GetControlValue(c) or ()) for c in
                ("UIDs", "Classes", "Owner UIDs", "Is Source", "Wire UIDs", "Names", "References")]
        values = _try(lambda: list(h.GetControlValue("Values") or ()), []) or []
        values += [None] * (len(cols[0]) - len(values))
        rows = [scan.Row(int(u), str(c), int(o), bool(s), int(w), str(n),
                         _jsonable(v) if str(c).endswith("Constant") else None)
                for u, c, o, s, w, n, _, v in zip(*cols, values)]
        # Labels name functions ("Add") and subVIs ("Scale.vi"); only nodes need them.
        lab = self._vi(str(Path(self.app.ApplicationDirectory) / _VILIB_TREF / "Get GObject Label.vi"))
        labels = {}
        for r, ref in zip(rows, cols[6]):
            if r.cls in ("Wire", "Diagram", "TopLevelDiagram", "Text", "ControlTerminal") or \
                    r.cls.endswith("Terminal") or r.cls.endswith("Tunnel") or r.cls.endswith("Constant"):
                continue
            lab.SetControlValue("GObject", ref)
            lab.Run(False)
            labels[r.uid] = lab.GetControlValue("Object Label")
        g = scan.build(rows, labels, _try(lambda: self._shift_register_sides(rows, cols[6])) or {},
                       _try(lambda: self._frame_order(rows, cols[6])) or {},
                       _try(lambda: self._case_selectors(rows, cols[6])) or {})
        if any(n["kind"] == "event_structure" for n in g["nodes"]):
            sel = _try(lambda: self.event_selectors(target)) or []
            evs = sorted((n for n in g["nodes"] if n["kind"] == "event_structure"), key=lambda n: n["uid"])
            for n, caps in zip(evs, sel):
                for fr, cap in zip(n.get("frames", []), caps):
                    fr["events"] = parse_event_caption(cap)
        _try(lambda: self._scan_types(target, g, dict(zip((r.uid for r in rows), cols[6]))))
        _try(lambda: self._static_vi_targets(target, g))
        _try(lambda: self._control_classes(target, g))
        return g

    def _case_selectors(self, rows, refs) -> dict:
        """{case structure UID: its selector's UID} for vi_dump's scan (the selector is a tunnel)."""
        out, h = {}, None
        for r, ref in zip(rows, refs):
            if r.cls == "CaseStructure":
                h = h or self._helper(CASESELECTOR_VI)
                h.SetControlValue("reference", ref)
                h.Run(False)
                if not h.GetControlValue("error out")[0]:
                    out[r.uid] = int(h.GetControlValue("UID"))
        return out

    def _frame_order(self, rows, refs) -> dict:
        """{multi-frame structure UID: its frames' diagram UIDs in frame order} for vi_dump's scan."""
        out, h = {}, None
        for r, ref in zip(rows, refs):
            if r.cls in ("CaseStructure", "EventStructure", "StackedSequence", "DisableStructure",
                         "ConditionalDisableStructure", "TypeSpecializationStructure"):
                h = h or self._helper(FRAMEDIAGS_VI)
                h.SetControlValue("reference", ref)
                h.Run(False)
                if not h.GetControlValue("error out")[0]:
                    out[r.uid] = [int(self._uid_of(d)) for d in h.GetControlValue("Diagrams[]") or ()]
        return out

    def _static_vi_targets(self, target, g: dict) -> None:
        """The VI each Static VI Reference points at ("path") and whether it is strictly typed."""
        nodes = [n for n in g["nodes"] if n.get("function") == "Static VI Reference" and "uid" in n]
        if not nodes:
            return
        refs, h = self._all_of(target, "StaticVIReference"), self._helper(STATICVIINFO_VI)
        for n in nodes:
            if n["uid"] not in refs:
                continue
            h.SetControlValue("reference", refs[n["uid"]])
            h.Run(False)
            if h.GetControlValue("error out")[0]:
                continue
            p = str(h.GetControlValue("path") or "")
            if p:
                n["path"] = p
                n["strict"] = bool(h.GetControlValue("strict"))

    def _shift_register_sides(self, rows, refs) -> dict:
        """{left shift register UID: (right side UID, top)} for vi_dump's scan reader."""
        out, h = {}, None
        for r, ref in zip(rows, refs):
            if r.cls == "LeftShiftRegister":
                h = h or self._helper(SRRIGHT_VI)
                h.SetControlValue("reference", ref)
                h.Run(False)
                if not h.GetControlValue("error out")[0]:
                    out[r.uid] = (int(h.GetControlValue("UID")), int(h.GetControlValue("top")))
        return out

    def _scan_types(self, target, g: dict, refs: dict) -> None:
        """Data types of constants and front-panel terminals, from their values' flattened type."""
        from . import td
        sp = _try(lambda: self._helper(SUBVIPATH_VI))
        for n in g["nodes"]:
            if sp is not None and n["kind"] == "subvi" and n.get("uid") in refs:
                sp.SetControlValue("reference", refs[n["uid"]])
                sp.Run(False)
                if not sp.GetControlValue("error out")[0]:
                    p = str(sp.GetControlValue("path") or "")
                    if p:
                        n["path"] = p
        pi, ii = _try(lambda: self._helper(PNODEINFO_VI)), _try(lambda: self._helper(INODEINFO_VI))
        for n in g["nodes"]:  # Property / Invoke Nodes: class, linked control, method
            h = pi if n["kind"] == "property_node" else ii if n["kind"] == "invoke_node" else None
            if h is None or n.get("uid") not in refs:
                continue
            h.SetControlValue("reference", refs[n["uid"]])
            h.Run(False)
            cls = str(h.GetControlValue("class name") or "")
            if cls:
                n["vi_class"] = cls.rsplit(":", 1)[-1].replace(" (strict)", "")
            label = str(h.GetControlValue("label") or "")
            if label and not h.GetControlValue("error out")[0]:
                n["control"] = label
            if n["kind"] == "property_node":
                order = _try(lambda: self._item_terminals(target, refs[n["uid"]], n["uid"]))
                if order and sorted(order) == sorted(n.get("items", [])):
                    n["items"] = order  # node order (the scan lists terminals by UID)
                # LabVIEW's names of each item, (ID, data name, short name, long name); an item whose
                # terminal shows none of them is a nested one ("Label.Text": Label, then Text.Text)
                props = list(_try(lambda: h.GetControlValue("Properties[]"), ()) or ())
                if props and len(props) == len(n.get("items", [])):
                    n["properties"] = [str(p[2]) for p in props]
                    nested = [t for t, p in zip(n["items"], props) if t not in (p[1], p[2], p[3])]
                    if nested:
                        n["nested"] = nested
            if n["kind"] == "invoke_node":
                m = _try(lambda: h.GetControlValue("Method"))  # (ID, data name, short name, long name)
                if m and len(m) >= 3 and m[2]:
                    n["method"] = str(m[2])
        cn = _try(lambda: self._helper(CASENAMES_VI))
        for n in g["nodes"]:  # case frames: LabVIEW's own case names ("1..3, 5", "Default")
            if cn is not None and n["kind"] == "case" and n.get("uid") in refs and n.get("frames"):
                cn.SetControlValue("reference", refs[n["uid"]])
                cn.Run(False)
                if not cn.GetControlValue("error out")[0]:
                    names = [str(x).strip() for x in cn.GetControlValue("names") or ()]
                    if len(names) == len(n["frames"]):
                        for fr, nm in zip(n["frames"], names):
                            fr["name"] = nm
                            if "Default" in [x.strip() for x in nm.split(",")]:
                                fr["default"] = True
        self._flat_types(target, g, lambda n: refs.get(n.get("uid")), always=True)

    def _flat_types(self, target, g: dict, const_ref, always: bool = False) -> None:
        """Types of constants and front-panel terminals from their values' flattened type + data.
        A readable type replaces n["type"] when `always` or the current one cannot be built; when the
        type cannot be written in vi_create notation (waveform, picture, class, ...) or a constant's
        value is unknown, n["flat"] keeps the exact type and default value for vi_create."""
        from . import gen, td

        def buildable(t):
            try:
                gen.parse_type(t)
                return True
            except gen.SpecError:
                return False
        ct, ft = self._helper(CONSTTYPE_VI), self._helper(CTLTYPE_VI)
        fp = None
        unlabeled = [n for n in g["nodes"] if n["kind"] in ("control", "indicator") and not n.get("label")]
        # terminals without a label (DFIR leaves some out) are paired, in UID order, with the panel
        # controls no other terminal names; DFIR's UIDs and the controls' UIDs grow together
        paired = {}
        if unlabeled:
            known = {n.get("label") for n in g["nodes"] if n["kind"] in ("control", "indicator")}
            # first by connector-pane terminal (DFIR's conpane_index is the terminal's index)
            pane = _try(lambda: self._conpane_by_index(target), {}) or {}
            for n in list(unlabeled):
                x = pane.get(n.get("conpane_index", -1))
                if x is not None and x[0] not in known:
                    paired[id(n)] = x
                    known.add(x[0])
                    unlabeled.remove(n)
            rest = [(lb, r) for lb, r in (_try(lambda: self._top_controls(target), []) or [])
                    if lb not in known or not lb]
            if unlabeled and len(rest) == len(unlabeled):
                rest.sort(key=lambda x: _try(lambda: self._uid_of(x[1]), 0))
                unlabeled.sort(key=lambda n: n.get("uid", 0))
                paired.update({id(n): x for n, x in zip(unlabeled, rest)})
        for n in g["nodes"]:
            is_const = n["kind"] == "constant"
            ok = buildable(n.get("type")) if n.get("type") is not None else False
            if not always and ok and (not is_const or "value" in n):
                continue
            if is_const:
                h, ref = ct, _try(lambda: const_ref(n))
            elif n["kind"] in ("control", "indicator") and n.get("label"):
                fp = fp if fp is not None else self._fp_refs_by_label(target)
                h, ref = ft, fp.get(n["label"])
            elif id(n) in paired:
                lb, ref = paired[id(n)]
                h = ft
                if lb:
                    n["label"] = lb
            else:
                continue
            if ref is None:
                continue
            h.SetControlValue("reference", ref)
            h.Run(False)
            if h.GetControlValue("error out")[0]:
                continue
            ts = _try(lambda: b"".join(struct.pack(">h", x) for x in h.GetControlValue("type string") or ()))
            if not ts or len(ts) < 4:
                continue
            t = _try(lambda: td.from_flattened(ts))
            if t is not None and (always or not ok):
                n["type"] = t
            if ts[3] == 0x00:  # void: a variant's Value (its contents' type, here none)
                continue
            if not buildable(n.get("type")) or (is_const and "value" not in n):
                ds = _try(lambda: bytes(h.GetControlValue("data bytes") or b""), b"")
                if len(ds) <= 65536:
                    n["flat"] = {"flat": ts.hex(), "data": ds.hex()}
                    if "lvclass" in json.dumps(n.get("type")):  # class data needs its class loaded
                        n["flat"]["from"] = str(target.Path)


def _flat_sources(o, out=None) -> set:
    """The "from" VIs of the copied types in a spec."""
    out = set() if out is None else out
    if isinstance(o, dict):
        if "flat" in o and isinstance(o.get("from"), str):
            out.add(o["from"])
        for v in o.values():
            _flat_sources(v, out)
    elif isinstance(o, list):
        for v in o:
            _flat_sources(v, out)
    return out


# Helper VIs; LVMCP_VIS_DIR points at another copy (used to test copies saved for older LabVIEW).
VIS_DIR = Path(os.environ.get("LVMCP_VIS_DIR") or Path(__file__).parent / "vis")
BOOTSTRAP_VI = VIS_DIR / "lvmcp_gen_script.vi"
BLANK_VI = VIS_DIR / "lvmcp_blank.vi"
DFIR_VI = VIS_DIR / "lvmcp_dfir.vi"
RAW_GEN_VI = VIS_DIR / "lvmcp_gen_script_raw.vi"
UIDS_VI = VIS_DIR / "lvmcp_uids.vi"
GETERRORS_VI = VIS_DIR / "lvmcp_geterrors.vi"
DELETE_VI = VIS_DIR / "lvmcp_delete.vi"
DIAG_GEN_VI = VIS_DIR / "lvmcp_gen_script_diag.vi"
REPLACE_VI = VIS_DIR / "lvmcp_replace.vi"
ADDFRAME_VI = VIS_DIR / "lvmcp_addframe.vi"
EVENTS_VI = VIS_DIR / "lvmcp_events.vi"
PROPCONFIG_VI = VIS_DIR / "lvmcp_propconfig.vi"
INVOKECONFIG_VI = VIS_DIR / "lvmcp_invokeconfig.vi"
# generated by tools/helpergen.py
PROPWRITE_VI = VIS_DIR / "lvmcp_propwrite.vi"
PROPLINK_VI = VIS_DIR / "lvmcp_proplink.vi"
INVOKELINK_VI = VIS_DIR / "lvmcp_invokelink.vi"
UID_VI = VIS_DIR / "lvmcp_uid.vi"
ALLPROPS_VI = VIS_DIR / "lvmcp_allprops.vi"
SIGNAL_VI = VIS_DIR / "lvmcp_signal.vi"
FIRE_VI = VIS_DIR / "lvmcp_fire.vi"
TIMEOUTTERM_VI = VIS_DIR / "lvmcp_timeoutterm.vi"
EVENTSEL_VI = VIS_DIR / "lvmcp_eventsel.vi"
TERMCONST_VI = VIS_DIR / "lvmcp_termconst.vi"
OUTCNT_VI = VIS_DIR / "lvmcp_outcnt.vi"
UNBNAMES_VI = VIS_DIR / "lvmcp_unbnames.vi"
CONNECT_VI = VIS_DIR / "lvmcp_connect.vi"
SRADD_VI = VIS_DIR / "lvmcp_sradd.vi"
TERMDIAG_VI = VIS_DIR / "lvmcp_termdiag.vi"
FRAMEDIAGS_VI = VIS_DIR / "lvmcp_framediags.vi"
CASESELECTOR_VI = VIS_DIR / "lvmcp_caseselector.vi"
USEDEFAULT_VI = VIS_DIR / "lvmcp_usedefault.vi"
INPUTCNT_VI = VIS_DIR / "lvmcp_inputcnt.vi"
STATICVI_VI = VIS_DIR / "lvmcp_staticvi.vi"
CONCAT_VI = VIS_DIR / "lvmcp_concat.vi"
INDEXMODE_VI = VIS_DIR / "lvmcp_indexmode.vi"
LIBADD_VI = VIS_DIR / "lvmcp_libadd.vi"
LIBSAVE_VI = VIS_DIR / "lvmcp_libsave.vi"
STATICVIINFO_VI = VIS_DIR / "lvmcp_staticviinfo.vi"


def _spec_functions(spec: dict) -> set:
    """The palette function names a vi_create spec uses, at any depth."""
    out = set()
    for f in spec.get("functions", []) or []:
        if isinstance(f, dict) and isinstance(f.get("function"), str):
            out.add(f["function"].strip())
    for s in spec.get("structures", []) or []:
        if isinstance(s, dict):
            out |= _spec_functions(s)
            for c in (s.get("cases") or []) + (s.get("events") or []):
                if isinstance(c, dict):
                    out |= _spec_functions(c)
    return out


def _spec_wired_terms(spec: dict) -> dict:
    """{palette function name: the terminal names wires of the spec give its nodes}, at any depth."""
    fn, ends = {}, []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("function"), str) and isinstance(x.get("name"), str):
                fn[x["name"]] = x["function"].strip()
            for k, v in x.items():
                if k in ("wires", "pass") and isinstance(v, list):
                    for w in v:
                        ends.extend([w.get("from"), w.get("to")] if isinstance(w, dict) else
                                    w if isinstance(w, (list, tuple)) else [])
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(spec)
    out: dict = {}
    for e in ends:
        if isinstance(e, str) and "." in e:
            node, _, term = e.partition(".")
            if node in fn:
                out.setdefault(fn[node], set()).add(term)
    return out


def parse_event_caption(cap: str) -> list[dict]:
    """An event frame's caption as LabVIEW shows it -> its events: '"a", "b": Value Change; Timeout'
    -> [{"control": "a", "event": "Value Change"}, {"control": "b", ...}, {"event": "Timeout"}].
    Dynamic and user events ('<Stop>: User Event') give {"dynamic": "Stop", "event": ...}."""
    out = []
    for part in re.findall(r'(?:"[^"]*"|<[^>]*>|[^;"<])+', cap):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'((?:"[^"]*"\s*,\s*)*"[^"]*")\s*:\s*(.+)$', part)
        if m:
            out += [{"control": c, "event": m.group(2).strip()} for c in re.findall(r'"([^"]*)"', m.group(1))]
            continue
        m = re.match(r"<([^>]*)>\s*:\s*(.+)$", part)
        if m:
            out.append({"dynamic": m.group(1), "event": m.group(2).strip()})
        else:
            out.append({"event": part})
    return out


def _end_text(end) -> str:
    """A ref_wires end for messages."""
    if end[0] == "sr":
        return f"shift register element {end[2]} ({end[3]})"
    return repr(end[2] or end[1])


def _owning_library(vi_path) -> dict:
    """{"library": path} of the .lvclass / .lvlib that lists the VI as a member (read from the library
    files near it), or {}."""
    import xml.etree.ElementTree as ET
    vp = Path(vi_path).resolve()
    d = vp.parent
    for _ in range(4):
        for lib in sorted(list(d.glob("*.lvclass")) + list(d.glob("*.lvlib"))):
            try:
                root = ET.parse(lib).getroot()
            except (ET.ParseError, OSError):
                continue
            for it in root.iter("Item"):
                url = it.get("URL")
                if it.get("Type") == "VI" and url and (lib / url).resolve() == vp:  # (relative to the file)
                    return {"library": str(lib)}
        if d.parent == d:
            break
        d = d.parent
    return {}


SRRIGHT_VI = VIS_DIR / "lvmcp_srright.vi"
BUNNAMES_VI = VIS_DIR / "lvmcp_bunnames.vi"
CONSTTYPE_VI = VIS_DIR / "lvmcp_consttype.vi"
CTLTYPE_VI = VIS_DIR / "lvmcp_ctltype.vi"
CONPANE_VI = VIS_DIR / "lvmcp_conpane.vi"
SUBVIPATH_VI = VIS_DIR / "lvmcp_subvipath.vi"
CASENAMES_VI = VIS_DIR / "lvmcp_casenames.vi"
PNODEINFO_VI = VIS_DIR / "lvmcp_pnodeinfo.vi"
PROPITEMSET_VI = VIS_DIR / "lvmcp_propitemset.vi"
PROPITEMS_VI = VIS_DIR / "lvmcp_propitems.vi"
ITEMTERM_VI = VIS_DIR / "lvmcp_itemterm.vi"
INODEINFO_VI = VIS_DIR / "lvmcp_inodeinfo.vi"
SETCASENAMES_VI = VIS_DIR / "lvmcp_setcasenames.vi"
REMOVEFRAME_VI = VIS_DIR / "lvmcp_removeframe.vi"
SETPOS_VI = VIS_DIR / "lvmcp_setpos.vi"
BOUNDS_VI = VIS_DIR / "lvmcp_bounds.vi"
AUTOERR_VI = VIS_DIR / "lvmcp_autoerr.vi"
RELABEL_VI = VIS_DIR / "lvmcp_relabel.vi"
CONSTVALUE_VI = VIS_DIR / "lvmcp_constvalue.vi"
REP_VI = VIS_DIR / "lvmcp_rep.vi"
CONSTREP_VI = VIS_DIR / "lvmcp_constrep.vi"
# Numeric.Representation values (measured on LabVIEW 2026 by setting each and reading the type back)
_REPRESENTATION = {"ext": 0, "dbl": 1, "sgl": 2, "i32": 3, "i16": 4, "i8": 5, "u32": 6, "u16": 7, "u8": 8,
                   "cxt": 9, "cdb": 10, "csg": 11, "i64": 12, "u64": 13}
_STD_TERMS = {"reference", "reference out", "error in (no error)", "error out"}
# ":<number>" in a terminal name is read by the script parser as a position (and has crashed LabVIEW)
_TERM_UNSCRIPTABLE = re.compile(r":\s*\d")
_ANY_STYLE = 4294967295  # GObject.Replace "Style": -1 as U32, find the object by its palette name
_REMOVE_CORE = Path("vi.lib") / "Utility" / "QuickDropSupport" / "Remove and Rewire Objects_core.vi"
# vi_dump node kinds that vi_edit can bind or remove -> gen kinds
_EDIT_KINDS = {"function": "function", "subvi": "subvi", "constant": "constant",
               "for_loop": "for", "while_loop": "while", "case": "case"}
_VILIB_TREF = Path("vi.lib") / "Utility" / "traverseref.llb"
_GEN_ERRORS = {
    1063: "a wire names a terminal that does not exist on that node. Call function_terminals(name) "
          "for a function's exact terminal names; a subVI's terminals are its control/indicator labels "
          "(see vi_text)",
    1374: "unknown function name or bad attribute in the generated script",
    1055: "invalid object reference",
}


# ---- helpers --------------------------------------------------------------
def _node_error(what: str, pn: dict, code: int) -> str:
    if code == 1311:
        return (f"{what}: LabVIEW has no VI Server class {pn['class']!r}. Use a class name as in "
                "LabVIEW's class browser without spaces, e.g. \"Application\", \"VI\", \"Control\", "
                "\"Boolean\", \"WaveformChart\".")
    if code == 1077:
        names = pn["items"] if pn["kind"] == "pnode" else [pn["method"]]
        return (f"{what}: class {pn['class'] or 'Control'} has no {'property' if pn['kind'] == 'pnode' else 'method'} "
                f"named {names}. lv_class_properties({pn['class'] or 'Control'!r}) lists its properties; "
                "a nested one is written with a dot, as LabVIEW shows it (\"Label.Text\", \"Plot.Plot Color\").")
    return f"configuring {what} failed (error {code})"



def _spec_names(d: dict) -> set[str]:
    """Every node name declared in a block of spec contents, including nested structures."""
    names: set[str] = set()
    if not isinstance(d, dict):
        return names
    for key in ("constants", "functions", "subvis"):
        names |= {x.get("name") for x in d.get(key, []) or [] if isinstance(x, dict)}
    for st in d.get("structures", []) or []:
        if isinstance(st, dict):
            names.add(st.get("name"))
            names |= set(st.get("shift_registers", []) or []) | set(st.get("output_tunnels", []) or [])
            names |= _spec_names(st)
            for fr in st.get("cases", []) or []:
                names |= _spec_names(fr)
    return names


def _byte_strings(items: list[bytes]):
    """A LabVIEW string array whose elements carry raw bytes. Each element is sent as a COM byte
    array (VT_UI1) so it bypasses the code-page conversion applied to text."""
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
                   [VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, b) for b in items])


def _matches(got, want, tol: float) -> bool:
    """Compare a front-panel value with an expected one: numbers within tol (relative to the
    expected size, at least absolute tol), lists/clusters element by element, others exactly."""
    if isinstance(want, bool) or isinstance(got, bool):
        return got is not None and bool(got) == bool(want)
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(got - want) <= tol * max(1.0, abs(want))
    if isinstance(want, (list, tuple)) and isinstance(got, (list, tuple)):
        return len(got) == len(want) and all(_matches(g, w, tol) for g, w in zip(got, want))
    return got == want


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
