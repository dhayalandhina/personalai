#!/usr/bin/env python3
"""dd_jarvis.py — JARVIS mode for dd: you talk, dd listens, thinks (Ollama + skills), and talks back.
All local, zero cost: ffmpeg mic capture -> faster-whisper STT -> dd agent loop -> macOS `say` (or Piper) TTS.

  python3 examples/dd_jarvis.py                push-to-talk: Enter = start, Enter = stop
  python3 examples/dd_jarvis.py --hands-free   always listening; start with "Jarvis ..." or "dd ..."
  python3 examples/dd_jarvis.py --text         type instead of the mic, dd still speaks
  python3 examples/dd_jarvis.py --mute         mic in, text out (no speech)
  python3 examples/dd_jarvis.py --devices      list microphones (macOS)
  python3 examples/dd_jarvis.py --selftest
Install once:  pip install faster-whisper    (ffmpeg + Ollama already set up)
Env (or ~/.dd/.env): DD_STT_MODEL=base|small (default base), DD_STT_LANG=auto|ta|en (default auto),
                     DD_MIC=0 (avfoundation audio index), DD_VOICE=<say voice>, DD_SAY_RATE=190, DD_WAKE=jarvis,dd
"""
from __future__ import annotations

import array
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)

MAC = platform.system() == "Darwin"
STT_MODEL = os.environ.get("DD_STT_MODEL", "base")
STT_LANG = os.environ.get("DD_STT_LANG", "auto")
WAKE = [w.strip().lower() for w in os.environ.get("DD_WAKE", "jarvis,dd,d d").split(",") if w.strip()]
# Biases Whisper toward romanised Thanglish instead of Tamil script for mixed speech.
THANGLISH_HINT = "Dhina speaking Thanglish: enna pannura, sollu, paaru, innaiku, ollama, github, code fix pannu."
TMP = Path(tempfile.mkdtemp(prefix="dd-jarvis-"))
_whisper = None


# ---------------- mic ----------------
def mic_input() -> list[str]:
    if MAC:
        return ["-f", "avfoundation", "-i", f":{os.environ.get('DD_MIC', '0')}"]
    return ["-f", "pulse", "-i", "default"] if shutil.which("pactl") else ["-f", "alsa", "-i", "default"]


def record_push_to_talk(out: Path) -> Path:
    input("  [Enter] pesa start ... ")
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", *mic_input(), "-ac", "1", "-ar", "16000", str(out)],
                         stdin=subprocess.PIPE)
    try:
        input("  listening... [Enter] stop ")
    finally:
        try:
            p.communicate(b"q", timeout=5)  # 'q' = graceful ffmpeg stop, wav header gets finalised
        except subprocess.TimeoutExpired:
            p.kill()
    return out


def record_seconds(out: Path, secs: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *mic_input(), "-t", str(secs), "-ac", "1", "-ar", "16000", str(out)],
                   stdin=subprocess.DEVNULL, check=False)
    return out


def rms(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return 1.0
            a = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, FileNotFoundError, EOFError):
        return 0.0
    return (sum(x * x for x in a[::4]) / max(1, len(a[::4]))) ** 0.5 if a else 0.0


# ---------------- speech to text ----------------
def transcribe(wav_path: Path) -> str:
    global _whisper
    if rms(wav_path) < 150:  # silence / mic blocked
        return ""
    if _whisper is None:
        from faster_whisper import WhisperModel  # pip install faster-whisper
        _whisper = WhisperModel(STT_MODEL, device="auto", compute_type="int8")
    segs, _ = _whisper.transcribe(str(wav_path), language=None if STT_LANG == "auto" else STT_LANG,
                                  vad_filter=True, initial_prompt=THANGLISH_HINT, beam_size=1)
    return " ".join(s.text.strip() for s in segs).strip()


def split_wake(text: str) -> tuple[bool, str]:
    """('jarvis, innaiku weather') -> (True, 'innaiku weather'); no wake word -> (False, text)."""
    t = text.strip()
    for w in WAKE:
        m = re.match(rf"^\W*(hey\s+|ok\s+|hi\s+)?{re.escape(w)}\b[\s,.!?:-]*", t, flags=re.I)
        if m:
            return True, t[m.end():].strip()
    return False, t


