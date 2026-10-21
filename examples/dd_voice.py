#!/usr/bin/env python3
"""dd_voice.py — dd's speaking voice: six natural neural voices (Kokoro-82M, free, offline) you pick from the menu.

  Heart   — female, warm (default)        Michael — male, calm
  Bella   — female, bright                George  — male, British
  Alpha   — female, Indian accent         Omega   — male, Indian accent

Kokoro runs locally through onnxruntime (int8 model, ~120 MB with the voices, fetched once into
~/.dd/models/kokoro). Replies are spoken sentence by sentence: the next sentence is synthesised while the
current one plays, so dd starts talking quickly. Until Kokoro is ready (first run) the Mac's own voice speaks.
The choice lives in ~/.dd/voice.json.

  python3 examples/dd_voice.py --setup          install + download (the app does this by itself)
  python3 examples/dd_voice.py --say "text" [--voice heart]
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

MAC = sys.platform == "darwin"
VOICES: dict[str, dict] = {  # id -> Kokoro voice, phonemiser language, label, gender
    "heart": {"kokoro": "af_heart", "lang": "en-us", "label": "Heart — female, warm", "gender": "female"},
    "bella": {"kokoro": "af_bella", "lang": "en-us", "label": "Bella — female, bright", "gender": "female"},
    "alpha": {"kokoro": "hf_alpha", "lang": "en-us", "label": "Alpha — female, Indian accent", "gender": "female"},
    "michael": {"kokoro": "am_michael", "lang": "en-us", "label": "Michael — male, calm", "gender": "male"},
    "george": {"kokoro": "bm_george", "lang": "en-gb", "label": "George — male, British", "gender": "male"},
    "omega": {"kokoro": "hm_omega", "lang": "en-us", "label": "Omega — male, Indian accent", "gender": "male"},
}
DEFAULT = "heart"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
SAMPLE = "Vanakkam Dhina! Naan {name}. Chrome open pannitten — innum enna venum?"


def home() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd"))


def model_dir() -> Path:
    return home() / "models" / "kokoro"


def settings_file() -> Path:
    return home() / "voice.json"


def current() -> str:
    try:
        v = json.loads(settings_file().read_text()).get("voice", DEFAULT)
    except (OSError, ValueError):
        v = DEFAULT
    return v if v in VOICES else DEFAULT


def speed() -> float:
    try:
        return max(0.7, min(1.4, float(json.loads(settings_file().read_text()).get("speed", 1.0))))
    except (OSError, ValueError, TypeError):
        return 1.0


def set_voice(voice: str) -> str:
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}: {', '.join(VOICES)}")
    try:
        data = json.loads(settings_file().read_text())
    except (OSError, ValueError):
        data = {}
    data["voice"] = voice
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(json.dumps(data))
    return voice


# ---------------- setup ----------------
def _fetch(url: str, dest: Path) -> None:
    """Download with certifi's CA bundle (python.org Pythons ship without certificates), else curl."""
    import ssl
    import urllib.request
    part = Path(str(dest) + ".part")
    try:
        ctx = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        with urllib.request.urlopen(url, timeout=120, context=ctx) as r, open(part, "wb") as out:
            shutil.copyfileobj(r, out, 1 << 20)
    except Exception:
        if not shutil.which("curl"):
            raise
        subprocess.run(["curl", "-fsSL", "--retry", "3", "--max-time", "1800", "-o", str(part), url], check=True)
    if part.stat().st_size < 1_000_000:
        part.unlink()
        raise RuntimeError(f"{dest.name}: download incomplete")
    os.replace(part, dest)


def installed() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:
        return False
    return all((model_dir() / f).is_file() for f in FILES)


def setup(log=print) -> bool:
    """pip install kokoro-onnx (once) and fetch the model + voices (~120 MB, once). True when ready."""
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:
        log("voice: installing kokoro-onnx (one-time)...")
        for extra in ([], ["--break-system-packages"], ["--user"]):
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, "kokoro-onnx"],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode == 0:
                break
        else:
            log(f"voice: kokoro-onnx install failed: {(r.stderr or '')[-200:]} — Mac voice stays")
            return False
    model_dir().mkdir(parents=True, exist_ok=True)
    for f in FILES:
        dest = model_dir() / f
        if not dest.is_file():
            log(f"voice: downloading {f} (one-time)...")
            try:
                _fetch(RELEASE + f, dest)
            except Exception as e:
                log(f"voice: {f} download failed ({str(e)[:120]}) — Mac voice stays")
                return False
    return installed()


