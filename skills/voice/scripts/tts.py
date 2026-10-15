#!/usr/bin/env python3
"""tts.py — text to speech with Piper (local, free); espeak-ng fallback; ffmpeg for opus voice notes.

  tts.py "text" --out speech.wav [--voice en_US-lessac-medium] [--rate 1.0] [--ogg]
  tts.py --selftest
Voices dir: $DD_HOME/voices (model.onnx + model.onnx.json). Env PIPER_VOICE sets the default voice name.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def voices_dir() -> Path:
    d = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_voice(name: str) -> Path | None:
    for cand in (voices_dir() / f"{name}.onnx", Path(name)):
        if cand.suffix == ".onnx" and cand.exists():
            return cand
    return None


def piper(text: str, out: str, voice: str, rate: float) -> str:
    model = find_voice(voice)
    if model is None:
        raise FileNotFoundError(f"voice '{voice}' not found in {voices_dir()} — download .onnx + .onnx.json from the Piper voices repo")
    exe = shutil.which("piper")
    cmd = [exe, "--model", str(model), "--output_file", out, "--length_scale", str(1 / rate)] if exe else \
          [sys.executable, "-m", "piper", "--model", str(model), "--output_file", out, "--length_scale", str(1 / rate)]
    r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"piper: {r.stderr[-200:]}")
    return "piper"


def espeak(text: str, out: str, rate: float) -> str:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise FileNotFoundError("no TTS engine: pip install piper-tts (and a voice) or apt install espeak-ng")
    r = subprocess.run([exe, "-s", str(int(165 * rate)), "-w", out, text], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return "espeak-ng"


def to_ogg(wav: str, ogg: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-c:a", "libopus", "-b:a", "48k", "-ac", "1", ogg],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg opus: {r.stderr[-200:]}")


def speak(text: str, out: str, voice: str | None = None, rate: float = 1.0, ogg: bool = False) -> dict:
    voice = voice or os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    wav = out if out.endswith(".wav") else str(Path(out).with_suffix(".wav"))
    try:
        engine = piper(text, wav, voice, rate)
    except FileNotFoundError:
        engine = espeak(text, wav, rate)
    res = {"ok": True, "engine": engine, "path": wav, "chars": len(text)}
    if ogg:
        o = str(Path(wav).with_suffix(".ogg"))
        to_ogg(wav, o)
        res["ogg"] = o
    return res


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dd-tts-"))
    try:
        r = speak("dd voice test", str(tmp / "t.wav"))
        note = f"engine {r['engine']} works"
        if shutil.which("ffmpeg"):
            to_ogg(r["path"], str(tmp / "t.ogg"))
            note += ", opus ok"
    except FileNotFoundError as e:
        note = f"no engine on this machine ({str(e)[:60]}...) — install piper-tts + a voice"
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"tts selftest ok ({note})")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    try:
        res = speak(argv[0], arg(argv, "--out", "speech.wav"), arg(argv, "--voice"), float(arg(argv, "--rate", 1.0)), "--ogg" in argv)
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
