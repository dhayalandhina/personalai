#!/usr/bin/env python3
"""rules_guard.py — hash-locked owner rules for dd.

Commands:
  init                      install assets/RULES.default.md into $DD_HOME/rules and lock it
  verify                    exit 0 if RULES.md matches RULES.lock, else exit 1
  show                      print the rules text (for the system prompt)
  update --file NEW.md      owner only: replace rules + re-lock (needs a TTY and typed YES)
  reinit                    owner only: re-lock the current RULES.md after a manual edit
  --selftest                run an isolated end-to-end check
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(os.environ.get("DD_SKILL_DIR", Path(__file__).resolve().parent.parent))
DEFAULT_RULES = SKILL_DIR / "assets" / "RULES.default.md"


def dd_home() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd"))


def rules_dir() -> Path:
    """$DD_RULES_DIR (e.g. root-owned /etc/dd/rules on the cloud server) or $DD_HOME/rules."""
    return Path(os.environ.get("DD_RULES_DIR") or dd_home() / "rules")


def paths() -> tuple[Path, Path]:
    d = rules_dir()
    return d / "RULES.md", d / "RULES.lock"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_lock(rules: Path, lock: Path) -> None:
    lock.write_text(json.dumps({"sha256": sha256(rules), "locked_at": int(time.time()),
                                "file": str(rules)}, indent=1))
    os.chmod(rules, 0o444)
    os.chmod(lock, 0o444)


def cmd_init() -> int:
    rules, lock = paths()
    rules.parent.mkdir(parents=True, exist_ok=True)
    if rules.exists() and lock.exists():
        print(f"already initialised: {rules}")
        return 0
    if not rules.exists():
        shutil.copy(DEFAULT_RULES, rules)
    write_lock(rules, lock)
    print(f"locked {rules} sha256={sha256(rules)[:12]}")
    return 0


def cmd_verify(quiet: bool = False) -> int:
    rules, lock = paths()
    if not rules.exists() or not lock.exists():
        print("RULES_MISSING: run `rules_guard.py init`")
        return 1
    expected = json.loads(lock.read_text()).get("sha256")
    actual = sha256(rules)
    if expected != actual:
        print(f"RULES_TAMPERED: expected {expected[:12]} got {actual[:12]} — owner must run `rules_guard.py reinit`")
        return 1
    if not quiet:
        print(f"RULES_OK sha256={actual[:12]}")
    return 0


def cmd_show() -> int:
    rules, _ = paths()
    if cmd_verify(quiet=True) != 0:
        return 1
    print(rules.read_text(encoding="utf-8"))
    return 0


def owner_gate(action: str) -> bool:
    """Only an interactive human terminal passes. Agent subprocesses run with DD_AGENT=1 and stdin=DEVNULL."""
    if os.environ.get("DD_AGENT"):
        print(f"REFUSED: `{action}` is owner-only; refused inside the agent loop (DD_AGENT set).")
        return False
    if not sys.stdin.isatty():
        print(f"REFUSED: `{action}` is owner-only and needs an interactive terminal (no TTY).")
        return False
    ans = input(f"Type YES to {action} the locked rules: ").strip()
    if ans != "YES":
        print("aborted")
        return False
    return True


def cmd_update(new_file: str) -> int:
    rules, lock = paths()
    src = Path(new_file)
    if not src.is_file():
        print(f"no such file: {src}")
        return 1
    if not owner_gate("replace"):
        return 1
    rules.parent.mkdir(parents=True, exist_ok=True)
    if rules.exists():
        os.chmod(rules, 0o644)
        shutil.copy(rules, rules.with_suffix(f".bak.{int(time.time())}"))
    shutil.copy(src, rules)
    if lock.exists():
        os.chmod(lock, 0o644)
    write_lock(rules, lock)
    print(f"rules replaced and re-locked sha256={sha256(rules)[:12]}")
    return 0


def cmd_reinit() -> int:
    rules, lock = paths()
    if not rules.exists():
        print("no RULES.md to lock — run init")
        return 1
    if not owner_gate("re-lock"):
        return 1
    if lock.exists():
        os.chmod(lock, 0o644)
    write_lock(rules, lock)
    print(f"re-locked sha256={sha256(rules)[:12]}")
    return 0


def selftest() -> int:
    tmp = tempfile.mkdtemp(prefix="dd-rules-")
    os.environ["DD_HOME"] = tmp
    os.environ["DD_AGENT"] = "1"  # selftest must never prompt, even when run from a real terminal
    assert cmd_init() == 0
    assert cmd_verify() == 0
    rules, _ = paths()
    os.chmod(rules, 0o644)
    rules.write_text(rules.read_text() + "\n17. injected rule\n")
    assert cmd_verify() == 1, "tamper must be detected"
    # owner gate must refuse inside the agent (DD_AGENT) and without a TTY — never prompt
    assert cmd_reinit() == 1
    assert cmd_update(str(rules)) == 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("rules_guard selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    if cmd == "init":
        return cmd_init()
    if cmd == "verify":
        return cmd_verify()
    if cmd == "show":
        return cmd_show()
    if cmd == "update":
        if len(argv) < 3 or argv[1] != "--file":
            print("usage: update --file NEW.md")
            return 2
        return cmd_update(argv[2])
    if cmd == "reinit":
        return cmd_reinit()
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
