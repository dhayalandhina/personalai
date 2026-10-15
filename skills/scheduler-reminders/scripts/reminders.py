#!/usr/bin/env python3
"""reminders.py — SQLite reminder/scheduler queue for dd (stdlib only; IST by default).

  add "text" (--at WHEN | --in DUR | --cron "m h dom mon dow") [--action SKILL]
  list [--all]
  due                      items due now -> JSON; one-offs marked done, cron items rescheduled
  done ID | delete ID | snooze ID --in DUR | --prune
  --selftest
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("DD_TZ", "Asia/Kolkata"))
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def db() -> sqlite3.Connection:
    home = Path(os.environ.get("DD_HOME", Path.home() / ".dd"))
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "reminders.db")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, text TEXT NOT NULL, due INTEGER NOT NULL,
                   cron TEXT, action TEXT, done INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, fired INTEGER)""")
    return con


def now() -> datetime:
    return datetime.now(TZ)


def parse_duration(s: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)\s*", s.lower())
    if not m:
        raise ValueError(f"bad duration '{s}' (use 45m, 2h, 3d, 1w)")
    n, u = int(m.group(1)), m.group(2)[0]
    unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[u]
    return timedelta(**{unit: n})


def parse_time_of_day(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_when(s: str, base: datetime | None = None) -> datetime:
    base = base or now()
    s = s.strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = (int(m.group(4)), int(m.group(5))) if m.group(4) else (9, 0)
        return datetime(y, mo, d, h, mi, tzinfo=TZ)
    parts = s.split()
    day = base.date()
    tod = (9, 0)
    i = 0
    if parts and parts[0] == "today":
        i = 1
    elif parts and parts[0] == "tomorrow":
        day = day + timedelta(days=1)
        i = 1
    elif parts and parts[0][:3] in DAYS:
        target = DAYS[parts[0][:3]]
        delta = (target - base.weekday()) % 7 or 7
        day = day + timedelta(days=delta)
        i = 1
    elif len(parts) >= 2 and parts[0].isdigit() and parts[1][:3] in MONTHS:
        day = base.date().replace(month=MONTHS[parts[1][:3]], day=int(parts[0]))
        if day < base.date():
            day = day.replace(year=day.year + 1)
        i = 2
    rest = " ".join(parts[i:])
    if rest:
        t = parse_time_of_day(rest)
        if t is None:
            raise ValueError(f"cannot parse time '{rest}'")
        tod = t
    dt = datetime(day.year, day.month, day.day, tod[0], tod[1], tzinfo=TZ)
    if dt <= base and i == 0:  # bare time already passed today -> tomorrow
        dt += timedelta(days=1)
    return dt


def cron_match(field: str, value: int) -> bool:
    for part in field.split(","):
        if part == "*":
            return True
        m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
        if not m:
            raise ValueError(f"bad cron field {field}")
        lo = 0 if m.group(1) == "*" else int(m.group(1))
        hi = 59 if m.group(1) == "*" else (int(m.group(2)) if m.group(2) else lo)
        step = int(m.group(3) or 1)
        if m.group(1) == "*":
            hi = 10 ** 6
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def next_cron(expr: str, after: datetime) -> datetime:
    f = expr.split()
    if len(f) != 5:
        raise ValueError("cron needs 5 fields: m h dom mon dow")
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366):
        if (cron_match(f[0], t.minute) and cron_match(f[1], t.hour) and cron_match(f[2], t.day)
                and cron_match(f[3], t.month) and cron_match(f[4], (t.weekday() + 1) % 7)):
            return t
        t += timedelta(minutes=1)
    raise ValueError("no cron match within a year")


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d-%b %H:%M")


def add(text: str, at: str | None = None, in_: str | None = None, cron: str | None = None, action: str | None = None) -> dict:
    if cron:
        due = next_cron(cron, now())
    elif in_:
        due = now() + parse_duration(in_)
    elif at:
        due = parse_when(at)
    else:
        return {"error": "need --at, --in or --cron"}
    with db() as con:
        cur = con.execute("INSERT INTO reminders(text,due,cron,action,created) VALUES(?,?,?,?,?)",
                          (text, int(due.timestamp()), cron, action, int(time.time())))
    return {"ok": True, "id": cur.lastrowid, "text": text, "due": fmt(int(due.timestamp())), "cron": cron, "action": action}


