"""LabVIEW projects (.lvproj) and Application Builder builds.

Projects are created and changed through LabVIEW's ActiveX Project interface (NewProject /
OpenProject, AddFile, BuildSpecifications.AddItem, Save), so LabVIEW itself decides item types and
URLs. A new build specification comes out of AddItem empty; its settings are the Property elements
LabVIEW writes when the build dialog or the Application Builder API creates one, and they are filled
into the saved project file here. Builds run NI's Application Builder API
(vi.lib/AppBuilder/AB_API_Simple/Build (path).vi), the same VI the LabVIEWCLI ExecuteBuildSpec
operation calls.
"""
from __future__ import annotations

import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .lv import LabVIEW, LabVIEWError, _com_msg

_AB_SIMPLE = Path("vi.lib") / "AppBuilder" / "AB_API_Simple"
# spec "type" -> project item type
BUILD_TYPES = {"exe": "EXE", "ppl": "Packed Library", "source": "Source Distribution"}
_TYPE_ALIASES = {"application": "exe", "app": "exe", "packed library": "ppl", "lvlibp": "ppl",
                 "source distribution": "source", "sdist": "source"}
_SYMBOLIC = {"vilib": "vi.lib", "userlib": "user.lib", "instrlib": "instr.lib",
             "resource": "resource", "menus": "menus", "help": "help"}
_SPEC_KEYS = {"name", "type", "startup", "library", "include", "file_name", "output_dir",
              "version", "description"}


def _guid() -> str:
    return "{%s}" % str(uuid.uuid4()).upper()


def _check_project_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise LabVIEWError(f"Project path must be absolute: {path}")
    if p.suffix.lower() != ".lvproj":
        raise LabVIEWError(f"A project file ends in .lvproj: {path}")
    return p


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


# ---- reading a project file ------------------------------------------------
def _resolve_url(url: str, proj: Path, app_dir: str | None) -> str:
    """An Item URL as an absolute path. URLs are relative to the project FILE ("../Main.vi" is next
    to it), or symbolic ("/<vilib>/...") or absolute ("/C/dir/x.vi")."""
    m = re.match(r"^/<(\w+)>(/.*)?$", url)
    if m:
        sub = _SYMBOLIC.get(m.group(1).lower())
        if sub and app_dir:
            return str(Path(app_dir) / sub / (m.group(2) or "/").lstrip("/"))
        return url
    m = re.match(r"^/([A-Za-z])/(.*)$", url)
    if m:
        return str(Path(f"{m.group(1)}:/") / m.group(2))
    if url.startswith("//"):  # UNC
        return "\\\\" + url[2:].replace("/", "\\")
    return os.path.normpath(str(proj) + "/" + url)


def _resolve_build_path(value: str, proj: Path) -> str:
    """A Path property of a build specification. Unlike item URLs these are relative to the project
    FOLDER: the default "../builds/<project>/<spec>" lands next to the project folder."""
    m = re.match(r"^/([A-Za-z])/(.*)$", value)
    if m:
        return str(Path(f"{m.group(1)}:/") / m.group(2))
    return os.path.normpath(str(proj.parent) + "/" + value)


def _build_path_value(path: str, proj: Path) -> str:
    """Absolute output path -> the relative form LabVIEW writes (or /C/... across drives)."""
    try:
        rel = os.path.relpath(path, proj.parent)
        return rel.replace("\\", "/")
    except ValueError:  # another drive
        p = Path(path)
        return "/" + p.drive.rstrip(":") + "/" + "/".join(p.parts[1:])


def _props(item: ET.Element) -> dict[str, str]:
    return {p.get("Name"): (p.text or "") for p in item.findall("Property")}


def read_project(path: str, app_dir: str | None = None) -> dict:
    proj = _check_project_path(path)
    if not proj.is_file():
        raise LabVIEWError(f"File not found: {path}")
    root = ET.parse(proj).getroot()
    items: list[dict] = []
    specs: list[dict] = []
    targets: list[str] = []

    def walk(el: ET.Element, ref: str, target: str) -> None:
        for it in el.findall("Item"):
            name, typ = it.get("Name"), it.get("Type")
            r = f"{ref}/{name}"
            if typ == "Dependencies":
                continue
            if typ == "Build":
                for b in it.findall("Item"):
                    specs.append(_spec_summary(b, target, proj, app_dir))
                continue
            entry = {"name": name, "type": typ, "item": r}
            if it.get("URL"):
                entry["path"] = _resolve_url(it.get("URL"), proj, app_dir)
            items.append(entry)
            if typ in ("Folder", "Library", "LVClass") or it.find("Item") is not None:
                walk(it, r, target)

    for t in root.findall("Item"):  # top-level items are targets ("My Computer", RT targets)
        targets.append(t.get("Name"))
        walk(t, "/" + t.get("Name"), t.get("Name"))
    return {"path": str(proj), "lv_version": root.get("LVVersion"), "targets": targets,
            "items": items, "build_specs": specs}


