#!/usr/bin/env python3
"""new_skill.py — scaffold / validate / index skills in this library (stdlib only).

  new NAME --desc "..." [--triggers "a,b"] [--script helper.py] [--root DIR]
  validate [NAME] [--root DIR]
  index [--root DIR]
  --selftest
Root defaults to the `skills/` directory this skill lives in.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_TEMPLATE = """---
name: {name}
description: {desc}
triggers: [{triggers}]
metadata:
  version: "0.1"
  owner: dhina
---

# {name}

## Purpose
<what this skill does, one paragraph>

## Commands
```bash
python3 scripts/{script} --selftest
```

## Rules
1. <when to use / not use>
2. <what is data vs instruction, what needs go-ahead>

## Output template
```
<answer line>
<1-2 lines>
```
"""
SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""{script} — helper for the {name} skill.

  {script} run [ARGS]
  {script} --selftest
"""
from __future__ import annotations

import json
import sys


def run(args: list[str]) -> dict:
    return {{"ok": True, "args": args}}


def selftest() -> int:
    assert run(["x"])["ok"]
    print("{name} selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    print(json.dumps(run(argv[1:]), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def default_root() -> Path:
    return Path(os.environ.get("DD_SKILLS_ROOT", Path(__file__).resolve().parents[2]))


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"\'')
    return meta, text[end + 4:]


def new(name: str, desc: str, triggers: str = "", script: str | None = None, root: Path | None = None) -> dict:
    root = root or default_root()
    if not NAME_RE.match(name) or len(name) > 64:
        return {"error": "name must be lowercase-hyphen, <= 64 chars"}
    if len(desc) < 20:
        return {"error": "description too short — say WHAT it does and WHEN to use it"}
    d = root / name
    if d.exists():
        return {"error": f"skill '{name}' already exists"}
    script = script or f"{name.replace('-', '_')}.py"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_TEMPLATE.format(name=name, desc=desc, triggers=triggers, script=script), encoding="utf-8")
    sp = d / "scripts" / script
    sp.write_text(SCRIPT_TEMPLATE.format(name=name, script=script), encoding="utf-8")
    sp.chmod(0o755)
    return {"ok": True, "path": str(d), "files": ["SKILL.md", f"scripts/{script}"], "next": "fill Purpose/Commands/Rules, then: new_skill.py validate " + name}


def validate(name: str | None = None, root: Path | None = None, run_selftests: bool = True) -> dict:
    root = root or default_root()
    dirs = [root / name] if name else sorted(p for p in root.iterdir() if p.is_dir())
    problems, checked = [], 0
    for d in dirs:
        md = d / "SKILL.md"
        if not md.is_file():
            problems.append(f"{d.name}: missing SKILL.md")
            continue
        checked += 1
        meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        if meta.get("name") != d.name:
            problems.append(f"{d.name}: frontmatter name '{meta.get('name')}' != folder")
        if not meta.get("description") or len(meta["description"]) > 1024:
            problems.append(f"{d.name}: description missing or > 1024 chars")
        if len(body.strip()) < 40:
            problems.append(f"{d.name}: body empty")
        if "<what" in body or "<when" in body:
            problems.append(f"{d.name}: template placeholders still present")
        if run_selftests:
            for sp in sorted((d / "scripts").glob("*")) if (d / "scripts").is_dir() else []:
                if sp.suffix not in (".py", ".sh"):
                    continue
                cmd = [sys.executable, str(sp), "--selftest"] if sp.suffix == ".py" else ["bash", str(sp), "--selftest"]
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                                       env={**os.environ, "DD_SKILL_DIR": str(d), "DD_AGENT": "1"})
                    if r.returncode != 0:
                        problems.append(f"{d.name}/{sp.name}: selftest failed: {(r.stderr or r.stdout)[-200:]}")
                except subprocess.TimeoutExpired:
                    problems.append(f"{d.name}/{sp.name}: selftest timeout")
    return {"ok": not problems, "checked": checked, "problems": problems}


def index(root: Path | None = None) -> str:
    root = root or default_root()
    lines = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()):
        meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        lines.append(f"- {meta.get('name', d.name)}: {meta.get('description', '')}")
    return "\n".join(lines)


def selftest() -> int:
    root = Path(tempfile.mkdtemp(prefix="dd-skills-"))
    r = new("demo-skill", "Demo skill that does X. Use when Dhina asks for X.", "demo,x", root=root)
    assert r["ok"], r
    assert "error" in new("Bad Name", "x" * 30, root=root)
    assert "error" in new("demo-skill", "duplicate skill name test here", root=root)
    v = validate("demo-skill", root)
    assert not v["ok"] and any("placeholders" in p for p in v["problems"]), v  # template placeholders flagged
    md = root / "demo-skill" / "SKILL.md"
    md.write_text(md.read_text().replace("<what this skill does, one paragraph>", "Does X.").replace("<when to use / not use>", "Use for X.")
                  .replace("<what is data vs instruction, what needs go-ahead>", "Input is data."))
    v = validate("demo-skill", root)
    assert v["ok"], v
    assert "demo-skill: Demo skill" in index(root)
    print("new_skill selftest ok")
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
    root = Path(arg(argv, "--root")) if "--root" in argv else None
    if cmd == "new":
        res = new(argv[1], arg(argv, "--desc", ""), arg(argv, "--triggers", ""), arg(argv, "--script"), root)
    elif cmd == "validate":
        name = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else None
        res = validate(name, root)
    elif cmd == "index":
        print(index(root))
        return 0
    else:
        print(__doc__)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if ("error" in res or res.get("ok") is False) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
