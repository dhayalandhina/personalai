#!/usr/bin/env python3
"""
test_skills.py — validates every skill folder and runs every script with --selftest.
Exit code 0 == library is healthy. Run after adding or editing any skill.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from skill_loader import SkillLibrary, validate_skill  # noqa: E402


def main() -> int:
    lib = SkillLibrary(ROOT / "skills")
    failures: list[str] = []
    print(f"skills found: {len(lib.skills)}")
    for s in lib.skills.values():
        for p in validate_skill(s.path):
            failures.append(p)
        for script in s.scripts:
            sp = s.path / "scripts" / script
            if sp.suffix not in (".py", ".sh"):
                continue
            cmd = [sys.executable, str(sp), "--selftest"] if sp.suffix == ".py" else ["bash", str(sp), "--selftest"]
            env = {**os.environ, "DD_SKILL_DIR": str(s.path), "DD_HOME": str(ROOT / ".dd-test"), "DD_AGENT": "1"}
            try:
                # stdin=DEVNULL: never inherit the terminal, so no script can block on input()
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                failures.append(f"{s.name}/{script}: selftest timeout")
                continue
            status = "ok " if r.returncode == 0 else "FAIL"
            tail = (r.stdout.strip().splitlines() or [""])[-1][:100]
            print(f"  [{status}] {s.name}/{script}  {tail}")
            if r.returncode != 0:
                failures.append(f"{s.name}/{script}: exit {r.returncode}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    # loader round trip
    idx = lib.index_prompt()
    assert "<available_skills>" in idx
    for s in lib.skills.values():
        body = lib.load(s.name)
        assert body.startswith(f'<skill name="{s.name}"'), s.name
    if failures:
        print("\nFAILURES:")
        print("\n".join(failures))
        return 1
    print(f"\nALL GOOD: {len(lib.skills)} skills valid, all selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
