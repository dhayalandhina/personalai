#!/usr/bin/env python3
"""dd_daemon.py — dd's quiet background service for macOS (started by launchd, see system-ops/background.sh).

What it does: every 30 s fires due reminders as Mac notifications (and the morning briefing if you scheduled one).
What it never does: no microphone, no screen or app control, no LLM, no network except a scheduled briefing.
Runs at low priority (nice 10, launchd ProcessType=Background) so the Mac stays free for you.

  python3 examples/dd_daemon.py            run until stopped (launchd does this)
  python3 examples/dd_daemon.py --once     one check, then exit
  python3 examples/dd_daemon.py --selftest
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dd_loop_ollama as dd  # noqa: E402  (skills + ~/.dd/.env; does not contact Ollama)

EVERY = 30
LOG_MAX = 5_000_000      # launchd appends forever — keep the log small
LOG_KEEP = 1_000_000


def log_path() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "logs" / "daemon.log"


def trim_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX:
            tail = path.read_bytes()[-LOG_KEEP:]
            path.write_bytes(b"(older log lines trimmed)\n" + tail)
    except OSError:
        pass


def say(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def run_forever() -> int:
    try:
        os.nice(10)
    except OSError:
        pass
    trim_log(log_path())
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    say(f"dd background service started — reminders every {EVERY}s (no mic, no screen control, no LLM)")
    checks = 0
    while not stop["now"]:
        try:
            fired = dd.check_reminders()
            if fired:
                say(f"fired {len(fired)} reminder(s)")
        except Exception as e:  # keep running; launchd restarts us only on a crash
            say(f"reminder check failed: {e}")
        checks += 1
        if checks % 120 == 0:  # about once an hour
            trim_log(log_path())
        for _ in range(EVERY):
            if stop["now"]:
                break
            time.sleep(1)
    say("dd background service stopped")
    return 0


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-daemon-")
    os.environ["DD_MAC_DRY"] = "1"
    dd.run_script("scheduler-reminders", "reminders.py", ["add", "standup call", "--at", "2026-01-01 09:00"])
    first = dd.check_reminders()
    assert [i["text"] for i in first] == ["standup call"], first
    assert dd.check_reminders() == []  # never twice
    lp = log_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_bytes(b"x" * (LOG_MAX + 10))
    trim_log(lp)
    assert lp.stat().st_size <= LOG_KEEP + 100, lp.stat().st_size
    print("dd_daemon selftest ok (reminder fired once, log trimmed)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--once" in argv:
        n = len(dd.check_reminders())
        say(f"one check done, fired {n}")
        return 0
    return run_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
