#!/usr/bin/env python3
"""files.py — read / write / edit / tree / find for dd (stdlib only).

  read PATH [--start N] [--lines N]                numbered lines (default first 200)
  write PATH (--content TEXT | --from-file F) [--force]   creates parent dirs; refuses overwrite unless --force
  edit PATH --old TEXT --new TEXT                  exact match, must occur exactly once
  tree DIR [--depth N]
  find DIR --name GLOB [--grep REGEX] [--max N]
  --selftest
Protected: anything under $DD_HOME/rules is read-only here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache"}


def rules_dir() -> Path:
    return (Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "rules").resolve()


def guard_write(p: Path) -> str | None:
    rp = p.resolve()
    if str(rp).startswith(str(rules_dir())):
        return "refused: rules directory is read-only for dd (use rules_guard.py as owner)"
    return None


def read(path: str, start: int = 1, lines: int = 200) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = all_lines[start - 1:start - 1 + lines]
    body = "\n".join(f"{start + i:>5}\t{l}" for i, l in enumerate(chunk))
    return {"path": str(p), "total_lines": len(all_lines), "start": start, "shown": len(chunk), "content": body}


def write(path: str, content: str, force: bool = False) -> dict:
    p = Path(path)
    if (g := guard_write(p)):
        return {"error": g}
    if p.exists() and not force:
        return {"error": f"exists: {path} (read it, then use edit, or --force to overwrite)"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(content.encode("utf-8"))}


def edit(path: str, old: str, new: str) -> dict:
    p = Path(path)
    if (g := guard_write(p)):
        return {"error": g}
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        return {"error": f"old text must occur exactly once, found {n}"}
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    line = text[:text.index(old)].count("\n") + 1
    return {"ok": True, "path": str(p), "line": line, "diff": f"@@ line {line}\n- {old}\n+ {new}"}


def tree(root: str, depth: int = 2) -> str:
    rp = Path(root)
    if not rp.is_dir():
        return f"not a dir: {root}"
    out = [str(rp)]

    def walk(d: Path, prefix: str, level: int) -> None:
        if level > depth:
            return
        entries = sorted([e for e in d.iterdir() if e.name not in SKIP_DIRS], key=lambda e: (not e.is_dir(), e.name))
        for i, e in enumerate(entries[:200]):
            last = i == len(entries) - 1
            out.append(f"{prefix}{'└── ' if last else '├── '}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir():
                walk(e, prefix + ("    " if last else "│   "), level + 1)
    walk(rp, "", 1)
    return "\n".join(out)


def find(root: str, name: str = "*", grep: str | None = None, max_hits: int = 100) -> list[dict]:
    hits: list[dict] = []
    pat = re.compile(grep) if grep else None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fnmatch.fnmatch(fn, name):
                continue
            fp = Path(dirpath) / fn
            if pat is None:
                hits.append({"path": str(fp)})
            else:
                try:
                    for ln, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if pat.search(line):
                            hits.append({"path": str(fp), "line": ln, "text": line.strip()[:200]})
                            if len(hits) >= max_hits:
                                return hits
                except OSError:
                    continue
            if len(hits) >= max_hits:
                return hits
    return hits


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dd-files-"))
    os.environ["DD_HOME"] = str(tmp / "home")
    f = tmp / "a" / "b.py"
    assert write(str(f), "x = 1\ny = 2\n")["ok"]
    assert "error" in write(str(f), "z")
    r = read(str(f))
    assert r["total_lines"] == 2 and "    1\tx = 1" in r["content"], r
    e = edit(str(f), "y = 2", "y = 3")
    assert e["ok"] and e["line"] == 2, e
    assert "error" in edit(str(f), "nope", "x")
    assert find(str(tmp), "*.py", "y = 3")[0]["line"] == 2
    assert "b.py" in tree(str(tmp))
    (tmp / "home" / "rules").mkdir(parents=True)
    assert "refused" in write(str(tmp / "home" / "rules" / "RULES.md"), "hack")["error"]
    print("files selftest ok")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    if cmd == "read":
        res = read(argv[1], int(arg(argv, "--start", 1)), int(arg(argv, "--lines", 200)))
    elif cmd == "write":
        content = arg(argv, "--content")
        if content is None and "--from-file" in argv:
            content = Path(arg(argv, "--from-file")).read_text(encoding="utf-8")
        res = write(argv[1], content or "", "--force" in argv)
    elif cmd == "edit":
        res = edit(argv[1], arg(argv, "--old", ""), arg(argv, "--new", ""))
    elif cmd == "tree":
        print(tree(argv[1], int(arg(argv, "--depth", 2))))
        return 0
    elif cmd == "find":
        res = find(argv[1], arg(argv, "--name", "*"), arg(argv, "--grep"), int(arg(argv, "--max", 100)))
    else:
        print(f"unknown command {cmd}")
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
