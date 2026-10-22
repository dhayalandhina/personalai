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
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)
import dd_listen  # noqa: E402
import dd_voice  # noqa: E402

CHAT = HERE / "dd_chat.py"
TEACH = "Teach 'Jarvis' (your voice)…"
ICONS = {"starting": "⏳", "listening": "🤖", "recording": "🎙", "thinking": "💭", "speaking": "🔊",
         "paused": "💤", "error": "⚠️", "stopped": "⏹"}  # text fallback when the images are missing
STATUS_DIR = HERE.parent / "macapp" / "status"  # voice-wave glyphs (examples/status_icons.py)
COLOUR_ICONS = {"recording", "error"}  # accent images; the rest are templates macOS tints for light/dark bars


def status_icon(state: str) -> tuple[str | None, bool]:
    """(png path, template?) for a listener state; (None, False) when the image is missing."""
    name = {"stopped": "paused"}.get(state, state)
    p = STATUS_DIR / f"{name}.png"
    if not p.is_file():
        p = STATUS_DIR / "listening.png"
    return (str(p), name not in COLOUR_ICONS) if p.is_file() else (None, False)


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
        self.test_result: dict | None = None
        self.problems: list[str] = []
        self.thread: threading.Thread | None = None
        self.hud = None                      # dd_hud.Hud when the on-screen panel could be created
        self.teach_result: dict | None = None  # voice setup outcome, shown once by the menu
        self._seen_wake, self._seen_said = 0.0, ""
        self._update_check = 0.0
        self._newer = False          # the background check found newer dd code on GitHub
        self._watch: threading.Thread | None = None

    # ----- listener wiring -----
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.listener.run, daemon=True, name="dd-ears")
        self.thread.start()

    def stop(self) -> None:
        self.listener.shutdown()  # stops the ears and releases the mic (ffmpeg / dd-mic)
        if self.chat_proc is not None and self.chat_proc.poll() is None:
            self.chat_proc.terminate()

    def heard(self, text: str, secs: float) -> None:
        self.last, self.last_at = text, time.time()

    def problem(self, msg: str) -> None:
        self.problems.append(msg)
        notify(msg)
        if self.hud is not None:
            self.hud.report(msg)
        if "Microphone" in msg or "mic" in msg.lower():
            dd_listen.open_mic_settings()

    def events(self) -> list[tuple[str, str]]:
        """What changed since the last poll: ('wake', '') when 'hey jarvis' was just heard, ('said', text) when
        dd spoke. Without the on-screen panel these become notifications, so you always know it heard you."""
        out = []
        lis = self.listener
        if lis.woke_at != self._seen_wake:
            self._seen_wake = lis.woke_at
            if lis.woke_at:
                out.append(("wake", ""))
        if lis.last_said != self._seen_said:
            self._seen_said = lis.last_said
            if lis.last_said:
                out.append(("said", lis.last_said))
        return out

    def update_requested(self) -> bool:
        """True once when someone asked for an update by touching <repo>/logs/update.req or ~/.dd/update.req
        (checked every 5 s) — lets a fix roll out without touching the menu. The marker is removed."""
        now = time.time()
        if now - self._update_check < 5:
            return False
        self._update_check = now
        for p in (dd.ROOT / "logs" / "update.req", Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "update.req"):
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
                return True
        return False

    def diag_requested(self) -> bool:
        """True once when someone touched <repo>/logs/diag.req (or ~/.dd/diag.req): run_diag() then writes
        logs/diag.txt — permissions, model speeds, ears, memory — so a fix can start from facts, not guesses."""
        for p in (dd.ROOT / "logs" / "diag.req", Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "diag.req"):
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
                return True
        return False

    def diag(self) -> None:
        if getattr(self, "_diag_thread", None) is not None and self._diag_thread.is_alive():
            return
        self._diag_thread = threading.Thread(target=lambda: run_diag(self.listener), daemon=True, name="dd-diag")
        self._diag_thread.start()

    def watch_updates(self, every: float | None = None) -> None:
        """Background: every 15 min `git fetch`; newer code on GitHub -> update_due() rolls it out when dd is idle.
        (Fixes used to wait for a manual update or a restart.) DD_NO_UPDATE=1 turns it off."""
        if os.environ.get("DD_NO_UPDATE") == "1" or not (dd.ROOT / ".git").is_dir() or self._watch is not None:
            return
        every = UPDATE_CHECK_S if every is None else every

        def loop():
            git = ["git", "-C", str(dd.ROOT)]
            while True:
                time.sleep(every)
                try:
                    subprocess.run(git + ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=10", "fetch", "-q"],
                                   capture_output=True, text=True, timeout=60)
                    n = subprocess.run(git + ["rev-list", "--count", "HEAD..@{u}"], capture_output=True, text=True,
                                       timeout=10).stdout.strip()
                    if n.isdigit() and int(n) > 0:
                        self._newer = True
                except Exception:
                    pass
        self._watch = threading.Thread(target=loop, daemon=True, name="dd-update-watch")
        self._watch.start()

    def ear_restart_due(self) -> bool:
        """dd-ear's tap stayed silent while the Mac played, even after a rebuild: restart dd (once an hour at most,
        when idle) — macOS applies a newly granted System Audio Recording permission to a fresh app only."""
        if not dd_listen.EAR_STATUS.get("relaunch"):
            return False
        lis = self.listener
        if lis.state not in ("listening", "paused") or lis.clock() - getattr(lis, "_last_activity", 0.0) < 60:
            return False
        marker = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "ear_restarted"
        try:
            if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
                dd_listen.EAR_STATUS["relaunch"] = False
                return False
            marker.write_text(f"{time.time():.0f}")
        except OSError:
            return False
        dd_listen.EAR_STATUS["relaunch"] = False
        return True

    def update_due(self) -> bool:
        """True once when newer code is waiting and dd is idle: listening, nothing heard for 2 min, no window open."""
        lis = self.listener
        if not self._newer or lis.state not in ("listening", "paused"):
            return False
        if lis.clock() - getattr(lis, "_last_activity", 0.0) < 120 or lis.clock() < max(lis.armed_until, lis.followup_until):
            return False
        self._newer = False
        return True

    # ----- menu -----
    def icon(self) -> str:
        return ICONS.get(self.listener.state, "🤖")

    def listening_label(self) -> str:
        return "Pause listening" if not self.listener.paused else "Resume listening"

    def toggle_listening(self) -> bool:
        self.listener.paused = not self.listener.paused
        return not self.listener.paused

    def last_label(self) -> str:
        lvl = f"mic {self.listener.level:.0f}"
        last = self.listener.last_heard or self.last
        if not last:
            return f"Heard: (nothing yet) · {lvl}"
        return f"Heard: {last[:40]}" + ("…" if len(last) > 40 else "") + f" · {lvl}"

    def voice_items(self) -> list[tuple[str, str, bool]]:
        """[(voice id, menu label, chosen?)] for the Voice submenu."""
        cur = dd_voice.current()
        return [(vid, v["label"], vid == cur) for vid, v in dd_voice.VOICES.items()]

    def speed_items(self) -> list[tuple[str, str, bool]]:
        cur = dd_voice.current_speed()
        return [(sp, f"Speed: {sp}", sp == cur) for sp in dd_voice.SPEEDS]

    def pick_voice(self, vid: str) -> None:
        """Choose a voice and let it introduce itself (on a thread: synthesis must not block the menu)."""
        dd_voice.set_voice(vid)
        line = dd_voice.sample_line(vid) if dd_voice.installed() else \
            f"{dd_voice.VOICES[vid]['label'].split(' —')[0]} voice install aagudhu, konjam neram la ready."
        threading.Thread(target=self.listener.say, args=(line,), daemon=True, name="dd-voice-preview").start()

    def pick_speed(self, speed: str) -> None:
        dd_voice.set_speed(speed)
        threading.Thread(target=self.listener.say, args=(f"Seri, ippo {speed} speed la pesuren.",), daemon=True,
                         name="dd-voice-preview").start()

    def teach(self) -> bool:
        """Siri-style voice setup: dd asks for 'Jarvis' five times and learns how *you* say it. False if busy."""
        if self.listener.enrolling:
            return False
        self.teach_result = None
        threading.Thread(target=self.listener.enroll, kwargs={"done": lambda r: setattr(self, "teach_result", r)},
                         daemon=True, name="dd-voice-setup").start()
        return True

    def teach_text(self) -> str:
        r = self.teach_result or {}
        if r.get("error"):
            return f"Voice setup mudiyala: {r['error']}\nAmaidhiyana idathula, mic pakkathula try pannu."
        return (f"Ready ✅ — {r.get('clips')} recordings of your 'Jarvis'.\n"
                f"Ini un voice la 'Jarvis' sonna wake aagum; video / vera sound ignore aagum.")

    def mic_test(self) -> None:
        """Say something for 3 s; the result (level + transcript) pops up in the menu bar app."""
        self.test_result = None
        self.listener.say("Sollu, moonu second.")
        self.listener.mic_test(3.0, done=lambda r: setattr(self, "test_result", r))

    def test_text(self) -> str:
        r = self.test_result or {}
        heard = r.get("heard") or "(onnum kekkala)"
        tip = ""
        if r.get("peak", 0) < 60:
            tip = "\nMic level romba low — Microphone permission / sariyaana mic ah paaru."
        elif r.get("peak", 0) < r.get("threshold", 0):
            tip = "\nUn voice threshold ku keezha — konjam pakkathula / sathamaa pesu."
        return f"Heard: {heard}\nmic peak {r.get('peak')}, speech starts above {r.get('threshold')}{tip}"

    def update(self) -> tuple[bool, str]:
        """git pull --ff-only in the dd folder, then compare with the code this process is running: a restart is
        due whenever HEAD moved — also when the folder was updated some other way while GitHub was unreachable."""
        repo = dd.ROOT
        running = RUNNING_COMMIT or subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                                                   capture_output=True, text=True).stdout.strip()
        try:
            r = subprocess.run(["git", "-C", str(repo), "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=10",
                                "pull", "--ff-only", "-q"], capture_output=True, text=True, timeout=60)
            err = "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:]
        except subprocess.TimeoutExpired:
            err = "git pull timed out"
        after = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        if after and after != running[:len(after)]:
            return True, f"updated {running[:7]} → {after} — dd restart aagudhu" + (f" (pull: {err})" if err else "")
        if err:
            return False, f"update aagala: {err}"
        return False, f"already latest ({after})"

    def relaunch(self) -> None:
        relaunch_app()

    def menu_labels(self) -> list[str]:
        return [self.listening_label(), self.last_label(), TEACH, "Mic test (3 s)", "Open chat", "Status", "Update dd", "Quit dd"]

    def open_chat(self) -> list[str]:
        cmd = [sys.executable, str(CHAT)]
        if self.chat_proc is None or self.chat_proc.poll() is not None:
            self.chat_proc = self.spawn(cmd)
        return cmd

    def status_text(self) -> str:
        r = dd.LIB.run("system-ops", "healthcheck.py", ["--brief"], timeout=60)
        brief = (r["stdout"] or r["stderr"]).strip()[:250] or "status kidaikkala"
        return f"ears: {self.listener.state}\nbrain: {dd.brain_label()}\nlast heard: {self.last or '-'}\n{brief}"


