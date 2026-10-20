#!/usr/bin/env python3
"""dd_app.py — dd in the Mac menu bar, always listening.

A 🤖 sits in the menu bar and shows what dd is doing: 🤖 listening · 🎙 waiting for your command ·
💭 thinking · 🔊 speaking · 💤 paused · ⚠️ mic problem. Say "dd, chrome open pannu" or "jarvis" from anywhere.
Menu: pause/resume listening, what it last heard, open the chat window, status, quit.
It is only a front-end around the same dd loop + mac-control the CLI uses — no new power over the Mac, and
irreversible actions still ask first. dd.app (install_app.sh) launches this; it also runs from a terminal.

  python3 examples/dd_app.py            menu-bar app (needs: pip install rumps) — falls back to terminal listening
  python3 examples/dd_app.py --selftest
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)
import dd_listen  # noqa: E402

CHAT = HERE / "dd_chat.py"
ICONS = {"starting": "⏳", "listening": "🤖", "recording": "🎙", "thinking": "💭", "speaking": "🔊",
         "paused": "💤", "error": "⚠️", "stopped": "⏹"}


def notify(text: str, title: str = "dd") -> None:
    """macOS notification via mac-control (osascript); silent elsewhere."""
    dd.run_script("mac-control", "mac.py", ["notify", f"{text}"[:200]])


class AppCore:
    """Everything the menu does, with no AppKit in sight — so it tests headless."""

    def __init__(self, listener: dd_listen.Listener | None = None, spawn=None):
        self.spawn = spawn or (lambda cmd: subprocess.Popen(cmd))
        self.chat_proc = None
        self.listener = listener or dd_listen.Listener(on_problem=self.problem, on_state=lambda s: None,
                                                       on_heard=self.heard)
        self.listener.on_problem, self.listener.on_heard = self.problem, self.heard
        self.last = ""
        self.last_at = 0.0
        self.problems: list[str] = []
        self.thread: threading.Thread | None = None

    # ----- listener wiring -----
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.listener.run, daemon=True, name="dd-ears")
        self.thread.start()

    def stop(self) -> None:
        self.listener.stop.set()
        if self.chat_proc is not None and self.chat_proc.poll() is None:
            self.chat_proc.terminate()

    def heard(self, text: str, secs: float) -> None:
        self.last, self.last_at = text, time.time()

    def problem(self, msg: str) -> None:
        self.problems.append(msg)
        notify(msg)
        if "Microphone" in msg or "mic" in msg.lower():
            dd_listen.open_mic_settings()

    # ----- menu -----
    def icon(self) -> str:
        return ICONS.get(self.listener.state, "🤖")

    def listening_label(self) -> str:
        return "Pause listening" if not self.listener.paused else "Resume listening"

    def toggle_listening(self) -> bool:
        self.listener.paused = not self.listener.paused
        return not self.listener.paused

    def last_label(self) -> str:
        if not self.last:
            return "Heard: (nothing yet)"
        return f"Heard: {self.last[:40]}" + ("…" if len(self.last) > 40 else "")

    def menu_labels(self) -> list[str]:
        return [self.listening_label(), self.last_label(), "Open chat", "Status", "Quit dd"]

    def open_chat(self) -> list[str]:
        cmd = [sys.executable, str(CHAT)]
        if self.chat_proc is None or self.chat_proc.poll() is not None:
            self.chat_proc = self.spawn(cmd)
        return cmd

    def status_text(self) -> str:
        r = dd.LIB.run("system-ops", "healthcheck.py", ["--brief"], timeout=60)
        brief = (r["stdout"] or r["stderr"]).strip()[:250] or "status kidaikkala"
        return f"ears: {self.listener.state}\nlast heard: {self.last or '-'}\n{brief}"


def run_menu_bar(core: AppCore) -> int:
    import rumps

    class DDBar(rumps.App):
        def __init__(self):
            super().__init__("dd", title=core.icon(), quit_button=None)
            self.menu = ["Pause listening", "Heard: (nothing yet)", "Open chat", "Status", None, "Quit dd"]
            self.menu["Heard: (nothing yet)"].set_callback(None)
            self.timer = rumps.Timer(self.refresh, 0.4)
            self.timer.start()

        def refresh(self, _):  # AppKit must be touched from the main thread: poll state from a timer
            self.title = core.icon()
            self.menu["Pause listening"].title = core.listening_label()
            self.menu["Heard: (nothing yet)"].title = core.last_label()

        @rumps.clicked("Pause listening")
        def toggle(self, _):
            core.toggle_listening()

        @rumps.clicked("Open chat")
        def open_chat(self, _):
            core.open_chat()

        @rumps.clicked("Status")
        def status(self, _):
            rumps.alert("dd", core.status_text())

        @rumps.clicked("Quit dd")
        def quit(self, _):
            core.stop()
            rumps.quit_application()

    DDBar().run()
    return 0


def run_app() -> int:
    """Warm up, start the ears, then the menu bar (or plain terminal mode without rumps)."""
    problem = dd.preflight()
    if problem:
        notify(f"Brain ready illa: {problem}")
    core = AppCore()
    threading.Thread(target=lambda: (dd_listen.warm_stt(), dd.warm()), daemon=True).start()
    dd.start_reminder_watcher(lambda text: core.listener.say(text))
    core.start()
    threading.Timer(1.5, lambda: core.listener.say("Vanakkam Dhina. dd ready.")).start()
    try:
        return run_menu_bar(core)
    except ImportError:
        print("menu bar ku rumps illa (pip3 install rumps) — terminal la listen pannudhu. Ctrl-C to stop.")
        try:
            while core.thread and core.thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            core.stop()
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
    # a listener fed by a fake mic that yields nothing and then ends the run (stop is set in the test)
    lis = dd_listen.Listener(source=lambda: iter([]), stt_fn=lambda b: "", speak=lambda t: None,
                             handle=lambda c: "ok", followup_s=8, log=lambda *_: None)
    core = AppCore(listener=lis, spawn=lambda cmd: (spawned.append(cmd), FakeProc(cmd))[1])

    # icons follow listener state; pause toggles; labels
    assert core.icon() == "⏳"
    lis.state = "listening"
    assert core.icon() == "🤖" and core.menu_labels()[0] == "Pause listening"
    assert core.toggle_listening() is False and lis.paused and core.listening_label() == "Resume listening"
    assert core.toggle_listening() is True and not lis.paused
    for st, ic in (("recording", "🎙"), ("thinking", "💭"), ("speaking", "🔊"), ("error", "⚠️")):
        lis.state = st
        assert core.icon() == ic
    core.heard("dd open chrome please now this is long enough to be cut off yes", 0.3)
    assert core.last_label().startswith("Heard: dd open chrome") and core.last_label().endswith("…")

    # chat window spawns once while alive, re-opens after close
    core.open_chat()
    core.open_chat()
    assert len(spawned) == 1 and spawned[0][1].endswith("examples/dd_chat.py")
    core.chat_proc.terminate()
    core.open_chat()
    assert len(spawned) == 2

    # problems are recorded (notification goes through mac-control dry run)
    core.problem("mic error: Input/output error — Microphone permission paaru")
    assert core.problems and "healthcheck" not in core.status_text() and "ears:" in core.status_text()

    # start/stop the ears thread
    lis.stop.wait = lambda *_: None
    core.start()
    time.sleep(0.2)
    core.stop()
    core.thread.join(timeout=3)
    assert not core.thread.is_alive() and lis.state == "stopped"
    assert "rumps" not in sys.modules, "rumps must only be imported inside run_menu_bar()"
    print("dd_app selftest ok (icons per state, pause toggle, heard label, chat spawn-once, problem notify, ears thread, no rumps at import)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    return run_app()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