def _spec_summary(b: ET.Element, target: str, proj: Path, app_dir: str | None) -> dict:
    pr = _props(b)
    out: dict = {"name": b.get("Name"), "type": b.get("Type"), "target": target}
    startup, include = [], []
    for i in range(int(pr.get("SourceCount", "0") or 0)):
        ref = pr.get(f"Source[{i}].itemID", "")
        how = pr.get(f"Source[{i}].sourceInclusion")
        if how == "TopLevel":
            startup.append(ref)
        elif how == "Include":
            include.append(ref)
    if startup:
        out["startup"] = startup
    if include:
        out["include"] = include
    if pr.get("Bld_localDestDir"):
        out["output_dir"] = _resolve_build_path(pr["Bld_localDestDir"], proj)
    if pr.get("Destination[0].path"):
        out["primary_output"] = _resolve_build_path(pr["Destination[0].path"], proj)
    ver = [pr.get(f"Bld_version.{k}") for k in ("major", "minor", "patch", "build")]
    if any(ver):
        out["version"] = ".".join(v or "0" for v in ver)
    if pr.get("Bld_buildSpecDescription"):
        out["description"] = pr["Bld_buildSpecDescription"]
    return out


def _item_refs(proj: Path) -> dict[str, str]:
    """normalized file path -> project item path ("/My Computer/Main.vi"), for build sources."""
    root = ET.parse(proj).getroot()
    out: dict[str, str] = {}

    def walk(el, ref):
        for it in el.findall("Item"):
            r = f"{ref}/{it.get('Name')}"
            if it.get("URL"):
                out.setdefault(_norm(_resolve_url(it.get("URL"), proj, None)), r)
            if it.get("Type") not in ("Build", "Dependencies"):
                walk(it, r)

    walk(root, "")
    return out


# ---- writing ---------------------------------------------------------------
_VI_EXT = {".vi", ".vit", ".vim", ".ctl", ".ctt"}


def _as_list(v) -> list:
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def _check_specs(specs: list) -> list[dict]:
    out = []
    for s in specs or []:
        if not isinstance(s, dict) or not s.get("name"):
            raise LabVIEWError("each build spec needs a \"name\"")
        name = s["name"]
        bad = set(s) - _SPEC_KEYS
        if bad:
            raise LabVIEWError(f"build spec {name!r}: unknown keys {sorted(bad)}; "
                               f"allowed: {sorted(_SPEC_KEYS)}")
        typ = str(s.get("type", "exe")).lower()
        typ = _TYPE_ALIASES.get(typ, typ)
        if typ not in BUILD_TYPES:
            raise LabVIEWError(f"build spec {name!r}: type {typ!r} is not supported; "
                               f"supported: {sorted(BUILD_TYPES)}")
        startup, include = _as_list(s.get("startup")), _as_list(s.get("include"))
        library = s.get("library")
        if typ == "exe" and not startup:
            raise LabVIEWError(f"build spec {name!r}: an application needs \"startup\": "
                               "the VI(s) that open when it runs")
        if typ == "ppl":
            if not library or Path(library).suffix.lower() != ".lvlib":
                raise LabVIEWError(f"build spec {name!r}: a packed library needs \"library\": the "
                                   ".lvlib to pack")
        elif library:
            raise LabVIEWError(f"build spec {name!r}: \"library\" is only for type \"ppl\"")
        if typ != "exe" and startup:
            raise LabVIEWError(f"build spec {name!r}: \"startup\" is only for type \"exe\"; "
                               "list the files in \"include\"")
        if typ == "source" and not include:
            raise LabVIEWError(f"build spec {name!r}: a source distribution needs \"include\"")
        for p in [*startup, *include, *([library] if library else [])]:
            if not Path(p).is_absolute() or not Path(p).is_file():
                raise LabVIEWError(f"build spec {name!r}: not an existing absolute path: {p}")
            if Path(p).suffix.lower() not in _VI_EXT | {".lvlib"}:
                raise LabVIEWError(f"build spec {name!r}: {p}: only VIs, controls and .lvlib "
                                   "libraries can be build sources")
        ver = str(s.get("version", "1.0.0.0"))
        if not re.fullmatch(r"\d+(\.\d+){0,3}", ver):
            raise LabVIEWError(f"build spec {name!r}: version must look like 1.2.3.4")
        file_name = s.get("file_name")
        if typ == "source" and file_name:
            raise LabVIEWError(f"build spec {name!r}: a source distribution has no file_name")
        ext = {"exe": ".exe", "ppl": ".lvlibp"}.get(typ)
        if ext:
            file_name = file_name or f"{name}{ext}"
            if not file_name.lower().endswith(ext):
                file_name += ext
        out.append({**s, "type": typ, "startup": startup, "include": include,
                    "library": library, "version": ver, "file_name": file_name})
    names = [s["name"] for s in out]
    if len(set(names)) != len(names):
        raise LabVIEWError("build spec names must be unique")
    return out


