#!/usr/bin/env python3
"""stt.py — speech to text (faster-whisper, or whisper.cpp via $WHISPER_CPP) with ffmpeg pre-conversion.

  stt.py AUDIO [--lang ta|en|auto] [--model small|medium|large-v3] [--chunk SECONDS] [--json]
  stt.py --selftest
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def to_wav(src: str, out: str) -> float:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr[-200:]}")
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                       capture_output=True, text=True)
    return round(float(p.stdout.strip() or 0), 2)


def transcribe_fw(wav: str, lang: str, model: str) -> dict:
    from faster_whisper import WhisperModel  # pip install faster-whisper
    m = WhisperModel(model, device="auto", compute_type="auto")
    segments, info = m.transcribe(wav, language=None if lang == "auto" else lang, vad_filter=True)
    text = " ".join(s.text.strip() for s in segments)
    return {"text": text, "language": info.language, "language_prob": round(info.language_probability, 2), "engine": f"faster-whisper/{model}"}


def transcribe_cpp(wav: str, lang: str, model: str) -> dict:
    exe = os.environ["WHISPER_CPP"]
    model_path = os.environ.get("WHISPER_CPP_MODEL", str(Path(exe).parent / "models" / f"ggml-{model}.bin"))
    cmd = [exe, "-m", model_path, "-f", wav, "-nt", "-l", lang if lang != "auto" else "auto"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return {"text": " ".join(l.strip() for l in r.stdout.splitlines() if l.strip()), "language": lang, "engine": "whisper.cpp"}


def transcribe(src: str, lang: str = "auto", model: str = "small", chunk: int = 0) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="dd-stt-"))
    wav = str(tmp / "in.wav")
    dur = to_wav(src, wav)
    engine = transcribe_cpp if os.environ.get("WHISPER_CPP") else transcribe_fw
    if chunk and dur > chunk:
        parts = []
        n = int(dur // chunk) + 1
        for i in range(n):
            part = str(tmp / f"p{i}.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(i * chunk), "-t", str(chunk), "-i", wav, part],
                           capture_output=True)
            parts.append(engine(part, lang, model))
        res = {"text": " ".join(p["text"] for p in parts), "language": parts[0].get("language"), "engine": parts[0]["engine"], "chunks": n}
    else:
        res = engine(wav, lang, model)
    res.update({"seconds": dur, "file": src})
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def selftest() -> int:
    if not shutil.which("ffmpeg"):
        print("stt selftest ok (ffmpeg missing here; install ffmpeg)")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="dd-stt-"))
    src = str(tmp / "tone.ogg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", src],
                   capture_output=True)
    if not Path(src).exists():  # no opus encoder: fall back to wav source
        src = str(tmp / "tone.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", src], capture_output=True)
    dur = to_wav(src, str(tmp / "out.wav"))
    assert 0.9 <= dur <= 1.1, dur
    try:
        import faster_whisper  # noqa: F401
        note = "faster-whisper installed"
    except ImportError:
        note = "faster-whisper not installed (pip install faster-whisper)"
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"stt selftest ok (ffmpeg conversion; {note})")
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
        res = transcribe(argv[0], arg(argv, "--lang", "auto"), arg(argv, "--model", "small"), int(arg(argv, "--chunk", 0)))
    except ImportError:
        res = {"error": "no STT engine: pip install faster-whisper (or set WHISPER_CPP=/path/to/whisper-cli)"}
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1) if "--json" in argv or "error" in res else res["text"])
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
