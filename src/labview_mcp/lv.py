"""Thin wrapper around the LabVIEW ActiveX server.

All COM calls run on one dedicated thread (COM objects are apartment-bound),
so callers use `LabVIEW.call(fn, *args)` which returns a concurrent Future.
"""
from __future__ import annotations

import datetime as _dt
import json
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
            raise LabVIEWError("VI is broken (has errors) and cannot run. Open it in LabVIEW to see the error list.")
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
            self._finish_nodes(target, script, out_refs, [], labels,
                               lambda s, refs: self._gen_script(target, s.xml, s.type_strings, s.data_strings,
                                                                refs=refs, raw=True))
            if script.events:
                self._build_events(target, script.events, dict(zip(script.outs, out_refs)), labels)
            if script.styles:
                self._apply_styles(target, script.styles)
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
            what = f"{'property' if pn['kind'] == 'pnode' else 'invoke'} node {pn['name']!r}"
            if pn["control"]:
                fp = fp if fp is not None else self._fp_refs_by_label(target)
                label = labels.get(pn["control"], pn["control"])
                if label not in fp:
                    raise LabVIEWError(f"{what}: no front-panel object {label!r}")
                # the control's own class, so its own properties (e.g. a chart's) are available
                pn = dict(pn, **{"class": self._control_class(target, fp[label])})
            cls = "VI Server:" + pn["class"]
            if pn["kind"] == "pnode":
                h = self._helper(PROPCONFIG_VI)
                h.SetControlValue("reference", ref)
                h.SetControlValue("Property Node Class Name", cls)
                h.SetControlValue("ID Strings[]", list(pn["items"]))
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
            if pn["write"]:
                items = sorted(self._owned(target, "PropertyItem", uid))
                w = self._helper(PROPWRITE_VI)
                for k in pn["write"]:
                    w.SetControlValue("item", items[k][1])
                    w.SetControlValue("write", True)
                    self._run_checked(w, f"making {pn['items'][k]!r} of {what} writable")
            # A terminal is named by LabVIEW, not always as the property was asked for (the item
            # "AllProps[]" has the terminal "AllProps2"); item terminals follow the item order.
            terms = [n for _, n in sorted(self._owned(target, "Terminal", uid, names=True))]
            node_terms[pn["name"]] = terms
            item_terms = [n for n in terms if n not in _STD_TERMS]
            if pn["kind"] == "pnode" and len(item_terms) == len(pn["items"]):
                for asked, real in zip(pn["items"], item_terms):
                    if asked != real:
                        rename[f"\"{pn['name']}.{asked}\""] = f"\"{pn['name']}.{real}\""
        if script.late is not None:
            refs = [outs[k] if src == "out" else ins[k] for src, k in script.late_refs]
            late = script.late
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

    def _build_events(self, target, events: list, outs: dict, labels: dict) -> None:
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
                if fr["control"] is not None:
                    reg.SetControlValue("Static App/VI Events", VARIANT(arr, []))
                    # cluster order: Event Type, Reference, Limit (-1 = none), Lock Panel?
                    reg.SetControlValue("Static Control Events", VARIANT(
                        arr, [VARIANT(arr, [fr["event"], fp[fr["control"]], -1, False])]))
                else:  # an event of this VI: Event Type, Limit, Lock Panel? (no reference)
                    reg.SetControlValue("Static Control Events", VARIANT(arr, []))
                    reg.SetControlValue("Static App/VI Events", VARIANT(
                        arr, [VARIANT(arr, [fr["event"], -1, False])]))
                self._run_checked(reg, f"registering the event of frame {i} of {ev['name']!r}")
        _try(lambda: reg.SetControlValue("Static Control Events", VARIANT(arr, [])))
        _try(lambda: reg.SetControlValue("Static App/VI Events", VARIANT(arr, [])))
        for ev in events:
            if ev.get("timeout") is not None:
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
                    self._finish_nodes(target, fr["script"], list(self._gen_out), passes[-1][3], labels,
                                       lambda s, refs, f=on_frame: f(s.xml, s.type_strings, s.data_strings, refs))

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
        if script.xml.strip() == "<Script>\n</Script>":  # only removals
            status, code, source = False, 0, ""
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
            if broken and not allow_broken:
                shutil.copy2(pristine, path)
                _try(lambda: target.Revert())
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
                "removed": removed, "added_front_panel": script.labels,
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
                return {"vi": str(path), "source": "dfir", **g}
            except LabVIEWError as e:
                if method == "dfir" or "password-protected" in str(e):
                    raise
        return {"vi": str(path), "source": "scan",
                "note": "read by walking the diagram objects: constant values are not included, and "
                        "case frames are numbered in creation order",
                **self._scan(path)}

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
        g = scan.build(rows, labels)
        _try(lambda: self._scan_types(target, g, dict(zip((r.uid for r in rows), cols[6]))))
        return g

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
        ct, ft = self._helper(CONSTTYPE_VI), self._helper(CTLTYPE_VI)
        fp = None
        for n in g["nodes"]:
            if n["kind"] == "constant" and n.get("uid") in refs:
                h, ref = ct, refs[n["uid"]]
            elif n["kind"] in ("control", "indicator") and n.get("label"):
                fp = fp if fp is not None else self._fp_refs_by_label(target)
                h, ref = ft, fp.get(n["label"])
            else:
                continue
            if ref is None:
                continue
            h.SetControlValue("reference", ref)
            h.Run(False)
            if h.GetControlValue("error out")[0]:
                continue
            t = _try(lambda: td.from_flattened(h.GetControlValue("type string")))
            if t is not None:
                n["type"] = t