def _spec_properties(s: dict, proj: Path, refs: dict[str, str]) -> list[tuple[str, str, str]]:
    """Property elements of a build spec, as LabVIEW writes them for each type (taken from specs
    that NI's Application Builder API examples create)."""
    typ = s["type"]
    out_dir = s.get("output_dir") or str(proj.parent / "builds" / s["name"])
    if not Path(out_dir).is_absolute():
        raise LabVIEWError(f"build spec {s['name']!r}: output_dir must be absolute")
    dest = _build_path_value(out_dir, proj)
    fname = s["file_name"]
    ver = (s["version"].split(".") + ["0", "0", "0"])[:4]
    p: list[tuple[str, str, str]] = []
    if typ == "exe":
        p += [("App_copyErrors", "Bool", "true"),
              ("App_INI_aliasGUID", "Str", _guid()),
              ("App_INI_GUID", "Str", _guid())]
    p += [("Bld_buildSpecName", "Str", s["name"])]
    if s.get("description"):
        p.append(("Bld_buildSpecDescription", "Str", s["description"]))
    if typ == "source":
        # keep LabVIEW's own libraries out of the distribution, as the build dialog does
        for i, d in enumerate(("vi.lib", "instr.lib", "user.lib", "resource/objmgr")):
            p += [(f"Bld_excludedDirectory[{i}]", "Path", d),
                  (f"Bld_excludedDirectory[{i}].pathType", "Str", "relativeToAppDir")]
        p.append(("Bld_excludedDirectoryCount", "Int", "4"))
    else:
        p += [("Bld_excludeLibraryItems", "Bool", "true"),
              ("Bld_excludePolymorphicVIs", "Bool", "true")]
    p += [("Bld_localDestDir", "Path", dest),
          ("Bld_localDestDirType", "Str", "relativeToProject")]
    if typ != "source":
        p.append(("Bld_modifyLibraryFile", "Bool", "true"))
    p += [("Bld_version.build", "Int", ver[3]),
          ("Bld_version.major", "Int", ver[0]),
          ("Bld_version.minor", "Int", ver[1]),
          ("Bld_version.patch", "Int", ver[2])]
    if typ == "source":
        p += [("Destination[0].destName", "Str", "Destination Directory"),
              ("Destination[0].path", "Path", dest),
              ("Destination[1].destName", "Str", "Support Directory"),
              ("Destination[1].path", "Path", f"{dest}/data")]
    else:
        p += [("Destination[0].destName", "Str", fname),
              ("Destination[0].path", "Path", f"{dest}/{fname}"),
              ("Destination[0].preserveHierarchy", "Bool", "true"),
              ("Destination[0].type", "Str", "App"),
              ("Destination[1].destName", "Str", "Support Directory"),
              ("Destination[1].path", "Path", dest if typ == "ppl" else f"{dest}/data")]
    p.append(("DestinationCount", "Int", "2"))
    if typ == "ppl":
        p.append(("PackedLib_callersAdapt", "Bool", "true"))
    p += [("Source[0].itemID", "Str", _guid()),
          ("Source[0].type", "Str", "Container")]
    sources = [("TopLevel", f) for f in s["startup"]]
    if s["library"]:
        sources.append(("TopLevel", s["library"]))
    sources += [("Include", f) for f in s["include"]]
    for n, (how, f) in enumerate(sources, start=1):
        ref = refs.get(_norm(f))
        if ref is None:
            raise LabVIEWError(f"build spec {s['name']!r}: {f} is not in the project")
        lib = Path(f).suffix.lower() == ".lvlib"
        p += [(f"Source[{n}].destinationIndex", "Int", "0"),
              (f"Source[{n}].itemID", "Ref", ref)]
        if lib and typ == "ppl" and how == "TopLevel":
            p += [(f"Source[{n}].Library.allowMissingMembers", "Bool", "true"),
                  (f"Source[{n}].Library.atomicCopy", "Bool", "true"),
                  (f"Source[{n}].Library.LVLIBPtopLevel", "Bool", "true"),
                  (f"Source[{n}].preventRename", "Bool", "true")]
        p += [(f"Source[{n}].sourceInclusion", "Str", how),
              (f"Source[{n}].type", "Str", "Library" if lib else "VI")]
    p.append(("SourceCount", "Int", str(len(sources) + 1)))
    if typ != "source":
        p += [("TgtF_fileDescription", "Str", s.get("description") or s["name"]),
              ("TgtF_internalName", "Str", s["name"]),
              ("TgtF_productName", "Str", s["name"]),
              ("TgtF_targetfileGUID", "Str", _guid()),
              ("TgtF_targetfileName", "Str", fname)]
    return p