def run_menu_bar(core: AppCore) -> int:
    import rumps

    class DDBar(rumps.App):
        def __init__(self):
            icon, tmpl = status_icon(core.listener.state)
            super().__init__("dd", title=None if icon else core.icon(), icon=icon, template=tmpl, quit_button=None)
            self._shown_state = core.listener.state
            self.voice_menu = rumps.MenuItem("Voice")
            self.voice_items = {}
            for vid, label, chosen in core.voice_items():
                if vid == "neerja":
                    self.voice_menu.add(None)  # Tamil Nadu voices first, then the Indian-English ones
                item = rumps.MenuItem(label, callback=self.choose_voice)
                item.state = int(chosen)
                self.voice_menu.add(item)
                self.voice_items[label] = (vid, item)
            self.voice_menu.add(None)
            self.speed_items = {}
            for sp, label, chosen in core.speed_items():
                item = rumps.MenuItem(label, callback=self.choose_speed)
                item.state = int(chosen)
                self.voice_menu.add(item)
                self.speed_items[label] = (sp, item)
            self.menu = ["Pause listening", "Heard: (nothing yet)", None, self.voice_menu, TEACH, "Mic test (3 s)",
                         None, "Open chat", "Status", "Update dd", None, "Quit dd"]
            self.menu["Heard: (nothing yet)"].set_callback(None)
            self.timer = rumps.Timer(self.refresh, 0.4)
            self.timer.start()
            self.hud_timer = None
            if os.environ.get("DD_HUD", "1") != "0":
                try:
                    import dd_hud
                    core.hud = dd_hud.Hud(core.listener)
                    self.hud_timer = rumps.Timer(self.tick_hud, 0.05)  # 20 fps: the voice bars follow you live
                    self.hud_timer.start()
                    print("hud: on-screen voice panel ready", flush=True)
                except Exception as e:
                    print(f"(hud unavailable: {type(e).__name__}: {str(e)[:120]} — notifications instead)", flush=True)

        def choose_voice(self, sender):
            vid, _item = self.voice_items[sender.title]
            for _label, (_v, it) in self.voice_items.items():
                it.state = int(it is sender)
            core.pick_voice(vid)

        def choose_speed(self, sender):
            sp, _item = self.speed_items[sender.title]
            for _label, (_s, it) in self.speed_items.items():
                it.state = int(it is sender)
            core.pick_speed(sp)

        def tick_hud(self, _):
            if core.hud is not None:
                core.hud.tick()

        def refresh(self, _):  # AppKit must be touched from the main thread: poll state from a timer
            st = core.listener.state
            if st != self._shown_state:
                self._shown_state = st
                icon, tmpl = status_icon(st)
                if icon:
                    self._template = tmpl  # (set directly: the template setter would reload the old image)
                    self.icon = icon
                else:
                    self.title = core.icon()
            self.menu["Pause listening"].title = core.listening_label()
            self.menu["Heard: (nothing yet)"].title = core.last_label()
            if core.hud is None or not core.hud.ok:  # no panel: say it with notifications
                for kind, text in core.events():
                    notify("🎙 Kekkuren… sollu" if kind == "wake" else text[:200])
            else:
                core.events()
            if core.test_result is not None:
                text, core.test_result = core.test_text(), None
                rumps.alert("dd mic test", text)
            if core.teach_result is not None:
                text, core.teach_result = core.teach_text(), None
                rumps.alert("dd voice setup", text)
            if core.update_requested() or core.update_due():
                self.do_update(quiet=True)
            elif core.diag_requested():
                core.diag()
            elif core.ear_restart_due():
                print("ear: restarting dd once so macOS applies the System Audio Recording permission", flush=True)
                quiet_start_mark()
                core.relaunch()
                core.stop()
                threading.Timer(2.0, lambda: os._exit(0)).start()
                rumps.quit_application()

        def do_update(self, quiet: bool = False) -> None:
            changed, msg = core.update()
            if not quiet:
                notify(msg)
            print(f"update: {msg}", flush=True)
            if changed:
                if quiet:  # (an automatic roll-out: no "Vanakkam Dhina" greeting over his film after the restart)
                    quiet_start_mark()
                core.relaunch()
                core.stop()
                threading.Timer(2.0, lambda: os._exit(0)).start()
                rumps.quit_application()

        @rumps.clicked("Mic test (3 s)")
        def mic_test(self, _):
            core.mic_test()

        @rumps.clicked(TEACH)
        def teach(self, _):
            core.teach()

        @rumps.clicked("Pause listening")
        def toggle(self, _):
            core.toggle_listening()

        @rumps.clicked("Open chat")
        def open_chat(self, _):
            core.open_chat()

        @rumps.clicked("Status")
        def status(self, _):
            rumps.alert("dd", core.status_text())

        @rumps.clicked("Update dd")
        def update(self, _):
            self.do_update()

        @rumps.clicked("Quit dd")
        def quit(self, _):
            core.stop()
            threading.Timer(2.0, lambda: os._exit(0)).start()  # never leave a zombie dd behind
            rumps.quit_application()

    DDBar().run()
    return 0


