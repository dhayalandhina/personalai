#!/usr/bin/env python3
"""dd_voice.py — dd's speaking voice: natural Tamil Nadu voices (free), picked from the menu (Voice ▸).

  Pallavi  — Tamil Nadu, female (default)      Neerja  — Indian English, female
  Valluvar — Tamil Nadu, male                  Prabhat — Indian English, male

These are Microsoft's neural voices (the ones in the Edge browser's read-aloud), free and with no key, through
edge-tts. dd thinks in Thanglish; before a Tamil voice speaks, dd_thanglish.py writes the Tamil words in Tamil
script and leaves the English words as they are ("Chrome open பண்ணிட்டேன்"), so it sounds like someone from Tamil
Nadu talking, not an English voice reading Tamil. Replies are spoken sentence by sentence: the next sentence is
fetched while the current one plays, so dd starts talking in well under a second.

Online voices: the reply text goes to Microsoft's speech service to be spoken (nothing else is sent). Offline (or if
that service fails) dd falls back on its own: the Mac's Tamil voice (Vani) when installed, else Kokoro (local, if it
was downloaded earlier), else the Mac voice. Choice + speed live in ~/.dd/voice.json.

  python3 examples/dd_voice.py --setup                 install edge-tts (the app does this by itself)
  python3 examples/dd_voice.py --say "text" [--voice valluvar] [--out reply.mp3]
  python3 examples/dd_voice.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dd_thanglish  # noqa: E402

MAC = sys.platform == "darwin"
VOICES: dict[str, dict] = {  # id -> online voice, whether it reads Tamil, offline Kokoro stand-in, label, gender
    "pallavi": {"edge": "ta-IN-PallaviNeural", "tamil": True, "kokoro": "hf_alpha",
                "label": "Pallavi — Tamil Nadu, female", "gender": "female"},
    "valluvar": {"edge": "ta-IN-ValluvarNeural", "tamil": True, "kokoro": "hm_omega",
                 "label": "Valluvar — Tamil Nadu, male", "gender": "male"},
    "neerja": {"edge": "en-IN-NeerjaExpressiveNeural", "tamil": False, "kokoro": "hf_alpha",
               "label": "Neerja — Indian English, female", "gender": "female"},
    "prabhat": {"edge": "en-IN-PrabhatNeural", "tamil": False, "kokoro": "hm_omega",
                "label": "Prabhat — Indian English, male", "gender": "male"},
}
DEFAULT = "pallavi"
SPEEDS = {"slow": ("-10%", 0.9), "normal": ("+0%", 1.0), "fast": ("+15%", 1.15)}  # edge rate, Kokoro speed
SAMPLE_TA = "Vanakkam Dhina! Naan {name}. Chrome open pannitten — innum enna venum?"
SAMPLE_EN = "Hello Dhina! I'm {name}. Chrome is open — what would you like next?"
SAMPLE = SAMPLE_TA  # (older name)
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
FILES = KOKORO_FILES  # (older name)


def home() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd"))


def model_dir() -> Path:
    return home() / "models" / "kokoro"


def settings_file() -> Path:
    return home() / "voice.json"


def _settings() -> dict:
    try:
        data = json.loads(settings_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(**kv) -> None:
    data = _settings()
    data.update(kv)
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(json.dumps(data))


def current() -> str:
    v = _settings().get("voice", DEFAULT)
    return v if v in VOICES else DEFAULT  # older Kokoro choices (heart, omega...) map to the default


def set_voice(voice: str) -> str:
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}: {', '.join(VOICES)}")
    _save(voice=voice)
    return voice


def current_speed() -> str:
    s = _settings().get("speed", "normal")
    return s if s in SPEEDS else "normal"


def set_speed(speed: str) -> str:
    if speed not in SPEEDS:
        raise ValueError(f"unknown speed {speed!r}: {', '.join(SPEEDS)}")
    _save(speed=speed)
    return speed


def sample_line(voice: str) -> str:
    v = VOICES[voice]
    return (SAMPLE_TA if v["tamil"] else SAMPLE_EN).format(name=v["label"].split(" —")[0])


# ---------------- setup ----------------
def edge_ready() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def kokoro_ready() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:
        return False
    return all((model_dir() / f).is_file() for f in KOKORO_FILES)


def installed() -> bool:
    """Can a neural voice speak right now? (edge-tts installed — it needs the internet — or Kokoro downloaded.)"""
    return edge_ready() or kokoro_ready()


def _pip(pkg: str, log=print) -> bool:
    for extra in ([], ["--break-system-packages"], ["--user"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, pkg],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            return True
    log(f"voice: {pkg} install failed: {(r.stderr or '')[-200:]}")
    return False


def setup(log=print) -> bool:
    """pip install edge-tts once (a few MB). True when a neural voice is ready. Kokoro is no longer downloaded — an
    existing download stays as the offline fallback."""
    if not edge_ready():
        log("voice: installing edge-tts (one-time)...")
        _pip("edge-tts", log)
    return installed()


# ---------------- engines ----------------
_edge_down_until = 0.0
_edge_lock = threading.Lock()
_edge_ok_once = False  # the very first request (TLS + token, at app start next to model loading) may take longer


def _edge_synth(text: str, voice: str, rate: str, path: str, timeout: float) -> bool:
    import asyncio
    import edge_tts

    async def run() -> bool:
        got = False
        c = edge_tts.Communicate(text, voice, rate=rate)
        with open(path, "wb") as f:
            async for chunk in c.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    f.write(chunk["data"])
                    got = True
        return got
    return asyncio.run(asyncio.wait_for(run(), timeout))


def edge_synth(text: str, voice: str, rate: str, path: str, timeout: float = 5.0) -> bool:
    """One sentence -> mp3 via Microsoft's online voices. A quick failure (a dropped connection) is retried once; a
    slow one is not (waiting longer than ~5 s for a voice is worse than the offline one). After a failure the online
    voices are skipped for 30 s, so with no internet every sentence does not wait for its own timeout."""
    global _edge_down_until, _edge_ok_once
    if time.monotonic() < _edge_down_until or not edge_ready():
        return False
    err = ""
    if not _edge_ok_once:
        timeout = max(timeout, 12.0)
    for attempt in range(2):
        t0 = time.monotonic()
        try:
            if _edge_synth(text, voice, rate, path, timeout) and os.path.getsize(path) > 0:
                _edge_ok_once = True
                return True
            err = "no audio"
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:80]}"
        if time.monotonic() - t0 > 1.5:
            break
        time.sleep(0.2)
    print(f"(voice: online voice failed — {err}; offline voice for 30 s)", flush=True)
    with _edge_lock:
        _edge_down_until = time.monotonic() + 30
    return False


_mac_tamil: str | None = None


def mac_tamil_voice() -> str:
    """The Mac's own Tamil voice (Vani — System Settings → Accessibility → Spoken Content → Manage Voices), or ''."""
    global _mac_tamil
    if _mac_tamil is None:
        _mac_tamil = ""
        if MAC and shutil.which("say"):
            try:
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
                for line in out.splitlines():
                    m = re.match(r"^(.+?)\s+(ta[_-][A-Z]{2})\b", line)
                    if m:
                        _mac_tamil = m.group(1).strip()
                        break
            except Exception:
                pass
    return _mac_tamil


