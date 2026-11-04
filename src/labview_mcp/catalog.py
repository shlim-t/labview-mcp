"""Catalog of LabVIEW palette functions and their terminal labels.

Read at runtime from the user's own LabVIEW install (resource/lvstring.rsc holds the Context Help
string lists: a function name followed by its terminal labels). Nothing from NI is redistributed.
The labels are Context Help text, so `function_terminals` verifies them against LabVIEW's
scripting before an agent relies on them.
"""
from __future__ import annotations

import re
import struct
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4)
def load(app_dir: str) -> dict[str, list[str]]:
    p = Path(app_dir) / "resource" / "lvstring.rsc"
    try:
        d = p.read_bytes()
    except OSError:
        return {}
    recs: dict[str, list[str]] = {}
    i, n_bytes = 0, len(d)
    while i < n_bytes - 6:
        # Each string list: u32 byte length, u16 count, then `count` Pascal strings.
        size = struct.unpack_from(">I", d, i)[0]
        count = struct.unpack_from(">H", d, i + 4)[0]
        if 4 < size < 4000 and 2 <= count <= 80 and i + 4 + size <= n_bytes:
            j, end, strs = i + 6, i + 4 + size, []
            for _ in range(count):
                if j >= end:
                    break
                ln = d[j]
                s = d[j + 1:j + 1 + ln]
                if len(s) != ln or any(c < 32 or c > 126 for c in s):
                    break
                strs.append(s.decode("ascii"))
                j += 1 + ln
            if (len(strs) == count and end - j < 4 and _looks_like_function(strs[0])
                    and all(len(x) <= 60 for x in strs[1:])):
                recs.setdefault(strs[0], [s for s in strs[1:] if s])
                i = end
                continue
        i += 1
    return recs


def _looks_like_function(name: str) -> bool:
    """Skip the UI message string lists that share the resource file."""
    return (bool(name) and name[0].isupper() and len(name) <= 48 and "%" not in name
            and not name.endswith((".", ":", "!")) and name.count(" ") <= 6)


def search(app_dir: str, query: str, limit: int = 40) -> list[str]:
    recs = load(app_dir)
    q = query.lower().strip()
    words = q.split()
    hits = [n for n in recs if all(w in n.lower() for w in words)]
    hits.sort(key=lambda n: (n.lower() != q, not n.lower().startswith(q), len(n)))
    return hits[:limit]


_SKIP = re.compile(r"^(\.\.\.|element|empty \d+)$")
_SERIES = re.compile(r"^(.*?) (n-1|n|\d+)(\s*\([^()]*\))?$")


def is_growable(labels: list[str]) -> bool:
    return any(l == "..." or " n-1" in l or re.search(r" n( \(|$)", l) for l in labels)


def candidates(labels: list[str], size: int | None = None) -> list[str]:
    """Names worth probing for each help label: as written, without a trailing "(default)", and
    capitalised. Numbered series ("string 0", "input n (0)") are expanded up to `size`.
    A bare "element" is never probed: on a growable node it has crashed LabVIEW."""
    out: list[str] = []

    def add(c):
        if c and ":" not in c and c not in out and not _SKIP.match(c):
            out.append(c)

    for lab in labels:
        if lab == "...":
            continue
        m = _SERIES.match(lab)
        if m and (m.group(2) in ("n", "n-1") or size):
            for i in range((size or 1) + 1):
                add(f"{m.group(1)} {i}")
            continue
        base = re.sub(r"\s*\([^()]*\)\s*$", "", lab).strip()
        for c in (lab, base, base[:1].upper() + base[1:]):
            add(c)
    if size:
        for i in range(size + 1):
            add(f"element[{i}]")
    return out