RUNNING_COMMIT = ""  # the commit this process started from (auto_update sets it)


def _sh(cmd: list[str], timeout: float = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except Exception as e:
        return f"({type(e).__name__})"


def run_diag(listener=None, out: Path | None = None) -> str:
    """What this Mac lets dd do and how fast it is, written to logs/diag.txt (and the summary to the log):
    permissions (Accessibility, Screen Recording, Full Disk Access, Automation), the ears (source, dd-ear, playback),
    Whisper base/small decode time, the brain, memory/CPU, installed apps the router can see. Read-only."""
    lines = [f"dd diag {time.strftime('%Y-%m-%d %H:%M:%S')} code {RUNNING_COMMIT or '?'}"]
    add = lines.append
    try:
        add(dd_listen.machine_info())
        add(f"python {sys.executable}")
        # ---- permissions
        try:
            import Quartz  # (pyobjc: rumps needs it)
            add(f"screen recording: {'OK' if Quartz.CGPreflightScreenCaptureAccess() else 'NOT allowed (read-screen cannot see other apps)'}")
        except Exception as e:
            add(f"screen recording: ? ({type(e).__name__})")
        try:
            import ctypes
            ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            add(f"accessibility: {'OK' if ax.AXIsProcessTrusted() else 'NOT allowed (type-text / keys will fail)'}")
        except Exception as e:
            add(f"accessibility: ? ({type(e).__name__})")
        r = _sh(["osascript", "-e", 'tell application "System Events" to get name of first process'], 15)
        add("automation (System Events): " + ("OK" if r and "error" not in r.lower() else f"NOT allowed: {r[:120]}"))
        mac_py = dd.ROOT / "skills" / "mac-control" / "scripts" / "mac.py"
        wa = _sh([sys.executable, str(mac_py), "whatsapp-db"], 20)
        add("full disk access / WhatsApp db: " + (("OK — " + wa[:160].replace("\n", " ")) if '"ok": true' in wa else
                                                 ("NOT allowed (System Settings > Privacy & Security > Full Disk Access > dd)" if "denied" in wa else
                                                  f"{wa[:160]}".replace("\n", " "))))
        wa_app = any(os.path.isdir(p) for p in ("/Applications/WhatsApp.app", os.path.expanduser("~/Applications/WhatsApp.app")))
        add(f"WhatsApp app installed: {wa_app}")
        # ---- ears
        if listener is not None:
            add(f"ears: state {listener.state}, source {'dd-ear' if dd_listen.EAR_STATUS.get('active') else 'other'}, "
                f"dd-ear playing {dd_listen.EAR_STATUS.get('playing')}, erle {dd_listen.EAR_STATUS.get('erle', 0):.1f} dB, "
                f"room floor {listener.seg.floor:.0f}, gain {listener.agc.gain:.1f}x, film mode {listener.film_mode()}")
        add("playback now: " + (dd_listen.playback_report() or "nothing"))
        # ---- speech models: a 3 s clip through each loaded model
        import numpy as np
        rng = np.random.default_rng(0)
        pcm = (rng.normal(size=16000 * 3) * 300).astype(np.int16).tobytes()
        for name in ("base", "small"):
            m = dd_listen.model_named(name)
            for _ in range(30):  # (a background load was just started: give it a moment)
                if m is not None or name in dd_listen._load_failed:
                    break
                time.sleep(1)
                m = dd_listen.model_named(name)
            if m is None:
                add(f"whisper {name}: not loaded")
                continue
            t0 = time.monotonic()
            try:
                segs, _ = m.transcribe(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0, language="en", beam_size=1)
                list(segs)
                add(f"whisper {name}: 3 s clip decoded in {time.monotonic() - t0:.1f}s")
            except Exception as e:
                add(f"whisper {name}: failed {type(e).__name__}")
        add(f"stt: command always {dd_listen.default_stt_model()}; wake-confirm "
            f"{'base (Mac busy / small slow)' if dd_listen.mac_busy() or dd_listen.small_slow() else dd_listen.default_stt_model()}")
        # ---- brain
        add(f"brain: {dd.brain_label()}")
        add("ollama ps: " + _sh(["ollama", "ps"], 10).replace("\n", " | ")[:300])
        t0 = time.monotonic()
        try:
            dd.ollama_chat([{"role": "user", "content": "hi"}], [], num_predict=1)
            add(f"ollama ping: {time.monotonic() - t0:.1f}s")
        except Exception as e:
            add(f"ollama ping failed: {type(e).__name__}")
        # ---- machine load
        add("memory: " + _sh(["memory_pressure", "-Q"], 10).replace("\n", " ")[:120] + " | swap " + _sh(["sysctl", "-n", "vm.swapusage"], 5)[:80])
        add("load: " + _sh(["sysctl", "-n", "vm.loadavg"], 5))
        top = _sh(["ps", "-Ao", "pcpu=,rss=,comm=", "-r"], 10).splitlines()[:8]
        add("top cpu: " + "; ".join(" ".join(x.split(None, 2)[i] for i in (0, 2)).rsplit("/", 1)[-1][:40] for x in top if len(x.split(None, 2)) == 3))
        # dd's own threads: which ones burn CPU while idle (ps -M: one line per thread, %CPU is the 3rd-to-last column)
        rows = []
        for line in _sh(["ps", "-M", "-p", str(os.getpid())], 10).splitlines()[1:]:
            cols = line.split()
            try:
                rows.append(float(cols[-3]))
            except (ValueError, IndexError):
                pass
        rows.sort(reverse=True)
        add(f"dd threads: {len(rows)}, cpu% of the busiest: {rows[:8]}, total {sum(rows):.0f}%; "
            f"python threads: {', '.join(sorted(t.name for t in threading.enumerate()))[:300]}")
        add("thread env: " + ", ".join(f"{k}={os.environ.get(k, '-')}" for k in ("OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OPENBLAS_NUM_THREADS")))
        # ---- apps the router can open
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("dd_mac_diag", mac_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            apps = mod.installed_apps()
            add(f"installed apps seen: {len(apps)} — " + ", ".join(sorted(apps)[:40]))
            for q in ("chrome", "whatsapp", "word", "spotify", "vs code", "telegram"):
                r_ = mod.resolve_open(q)
                add(f"  open '{q}' -> {r_['kind']} {r_['target']}")
        except Exception as e:
            add(f"apps: failed {type(e).__name__}: {str(e)[:100]}")
        # ---- certificates
        add(f"SSL_CERT_FILE: {os.environ.get('SSL_CERT_FILE', '(python default)')}")
    except Exception as e:
        add(f"diag failed: {type(e).__name__}: {str(e)[:200]}")
    text = "\n".join(lines)
    out = out or (dd.ROOT / "logs" / "diag.txt")
    try:
        out.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    print("diag: " + " | ".join(lines[3:9])[:600] + f"\n  (full report: {out})", flush=True)
    return text


def relaunch_app() -> None:
    """Quit and reopen (macOS): a detached shell waits until this process has really exited, then reopens the
    app. (A fixed 2 s sleep lost the race once: the old dd was still shutting down, `open` just activated it,
    and then nothing was running.)"""
    pid = os.getpid()
    script = (f"for i in $(seq 1 120); do kill -0 {pid} 2>/dev/null || break; sleep 0.5; done; "
              # then the dd applet itself (it lives until its launcher returns): `open` must start a new one
              "for i in $(seq 1 40); do pgrep -f 'dd.app/Contents/MacOS/' >/dev/null || break; sleep 0.5; done; "
              "sleep 0.5; open -a dd || open -a /Applications/dd.app || open -a \"$HOME/Applications/dd.app\" || true")
    subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


CAPTURE_TEXT = ("dd hears what this Mac is playing (a film, music, its own voice) and takes it out of the microphone "
                "sound, so it still hears you say Jarvis. Nothing is recorded or sent anywhere.")


def app_bundle() -> Path | None:
    """The dd.app this process runs under (the applet that started the launcher), else the installed one."""
    try:
        out = subprocess.run(["pgrep", "-lf", "dd.app/Contents/MacOS/"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        m = re.search(r"(/\S.*?/dd\.app)/Contents/MacOS/", line)
        if m:
            return Path(m.group(1))
    for p in (Path.home() / "Applications" / "dd.app", Path("/Applications/dd.app")):
        if p.is_dir():
            return p
    return None


def ensure_audio_capture_text(app: Path | None = None, codesign: str = "codesign", force: bool = False) -> bool:
    """dd-ear's system-audio tap gives only silence unless the app that launched it — dd.app — carries
    NSAudioCaptureUsageDescription (macOS then denies quietly: every buffer zeros, every call 'OK'). Adds it once
    and re-signs the bundle ad hoc, as install_app.sh does. True = the bundle changed: dd must start again so
    macOS sees the new signature (it then asks once for the Microphone and for System Audio Recording)."""
    if not force and (not dd_listen.MAC or not dd_listen.ear_wanted()):
        return False
    app = app or app_bundle()
    if app is None:
        return False
    plist = app / "Contents" / "Info.plist"
    marker = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "capture_text_added"
    try:
        original = plist.read_bytes()
        d = plistlib.loads(original)
    except Exception as e:
        print(f"(dd.app Info.plist unreadable: {e})", flush=True)
        return False
    if d.get("NSAudioCaptureUsageDescription") or marker.exists():  # (never twice: no restart loop)
        return False
    d["NSAudioCaptureUsageDescription"] = CAPTURE_TEXT
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time():.0f} {app}")
        plist.write_bytes(plistlib.dumps(d))
        r = subprocess.run([codesign, "--force", "--deep", "--sign", "-", str(app)], capture_output=True, text=True,
                           timeout=120)
    except Exception as e:
        r = None
        err = str(e)
    if r is None or r.returncode != 0:
        plist.write_bytes(original)  # the original again: its signature is valid as it was
        print(f"(dd.app not re-signed — Info.plist restored: {(r.stderr.strip()[-200:] if r else err)})", flush=True)
        return False
    print(f"dd.app: added the System Audio Recording text and re-signed {app}", flush=True)
    return True


UPDATE_CHECK_S = float(os.environ.get("DD_UPDATE_EVERY_MIN", "15")) * 60


def _quiet_marker() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "quiet_start"


def quiet_start_mark() -> None:
    try:
        _quiet_marker().write_text(f"{time.time():.0f}")
    except OSError:
        pass


def quiet_start() -> bool:
    """True (once) when this start is an automatic update restart: skip the spoken greeting."""
    p = _quiet_marker()
    try:
        fresh = time.time() - float(p.read_text().strip() or 0) < 300
        p.unlink()
        return fresh
    except (OSError, ValueError):
        return False


def auto_update() -> bool:
    """At launch: fast-forward the dd code (bounded). True when it changed (caller re-execs so the new code runs)."""
    global RUNNING_COMMIT
    if not (dd.ROOT / ".git").is_dir():
        return False
    try:
        git = ["git", "-C", str(dd.ROOT)]
        before = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        if os.environ.get("DD_NO_UPDATE") == "1":
            RUNNING_COMMIT = before
            return False
        subprocess.run(git + ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=10", "pull", "--ff-only", "-q"],
                       capture_output=True, text=True, timeout=40)
        after = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        RUNNING_COMMIT = after
        print(f"code: {after[:7]}" + (" (updated)" if after != before else ""), flush=True)
        return bool(before) and after != before
    except Exception as e:
        print(f"(update skipped: {e})", flush=True)
        return False


def mic_helper_build_cmd(name: str = "dd-mic") -> tuple[list[str] | None, str]:
    """swiftc command to (re)build a mic helper (dd-mic: voice processing; dd-ear: mic + this Mac's own sound for
    the echo canceller), or (None, reason) when nothing to do."""
    src, out = dd.ROOT / "macapp" / f"{name}.swift", Path.home() / ".dd" / "bin" / name
    if not src.is_file():
        return None, f"no {name}.swift"
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return None, "up to date"
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "swiftc missing (xcode-select --install) — using ffmpeg mic, no echo cancellation"
    return [swiftc, "-O", "-suppress-warnings", "-o", str(out), str(src)], str(out)


def ensure_mic_helper() -> None:
    for name in ("dd-mic", "dd-ear"):
        cmd, note = mic_helper_build_cmd(name)
        if cmd is None:
            print(f"mic helper {name}: {note}", flush=True)
            continue
        Path(note).parent.mkdir(parents=True, exist_ok=True)
        print(f"building {name}...", flush=True)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            print(f"{name} build timed out", flush=True)
            continue
        print((f"{name} built: " + note) if r.returncode == 0 else f"{name} build failed: {r.stderr.strip()[-600:]}",
              flush=True)
        if r.returncode == 0 and name == "dd-mic":  # a new helper may manage voice processing where the old one
            try:                                     # could not: try all its wirings again
                dd_listen.mic_mode_file().unlink()
            except OSError:
                pass


def ensure_python_deps(mods=(("openwakeword", "openwakeword"), ("faster_whisper", "faster-whisper"),
                              ("edge_tts", "edge-tts"))) -> None:
    """Install missing voice deps into this python once (hey-jarvis wake model, Whisper, the Tamil voices)."""
    missing = []
    for mod, pkg in mods:
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if not missing:
        return
    print(f"installing {' '.join(missing)} (one-time)...", flush=True)
    notify(f"First run: {' '.join(missing)} install pannudhu (1-2 nimisham)…")
    for extra in ([], ["--break-system-packages"], ["--user"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, *missing], capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            print("installed", flush=True)
            return
    print("install failed: " + (r.stderr or "")[-300:], flush=True)


def run_app() -> int:
    """Update, deps, mic helper, warm up, start the ears, then the menu bar (terminal mode without rumps)."""
    if auto_update():
        os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])  # run the new code right away
    try:
        ensure_python_deps()
    except Exception as e:
        print(f"(deps skipped: {e})", flush=True)
    try:
        ensure_mic_helper()
    except Exception as e:
        print(f"(mic helper skipped: {e})", flush=True)
    try:  # once: without this text in dd.app the film-cancelling ear hears only silence from the Mac
        if ensure_audio_capture_text():
            print("restarting once so macOS uses the new signature (Allow: Microphone + System Audio Recording)",
                  flush=True)
            notify("dd restart aagudhu — Microphone, System Audio Recording kekkum: rendukkum Allow kudu.")
            relaunch_app()
            return 0
    except Exception as e:
        print(f"(system audio permission text skipped: {e})", flush=True)
    problem = dd.preflight()
    if problem:
        notify(f"Brain ready illa: {problem}")
    print(dd_listen.machine_info(), flush=True)
    copied = dd_listen.mirror_setup_clips()
    if copied:
        print(f"voice setup clips mirrored to logs/voice ({copied})", flush=True)
    core = AppCore()
    wake = core.listener.wake
    print("wake: " + (("'hey jarvis' model (openwakeword)" + (" + 'dee dee' head" if getattr(wake, "dd", None) else "")
                       + " — Whisper runs only after the wake") if wake
                      else "Whisper text wake (install openwakeword for the always-on model)"), flush=True)
    print(f"brain: {dd.brain_label()}", flush=True)
    vp = getattr(wake, "voice", None) if wake else None
    print(f"voice print: {vp.clips} recordings, detect {vp.threshold:.2f}, gate {vp.gate:.2f}" if vp
          else "voice print: none yet — menu → " + TEACH, flush=True)

    def warm_all():
        dd_listen.warm_stt()
        if dd_listen.ram_gb() < 12:
            time.sleep(20)  # 8 GB Mac: let Whisper settle before the LLM load (both at once starve the mic)
        dd.warm()
        try:  # the voice brain's prompt read into the local model's cache now, not on the first command (29 s)
            core.listener.prepare_brain()
        except Exception as e:
            print(f"(brain prefill skipped: {type(e).__name__}: {str(e)[:120]})", flush=True)
        try:  # the Tamil Nadu voices (edge-tts, a few MB): installed once; Kokoro stays as the offline fallback
            if dd_voice.setup(log=lambda m: print(m, flush=True)):
                # (Kokoro is only the offline fallback now: loaded when first needed, not held in RAM — 8 GB Macs)
                tamil = dd_voice.mac_tamil_voice()
                ok, secs, err = dd_voice.selfcheck()  # is Microsoft's voice reachable from this Mac at all?
                print(f"voice check: online voice {'OK' if ok else 'FAILED'} in {secs:.1f}s" + (f" — {err}" if err else ""),
                      flush=True)
                print(f"voice: {dd_voice.VOICES[dd_voice.current()]['label']} ({dd_voice.current_speed()} speed) ready"
                      f"; offline: {('Mac ' + tamil) if tamil else ''}{' + ' if tamil and dd_voice.kokoro_ready() else ''}"
                      f"{'Kokoro' if dd_voice.kokoro_ready() else ('' if tamil else 'Mac voice')}", flush=True)
        except Exception as e:
            print(f"(voice setup skipped: {type(e).__name__}: {str(e)[:120]})", flush=True)
        if dd_listen.mlx_wanted():
            dd_listen.setup_mlx()
    threading.Thread(target=warm_all, daemon=True, name="dd-warm").start()
    dd.start_reminder_watcher(lambda text: core.listener.say(text))
    core.start()
    if not quiet_start():
        threading.Timer(1.5, lambda: core.listener.say("Vanakkam Dhina. dd ready.")).start()
    core.watch_updates()
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
                             handle=lambda c: "ok", followup_s=8, log=lambda *_: None, inline=True)
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
    assert core.last_label().startswith("Heard: dd open chrome") and "… · mic" in core.last_label(), core.last_label()

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

    # mic test: ack, capture 3 s from the stream, result text for the alert
    said = []
    lis.speak_fn = said.append
    lis.stt = lambda b: "jarvis mani enna"
    core.mic_test()
    assert said == ["Sollu, moonu second."] and lis.test is not None
    for f in dd_listen._tone(1500) + dd_listen._quiet(1600):
        lis.feed(f)
    assert core.test_result and core.test_result["heard"] == "jarvis mani enna", core.test_result
    assert "Heard: jarvis mani enna" in core.test_text() and "mic peak" in core.test_text()
    assert "Mic test (3 s)" in core.menu_labels() and "mic " in core.last_label() and "Update dd" in core.menu_labels()
    changed, msg = core.update()  # this checkout is at HEAD of its branch or offline: never an exception
    assert isinstance(changed, bool) and msg

    # start/stop the ears thread
    lis.stop.wait = lambda *_: None
    core.start()
    time.sleep(0.2)
    core.stop()
    core.thread.join(timeout=3)
    assert not core.thread.is_alive() and lis.state == "stopped"
    for name in ("dd-mic", "dd-ear"):  # no swiftc / no macOS here: a reason, never an exception
        cmd, note = mic_helper_build_cmd(name)
        assert cmd is None or (cmd[0].endswith("swiftc") and cmd[-1].endswith(f"{name}.swift")), (cmd, note)
    # events for the notification fallback: a wake and a spoken reply are each reported once
    assert core.events() == [("said", "Sollu, moonu second.")] and core.events() == []  # the mic test's prompt above
    lis.woke_at = 123.0
    assert core.events() == [("wake", "")] and core.events() == []
    lis.last_said = "Chrome open pannitten."
    assert core.events() == [("said", "Chrome open pannitten.")] and core.events() == []
    # update marker: touch ~/.dd/update.req -> one True (marker removed), then False; checked every 5 s
    marker = Path(os.environ["DD_HOME"]) / "update.req"
    marker.write_text("")
    core._update_check = 0.0
    assert core.update_requested() is True and not marker.exists()
    core._update_check = 0.0
    assert core.update_requested() is False
    # diag marker -> a report file (read-only checks; off the Mac most lines say what could not be checked)
    dmark = Path(os.environ["DD_HOME"]) / "diag.req"
    dmark.write_text("")
    assert core.diag_requested() is True and not dmark.exists() and core.diag_requested() is False
    dout = Path(os.environ["DD_HOME"]) / "diag-test.txt"
    report = run_diag(lis, out=dout)
    assert dout.is_file() and report.startswith("dd diag ") and "brain:" in report and "whisper base:" in report, report[:300]
    # newer code found by the background check: rolled out only when dd is idle (listening, quiet for 2 min)
    core._newer = True
    lis.state, lis._last_activity, lis.armed_until, lis.followup_until = "recording", lis.clock(), 0.0, 0.0
    assert core.update_due() is False, "never in the middle of a command"
    lis.state = "listening"
    assert core.update_due() is False, "not right after something was heard"
    lis._last_activity = lis.clock() - 200
    assert core.update_due() is True and core.update_due() is False and core._newer is False
    # dd.app gets the System Audio Recording text once and is re-signed; a failing codesign restores the original
    import plistlib as _pl
    fake_app = Path(tempfile.mkdtemp(prefix="dd-bundle-")) / "dd.app"
    (fake_app / "Contents").mkdir(parents=True)
    (fake_app / "Contents" / "Info.plist").write_bytes(_pl.dumps({"CFBundleIdentifier": "com.dhina.dd"}))
    ok_sign, bad_sign = Path(tempfile.mkdtemp()) / "cs_ok", Path(tempfile.mkdtemp()) / "cs_bad"
    ok_sign.write_text("#!/bin/sh\nexit 0\n"); bad_sign.write_text("#!/bin/sh\necho nope >&2; exit 1\n")
    ok_sign.chmod(0o755); bad_sign.chmod(0o755)
    assert ensure_audio_capture_text(fake_app, str(bad_sign), force=True) is False
    assert "NSAudioCaptureUsageDescription" not in _pl.loads((fake_app / "Contents" / "Info.plist").read_bytes())
    (Path(os.environ["DD_HOME"]) / "capture_text_added").unlink()
    assert ensure_audio_capture_text(fake_app, str(ok_sign), force=True) is True
    assert _pl.loads((fake_app / "Contents" / "Info.plist").read_bytes())["NSAudioCaptureUsageDescription"] == CAPTURE_TEXT
    assert ensure_audio_capture_text(fake_app, str(ok_sign), force=True) is False, "only once"
    assert ensure_audio_capture_text(fake_app, str(ok_sign)) is False, "never off the Mac"
    # a silent system-audio tap (after a rebuild) restarts dd once an hour at most, only when idle
    dd_listen.EAR_STATUS["relaunch"] = True
    lis.state, lis._last_activity = "recording", lis.clock()
    assert core.ear_restart_due() is False
    lis.state, lis._last_activity = "listening", lis.clock() - 120
    assert core.ear_restart_due() is True and core.ear_restart_due() is False
    dd_listen.EAR_STATUS["relaunch"] = True
    assert core.ear_restart_due() is False and dd_listen.EAR_STATUS["relaunch"] is False, "once an hour"
    # an automatic update restart skips the spoken greeting (once)
    quiet_start_mark()
    assert quiet_start() is True and quiet_start() is False
    # the HUD model follows the same listener (panel itself needs macOS; the model is exercised here)
    import dd_hud
    hud = dd_hud.Hud(lis, panel=type("P", (), {"render": lambda self, f, t: None})())
    lis.state, lis.armed_until = "recording", time.monotonic() + 5
    assert hud.tick()["mode"] == "listen"
    lis.state = "listening"
    assert "AppKit" not in sys.modules
    for st in ("starting", "listening", "recording", "thinking", "speaking", "paused", "error", "stopped"):
        path, tmpl = status_icon(st)
        assert path and Path(path).is_file(), st
        assert tmpl is (st not in ("recording", "error")), (st, tmpl)
    assert TEACH in core.menu_labels()
    items = core.voice_items()
    assert len(items) == 4 and sum(1 for _, _, c in items if c) == 1 and items[0][0] == "pallavi"
    assert [sp for sp, _, _ in core.speed_items()] == ["slow", "normal", "fast"]
    said_voice = []
    lis.speak_fn = said_voice.append
    core.pick_voice("valluvar")
    time.sleep(0.2)
    assert dd_voice.current() == "valluvar" and said_voice and "Valluvar" in said_voice[-1], said_voice
    core.pick_speed("fast")
    time.sleep(0.2)
    assert dd_voice.current_speed() == "fast" and "fast" in said_voice[-1], said_voice
    dd_voice.set_voice("pallavi")
    dd_voice.set_speed("normal")
    core.teach_result = {"clips": 5, "detect": 0.7}
    assert "Ready" in core.teach_text() and "5 recordings" in core.teach_text()
    core.teach_result = {"error": "ValueError: not enough clear recordings"}
    assert "mudiyala" in core.teach_text()
    core.teach_result = None
    # crash guard: three crashes inside ten minutes stop the automatic restart
    cdir = Path(os.environ["DD_HOME"]) / "logs"
    assert crash_guard(cdir, now=1000) and crash_guard(cdir, now=1100) and not crash_guard(cdir, now=1200)
    assert crash_guard(cdir, now=1200 + 700), "old crashes expire"
    os.environ["DD_NO_UPDATE"] = "1"
    assert auto_update() is False
    os.environ.pop("DD_NO_UPDATE")
    assert "rumps" not in sys.modules, "rumps must only be imported inside run_menu_bar()"
    print("dd_app selftest ok (icons per state, pause toggle, heard label, chat spawn-once, problem notify, ears thread, no rumps at import)")
    return 0


def crash_guard(log_dir: Path | None = None, now: float | None = None, window: float = 600, limit: int = 3) -> bool:
    """Record a crash; True when dd may come back by itself (fewer than `limit` crashes in `window` seconds),
    False when it is crash-looping and should stay down for a human to look at the log."""
    log_dir = log_dir or Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "logs"
    now = time.time() if now is None else now
    f = log_dir / "crashes"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamps = [float(x) for x in f.read_text().split()] if f.is_file() else []
        stamps = [t for t in stamps if now - t < window] + [now]
        f.write_text("\n".join(f"{t:.0f}" for t in stamps[-20:]) + "\n")
    except Exception:
        return True
    return len(stamps) < limit


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    try:
        return run_app()
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # never die quietly: log it, and when dd.app launched us come back in a few seconds
        import traceback
        traceback.print_exc()
        print(f"dd crashed: {type(e).__name__}: {e}", flush=True)
        if os.environ.get("DD_APP") == "1" and sys.platform == "darwin":
            if crash_guard():
                subprocess.Popen(["/bin/sh", "-c", "sleep 5; open -a dd || open -a /Applications/dd.app || true"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                print("restarting dd in 5 s", flush=True)
            else:
                notify(f"dd repeatedly crashed ({type(e).__name__}) — ~/.dd/logs/app.log paaru")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