# Helper VIs; LVMCP_VIS_DIR points at another copy (used to test copies saved for older LabVIEW).
VIS_DIR = Path(os.environ.get("LVMCP_VIS_DIR") or Path(__file__).parent / "vis")
BOOTSTRAP_VI = VIS_DIR / "lvmcp_gen_script.vi"
BLANK_VI = VIS_DIR / "lvmcp_blank.vi"
DFIR_VI = VIS_DIR / "lvmcp_dfir.vi"
RAW_GEN_VI = VIS_DIR / "lvmcp_gen_script_raw.vi"
UIDS_VI = VIS_DIR / "lvmcp_uids.vi"
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
CONSTTYPE_VI = VIS_DIR / "lvmcp_consttype.vi"
CTLTYPE_VI = VIS_DIR / "lvmcp_ctltype.vi"
CONPANE_VI = VIS_DIR / "lvmcp_conpane.vi"
SUBVIPATH_VI = VIS_DIR / "lvmcp_subvipath.vi"
CASENAMES_VI = VIS_DIR / "lvmcp_casenames.vi"
SETCASENAMES_VI = VIS_DIR / "lvmcp_setcasenames.vi"
REMOVEFRAME_VI = VIS_DIR / "lvmcp_removeframe.vi"
SETPOS_VI = VIS_DIR / "lvmcp_setpos.vi"
RELABEL_VI = VIS_DIR / "lvmcp_relabel.vi"
CONSTVALUE_VI = VIS_DIR / "lvmcp_constvalue.vi"
REP_VI = VIS_DIR / "lvmcp_rep.vi"
CONSTREP_VI = VIS_DIR / "lvmcp_constrep.vi"
# Numeric.Representation values (measured on LabVIEW 2026 by setting each and reading the type back)
_REPRESENTATION = {"ext": 0, "dbl": 1, "sgl": 2, "i32": 3, "i16": 4, "i8": 5, "u32": 6, "u16": 7, "u8": 8,
                   "cxt": 9, "cdb": 10, "csg": 11, "i64": 12, "u64": 13}
_STD_TERMS = {"reference", "reference out", "error in (no error)", "error out"}
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
                "sub-properties such as \"Label.Text\" are not listed but may work by their short name.")
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
