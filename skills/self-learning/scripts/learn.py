#!/usr/bin/env python3
"""learn.py — dd self-learning loop (stdlib only).

  log --user U --assistant A [--skill S]
  feedback (--id N | --last) --rating good|bad [--correction TEXT]
  extract [--apply] [--days N]
  export --format sft|dpo --out FILE [--days N]
  stats
  daily
  --selftest
Data: $DD_HOME/learning/interactions.jsonl   Rules (system msg for exports): $DD_HOME/rules/RULES.md
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|\b\d{8,10}:[A-Za-z0-9_\-]{35}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?i:((?:password|passwd|api[_\- ]?key|secret|token)\s*[=:]\s*)\S{6,})")
PREF_RE = re.compile(
    r"(?i)\b(always|never|from now on|ini(?:mel)?|eppovum|eppavum|prefer|don'?t ever|stop doing|"
    r"pannathe|pannadha|vendam|mattum|only)\b")
TRACK_A_RE = re.compile(r"(?i)\b(" + "|".join(re.escape(p) for p in os.environ.get("DD_TRACK_A_PATTERNS", "codeigniter").split(",") if p) + r")\b")


def home() -> Path:
    h = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "learning"
    h.mkdir(parents=True, exist_ok=True)
    return h


def log_path() -> Path:
    return home() / "interactions.jsonl"


def redact(t: str) -> str:
    def _r(m: re.Match) -> str:
        return (m.group(1) + "***REDACTED***") if m.lastindex else "***REDACTED***"
    return SECRET_RE.sub(_r, t or "")


def read_all() -> list[dict]:
    p = log_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_all(items: list[dict]) -> None:
    log_path().write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")


def cmd_log(user: str, assistant: str, skill: str | None = None) -> dict:
    items = read_all()
    rec = {"id": (items[-1]["id"] + 1) if items else 1, "ts": int(time.time()),
           "user": redact(user), "assistant": redact(assistant), "skill": skill,
           "rating": None, "correction": None}
    with log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "id": rec["id"]}


def cmd_feedback(fid: int | None, last: bool, rating: str, correction: str | None) -> dict:
    if rating not in ("good", "bad"):
        return {"error": "rating must be good|bad"}
    items = read_all()
    if not items:
        return {"error": "no interactions logged"}
    target = items[-1] if last else next((i for i in items if i["id"] == fid), None)
    if not target:
        return {"error": f"id {fid} not found"}
    target["rating"] = rating
    if correction:
        target["correction"] = redact(correction)
    write_all(items)
    return {"ok": True, "id": target["id"], "rating": rating, "corrected": bool(correction)}


def cmd_extract(days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    props: dict[str, dict] = {}
    for it in read_all():
        if it["ts"] < since:
            continue
        texts = [("user", it["user"])]
        if it.get("correction"):
            texts.append(("correction", it["correction"]))
        for src, t in texts:
            for sent in re.split(r"(?<=[.!?\n])\s+", t or ""):
                s = sent.strip()
                if 12 <= len(s) <= 240 and PREF_RE.search(s):
                    key = re.sub(r"[^a-z0-9]+", "-", s.lower())[:40].strip("-")
                    props.setdefault(key, {"key": key, "content": s, "evidence": f"#{it['id']} ({src})"})
    return list(props.values())


def apply_prefs(props: list[dict]) -> int:
    mem = Path(os.environ.get("DD_SKILLS_ROOT", Path(__file__).resolve().parents[2])) / "memory" / "scripts" / "memory.py"
    n = 0
    for p in props:
        if not mem.exists():
            break
        r = subprocess.run([sys.executable, str(mem), "add", "--kind", "preference", "--key", p["key"],
                            "--content", p["content"]], capture_output=True, text=True)
        n += int(r.returncode == 0)
    return n


def rules_text() -> str:
    p = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "rules" / "RULES.md"
    return p.read_text(encoding="utf-8") if p.exists() else "You are dd, Dhina's personal assistant. Reply in Thanglish."


def cmd_export(fmt: str, out: str, days: int = 365) -> dict:
    since = int(time.time()) - days * 86400
    system = rules_text()
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for it in read_all():
            if it["ts"] < since or TRACK_A_RE.search((it["user"] or "") + (it["assistant"] or "")):
                continue
            if fmt == "sft":
                target = it.get("correction") or (it["assistant"] if it.get("rating") == "good" else None)
                if not target:
                    continue
                f.write(json.dumps({"messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": it["user"]},
                                                 {"role": "assistant", "content": target}]}, ensure_ascii=False) + "\n")
                n += 1
            elif fmt == "dpo":
                if it.get("correction") and it.get("assistant") and it["correction"] != it["assistant"]:
                    f.write(json.dumps({"prompt": it["user"], "chosen": it["correction"],
                                        "rejected": it["assistant"]}, ensure_ascii=False) + "\n")
                    n += 1
            else:
                return {"error": "format must be sft|dpo"}
    return {"ok": True, "format": fmt, "examples": n, "file": out}


def cmd_stats() -> dict:
    items = read_all()
    return {"interactions": len(items),
            "good": sum(1 for i in items if i.get("rating") == "good"),
            "bad": sum(1 for i in items if i.get("rating") == "bad"),
            "corrected": sum(1 for i in items if i.get("correction")),
            "trainable_sft": sum(1 for i in items if i.get("correction") or i.get("rating") == "good"),
            "file": str(log_path())}


def cmd_daily() -> dict:
    props = cmd_extract(days=2)
    applied = apply_prefs(props)
    h = home()
    sft = cmd_export("sft", str(h / "sft.jsonl"))
    dpo = cmd_export("dpo", str(h / "dpo.jsonl"))
    return {"preferences_found": len(props), "preferences_applied": applied, "sft": sft, "dpo": dpo, "stats": cmd_stats()}


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-learn-")
    assert cmd_log("ollama slow, my token=abcdef123456 fix pannu", "Q4 quant use pannu", "local-models")["id"] == 1
    assert "***REDACTED***" in read_all()[0]["user"]
    cmd_log("from now on always give full file not diff", "ok")
    cmd_log("codeigniter model fix pannu", "done")
    r = cmd_feedback(1, False, "bad", "Use q4_K_M and lower num_ctx to 4096")
    assert r["ok"] and r["corrected"]
    cmd_feedback(None, True, "good", None)
    props = cmd_extract()
    assert any("always" in p["content"].lower() for p in props), props
    out = str(Path(os.environ["DD_HOME"]) / "sft.jsonl")
    e = cmd_export("sft", out)
    assert e["examples"] == 1, e  # corrected #1 only; #3 is Track A and excluded, #2 unrated
    d = cmd_export("dpo", out)
    assert d["examples"] == 1, d
    s = cmd_stats()
    assert s["interactions"] == 3 and s["corrected"] == 1
    print("learn selftest ok")
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
    if cmd == "log":
        res = cmd_log(arg(argv, "--user", ""), arg(argv, "--assistant", ""), arg(argv, "--skill"))
    elif cmd == "feedback":
        fid = arg(argv, "--id")
        res = cmd_feedback(int(fid) if fid else None, "--last" in argv, arg(argv, "--rating", ""), arg(argv, "--correction"))
    elif cmd == "extract":
        props = cmd_extract(int(arg(argv, "--days", 30)))
        res = {"proposed": props, "applied": apply_prefs(props) if "--apply" in argv else 0}
    elif cmd == "export":
        res = cmd_export(arg(argv, "--format", "sft"), arg(argv, "--out", "train.jsonl"), int(arg(argv, "--days", 365)))
    elif cmd == "stats":
        res = cmd_stats()
    elif cmd == "daily":
        res = cmd_daily()
    else:
        print(f"unknown command {cmd}")
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
