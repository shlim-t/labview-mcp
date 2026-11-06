"""Measurement harness: how much of real LabVIEW code labview-mcp can read and rebuild.

For each NI example VI (shipped with LabVIEW, not user code):
  1. dump     vi_dump the original                        -> can we read it?
  2. convert  turn the dump into a vi_create spec           -> which features have no spec yet?
  3. rebuild  vi_create the spec into a scratch folder,
              vi_dump the copy and compare with the original -> how close is the rebuilt VI?

Results are cached per VI under --out (default: %TEMP%/lvmcp-measure) so a run can be resumed;
a worker process does the LabVIEW work and the parent restarts LabVIEW and the worker when an
example hangs (a dialog, a missing dependency) and records that VI as "hang".

usage:
  python tools/measure.py run [--limit N] [--category Structures ...]
  python tools/measure.py report
Originals are only read (never saved); rebuilt VIs are written under --out/rebuilt only.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

EXAMPLES = Path(r"C:\Program Files\National Instruments\LabVIEW 2026\examples")
# software-only categories (hardware, toolkits and other add-ons are left out)
CATEGORIES = ["Structures", "Arrays", "Numerics", "Strings", "Booleans", "Comparison", "Collections",
              "Mathematics", "Signal Processing", "Waveform", "File IO", "Controls and Indicators",
              "Design Patterns", "Application Control", "Synchronization", "Dialog and User Interface",
              "Data Communication", "Performance", "State Diagram", "Malleable VIs", "UTF-8",
              "Graphics and Sound", "Object-Oriented Programming"]
OUT = Path(tempfile.gettempdir()) / "lvmcp-measure"
HANG_S = 90


def vi_list(categories, limit=None):
    vis = []
    for c in categories:
        vis += sorted((EXAMPLES / c).rglob("*.vi"))
    return vis[:limit] if limit else vis


def key(p: Path) -> str:
    return hashlib.sha1(str(p).lower().encode()).hexdigest()[:16]


# ---- worker ---------------------------------------------------------------------------------------
def worker(out: Path, paths: list[str]) -> None:
    from labview_mcp.lv import LabVIEW
    from labview_mcp import convert
    lv = LabVIEW()
    for p in map(Path, paths):
        f = out / "results" / f"{key(p)}.json"
        if f.exists():
            continue
        (out / "current.txt").write_text(str(p), encoding="utf-8")
        res = {"vi": str(p), "category": p.relative_to(EXAMPLES).parts[0]}
        t0 = time.time()
        try:
            g = lv.vi_dump(str(p))
            res["dump"] = {"ok": True, "source": g["source"], "nodes": len(g["nodes"]),
                           "wires": len(g["wires"])}
            (out / "dumps").mkdir(exist_ok=True)
            (out / "dumps" / f"{key(p)}.json").write_text(json.dumps(g), encoding="utf-8")
        except Exception as e:
            res["dump"] = {"ok": False, "error": str(e)[:300]}
            g = None
        if g is not None:
            try:
                res.update(convert.measure(lv, g, out / "rebuilt" / f"rt_{key(p)}.vi"))
            except Exception as e:  # a converter bug must not stop the run
                res.update(stage="converter error", error=f"{type(e).__name__}: {str(e)[:200]}")
        res["seconds"] = round(time.time() - t0, 1)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(res), encoding="utf-8")
        print(json.dumps({"vi": p.name, "dump": res["dump"]["ok"], "stage": res.get("stage")}), flush=True)


# ---- parent: run the worker, restart LabVIEW when an example hangs ---------------------------------
def kill_labview():
    subprocess.run(["taskkill", "/F", "/IM", "LabVIEW.exe"], capture_output=True)
    time.sleep(3)


def run(args):
    out = Path(args.out)
    (out / "results").mkdir(parents=True, exist_ok=True)
    vis = [str(p) for p in vi_list(args.category or CATEGORIES, args.limit)]
    todo_list = out / "todo.json"
    todo_list.write_text(json.dumps(vis), encoding="utf-8")
    done_before = -1
    while True:
        done = {f.stem for f in (out / "results").glob("*.json")}
        todo = [p for p in vis if key(Path(p)) not in done]
        print(f"{len(vis) - len(todo)}/{len(vis)} done", flush=True)
        if not todo or len(done) == done_before == len(done) and False:
            break
        if not todo:
            break
        w = subprocess.Popen([sys.executable, __file__, "worker", "--out", str(out)],
                             stdout=sys.stdout, stderr=subprocess.STDOUT)
        last, n = time.time(), len(done)
        restart_every = 150  # LabVIEW slows down with hundreds of VIs in memory
        while w.poll() is None:
            time.sleep(2)
            m = len(list((out / "results").glob("*.json")))
            if m != n:
                last, n = time.time(), m
                if m - len(done) >= restart_every:
                    w.kill()
                    kill_labview()
                    break
            elif time.time() - last > HANG_S:
                cur = (out / "current.txt").read_text(encoding="utf-8")
                print(f"HANG: {cur}", flush=True)
                w.kill()
                kill_labview()
                p = Path(cur)
                (out / "results" / f"{key(p)}.json").write_text(json.dumps(
                    {"vi": cur, "category": p.relative_to(EXAMPLES).parts[0], "dump": {"ok": False,
                     "error": "hang (LabVIEW stopped responding, e.g. a dialog)"}}), encoding="utf-8")
                break
        done_before = len(done)


def worker_main(args):
    out = Path(args.out)
    vis = json.loads((out / "todo.json").read_text(encoding="utf-8"))
    worker(out, vis)


# ---- report -----------------------------------------------------------------------------------------
def report(args):
    out = Path(args.out)
    rs = [json.loads(f.read_text(encoding="utf-8")) for f in (out / "results").glob("*.json")]
    n = len(rs)
    if not n:
        print("no results")
        return
    stages = collections.Counter(r.get("stage", "dump failed") if r["dump"]["ok"] else "dump failed"
                                 for r in rs)
    print(f"{n} example VIs")
    dump_ok = sum(r["dump"]["ok"] for r in rs)
    print(f"[1 read]    dump ok: {dump_ok}/{n} ({100 * dump_ok // n}%)")
    order = ["dump failed", "converter error", "unsupported", "create failed", "broken", "different", "same"]
    for s in order:
        if stages.get(s):
            print(f"    {s:14} {stages[s]:4}  ({100 * stages[s] // n}%)")
    gaps = collections.Counter()
    for r in rs:
        for g in r.get("unsupported", []):
            gaps[g] += 1
    print("\n[2 create] features with no spec yet (number of VIs using them):")
    for g, c in gaps.most_common(args.top):
        print(f"    {c:4}  {g}")
    errs = collections.Counter()
    for r in rs:
        if r.get("stage") in ("create failed", "broken"):
            errs[(r.get("error") or "broken")[:110]] += 1
    if errs:
        print("\nrebuild errors:")
        for e, c in errs.most_common(args.top):
            print(f"    {c:4}  {e}")
    feats = collections.Counter(f for r in rs for f in r.get("features", []))
    if feats:
        print("\nfeature use (VIs):", ", ".join(f"{k} {v}" for k, v in feats.most_common(40)))
    cats = collections.defaultdict(collections.Counter)
    for r in rs:
        cats[r["category"]][r.get("stage", "dump failed") if r["dump"]["ok"] else "dump failed"] += 1
    print("\nby category (same / total):")
    for c, cnt in sorted(cats.items(), key=lambda x: -sum(x[1].values())):
        print(f"    {c:32} {cnt['same']:3}/{sum(cnt.values()):3}  " + " ".join(
            f"{k}:{v}" for k, v in cnt.items() if k != "same"))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report", "worker"])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--category", action="append")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args(argv)
    {"run": run, "report": report, "worker": worker_main}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
