#!/usr/bin/env python3
"""plan.py — persistent plan/step tracker for dd tasks (stdlib only).

  new "title" --steps "s1" "s2" ...     -> creates $DD_HOME/plans/<slug>.json, prints slug
  start|done|fail|block SLUG N [--note TEXT]
  note SLUG N --note TEXT
  add SLUG --steps "s3" "s4"
  report SLUG                            compact text status
  show SLUG                              full JSON
  list [--open]
  --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

STATUSES = ("pending", "in_progress", "done", "failed", "blocked")


def plans_dir() -> Path:
    d = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "task"
    base, i = s, 1
    while (plans_dir() / f"{s}.json").exists():
        i += 1
        s = f"{base}-{i}"
    return s


def load(slug: str) -> dict:
    p = plans_dir() / f"{slug}.json"
    if not p.exists():
        raise FileNotFoundError(f"no plan '{slug}'")
    return json.loads(p.read_text(encoding="utf-8"))


def save(plan: dict) -> None:
    plan["updated"] = int(time.time())
    (plans_dir() / f"{plan['slug']}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")


def new(title: str, steps: list[str]) -> dict:
    if not steps:
        return {"error": "need at least one step"}
    plan = {"slug": slugify(title), "title": title, "created": int(time.time()), "updated": int(time.time()),
            "steps": [{"n": i + 1, "text": s, "status": "pending", "note": "", "ts": None} for i, s in enumerate(steps)]}
    save(plan)
    return {"ok": True, "slug": plan["slug"], "steps": len(steps)}


def set_status(slug: str, n: int, status: str, note: str | None = None) -> dict:
    plan = load(slug)
    step = next((s for s in plan["steps"] if s["n"] == n), None)
    if not step:
        return {"error": f"no step {n} in {slug}"}
    step["status"] = status
    step["ts"] = int(time.time())
    if note:
        step["note"] = note
    save(plan)
    return {"ok": True, "slug": slug, "n": n, "status": status}


def add_steps(slug: str, steps: list[str]) -> dict:
    plan = load(slug)
    start = len(plan["steps"])
    plan["steps"] += [{"n": start + i + 1, "text": s, "status": "pending", "note": "", "ts": None} for i, s in enumerate(steps)]
    save(plan)
    return {"ok": True, "slug": slug, "steps": len(plan["steps"])}


def is_open(plan: dict) -> bool:
    return any(s["status"] in ("pending", "in_progress", "blocked") for s in plan["steps"])


def report(slug: str) -> str:
    plan = load(slug)
    icons = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "failed": "[!]", "blocked": "[B]"}
    lines = [f"{plan['title']} ({slug}) — {'OPEN' if is_open(plan) else 'COMPLETE'}"]
    for s in plan["steps"]:
        note = f" — {s['note']}" if s["note"] else ""
        lines.append(f"{icons[s['status']]} {s['n']}. {s['text']}{note}")
    counts = {k: sum(1 for s in plan["steps"] if s["status"] == k) for k in STATUSES}
    lines.append(" ".join(f"{k}:{v}" for k, v in counts.items() if v))
    return "\n".join(lines)


def list_plans(open_only: bool = False) -> list[dict]:
    out = []
    for p in sorted(plans_dir().glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        plan = json.loads(p.read_text(encoding="utf-8"))
        if open_only and not is_open(plan):
            continue
        out.append({"slug": plan["slug"], "title": plan["title"], "open": is_open(plan),
                    "done": sum(1 for s in plan["steps"] if s["status"] == "done"), "total": len(plan["steps"])})
    return out


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-plan-")
    r = new("Build dd skills library", ["scaffold loader", "write skills", "verify"])
    slug = r["slug"]
    assert slug == "build-dd-skills-library", slug
    assert set_status(slug, 1, "in_progress")["ok"]
    assert set_status(slug, 1, "done", "loader at skill_loader.py")["ok"]
    assert set_status(slug, 2, "blocked", "needs GPU")["ok"]
    assert add_steps(slug, ["package zip"])["steps"] == 4
    rep = report(slug)
    assert "[x] 1." in rep and "[B] 2." in rep and "OPEN" in rep, rep
    assert list_plans(open_only=True)[0]["slug"] == slug
    assert "error" in set_status(slug, 99, "done")
    assert new("Build dd skills library", ["x"])["slug"] == "build-dd-skills-library-2"
    print("plan selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    note = argv[argv.index("--note") + 1] if "--note" in argv else None
    steps = argv[argv.index("--steps") + 1:] if "--steps" in argv else []
    steps = [s for s in steps if not s.startswith("--")]
    try:
        if cmd == "new":
            res = new(argv[1], steps)
        elif cmd in ("start", "done", "fail", "block"):
            status = {"start": "in_progress", "done": "done", "fail": "failed", "block": "blocked"}[cmd]
            res = set_status(argv[1], int(argv[2]), status, note)
        elif cmd == "note":
            plan = load(argv[1])
            res = set_status(argv[1], int(argv[2]), next(s["status"] for s in plan["steps"] if s["n"] == int(argv[2])), note)
        elif cmd == "add":
            res = add_steps(argv[1], steps)
        elif cmd == "report":
            print(report(argv[1]))
            return 0
        elif cmd == "show":
            res = load(argv[1])
        elif cmd == "list":
            res = list_plans("--open" in argv)
        else:
            print(f"unknown command {cmd}")
            return 2
    except (FileNotFoundError, IndexError, ValueError) as e:
        res = {"error": str(e)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