def listing(all_: bool = False) -> list[dict]:
    with db() as con:
        q = "SELECT * FROM reminders" + ("" if all_ else " WHERE done=0") + " ORDER BY due"
        return [{"id": r["id"], "text": r["text"], "due": fmt(r["due"]), "cron": r["cron"], "action": r["action"], "done": r["done"]}
                for r in con.execute(q)]


def due() -> list[dict]:
    ts = int(time.time())
    out = []
    with db() as con:
        rows = con.execute("SELECT * FROM reminders WHERE done=0 AND due<=?", (ts,)).fetchall()
        for r in rows:
            out.append({"id": r["id"], "text": r["text"], "action": r["action"], "due": fmt(r["due"])})
            if r["cron"]:
                con.execute("UPDATE reminders SET due=?, fired=? WHERE id=?", (int(next_cron(r["cron"], now()).timestamp()), ts, r["id"]))
            else:
                con.execute("UPDATE reminders SET done=1, fired=? WHERE id=?", (ts, r["id"]))
    return out


def mark(rid: int, done: bool = True) -> dict:
    with db() as con:
        n = con.execute("UPDATE reminders SET done=? WHERE id=?", (int(done), rid)).rowcount
    return {"ok": True, "id": rid, "updated": n}


def delete(rid: int) -> dict:
    with db() as con:
        n = con.execute("DELETE FROM reminders WHERE id=?", (rid,)).rowcount
    return {"ok": True, "id": rid, "deleted": n}


def snooze(rid: int, in_: str) -> dict:
    due_ts = int((now() + parse_duration(in_)).timestamp())
    with db() as con:
        n = con.execute("UPDATE reminders SET due=?, done=0 WHERE id=?", (due_ts, rid)).rowcount
    return {"ok": True, "id": rid, "due": fmt(due_ts), "updated": n}


def prune(days: int = 30) -> dict:
    with db() as con:
        n = con.execute("DELETE FROM reminders WHERE done=1 AND fired<?", (int(time.time()) - days * 86400,)).rowcount
    return {"ok": True, "pruned": n}


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-rem-")
    base = datetime(2026, 10, 15, 17, 0, tzinfo=TZ)  # Thu
    assert parse_when("tomorrow 9am", base) == datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
    assert parse_when("mon 10am", base) == datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
    assert parse_when("18:30", base) == datetime(2026, 10, 15, 18, 30, tzinfo=TZ)
    assert parse_when("9am", base) == datetime(2026, 10, 16, 9, 0, tzinfo=TZ)  # passed today -> tomorrow
    assert parse_when("25 oct 6pm", base) == datetime(2026, 10, 25, 18, 0, tzinfo=TZ)
    assert parse_when("2026-11-01 08:15", base) == datetime(2026, 11, 1, 8, 15, tzinfo=TZ)
    assert parse_duration("2h") == timedelta(hours=2) and parse_duration("45m") == timedelta(minutes=45)
    nc = next_cron("30 7 * * *", base)
    assert nc == datetime(2026, 10, 16, 7, 30, tzinfo=TZ), nc
    assert next_cron("0 18 * * 5", base) == datetime(2026, 10, 16, 18, 0, tzinfo=TZ)  # Fri
    assert add("past item", at="2026-01-01 09:00")["ok"]
    assert add("future", in_="2h")["ok"]
    assert add("brief", cron="30 7 * * *", action="daily-briefing")["ok"]
    d = due()
    assert len(d) == 1 and d[0]["text"] == "past item", d
    assert due() == []  # never fires twice
    assert len(listing()) == 2 and len(listing(True)) == 3
    assert snooze(1, "30m")["updated"] == 1 and len(listing()) == 3
    assert delete(1)["deleted"] == 1
    print("reminders selftest ok")
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
    try:
        if cmd == "add":
            res = add(argv[1], arg(argv, "--at"), arg(argv, "--in"), arg(argv, "--cron"), arg(argv, "--action"))
        elif cmd == "list":
            res = listing("--all" in argv)
        elif cmd == "due":
            res = due()
        elif cmd == "done":
            res = mark(int(argv[1]))
        elif cmd == "delete":
            res = delete(int(argv[1]))
        elif cmd == "snooze":
            res = snooze(int(argv[1]), arg(argv, "--in", "30m"))
        elif cmd == "--prune":
            res = prune()
        else:
            print(__doc__)
            return 2
    except (ValueError, IndexError) as e:
        res = {"error": str(e)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
