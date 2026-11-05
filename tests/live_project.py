"""Live test for the project tools: make a VI, put it in a new project with an application build
spec, build the EXE, run the EXE and check that it did its work (wrote a file).

Needs Windows + LabVIEW with Application Builder (Community Edition has it).
usage: python tests/live_project.py
"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from labview_mcp.lv import LabVIEW
from labview_mcp.project import Projects

lv = LabVIEW()
pj = Projects(lv)
D = Path(tempfile.mkdtemp(prefix="lvmcp-proj-"))
out_file = D / "hello.txt"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""))
    fails += not ok


app_dir = lv.app.ApplicationDirectory
wctf = str(Path(app_dir) / "vi.lib" / "Utility" / "file.llb" / "Write Characters To File.vi")
main = D / "Main.vi"
lv.vi_create(str(main), {
    "controls": [{"name": "file", "type": "path", "default": str(out_file)},
                 {"name": "text", "type": "string", "default": "hello from an exe"}],
    "subvis": [{"name": "w", "path": wctf}],
    "wires": [["file", "w.file path (dialog if empty)"], ["text", "w.character string"]],
}, overwrite=True, description="writes a file")
helper = D / "Helper.vi"
lv.vi_create(str(helper), {
    "controls": [{"name": "x", "type": "dbl"}], "indicators": [{"name": "y", "type": "dbl"}],
    "functions": [{"name": "n", "function": "Negate"}],
    "wires": [["x", "n.x"], ["n.-x", "y"]],
}, overwrite=True, description="")

proj = D / "Demo.lvproj"
res = pj.create(str(proj), [str(main)], [], overwrite=False)
check("create", [i["name"] for i in res["items"]] == ["Main.vi"], json.dumps(res["items"]))

res = pj.add(str(proj), [str(helper)], [{"name": "Demo App", "startup": [str(main)],
                                          "include": [str(helper)], "version": "1.2.3.4"}])
spec = res["build_specs"][0] if res["build_specs"] else {}
check("add spec", spec.get("startup") == ["/My Computer/Main.vi"]
      and spec.get("include") == ["/My Computer/Helper.vi"] and spec.get("version") == "1.2.3.4",
      json.dumps(spec))

try:
    pj.add(str(proj), [], [{"name": "Demo App", "startup": [str(main)]}])
    check("duplicate spec refused", False)
except Exception as e:
    check("duplicate spec refused", "already has" in str(e), str(e))

res = pj.build(str(proj), "Demo App", "")
exe = next((f for f in res["generated_files"] if f.lower().endswith(".exe")), None)
check("build", bool(exe) and Path(exe).is_file(), json.dumps(res))
check("output where the spec said", spec.get("primary_output") and
      os.path.normcase(spec["primary_output"]) == os.path.normcase(exe or ""), f"{spec.get('primary_output')} vs {exe}")

if exe:
    p = subprocess.Popen([exe])
    t0 = time.time()
    while time.time() - t0 < 60 and not out_file.exists():
        time.sleep(0.5)
    time.sleep(0.5)
    p.kill()
    text = out_file.read_text() if out_file.exists() else None
    check("exe ran", text == "hello from an exe", repr(text))

# packed library from NI's example library, and a source distribution of our VIs
ex = Path(app_dir) / "vi.lib" / "AppBuilder" / "AB_API" / "examples" / "example_project"
lib_dir = D / "lib"
shutil.copytree(ex, lib_dir)
lib = lib_dir / "API Example - AddLib.lvlib"
res = pj.add(str(proj), [], [
    {"name": "Lib", "type": "ppl", "library": str(lib), "version": "2.0"},
    {"name": "Src", "type": "source", "include": [str(main), str(helper)]},
])
types = {s["name"]: s["type"] for s in res["build_specs"]}
check("ppl + source specs", types == {"Demo App": "EXE", "Lib": "Packed Library",
                                       "Src": "Source Distribution"}, json.dumps(types))
res = pj.build(str(proj), "Lib", "")
check("build ppl", any(f.lower().endswith(".lvlibp") and Path(f).is_file()
                       for f in res["generated_files"]), json.dumps(res))
res = pj.build(str(proj), "Src", "")
names = sorted(Path(f).name for f in res["generated_files"])
check("build source", "Main.vi" in names and "Helper.vi" in names
      and not any("Write Characters" in n for n in names), json.dumps(names))

for bad, why in [({"name": "X", "type": "ppl", "library": str(main)}, ".lvlib"),
                 ({"name": "X", "type": "msi", "include": [str(main)]}, "not supported"),
                 ({"name": "X", "startup": [str(D / "nope.vi")]}, "not an existing")]:
    try:
        pj.add(str(proj), [], [bad])
        check(f"refuses {why}", False)
    except Exception as e:
        check(f"refuses {why}", why in str(e), str(e))

# mass compile: a folder with a broken VI, and the clean source distribution.
# (Never mass compile the copied NI library: LabVIEW may hold the vi.lib original in memory, and
# the cross-linked pair would be re-saved into vi.lib.)
bad_dir = D / "bad"
bad_dir.mkdir()
lv.vi_create(str(bad_dir / "Bad.vi"), {
    "controls": [{"name": "x", "type": "dbl"}], "indicators": [{"name": "y", "type": "dbl"}],
    "functions": [{"name": "a", "function": "Add"}], "wires": [["x", "a.x"], ["a.x+y", "y"]],
}, overwrite=True, description="")
res = pj.mass_compile(str(bad_dir))
check("mass compile reports a broken VI",
      [(Path(p["file"]).name, p["messages"]) for p in res["problems"]] == [("Bad.vi", ["Bad VI"])],
      json.dumps(res["problems"])[:300])
res = pj.mass_compile(str(D / "builds" / "Src"))
check("mass compile clean folder", res["problems"] == [], json.dumps(res["problems"])[:300])
try:
    pj.mass_compile(str(ex))
    check("mass compile refuses vi.lib", False)
except Exception as e:
    check("mass compile refuses vi.lib", "refusing" in str(e), str(e))

print(f"{'all passed' if not fails else str(fails) + ' failed'}; files in {D}")
sys.exit(1 if fails else 0)