# ---------------- synthesis ----------------
_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from kokoro_onnx import Kokoro
            _engine = Kokoro(str(model_dir() / FILES[0]), str(model_dir() / FILES[1]))
        return _engine


def sentences(text: str, max_len: int = 220) -> list[str]:
    """Speakable pieces: sentence by sentence (long ones split at commas) so the first can play while the rest
    is synthesised."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    out = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(", ", 0, max_len)
            cut = cut if cut > 40 else max_len
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out


def synth(text: str, voice: str | None = None) -> tuple:
    v = VOICES[voice or current()]
    samples, rate = engine().create(text, voice=v["kokoro"], speed=speed(), lang=v["lang"])
    return samples, rate


def write_wav(path: str, samples, rate: int) -> None:
    import numpy as np
    a = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(a.tobytes())


# ---------------- playback ----------------
_player: subprocess.Popen | None = None
_cancel = threading.Event()


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
    player = shutil.which("afplay") or shutil.which("ffplay") or shutil.which("aplay")
    if not player:
        return
    extra = ["-nodisp", "-autoexit", "-loglevel", "error"] if player.endswith("ffplay") else []
    _player = subprocess.Popen([player, *extra, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _player.wait()
    finally:
        _player = None


def speak(text: str, voice: str | None = None, play=None) -> bool:
    """Say `text` with the chosen neural voice. False when Kokoro is not ready (the caller falls back)."""
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
                samples, rate = synth(piece, voice)
                path = str(tmp / f"{i}.wav")
                write_wav(path, samples, rate)
                ready[i] = path
            except Exception as e:
                print(f"(voice: synthesis failed: {str(e)[:100]})", flush=True)
            finally:
                done[i].set()
        for ev in done:
            ev.set()
    threading.Thread(target=produce, daemon=True, name="dd-voice-synth").start()
    for i in range(len(pieces)):
        done[i].wait(60)
        if _cancel.is_set():
            break
        if ready[i]:
            play(ready[i])
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def preview(voice: str) -> bool:
    return speak(SAMPLE.format(name=VOICES[voice]["label"].split(" —")[0]), voice)


# ---------------- selftest ----------------
def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-voice-test-")
    assert current() == DEFAULT and set_voice("omega") == "omega" and current() == "omega"
    try:
        set_voice("robot")
        raise AssertionError("unknown voices must be refused")
    except ValueError:
        pass
    assert len(VOICES) == 6 and sum(v["gender"] == "female" for v in VOICES.values()) == 3
    s = sentences("Chrome open pannitten. Innaiku weather nalla irukku! Enna venum? " + "word, " * 60 + "end.")
    assert s[0] == "Chrome open pannitten." and s[2] == "Enna venum?" and all(len(x) <= 221 for x in s), s
    assert speak("hello") is False or installed(), "not installed -> False, the caller falls back"
    # the pipeline with a fake engine + fake player: every sentence played in order; stop() cancels the rest
    global _engine
    import numpy as np

    class FakeEngine:
        def create(self, text, voice, speed, lang):
            return np.zeros(2400, dtype=np.float32), 24000
    _engine = FakeEngine()
    md = model_dir()
    md.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        (md / f).write_bytes(b"x")
    real_installed = globals()["installed"]
    globals()["installed"] = lambda: True
    try:
        played: list[str] = []
        assert speak("One. Two. Three.", play=lambda p: played.append(Path(p).name))
        assert played == ["0.wav", "1.wav", "2.wav"], played
        played.clear()

        def play_then_stop(p):
            played.append(p)
            stop()
        speak("One. Two. Three.", play=play_then_stop)
        assert len(played) == 1, played
    finally:
        globals()["installed"] = real_installed
        _engine = None
    print("dd_voice selftest ok (6 voices, settings, sentence split, pipelined playback, barge-in stop)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--setup" in argv:
        return 0 if setup() else 1
    if "--say" in argv:
        text = argv[argv.index("--say") + 1]
        voice = argv[argv.index("--voice") + 1] if "--voice" in argv else None
        if not speak(text, voice):
            print("kokoro not ready: python3 examples/dd_voice.py --setup")
            return 1
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
