#!/usr/bin/env python3
"""memory.py — dd long-term memory on SQLite + FTS5 (stdlib only).

  add --kind K --key KEY --content TEXT [--source stated|observed] [--keep-old]
  search QUERY [--limit N]            ranked full-text search
  recall KEY                          all active facts under a key
  context "user message"              <memory> block for the prompt (profile + top hits)
  profile                             profile + preference facts
  forget --key KEY | --id ID          hard delete
  recent [--days N]                   recently added facts
  export --out FILE.jsonl             backup / training input
  stats
  --selftest
Secrets are refused at write time. DB: $DD_HOME/memory.db
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

KINDS = {"profile", "preference", "project", "person", "topic", "episode", "decision"}
SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|\b\d{8,10}:[A-Za-z0-9_\-]{35}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(password|passwd|api[_\- ]?key|secret|token)\s*[=:]\s*\S{6,})"
    r"|\b(?:\d[ -]?){13,19}\b")


def db_path() -> Path:
    home = Path(os.environ.get("DD_HOME", Path.home() / ".dd"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "memory.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS facts(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'stated', active INTEGER NOT NULL DEFAULT 1,
        created INTEGER NOT NULL, updated INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS facts_key ON facts(key, active);
    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(content, key, kind, content='facts', content_rowid='id');
    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, content, key, kind) VALUES (new.id, new.content, new.key, new.kind); END;
    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, content, key, kind) VALUES('delete', old.id, old.content, old.key, old.kind); END;
    CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, content, key, kind) VALUES('delete', old.id, old.content, old.key, old.kind);
        INSERT INTO facts_fts(rowid, content, key, kind) VALUES (new.id, new.content, new.key, new.kind); END;
    """)
    return con


def row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def add(kind: str, key: str, content: str, source: str = "stated", keep_old: bool = False) -> dict:
    if kind not in KINDS:
        return {"error": f"kind must be one of {sorted(KINDS)}"}
    if SECRET_RE.search(content):
        return {"error": "refused: content looks like a secret/credential/card number"}
    key = key.strip().lower().replace(" ", "-")
    now = int(time.time())
    with connect() as con:
        if not keep_old:
            con.execute("UPDATE facts SET active=0, updated=? WHERE key=? AND kind=? AND active=1 AND content=?",
                        (now, key, kind, content))
        cur = con.execute("INSERT INTO facts(kind,key,content,source,created,updated) VALUES(?,?,?,?,?,?)",
                          (kind, key, content.strip(), source, now, now))
        return {"ok": True, "id": cur.lastrowid, "key": key}


def fts_query(q: str) -> str:
    words = re.findall(r"[A-Za-z0-9]{2,}", q)
    return " OR ".join(f'"{w}"' for w in words) if words else '""'


def search(q: str, limit: int = 8) -> list[dict]:
    with connect() as con:
        rows = con.execute("""SELECT f.*, bm25(facts_fts) AS rank FROM facts_fts
                              JOIN facts f ON f.id = facts_fts.rowid
                              WHERE facts_fts MATCH ? AND f.active=1 ORDER BY rank LIMIT ?""",
                           (fts_query(q), limit)).fetchall()
    return [row(r) for r in rows]


def recall(key: str) -> list[dict]:
    with connect() as con:
        return [row(r) for r in con.execute("SELECT * FROM facts WHERE key=? AND active=1 ORDER BY created",
                                            (key.strip().lower(),))]


def profile() -> list[dict]:
    with connect() as con:
        return [row(r) for r in con.execute(
            "SELECT * FROM facts WHERE kind IN ('profile','preference') AND active=1 ORDER BY kind, created")]


def context(msg: str, limit: int = 8) -> str:
    prof = profile()
    hits = [h for h in search(msg, limit) if h["kind"] not in ("profile", "preference")]
    if not prof and not hits:
        return ""
    out = ["<memory>"]
    for p in prof[:20]:
        out.append(f"- [{p['kind']}] {p['key']}: {p['content']}")
    for h in hits:
        out.append(f"- [{h['kind']}] {h['key']}: {h['content']}")
    out.append("</memory>")
    return "\n".join(out)


def forget(key: str | None = None, fid: int | None = None) -> dict:
    with connect() as con:
        if fid is not None:
            n = con.execute("DELETE FROM facts WHERE id=?", (fid,)).rowcount
        elif key:
            n = con.execute("DELETE FROM facts WHERE key=?", (key.strip().lower(),)).rowcount
        else:
            return {"error": "need --key or --id"}
    return {"ok": True, "deleted": n}


def recent(days: int = 7) -> list[dict]:
    since = int(time.time()) - days * 86400
    with connect() as con:
        return [row(r) for r in con.execute("SELECT * FROM facts WHERE created>=? AND active=1 ORDER BY created DESC",
                                            (since,))]


def export(out: str) -> dict:
    n = 0
    with connect() as con, open(out, "w", encoding="utf-8") as f:
        for r in con.execute("SELECT * FROM facts WHERE active=1 ORDER BY id"):
            f.write(json.dumps(row(r), ensure_ascii=False) + "\n")
            n += 1
    return {"ok": True, "written": n, "file": out}


def stats() -> dict:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0]
        by_kind = {r[0]: r[1] for r in con.execute(
            "SELECT kind, COUNT(*) FROM facts WHERE active=1 GROUP BY kind")}
    return {"active_facts": total, "by_kind": by_kind, "db": str(db_path())}


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-mem-")
    assert add("profile", "name", "Dhina, Coimbatore, freelance full-stack + AI automation")["ok"]
    assert add("preference", "reply-style", "Thanglish, max 3 lines, paste-ready code")["ok"]
    assert add("project", "aguaclan-erp", "manufacturing ERP on Next.js + Supabase")["ok"]
    assert add("episode", "2026-10-15", "asked for dd skills library")["ok"]
    assert "error" in add("topic", "x", "api_key=sk-ant-abcdefghijklmnopqrstuvwxyz1234")
    hits = search("supabase erp")
    assert hits and hits[0]["key"] == "aguaclan-erp", hits
    ctx = context("erp status enna?")
    assert "aguaclan-erp" in ctx and "[profile]" in ctx, ctx
    assert recall("name")[0]["content"].startswith("Dhina")
    assert forget(key="aguaclan-erp")["deleted"] == 1
    assert not search("supabase")
    assert stats()["active_facts"] == 3
    print("memory selftest ok")
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
    if cmd == "add":
        res = add(arg(argv, "--kind", "topic"), arg(argv, "--key", "misc"), arg(argv, "--content", ""),
                  arg(argv, "--source", "stated"), "--keep-old" in argv)
    elif cmd == "search":
        res = search(argv[1], int(arg(argv, "--limit", 8)))
    elif cmd == "recall":
        res = recall(argv[1])
    elif cmd == "context":
        print(context(argv[1] if len(argv) > 1 else ""))
        return 0
    elif cmd == "profile":
        res = profile()
    elif cmd == "forget":
        fid = arg(argv, "--id")
        res = forget(arg(argv, "--key"), int(fid) if fid else None)
    elif cmd == "recent":
        res = recent(int(arg(argv, "--days", 7)))
    elif cmd == "export":
        res = export(arg(argv, "--out", "memory.jsonl"))
    elif cmd == "stats":
        res = stats()
    else:
        print(f"unknown command {cmd}")
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
