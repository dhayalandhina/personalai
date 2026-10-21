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
        """Quit and reopen (macOS): a detached shell reopens the app after we exit."""
        subprocess.Popen(["/bin/sh", "-c", "sleep 2; open -a dd || open -a /Applications/dd.app || true"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

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
            if core.update_requested():
                self.do_update()

        def do_update(self) -> None:
            changed, msg = core.update()
            notify(msg)
            print(f"update: {msg}", flush=True)
            if changed:
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


def mic_helper_build_cmd() -> tuple[list[str] | None, str]:
    """swiftc command to (re)build the echo-cancelling mic helper, or (None, reason) when nothing to do."""
    src, out = dd.ROOT / "macapp" / "dd-mic.swift", Path.home() / ".dd" / "bin" / "dd-mic"
    if not src.is_file():
        return None, "no dd-mic.swift"
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return None, "up to date"
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "swiftc missing (xcode-select --install) — using ffmpeg mic, no echo cancellation"
    return [swiftc, "-O", "-suppress-warnings", "-o", str(out), str(src)], str(out)


def ensure_mic_helper() -> None:
    cmd, note = mic_helper_build_cmd()
    if cmd is None:
        print(f"mic helper: {note}", flush=True)
        return
    Path(note).parent.mkdir(parents=True, exist_ok=True)
    print("building dd-mic (echo cancellation)...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    print(("dd-mic built: " + note) if r.returncode == 0 else f"dd-mic build failed: {r.stderr.strip()[-400:]}", flush=True)
    if r.returncode == 0:  # a new helper may manage voice processing where the old one could not: try all again
        try:
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
        try:  # the Tamil Nadu voices (edge-tts, a few MB): installed once; Kokoro stays as the offline fallback
            if dd_voice.setup(log=lambda m: print(m, flush=True)):
                if dd_voice.kokoro_ready():
                    dd_voice.engine()
                tamil = dd_voice.mac_tamil_voice()
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
    cmd, note = mic_helper_build_cmd()  # no swiftc / no macOS here: a reason, never an exception
    assert cmd is None or cmd[0].endswith("swiftc"), (cmd, note)
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