def _fill_specs(proj: Path, specs: list[dict]) -> None:
    """Put the settings into the empty spec items LabVIEW saved (<Item Name=".." Type="EXE"/>)."""
    raw = proj.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    refs = _item_refs(proj)
    for s in specs:
        typ = BUILD_TYPES[s["type"]]
        empty = re.compile(r'([ \t]*)<Item Name=' + re.escape(quoteattr(s["name"])) +
                           r' Type="' + typ + r'"\s*/>')
        m = empty.search(text)
        if not m:
            raise LabVIEWError(f"build spec {s['name']!r} was not found empty in the saved project")
        ind = m.group(1)
        body = "".join(f'{ind}\t<Property Name="{n}" Type="{t}">{escape(v)}</Property>\n'
                       for n, t, v in _spec_properties(s, proj, refs))
        text = (text[:m.start()] + f'{ind}<Item Name={quoteattr(s["name"])} Type="{typ}">\n'
                + body + f"{ind}</Item>" + text[m.end():])
    proj.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.replace("\r\n", "\n")
                     .replace("\n", "\r\n" if b"\r\n" in raw else "\n").encode("utf-8"))


class Projects:
    """Project tools on top of a LabVIEW connection (runs on its COM thread)."""

    def __init__(self, lv: LabVIEW) -> None:
        self.lv = lv

    def _app(self):
        a = self.lv.app
        for m in ("NewProject", "OpenProject"):
            a._FlagAsMethod(m)
        return a

    @staticmethod
    def _flag(o, *names):
        for m in names:
            o._FlagAsMethod(m)
        return o

    def info(self, path: str) -> dict:
        app_dir = None
        try:
            app_dir = self.lv.app.ApplicationDirectory
        except Exception:  # reading the file does not need LabVIEW
            pass
        return read_project(path, app_dir)

    def create(self, path: str, files: list[str], build_specs: list, overwrite: bool) -> dict:
        proj = _check_project_path(path)
        if proj.exists() and not overwrite:
            raise LabVIEWError(f"{proj} exists; pass overwrite=true, or use project_add to change it")
        proj.parent.mkdir(parents=True, exist_ok=True)
        if proj.exists():
            proj.unlink()
        return self._change(proj, None, files, build_specs)

    def add(self, path: str, files: list[str], build_specs: list) -> dict:
        proj = _check_project_path(path)
        if not proj.is_file():
            raise LabVIEWError(f"File not found: {path} (project_create makes a new project)")
        return self._change(proj, proj, files, build_specs)

    def _change(self, proj: Path, existing: Path | None, files: list[str], build_specs: list) -> dict:
        specs = _check_specs(build_specs)
        wanted: list[str] = []
        for f in [*(files or []), *(p for s in specs for p in [*s["startup"], *s["include"],
                                                              *([s["library"]] if s["library"] else [])])]:
            if not Path(f).is_absolute() or not Path(f).exists():
                raise LabVIEWError(f"not an existing absolute path: {f}")
            if Path(f).is_dir():
                raise LabVIEWError(f"{f} is a folder; list the files in it")
            if _norm(f) not in map(_norm, wanted):
                wanted.append(f)
        have = {}
        if existing is not None:
            info = read_project(str(existing))
            have = {_norm(i["path"]) for i in info["items"] if i.get("path")}
            taken = {s["name"] for s in info["build_specs"]}
            dup = [s["name"] for s in specs if s["name"] in taken]
            if dup:
                raise LabVIEWError(f"the project already has build specs named {dup}")
        a = self._app()
        try:
            p = a.OpenProject(str(proj)) if existing else a.NewProject()
        except Exception as e:
            raise LabVIEWError(f"LabVIEW could not open the project: {_com_msg(e)}") from None
        self._flag(p, "Save", "Close")
        added = []
        try:
            mc = self._flag(p.MyComputer, "AddFile")
            for f in wanted:
                if _norm(f) in have:
                    continue
                try:
                    mc.AddFile(os.path.normpath(f))
                except Exception as e:
                    raise LabVIEWError(f"LabVIEW could not add {f}: {_com_msg(e)}") from None
                added.append(f)
            bs = self._flag(mc.BuildSpecifications, "AddItem")
            for s in specs:
                bs.AddItem(s["name"], "", BUILD_TYPES[s["type"]])
            p.Save(os.path.normpath(str(proj)))
        finally:
            try:
                p.Close()
            except Exception:
                pass
        if specs:
            _fill_specs(proj, specs)
        res = read_project(str(proj), self.lv.app.ApplicationDirectory)
        res["added_files"] = added
        return res

    def build(self, path: str, build_spec: str, target: str) -> dict:
        proj = _check_project_path(path)
        if not proj.is_file():
            raise LabVIEWError(f"File not found: {path}")
        names = [s["name"] for s in read_project(str(proj))["build_specs"]]
        if build_spec and build_spec not in names:
            raise LabVIEWError(f"no build spec {build_spec!r} in the project; it has {names}")
        if not names:
            raise LabVIEWError("the project has no build specifications (add one with project_add)")
        vi = self.lv._vi(str(Path(self.lv.app.ApplicationDirectory) / _AB_SIMPLE / "Build (path).vi"))
        vi.SetControlValue("Path to project", str(proj))
        vi.SetControlValue("Name of target (My Computer)", target or "My Computer")
        vi.SetControlValue("Name of build specification (if empty: build all)", build_spec or "")
        vi.SetControlValue("error in (no error)", (False, 0, ""))
        t0 = time.time()
        vi.Run(False)
        status, code, source = vi.GetControlValue("error out")
        res = {"elapsed_s": round(time.time() - t0, 1),
               "generated_files": list(vi.GetControlValue("Generated files") or ())}
        if status or code:
            res["error"] = {"code": code, "source": source}
            msg = re.sub(r"\n\s*\n+", "\n", (source or "").replace("\r", "")).strip()
            raise LabVIEWError(f"build failed (error {code}): {msg}" + _build_hint(code, source))
        res["built"] = True
        return res

    def mass_compile(self, directory: str) -> dict:
        d = Path(directory)
        if not d.is_absolute() or not d.is_dir():
            raise LabVIEWError(f"not an existing absolute folder: {directory}")
        a = self.lv.app
        app_dir = Path(a.ApplicationDirectory)
        if _norm(d).startswith(_norm(app_dir)):
            raise LabVIEWError("refusing to mass compile inside the LabVIEW installation "
                               f"({app_dir}): it would re-save LabVIEW's own files")
        a._FlagAsMethod("MassCompile")
        log = self.lv._scratch("masscompile") / "masscompile.log"
        t0 = time.time()
        a.MassCompile(str(d), str(log), False, 10, False)
        raw = log.read_bytes() if log.exists() else b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("mbcs", errors="replace")
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        # a problem is a file line followed by indented "- reason" lines
        problems: list[dict] = []
        for ln in lines:
            if ln.startswith("####") or ln.strip().startswith("Directory:"):
                continue
            m = re.match(r'\s*###\s*(.+?):\s*".*?"\s*Path="(.*)"', ln)
            if m:  # e.g. ### Bad VI:    "x.vi"	Path="C:\...\x.vi"
                problems.append({"file": m.group(2), "messages": [m.group(1)]})
            elif ln[:1].isspace() and problems:
                problems[-1]["messages"].append(ln.strip().lstrip("- ").strip())
            else:
                problems.append({"file": ln.strip(), "messages": []})
        return {"directory": str(d), "elapsed_s": round(time.time() - t0, 1),
                "problems": problems, "log": "\n".join(lines[-200:])}


def _build_hint(code: int, source: str) -> str:
    s = (source or "").lower()
    if "broken" in s:
        return " - a VI in the build is broken; check it with vi_info / vi_snapshot."
    if code == 1003:
        return " - a VI in the build is broken (error 1003)."
    return ""