# ---------------- text to speech ----------------
def for_speech(text: str) -> str:
    t = re.sub(r"```[\s\S]*?```", " Code screen la kaattiruken. ", text)
    t = re.sub(r"https?://\S+", " link screen la irukku ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"[*_#>|\[\]{}]", " ", t)
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:900]


def speak(text: str, mute: bool = False) -> None:
    line = for_speech(text)
    if mute or not line:
        return
    if MAC and shutil.which("say"):
        cmd = ["say", "-r", os.environ.get("DD_SAY_RATE", "190")]
        if os.environ.get("DD_VOICE"):
            cmd += ["-v", os.environ["DD_VOICE"]]
        subprocess.run(cmd + [line], check=False)
        return
    wav = TMP / "reply.wav"
    r = dd.run_script("voice", "tts.py", [line, "--out", str(wav)])
    player = shutil.which("afplay") or shutil.which("ffplay") or shutil.which("aplay")
    if r["ok"] and player:
        extra = ["-nodisp", "-autoexit", "-loglevel", "error"] if player.endswith("ffplay") else []
        subprocess.run([player, *extra, str(wav)], check=False)


# ---------------- main loop ----------------
def handle(messages: list[dict], heard: str, mute: bool) -> None:
    print(f"\nyou (voice)> {heard}")
    reply = dd.turn(messages, heard)
    print(f"dd> {reply}")
    speak(reply, mute)
    messages[:] = [messages[0]] + messages[-20:]


def hands_free(messages: list[dict], mute: bool) -> None:
    print(f"Hands-free: '{WAKE[0].title()} ...' nu sollunga. Ctrl-C to stop.")
    while True:
        heard = transcribe(record_seconds(TMP / "chunk.wav", 5))
        if not heard:
            continue
        woke, cmd = split_wake(heard)
        if not woke:
            continue
        if not cmd:
            speak("Haan Dhina, sollunga.", mute)
            cmd = transcribe(record_seconds(TMP / "cmd.wav", 8))
        if cmd:
            handle(messages, cmd, mute)


def push_to_talk(messages: list[dict], mute: bool, typed: bool) -> None:
    print("Push-to-talk mode. Ctrl-C to stop." if not typed else "Text mode (dd speaks back). Ctrl-C to stop.")
    while True:
        if typed:
            heard = input("\nyou> ").strip()
        else:
            heard = transcribe(record_push_to_talk(TMP / "ptt.wav"))
            if not heard:
                print("  (onnum kekkala — mic permission / volume check pannu)")
                continue
        if heard:
            handle(messages, split_wake(heard)[1] or heard, mute)


def check_deps(typed: bool) -> str | None:
    if not typed and not shutil.which("ffmpeg"):
        return "ffmpeg missing: brew install ffmpeg"
    if not typed:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "STT missing: pip3 install faster-whisper   (first run downloads the '%s' model, ~150 MB)" % STT_MODEL
    return dd.preflight()


def selftest() -> int:
    assert split_wake("Jarvis, innaiku weather enna?") == (True, "innaiku weather enna?")
    assert split_wake("hey jarvis") == (True, "")
    assert split_wake("dd status kaattu") == (True, "status kaattu")
    assert split_wake("add this file")[0] is False  # 'dd' inside a word must not wake
    assert split_wake("enna news")[0] is False
    s = for_speech("Done ✅ `ls -la`\n```bash\nrm x\n```\nSee https://ollama.com **now**")
    assert "Code screen la" in s and "link screen la" in s and "```" not in s and "**" not in s, s
    silent = TMP / "silent.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x00\x00" * 16000)
    assert transcribe(silent) == ""  # silence never reaches Whisper
    loud = TMP / "loud.wav"
    with wave.open(str(loud), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(array.array("h", [8000 if (i // 20) % 2 else -8000 for i in range(16000)]).tobytes())
    assert rms(loud) > 1000
    # full voice turn with stubbed STT/model/TTS: heard text -> agent -> tool -> spoken reply
    os.environ.setdefault("DD_HOME", str(TMP / "home"))
    dd.run_script("rules-guard", "rules_guard.py", ["init"])
    spoken: list[str] = []
    global speak
    real_speak, speak = speak, (lambda t, m=False: spoken.append(for_speech(t)))
    seq = iter([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "echo 42"}}}]},
                {"role": "assistant", "content": "Answer `42` dhaan."}])
    real_turn = dd.turn
    dd.turn = lambda m, u: real_turn(m, u, lambda mm, tt: next(seq))
    msgs = [{"role": "system", "content": "s"}]
    handle(msgs, "jarvis answer enna", mute=False)
    dd.turn, speak = real_turn, real_speak
    assert spoken == ["Answer 42 dhaan."], spoken
    assert any(m.get("role") == "tool" and "42" in m["content"] for m in msgs)
    print("dd_jarvis selftest ok (wake word, speech cleanup, silence gate, voice turn)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--devices" in argv:
        subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
        return 0
    typed, mute = "--text" in argv, "--mute" in argv
    problem = check_deps(typed)
    if problem:
        print(problem)
        return 1
    messages = [{"role": "system", "content": dd.boot_system_prompt() +
                 "\n\nVOICE MODE: your reply is spoken aloud. 1-2 short sentences, no tables, no lists; "
                 "put code only when asked (it is shown, not read)."}]
    print(f"JARVIS mode — dd ({dd.MODEL}), STT whisper-{STT_MODEL}, TTS {'say' if MAC else 'piper/espeak'}")
    speak("Vanakkam Dhina. dd ready.", mute)
    try:
        hands_free(messages, mute) if "--hands-free" in argv else push_to_talk(messages, mute, typed)
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