def say_synth(text: str, voice: str, path: str) -> bool:
    if not (MAC and shutil.which("say")):
        return False
    cmd = ["say", "-o", path] + (["-v", voice] if voice else []) + [text]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0 and os.path.getsize(path) > 0
    except Exception:
        return False


_engine = None
_engine_lock = threading.Lock()


def engine():
    """Kokoro (offline fallback), loaded once when it was downloaded earlier."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from kokoro_onnx import Kokoro
            _engine = Kokoro(str(model_dir() / KOKORO_FILES[0]), str(model_dir() / KOKORO_FILES[1]))
        return _engine


def write_wav(path: str, samples, rate: int) -> None:
    import numpy as np
    a = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(a.tobytes())


def kokoro_synth(text: str, kvoice: str, speed: float, path: str) -> bool:
    if not kokoro_ready() and _engine is None:
        return False
    try:
        samples, rate = engine().create(text, voice=kvoice, speed=speed, lang="en-us")
        write_wav(path, samples, rate)
        return True
    except Exception as e:
        print(f"(voice: Kokoro failed: {str(e)[:100]})", flush=True)
        return False


def synth(text: str, voice: str | None, path_stem: str) -> tuple[str, str] | None:
    """One sentence -> (audio file, engine name), trying the chosen online voice, then the offline ones."""
    vid = voice or current()
    v = VOICES[vid]
    rate, kspeed = SPEEDS[current_speed()]
    spoken = dd_thanglish.to_tamil(text) if v["tamil"] else text
    if edge_synth(spoken, v["edge"], rate, path_stem + ".mp3"):
        return path_stem + ".mp3", "edge"
    if v["tamil"] and mac_tamil_voice() and dd_thanglish.has_tamil(spoken) \
            and say_synth(spoken, mac_tamil_voice(), path_stem + ".aiff"):
        return path_stem + ".aiff", "mac-tamil"
    if kokoro_synth(text, v["kokoro"], kspeed, path_stem + ".wav"):
        return path_stem + ".wav", "kokoro"
    return None


def sentences(text: str, max_len: int = 220, first_len: int = 90) -> list[str]:
    """Speakable pieces: sentence by sentence (long ones split at commas; the first piece kept short) so the first
    can play while the rest is fetched."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", text.strip()) if p.strip()]
    out: list[str] = []
    for p in parts:
        limit = first_len if not out else max_len
        while len(p) > limit:
            cut = max(p.rfind(", ", 0, limit), p.rfind(" — ", 0, limit))
            cut = cut if cut > 25 else (p.rfind(" ", 0, limit) if p.rfind(" ", 0, limit) > 25 else limit)
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
            limit = max_len
        if p:
            out.append(p)
    return out


