#!/usr/bin/env python3
"""dd_app.py — dd in the Mac menu bar. A small 🤖 icon up top; click it to chat, talk, or check status.

It opens the chat window (dd_chat.py), toggles JARVIS voice, and keeps firing reminders as Mac
notifications while it sits in the bar. It is only a launcher around the same dd loop + mac-control the
CLI already uses — no new power over the Mac, and irreversible actions still ask first.

  python3 examples/dd_app.py            run the menu-bar app   (needs: pip install rumps)
  python3 examples/dd_app.py --selftest
Env (or ~/.dd/.env): DD_MODEL, OLLAMA_URL, DD_VOICE / DD_SAY_RATE, DD_CHAT_SPEAK.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)

CHAT = HERE / "dd_chat.py"
JARVIS = HERE / "dd_jarvis.py"


class AppCore:
    """Menu actions as plain functions, testable without rumps or a Mac."""

    def __init__(self, spawn=None):
        self.spawn = spawn or (lambda cmd: subprocess.Popen(cmd))  # overridden in tests
        self.chat_proc = None
        self.voice_proc = None

    def menu_labels(self) -> list[str]:
        return ["Open chat", "Voice: " + ("on" if self.voice_on() else "off"), "Status", "New conversation", "Quit dd"]

    def chat_proc_alive(self) -> bool:
        return self.chat_proc is not None and self.chat_proc.poll() is None

    def voice_on(self) -> bool:
        return self.voice_proc is not None and self.voice_proc.poll() is None

    def open_chat(self) -> list[str]:
        cmd = [sys.executable, str(CHAT)]
        if not self.chat_proc_alive():
            self.chat_proc = self.spawn(cmd)
        return cmd

    def toggle_voice(self) -> bool:
        if self.voice_on():
            self.voice_proc.terminate()
            self.voice_proc = None
            return False
        self.voice_proc = self.spawn([sys.executable, str(JARVIS), "--hands-free"])
        return True

    def status_text(self) -> str:
        r = dd.LIB.run("system-ops", "healthcheck.py", ["--brief"], timeout=60)
        return (r["stdout"] or r["stderr"]).strip()[:250] or "status kidaikkala"

    def new_conversation(self) -> None:
        """The chat window owns its own history; reopening a closed window starts fresh."""
        if not self.chat_proc_alive():
            self.open_chat()

    def shutdown(self) -> None:
        for p in (self.chat_proc, self.voice_proc):
            if p is not None and p.poll() is None:
                p.terminate()


def notify(title: str, text: str) -> None:
    """Prefer a rumps notification; fall back to mac-control (osascript)."""
    try:
        import rumps
        rumps.notification(title, "", text)
    except Exception:
        dd.run_script("mac-control", "mac.py", ["notify", f"{title}: {text}"[:200]])


def run_app() -> int:
    import rumps

    core = AppCore()

    class DDBar(rumps.App):
        def __init__(self):
            super().__init__("dd", title="🤖", quit_button=None)
            self.menu = ["Open chat", "Voice", "Status", "New conversation", None, "Quit dd"]
            self.menu["Voice"].state = False

        @rumps.clicked("Open chat")
        def open_chat(self, _):
            core.open_chat()

        @rumps.clicked("Voice")
        def voice(self, sender):
            sender.state = core.toggle_voice()

        @rumps.clicked("Status")
        def status(self, _):
            rumps.alert("dd status", core.status_text())

        @rumps.clicked("New conversation")
        def new_conv(self, _):
            core.new_conversation()

        @rumps.clicked("Quit dd")
        def quit(self, _):
            core.shutdown()
            rumps.quit_application()

    try:  # preflight is advisory — quick Mac actions work even if the brain is down
        problem = dd.preflight()
    except Exception:
        problem = None
    dd.start_reminder_watcher(on_fire=lambda t: notify("dd", t))
    if problem:
        threading.Timer(2, lambda: notify("dd", f"Brain ready illa: {problem}")).start()
    DDBar().run()
    return 0


def selftest() -> int:
    import tempfile
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-app-test-")
    os.environ["DD_MAC_DRY"] = "1"
    assert dd.run_script("rules-guard", "rules_guard.py", ["init"])["ok"]

    class FakeProc:
        def __init__(self, cmd):
            self.cmd, self._alive = cmd, True
        def poll(self):
            return None if self._alive else 0
        def terminate(self):
            self._alive = False

    spawned: list[list[str]] = []

    def spawn(cmd):
        spawned.append(cmd)
        return FakeProc(cmd)

    core = AppCore(spawn=spawn)

    # open chat spawns dd_chat.py once; a second click with the window still open does not re-spawn
    cmd = core.open_chat()
    assert cmd[1].endswith("examples/dd_chat.py") and len(spawned) == 1, (cmd, spawned)
    core.open_chat()
    assert len(spawned) == 1, "re-spawned while window alive"
    core.chat_proc.terminate()  # window closed
    core.open_chat()
    assert len(spawned) == 2, "did not re-open after close"

    # voice toggles a hands-free JARVIS process on and off
    assert core.voice_on() is False
    assert core.toggle_voice() is True and core.voice_on()
    assert spawned[-1][1].endswith("examples/dd_jarvis.py") and "--hands-free" in spawned[-1]
    assert core.toggle_voice() is False and core.voice_on() is False

    # menu labels reflect voice state; status comes from healthcheck
    assert core.menu_labels()[0] == "Open chat" and core.menu_labels()[-1] == "Quit dd"
    core.toggle_voice()
    assert "Voice: on" in core.menu_labels()
    st = core.status_text()
    assert st and "status kidaikkala" not in st, st

    # shutdown stops child processes
    core.shutdown()
    assert not core.voice_on() and not core.chat_proc_alive()

    # app module must not import rumps at load (cloud has no rumps); it's imported only in run_app()
    assert "rumps" not in sys.modules, "rumps imported at module load"
    print("dd_app selftest ok (spawn-once chat, voice toggle, labels, status, shutdown, no rumps at import)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    try:
        return run_app()
    except ImportError:
        print("menu-bar app ku rumps venum:  pip3 install rumps")
        print("Window mattum venumna:  python3 examples/dd_chat.py")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