# ---------------- playback ----------------
_player: subprocess.Popen | None = None
_cancel = threading.Event()
last_engine = ""


def stop() -> bool:
    """Barge-in: stop the current sentence and everything queued after it. True if something was playing."""
    _cancel.set()
    p = _player
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:
            return False
        return True
    return False


def _play_file(path: str) -> None:
    global _player
    player = shutil.which("afplay") or shutil.which("ffplay") or shutil.which("mpg123") or shutil.which("aplay")
    if not player:
        return
    extra = ["-nodisp", "-autoexit", "-loglevel", "error"] if player.endswith("ffplay") else \
        (["-q"] if player.endswith("mpg123") else [])
    _player = subprocess.Popen([player, *extra, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _player.wait()
    finally:
        _player = None


def speak(text: str, voice: str | None = None, play=None) -> bool:
    """Say `text` with the chosen voice. False when no neural voice can speak at all (the caller uses the Mac's)."""
    global last_engine
    if not text.strip() or not installed():
        return False
    play = play or _play_file
    _cancel.clear()
    pieces = sentences(text)
    tmp = Path(tempfile.mkdtemp(prefix="dd-voice-"))
    ready: list = [None] * len(pieces)
    done = [threading.Event() for _ in pieces]

    def produce():
        for i, piece in enumerate(pieces):
            if _cancel.is_set():
                break
            try:
                ready[i] = synth(piece, voice, str(tmp / str(i)))
            except Exception as e:
                print(f"(voice: synthesis failed: {str(e)[:100]})", flush=True)
            finally:
                done[i].set()
        for ev in done:
            ev.set()
    threading.Thread(target=produce, daemon=True, name="dd-voice-synth").start()
    spoke = False
    for i in range(len(pieces)):
        done[i].wait(30)
        if _cancel.is_set():
            spoke = True
            break
        if ready[i]:
            last_engine = ready[i][1]
            play(ready[i][0])
            spoke = True
        elif i == 0:
            break  # not even the first sentence: let the caller's voice say it all
    shutil.rmtree(tmp, ignore_errors=True)
    return spoke


def preview(voice: str) -> bool:
    return speak(sample_line(voice), voice)


def save(text: str, out: str, voice: str | None = None) -> str | None:
    """Text -> one audio file (for --say --out and for sending samples)."""
    v = VOICES[voice or current()]
    spoken = dd_thanglish.to_tamil(text) if v["tamil"] else text
    stem = str(Path(out).with_suffix(""))
    if edge_synth(spoken, v["edge"], SPEEDS[current_speed()][0], stem + ".mp3", timeout=20):
        return stem + ".mp3"
    return None


# ---------------- selftest ----------------
def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-voice-test-")
    os.environ["DD_TRANSLIT_LEARN"] = "0"
    assert current() == DEFAULT == "pallavi" and set_voice("valluvar") == "valluvar" and current() == "valluvar"
    settings_file().write_text(json.dumps({"voice": "heart"}))  # an old Kokoro choice
    assert current() == "pallavi"
    try:
        set_voice("robot")
        raise AssertionError("unknown voices must be refused")
    except ValueError:
        pass
    assert set_speed("fast") == "fast" and current_speed() == "fast" and current() == "pallavi"
    assert len(VOICES) == 4 and sum(v["gender"] == "female" for v in VOICES.values()) == 2
    assert sum(v["tamil"] for v in VOICES.values()) == 2
    assert "Naan Pallavi" in sample_line("pallavi") and "I'm Prabhat" in sample_line("prabhat")
    s = sentences("Chrome open pannitten. Innaiku weather nalla irukku! Enna venum? " + "word, " * 60 + "end.")
    assert s[0] == "Chrome open pannitten." and s[2] == "Enna venum?" and all(len(x) <= 221 for x in s), s
    s = sentences("Innaiku Coimbatore la 31 degree, konjam mazhai varum pola irukku, umbrella eduthuttu ponga Dhina.")
    assert len(s[0]) <= 91 and len(s) == 2, s  # a long first sentence is cut early: dd starts talking sooner

    # the pipeline with fake engines + a fake player: every sentence in order, Tamil voices get Tamil script,
    # stop() cancels the rest, a dead online voice falls back to the offline one
    global edge_synth, kokoro_synth
    real_edge, real_kokoro = edge_synth, kokoro_synth
    sent: list[tuple[str, str]] = []

    def fake_edge(text, voice, rate, path, timeout=8.0):
        sent.append((voice, text))
        Path(path).write_bytes(b"mp3")
        return True
    edge_synth = fake_edge
    real_installed = globals()["installed"]
    globals()["installed"] = lambda: True
    try:
        played: list[str] = []
        set_voice("pallavi")
        assert speak("Chrome open pannitten. Innum enna venum? Done.", play=lambda p: played.append(Path(p).name))
        assert played == ["0.mp3", "1.mp3", "2.mp3"], played
        assert sent[0] == ("ta-IN-PallaviNeural", "Chrome open பண்ணிட்டேன்."), sent[0]
        assert sent[1][1] == "இன்னும் என்ன வேணும்?" and last_engine == "edge"
        sent.clear()
        set_voice("prabhat")
        speak("Chrome open pannitten.", play=lambda p: None)
        assert sent == [("en-IN-PrabhatNeural", "Chrome open pannitten.")], sent  # English voices: as written
        played.clear()

        def play_then_stop(p):
            played.append(p)
            stop()
        speak("One. Two. Three.", play=play_then_stop)
        assert len(played) == 1, played
        # online voice down -> Kokoro stand-in of the same gender; nothing at all -> False (the caller speaks)
        edge_synth = lambda *a, **k: False  # noqa: E731
        used: list[str] = []

        def fake_kokoro(text, kvoice, speed, path):
            used.append(kvoice)
            Path(path).write_bytes(b"wav")
            return True
        kokoro_synth = fake_kokoro
        set_voice("valluvar")
        played.clear()
        assert speak("Seri. Ok.", play=lambda p: played.append(Path(p).name)) and played == ["0.wav", "1.wav"]
        assert used == ["hm_omega", "hm_omega"] and last_engine == "kokoro", used
        kokoro_synth = lambda *a, **k: False  # noqa: E731
        globals()["_mac_tamil"] = ""
        assert speak("Seri.", play=lambda p: None) is False
    finally:
        edge_synth, kokoro_synth = real_edge, real_kokoro
        globals()["installed"] = real_installed
        globals()["_mac_tamil"] = None
    print("dd_voice selftest ok (4 voices + speed, Tamil script for Tamil voices, pipelined playback, barge-in, "
          "offline fallback)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--setup" in argv:
        return 0 if setup() else 1
    if "--say" in argv:
        text = argv[argv.index("--say") + 1]
        voice = argv[argv.index("--voice") + 1] if "--voice" in argv else None
        if "--out" in argv:
            out = save(text, argv[argv.index("--out") + 1], voice)
            print(out or "online voice unavailable")
            return 0 if out else 1
        if not speak(text, voice):
            print("no neural voice ready: python3 examples/dd_voice.py --setup")
            return 1
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
