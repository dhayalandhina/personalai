#!/usr/bin/env python3
"""dd_listen.py — dd's always-on ears. Streams the mic continuously (no fixed chunks, no gaps), detects when
you start and stop speaking, transcribes that utterance, and acts when it begins with "dd" / "jarvis".

  "dd, chrome open pannu"      -> one utterance, done at once (common Mac actions never touch the LLM)
  "dd"  ...  "volume 30"       -> "Sollu." then the next thing you say is the command
  after any reply, 8 s follow-up window: speak again without the wake word

Robust to how Whisper hears "dd" (D.D., didi, dee dee…), ignores its own voice while speaking, drops Whisper's
noise hallucinations ("Thank you."), shows what it heard and how long each step took, and tells you (and opens
System Settings) if the mic is silent or blocked. The menu-bar app (dd_app.py) runs this in a thread.

  python3 examples/dd_listen.py            run in the terminal (prints what it hears)
  python3 examples/dd_listen.py --devices  list microphones
  python3 examples/dd_listen.py --selftest
Env: DD_MIC (avfoundation index/name; auto-picks the built-in mic), DD_STT_MODEL (base), DD_STT_LANG (auto),
     DD_WAKE (comma list), DD_FOLLOWUP (8 s), DD_VOICE / DD_SAY_RATE (speech), DD_MUTE=1.
"""
from __future__ import annotations

import array
import difflib
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000  # 640 bytes = 20 ms of 16-bit mono
MAC = platform.system() == "Darwin"
HINT = "Jarvis. dd. Chrome open pannu. Volume thirty. Innaiku weather enna? Screenshot edu."  # (never dd's own replies)
VOICE_SUFFIX = ("\n\nVOICE MODE: your reply is spoken aloud. 1-2 short sentences, no tables, no lists; "
                "put code only when asked (it is shown, not read).")
# How Whisper tends to write a spoken "dd" / "jarvis". Compared after lower-casing and dropping punctuation/spaces.
WAKE_FORMS = {"dd", "ddd", "d", "didi", "deedee", "dede", "dedi", "didee", "teedee", "jarvis", "jarviz", "jarvi",
              "jarvice", "jarvies", "jervis", "jarves", "javis"}
LEAD_SKIP = {"hey", "ok", "okay", "hi", "hello", "so", "yo", "the", "a", "ey", "hey,"}
# Things Whisper invents from silence / noise — never treat as a command.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "subtitles by",
                  "the end", "so", "okay", "oh", "hmm", "uh", "um", "i", "a", "sollu", "up", "ah", "bump",
                  "jarvis dd", "dd jarvis"}  # the last ones: prompt words Whisper echoes on noise


def rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    a = array.array("h", frame[: len(frame) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


class AutoGain:
    """Software gain for quiet inputs. Some Macs' raw microphone delivers speech at ~150-250 RMS (the wake model and
    the segmenter expect ~1000-4000). The gain follows the loudest recent sound: it drops *at once* when a louder
    sound arrives (so the first syllable of "Jarvis" is never clipped — the old smooth attack distorted it after a
    quiet minute), rises slowly in quiet (~6 s to double), stays within 1x..MAX_GAIN, and a limiter keeps every
    frame below full scale."""
    TARGET, MAX_GAIN, PEAK = 2000.0, 8.0, 28000.0

    def __init__(self, enabled: bool | None = None):
        self.enabled = (os.environ.get("DD_AUTOGAIN", "1") != "0") if enabled is None else enabled
        self.env = 0.0    # decaying peak of the raw frame RMS (~10 s memory)
        self.gain = 1.0
        self.limited = 0  # frames the limiter had to pull down (diagnostics)

    def feed(self, frame: bytes, learn: bool = True) -> bytes:
        if not self.enabled:
            return frame
        if learn:  # (while dd speaks: apply the current gain, learn nothing from its own voice)
            e = rms(frame)
            self.env = max(e, self.env * 0.998)
            if self.env > 40:  # only once there is a real signal to judge by
                want = max(1.0, min(self.MAX_GAIN, self.TARGET / self.env))
                self.gain = want if want < self.gain else self.gain + (want - self.gain) * 0.0025
        if self.gain <= 1.02:
            return frame
        try:
            import numpy as np
            a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            g = self.gain
            peak = float(np.abs(a).max()) if len(a) else 0.0
            if peak * g > self.PEAK:  # limiter: this frame alone would clip
                g = self.PEAK / peak
                self.limited += 1
            return (a * g).astype(np.int16).tobytes()
        except Exception:
            return frame


class Segmenter:
    """Energy-based voice activity: feed 20 ms frames; returns one utterance (PCM bytes) when the speaker stops.
    Thresholds adapt to the room: the start level sits well above the measured noise floor, and "silence" means
    back down near that floor — so a fan or AC never keeps an utterance open until the max window."""

    def __init__(self, min_start: float | None = None, end_silence_ms: int = 600, min_speech_ms: int = 250,
                 max_ms: int = 8000, pre_roll_ms: int = 300):
        # DD_VAD_START: absolute minimum RMS that counts as speech (the adaptive floor usually sets it higher)
        self.min_start = float(os.environ.get("DD_VAD_START", "300")) if min_start is None else min_start
        self.end_silence_ms, self.min_speech_ms, self.max_ms = end_silence_ms, min_speech_ms, max_ms
        self.idle: deque[float] = deque(maxlen=150)  # last 3 s of idle levels -> noise floor (median) and p90
        self.floor, self.p90 = 100.0, 140.0
        self.pre: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // FRAME_MS))
        self.reset()

    def reset(self) -> None:
        self.speaking, self.frames, self.silence, self.speech_frames, self.loud_run = False, [], 0, 0, 0
        self.energy_sum = 0.0
        self.last_forced = False  # True when the last utterance hit max_ms (continuous audio: a video, music, a chat)

    @property
    def noise(self) -> float:
        return self.floor

    def _idle_level(self, e: float) -> None:
        self.idle.append(e)
        if len(self.idle) >= 25:
            s = sorted(self.idle)
            self.floor, self.p90 = s[len(s) // 2], s[int(len(s) * 0.9)]

    def threshold(self) -> float:  # speech starts above this
        return max(self.min_start, self.floor * 3.0, self.p90 * 1.4)

    def end_threshold(self) -> float:  # ... and has stopped when it stays below this
        return max(self.floor * 1.5, self.threshold() * 0.4)

    def feed(self, frame: bytes) -> bytes | None:
        e = rms(frame)
        thr = self.threshold()
        if not self.speaking:
            if e < thr:
                self._idle_level(e)
                self.loud_run = 0
            else:
                self.loud_run += 1
            self.pre.append(frame)
            if self.loud_run >= 4:  # 80 ms of sound = speech started; keep the pre-roll so the first syllable survives
                self.speaking, self.frames, self.silence, self.speech_frames = True, list(self.pre), 0, self.loud_run
                self.energy_sum = 0.0
            return None
        self.frames.append(frame)
        self.energy_sum += e
        if e >= self.end_threshold():
            self.silence, self.speech_frames = 0, self.speech_frames + 1
        else:
            self.silence += 1
        forced = len(self.frames) * FRAME_MS >= self.max_ms
        if self.silence * FRAME_MS < self.end_silence_ms and not forced:
            return None
        utt, spoken = b"".join(self.frames), self.speech_frames * FRAME_MS
        if forced:  # steady noise kept it "speaking" for the whole window: treat that level as room noise from now on
            avg = self.energy_sum / max(1, len(self.frames))
            for _ in range(15):  # enough to lift p90 for ~3 s; forgotten once the room is quiet again
                self._idle_level(avg)
        self.reset()
        self.last_forced = forced
        return utt if spoken >= self.min_speech_ms else None  # a click or cough is not an utterance


def wake_words() -> set[str]:
    extra = {re.sub(r"[^a-z0-9]", "", w.lower()) for w in os.environ.get("DD_WAKE", "").split(",") if w.strip()}
    return WAKE_FORMS | extra


def _is_wake(cand: str, forms: set[str], loose: bool = False) -> bool:
    """loose=True also accepts garbled repeats of the wake word ("gavis", "narvis", "jarvi") after a real one."""
    c = re.sub(r"[^a-z0-9]", "", cand)
    if not c:
        return False
    if c in forms:
        return True
    if len(c) < 4:
        return False
    if len(c) <= 6 and re.fullmatch(r"d[eiy]+[dt][eiy]+", c):
        return True  # "diti", "dedee", "didy": Whisper's spellings of a spoken "dee dee" (not data/deed/daddy)
    r = difflib.SequenceMatcher(None, c, "jarvis").ratio()
    return r >= 0.75 or (loose and len(c) <= 8 and r >= 0.6)


_DD_PAIRS = {"dg", "gd", "dt", "td", "bd", "db", "pd", "dp", "vd", "dv", "zd", "dz"}


def spelled_dd(text: str) -> bool:
    """Only after the wake head already fired: Whisper often writes a spoken 'dee dee' as two letter names
    ('HDD', 'D.G.', 'D.T.', 'E-D D') — accept those spellings, but never real words (add, CD, ed)."""
    toks = [re.sub(r"[^a-z]", "", raw) for raw in re.split(r"\s+", text.lower())]
    toks = [t for t in toks if t]
    for i, t in enumerate(toks):
        if t == "add":
            continue
        if (t.count("d") >= 2 and len(t) <= 4) or t in _DD_PAIRS:
            return True
        if t == "d" and i + 1 < len(toks) and toks[i + 1] == "d":  # "D D"
            return True
    return False


def parse_wake(text: str) -> tuple[bool, str]:
    """('D.D. open chrome') -> (True, 'open chrome'); ('enna rvis, open chrome') -> (True, 'open chrome');
    ('address check') -> (False, 'address check'). The wake word may sit anywhere in the first four words."""
    words = re.findall(r"[\w']+", text.lower())
    forms = wake_words()
    for i in range(min(len(words), 4)):
        for n in (3, 2, 1):  # longest first: "D.D." is the two words "d d" -> "dd", not a lone "d"
            if i + n > len(words) or not _is_wake("".join(words[i:i + n]), forms):
                continue
            rest = words[i + n:]
            while rest and (_is_wake(rest[0], forms, loose=True) or rest[0] in LEAD_SKIP):  # "dd jarvis gavis narvis ..."
                rest = rest[1:]
            return True, " ".join(rest).strip()
    return False, text.strip()


def _norm_sentences(text: str) -> list[str]:
    return [re.sub(r"[^a-z0-9 ]", "", p.lower()).strip() for p in re.split(r"[.?!,;]+", text) if p.strip()]


HINT_SENTENCES = set(_norm_sentences(HINT))


def prompt_echo(text: str) -> bool:
    """Whisper repeats its initial prompt on noise/music ('Innaiku weather enna. Screenshot edu.'): a transcript made
    only of prompt sentences is an echo, not a command (a real 'innaiku weather enna' alone is still accepted)."""
    parts = [p for p in _norm_sentences(text) if p]
    if any(p in ("jarvis", "dd", "hey jarvis") for p in parts):  # "Jarvis. Chrome open pannu." is a real command
        return False
    return len(parts) >= 2 and all(p in HINT_SENTENCES for p in parts)


def blocked(text: str) -> bool:
    t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    if not re.search(r"[a-z]", t) or t in HALLUCINATIONS or prompt_echo(text):
        return True
    words = t.split()
    return len(words) >= 3 and len(set(words)) == 1  # "bump bump bump bump": a loop Whisper invents from noise


def pick_mic(listing: str) -> str:
    """Choose the built-in microphone from `ffmpeg -f avfoundation -list_devices true -i ""` output."""
    if os.environ.get("DD_MIC"):
        return os.environ["DD_MIC"]
    audio, seen_audio = [], False
    for line in listing.splitlines():
        if "audio devices" in line.lower():
            seen_audio = True
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if seen_audio and m:
            audio.append((m.group(1), m.group(2)))
    for idx, name in audio:
        if re.search(r"macbook|built-?in|internal", name, re.I) and re.search(r"mic", name, re.I):
            return idx
    for idx, name in audio:
        if re.search(r"mic", name, re.I):
            return idx
    return audio[0][0] if audio else "0"


def list_devices() -> str:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True)
    return r.stderr + r.stdout


_children: list = []  # mic processes (ffmpeg / dd-mic) — killed on stop/quit so the mic is released


def kill_children() -> None:
    for p in list(_children):
        try:
            p.kill()
        except Exception:
            pass
    _children.clear()


def ffmpeg_source():
    """Yields 20 ms PCM frames from the microphone until ffmpeg stops (then raises with its last error)."""
    if MAC:
        inp = ["-f", "avfoundation", "-i", f":{pick_mic(list_devices())}"]
    else:
        inp = ["-f", "pulse", "-i", "default"] if shutil.which("pactl") else ["-f", "alsa", "-i", "default"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *inp, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    _children.append(p)
    try:
        while True:
            frame = p.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                break
            yield frame
    finally:
        p.kill()
    err = (p.stderr.read() or b"").decode(errors="ignore").strip()
    raise RuntimeError(err[-300:] or "mic stream ended")


def helper_binary() -> str:
    return os.environ.get("DD_MIC_BIN") or str(Path.home() / ".dd" / "bin" / "dd-mic")


_helper_failures = 0
HELPER_STALL_S = 6.0  # no bytes at all from dd-mic for this long -> it is stuck: ffmpeg instead
SILENCE_FALLBACK_MS = 8000  # dd-mic gave nothing but digital zeros for this long since start -> ffmpeg instead (the helper itself cascades through its modes within that)


def choose_source() -> tuple[str, str | None]:
    """('helper', path) when the echo-cancelling dd-mic helper is built and allowed, else ('ffmpeg', None).
    mic_mode 5 = this Mac's helper gave no audio at all last time: ffmpeg straight away (reset on a rebuild)."""
    b = helper_binary()
    if os.environ.get("DD_AEC", "1") != "0" and _helper_failures < 2 and os.access(b, os.X_OK) \
            and load_mic_mode() != 5:
        return "helper", b
    return "ffmpeg", None


def _relay_stderr(p, tag: str, sink: list, log=print) -> None:
    """Log a child's stderr lines as they come (its diagnostics: permission, device, format), keep the last few."""
    try:
        for raw in iter(p.stderr.readline, b""):
            line = raw.decode(errors="ignore").rstrip()
            if line:
                sink.append(line)
                del sink[:-6]
                log(f"  {tag}: {line}")
    except Exception:
        pass


class HelperSilent(RuntimeError):
    """dd-mic exited with code 7: that voice-processing wiring only gives digital zeros on this Mac."""

    def __init__(self, mode: int):
        super().__init__(f"dd-mic mode {mode} silent")
        self.mode = mode


def mic_mode_file() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "mic_mode"


def load_mic_mode() -> int:
    try:
        return max(0, min(5, int(mic_mode_file().read_text().strip())))
    except (OSError, ValueError):
        return 0


def save_mic_mode(mode: int) -> None:
    try:
        mic_mode_file().parent.mkdir(parents=True, exist_ok=True)
        mic_mode_file().write_text(str(mode))
    except OSError:
        pass


def helper_source(binary: str, log=print, mode: int = 0):
    """Frames from dd-mic (AVAudioEngine voice processing: cancels the Mac's own playback). Same pipe protocol.
    mode: which voice-processing wiring to start with (see dd-mic.swift); exit code 7 -> HelperSilent.
    Digital silence from the first frame on (wrong input device, no permission, voice processing muted) is
    detected within SILENCE_FALLBACK_MS and the helper is given up for this run -> ffmpeg takes over."""
    global _helper_failures
    started = time.monotonic()
    p = subprocess.Popen([binary, "--mode", str(mode)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL)
    _children.append(p)
    said: list[str] = []
    relay = threading.Thread(target=_relay_stderr, args=(p, "dd-mic", said, log), daemon=True)
    relay.start()
    import queue as _queue
    frames_q: _queue.Queue = _queue.Queue(maxsize=500)

    def pump():  # blocking pipe reads on their own thread, so a helper that hangs cannot freeze the listener
        try:
            while True:
                b = p.stdout.read(FRAME_BYTES)
                frames_q.put(b)
                if len(b) < FRAME_BYTES:
                    return
        except Exception:
            frames_q.put(b"")
    threading.Thread(target=pump, daemon=True, name="dd-mic-pump").start()
    got = zeros = 0
    silent = saved = stalled = False
    try:
        while True:
            try:
                frame = frames_q.get(timeout=HELPER_STALL_S)
            except _queue.Empty:
                stalled = True  # started but no audio callbacks at all (seen after voice-processing attempts)
                break
            if len(frame) < FRAME_BYTES:
                break
            got += 1
            if frame.count(b"\x00") == len(frame):
                zeros += 1
                if zeros == got and zeros * FRAME_MS >= SILENCE_FALLBACK_MS:
                    silent = True
                    break
            else:
                zeros = 0
                if not saved and got >= 1500:  # 30 s of real audio: this wiring works on this Mac — remember it
                    saved = True
                    used = next((int(m.group(1)) for line in said if (m := re.search(r"\[mode (\d)\]", line))), mode)
                    save_mic_mode(used)
            yield frame
    finally:
        try:
            code = p.wait(timeout=0.3)
        except Exception:
            code = None
        p.kill()
        try:
            p.wait(timeout=2)
        except Exception:
            pass
        relay.join(timeout=1)
    if code == 7:
        m = next((int(x.group(1)) for line in said if (x := re.search(r"silent in mode (\d)", line))), mode)
        raise HelperSilent(m)
    if stalled or code == 8:  # (8: the helper's own watchdog — no audio callbacks)
        _helper_failures = 2  # give the helper up for this run: ffmpeg takes over at once
        raise RuntimeError(f"dd-mic delivered no audio for {HELPER_STALL_S:.0f} s")
    if silent:
        _helper_failures = 2
        raise RuntimeError("dd-mic hears nothing (digital silence since start — input device / permission)")
    if time.monotonic() - started < 3 or got == 0:  # died at once: count it; after two, fall back to ffmpeg
        _helper_failures += 1
    raise RuntimeError(f"dd-mic ended: {' | '.join(said[-2:]) or 'no output'}")


def mic_source(log=print):
    kind, path = choose_source()
    if kind == "helper":
        mode = min(4, load_mic_mode())
        log(f"  mic: dd-mic helper (echo cancellation) {path} — mode {mode}")
        try:
            while True:
                try:
                    yield from helper_source(path, log=log, mode=mode)
                    return
                except HelperSilent as e:  # that wiring gives only zeros here: next one, in a fresh process
                    mode = min(4, e.mode + 1)
                    save_mic_mode(mode)
                    log(f"  mic: dd-mic mode {e.mode} silent on this Mac -> mode {mode}")
        except RuntimeError as e:
            if _helper_failures < 2:
                raise  # it worked and then died: the listener restarts the stream
            log(f"  mic: {e} -> ffmpeg avfoundation instead (no echo cancellation)")
            if "no audio" in str(e):
                save_mic_mode(5)  # next start: ffmpeg straight away
    else:
        log("  mic: ffmpeg avfoundation (no echo cancellation — build dd-mic for playback-proof listening)")
    yield from ffmpeg_source()


# ---------------- speech to text ----------------
_whisper = None
_whisper_lock = threading.Lock()


def whisper_cached(name: str, hub: Path | None = None) -> bool:
    """True when faster-whisper's files for `name` are already in the Hugging Face cache (no download needed)."""
    if hub is None:
        try:
            from huggingface_hub.constants import HF_HUB_CACHE
            hub = Path(HF_HUB_CACHE)
        except Exception:
            hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    repo = hub / f"models--Systran--faster-whisper-{name}"
    return any((repo / "snapshots").glob("*/model.bin")) if repo.is_dir() else False


def default_stt_model() -> str:
    """small is clearly better for Thanglish, but on an 8 GB Mac next to Ollama it starves (10-20 s per command):
    base there, small from 16 GB. DD_STT_MODEL overrides."""
    return os.environ.get("DD_STT_MODEL") or ("small" if ram_gb() >= 12 else "base")


def _load(name: str):
    from faster_whisper import WhisperModel  # pip install faster-whisper
    # already downloaded -> never touch the hub again for it (a flaky line must not stall a cached model)
    threads = max(2, min(4, (os.cpu_count() or 4) // 2))
    return WhisperModel(name, device="auto", compute_type="int8", local_files_only=whisper_cached(name),
                        cpu_threads=threads)


def load_whisper():
    """The faster-whisper model, loaded once (the warm-up thread and the first command share one load).
    DD_STT_MODEL (default small — clearly better for Thanglish). First launch on a slow line: whatever is already
    cached (base) answers right away while small downloads in the background, then dd switches to it."""
    global _whisper
    with _whisper_lock:
        if _whisper is not None:
            return _whisper
        name = default_stt_model()
        if name != "base" and not whisper_cached(name) and whisper_cached("base"):
            print(f"(whisper: using cached base now; '{name}' downloads in the background)", flush=True)
            _whisper = _load("base")
            threading.Thread(target=_upgrade_whisper, args=(name,), daemon=True, name="dd-whisper-dl").start()
            return _whisper
        last = None
        for candidate in [name, name] + (["base"] if name != "base" else []):
            try:
                _whisper = _load(candidate)
                if candidate != name:
                    print(f"(whisper '{name}' unavailable: {str(last)[:80]} — using base)", flush=True)
                return _whisper
            except Exception as e:  # a flaky download: once more, then the base fallback
                last = e
                print(f"(whisper '{candidate}' load failed: {str(e)[:100]})", flush=True)
                time.sleep(2)
        raise RuntimeError(f"whisper unavailable: {last}")


def _upgrade_whisper(name: str) -> None:
    global _whisper
    try:
        better = _load(name)
    except Exception as e:
        print(f"(whisper '{name}' download failed: {str(e)[:100]} — staying on base)", flush=True)
        return
    with _whisper_lock:
        _whisper = better
    print(f"(whisper: switched to '{name}')", flush=True)


# ---- Apple-silicon upgrade: mlx-whisper runs Whisper large-v3-turbo on the Mac's GPU (a 3 s command in <1 s,
# clearly better Thanglish than small). Set up in the background on a 16 GB+ Apple-silicon Mac (DD_STT=mlx forces,
# DD_STT=faster disables); faster-whisper keeps answering until it is ready.
MLX_MODEL = os.environ.get("DD_STT_MLX_MODEL", "mlx-community/whisper-large-v3-turbo")
_mlx_ready = False


def ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except (ValueError, OSError, AttributeError):
        return 0.0


def machine_info() -> str:
    chip = platform.machine()
    if MAC:
        try:
            chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=5).stdout.strip() or chip
        except Exception:
            pass
    return f"machine: {chip}, {ram_gb():.0f} GB RAM, {platform.system()} {platform.mac_ver()[0] or platform.release()}, python {platform.python_version()}"


def mlx_wanted() -> bool:
    v = os.environ.get("DD_STT", "auto").lower()
    if v == "mlx":
        return True
    if v != "auto":
        return False
    return MAC and platform.machine() == "arm64" and ram_gb() >= 16


def setup_mlx(log=print) -> bool:
    """Install mlx-whisper if needed and fetch the model (one-time, minutes); then stt() switches to it."""
    global _mlx_ready
    try:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            log("  stt: installing mlx-whisper (one-time, background)...")
            for extra in ([], ["--break-system-packages"], ["--user"]):
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, "mlx-whisper"],
                                   capture_output=True, text=True, timeout=1800)
                if r.returncode == 0:
                    break
            else:
                log(f"  stt: mlx-whisper install failed: {(r.stderr or '')[-200:]} — staying on faster-whisper")
                return False
            import mlx_whisper  # noqa: F401
        import numpy as np
        log(f"  stt: fetching {MLX_MODEL} (one-time)...")
        _mlx_transcribe(np.zeros(RATE // 2, dtype=np.float32))  # downloads + warms the model
        _mlx_ready = True
        log(f"  stt: mlx-whisper {MLX_MODEL.split('/')[-1]} ready (Apple GPU) — switching from faster-whisper")
        return True
    except Exception as e:
        log(f"  stt: mlx-whisper unavailable ({type(e).__name__}: {str(e)[:120]}) — staying on faster-whisper")
        return False


def _mlx_transcribe(audio, prompt: str | None = HINT) -> str:
    import mlx_whisper
    lang = os.environ.get("DD_STT_LANG", "en")
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=MLX_MODEL, language=None if lang == "auto" else lang,
                               temperature=0.0, condition_on_previous_text=False, initial_prompt=prompt,
                               no_speech_threshold=0.6, verbose=None)
    return (r.get("text") or "").strip()


def stt_plain(pcm: bytes) -> str:
    """stt() without the vocabulary prompt: used to *confirm* a wake word, so a 'Jarvis' in the text came from
    the audio and not from Whisper repeating its prompt on noise."""
    return stt(pcm, hint="")


def stt(pcm: bytes, hint: str | None = None) -> str:
    """16 kHz mono PCM -> text (mlx-whisper when ready, else faster-whisper int8). Short utterances are padded so
    Whisper hears them whole. hint: initial prompt (default HINT; "" = none)."""
    import numpy as np
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    pad = np.zeros(int(0.3 * RATE), dtype=np.float32)
    audio = np.concatenate([pad, a, pad])
    prompt = HINT if hint is None else (hint or None)
    if _mlx_ready:
        try:
            return _mlx_transcribe(audio, prompt)
        except Exception as e:
            print(f"(mlx stt failed: {str(e)[:80]} — faster-whisper for this one)", flush=True)
    model = load_whisper()
    lang = os.environ.get("DD_STT_LANG", "en")  # English decoding reads romanised Tamil best; "auto" wanders
    segs, _ = model.transcribe(audio, language=None if lang == "auto" else lang,
                               beam_size=1, vad_filter=False, condition_on_previous_text=False,
                               initial_prompt=prompt, no_speech_threshold=0.6)
    return " ".join(s.text.strip() for s in segs).strip()


def spoken_form(reply: str, limit: int = 300) -> str:
    """What gets read aloud: the first 1-2 sentences, never a transcript-like ramble or a code dump."""
    text = re.sub(r"```[\s\S]*?```", " code screen la irukku. ", reply)
    text = re.sub(r"(?im)(^|\s)(user|dd|assistant|system)\s*:[^\n]*", " ", text)  # hallucinated dialogue lines
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:end + 1] if end > 40 else cut).strip()


def default_speak(text: str) -> None:
    if os.environ.get("DD_MUTE") == "1":
        return
    import dd_jarvis
    dd_jarvis.speak(text, mute=False)


def default_stop_speaking() -> bool:
    import dd_jarvis
    return dd_jarvis.stop_speaking()


def make_handle():
    """The brain: same dd agent loop + skills; short answers for speech (num_predict cap keeps it quick)."""
    try:
        messages = [{"role": "system", "content": dd.boot_system_prompt() + VOICE_SUFFIX}]
    except SystemExit:
        return lambda _text: "Rules file hash mismatch — terminal la rules_guard.py reinit run pannu."

    def model(m, t):
        return dd.chat(m, t, num_predict=int(os.environ.get("DD_VOICE_TOKENS", "220")))

    def handle(text: str) -> str:
        reply = dd.turn(messages, text, model)
        messages[:] = [messages[0]] + messages[-20:]
        return reply
    return handle


def open_mic_settings() -> None:
    if MAC:
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"], check=False)


# ---------------- wake-word detector (Siri-style: tiny always-on model, Whisper only after the wake) ----------------
OWW_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")  # ~10 MB together
OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


OWW_BUNDLED = str(dd.ROOT / "skills" / "voice" / "assets" / "oww")  # shipped with the repo (Apache-2.0, ~3.7 MB)


def oww_model_dir() -> str:
    """Where the hey-jarvis ONNX files live: the copies shipped in this repo, else inside openwakeword 0.4.x,
    else ~/.dd/models/oww (downloaded once from the openWakeWord GitHub release)."""
    if all(os.path.isfile(os.path.join(OWW_BUNDLED, f)) for f in OWW_FILES):
        return OWW_BUNDLED
    import openwakeword
    pkg = os.path.join(os.path.dirname(os.path.abspath(openwakeword.__file__)), "resources", "models")
    if all(os.path.isfile(os.path.join(pkg, f)) for f in OWW_FILES):
        return pkg
    return str(Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "models" / "oww")


def fetch(url: str, dest: str, timeout: int = 60) -> None:
    """Download with Python's SSL (certifi's CA bundle when the python has no certificates — python.org builds on
    macOS until 'Install Certificates.command' is run), else with curl (uses the system keychain)."""
    import ssl
    import urllib.request
    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
        return
    except Exception as e:
        if not shutil.which("curl"):
            raise
        last = e
    r = subprocess.run(["curl", "-fsSL", "--max-time", str(timeout * 3), "-o", dest, url], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{last}; curl: {r.stderr.strip()[-120:]}")


def ensure_oww_models(folder: str, log=print) -> None:
    """Download the missing wake-model files into `folder` (atomic: .part then rename)."""
    missing = [f for f in OWW_FILES if not os.path.isfile(os.path.join(folder, f))]
    if not missing:
        return
    os.makedirs(folder, exist_ok=True)
    for i, name in enumerate(missing, 1):
        log(f"  wake model: downloading {name} ({i}/{len(missing)}, one-time)...")
        part = os.path.join(folder, name + ".part")
        last: Exception | None = None
        for attempt in range(3):  # a flaky line must not cost the always-on wake word
            try:
                fetch(OWW_RELEASE + name, part)
                if os.path.getsize(part) < 10_000:
                    raise RuntimeError("download came back empty")
                break
            except Exception as e:
                last = e
                time.sleep(3 * (attempt + 1))
        else:
            if os.path.exists(part):
                os.remove(part)
            raise RuntimeError(f"{name}: {last}")
        os.replace(part, os.path.join(folder, name))


def build_oww_model(folder: str):
    """openwakeword.Model for hey_jarvis from `folder`, whatever the installed openwakeword version calls its args
    (0.5+: wakeword_models + inference_framework="onnx" — the tflite default does not exist on macOS; 0.4: *_paths)."""
    import importlib.metadata as md
    from openwakeword.model import Model
    paths = [os.path.join(folder, "hey_jarvis_v0.1.onnx")]
    mel, emb = os.path.join(folder, "melspectrogram.onnx"), os.path.join(folder, "embedding_model.onnx")
    try:
        ver = tuple(int(x) for x in md.version("openwakeword").split(".")[:2])
    except Exception:
        ver = (0, 5)
    new = dict(wakeword_models=paths, inference_framework="onnx", melspec_model_path=mel, embedding_model_path=emb)
    old = dict(wakeword_model_paths=paths, melspec_onnx_model_path=mel, embedding_onnx_model_path=emb)
    first, second = (new, old) if ver >= (0, 5) else (old, new)
    try:
        return Model(**first)
    except TypeError:
        return Model(**second)


ASSETS = dd.ROOT / "skills" / "voice" / "assets"
DD_HEAD_FILE = os.environ.get("DD_WAKE_DD_MODEL") or str(ASSETS / "wake_dd.npz")
JARVIS_HEAD_FILE = os.environ.get("DD_WAKE_JARVIS_MODEL") or str(ASSETS / "wake_jarvis.npz")


class WakeHead:
    """A custom wake word ('dee dee', or plain 'jarvis'): a tiny classifier (one hidden layer, numpy) on the same
    openWakeWord embeddings the hey-jarvis model reads — trained from thousands of synthetic voices by
    skills/voice/scripts/wakeword_train.py. Fires only when two consecutive 80 ms chunks agree, and the listener
    then confirms the candidate with Whisper on the last 2 s of audio (a two-stage wake, like Siri's) — so a head
    can be recall-oriented. Candidates Whisper rejects, and wakes never followed by a command, raise its threshold
    by themselves."""

    def __init__(self, path: str = DD_HEAD_FILE, threshold: float | None = None, word: str = "dd"):
        import numpy as np
        z = np.load(path)
        self.word = word
        self.mu, self.sd = z["mu"], z["sd"]
        self.w1, self.b1, self.w2, self.b2 = z["w1"], z["b1"], z["w2"], z["b2"]
        self.T = int(z["T"])
        env = f"DD_WAKE_{word.upper()}_THRESHOLD"
        self.threshold = float(os.environ.get(env, "0.8")) if threshold is None else threshold
        self.base_threshold = self.threshold
        self.streak = 0
        self.last_score = 0.0
        self.false_wakes: deque[float] = deque(maxlen=20)
        self.rejects: deque[float] = deque(maxlen=40)

    def score(self, feats) -> float:
        import numpy as np
        x = (np.asarray(feats, dtype=np.float32).reshape(1, -1) - self.mu) / self.sd
        h = np.maximum(0.0, x @ self.w1 + self.b1)
        o = float((h @ self.w2 + self.b2)[0, 0])
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, o))))

    def step(self, feats) -> float | None:
        """Score one chunk's embedding window; the score when it fires (2 chunks in a row above threshold)."""
        s = self.score(feats)
        self.last_score = s
        self.streak = self.streak + 1 if s >= self.threshold else 0
        if self.streak >= 2:
            self.streak = 0
            return s
        return None

    def false_wake(self, now: float) -> None:
        """Called when a wake was followed by no command: three of those within 10 min nudge the threshold up."""
        self.false_wakes.append(now)
        recent = [t for t in self.false_wakes if now - t < 600]
        if len(recent) >= 3 and self.threshold < 0.985:
            self.threshold = min(0.985, self.threshold + 0.02)
            self.false_wakes.clear()

    def good_wake(self) -> None:
        """A wake followed by a real command: relax a little back towards the base threshold."""
        self.threshold = max(self.base_threshold, self.threshold - 0.01)

    def rejected(self, now: float) -> None:
        """Whisper heard something else: eight such candidates within 10 min raise the threshold a notch."""
        self.rejects.append(now)
        recent = [t for t in self.rejects if now - t < 600]
        if len(recent) >= 8 and self.threshold < 0.985:
            self.threshold = min(0.985, self.threshold + 0.05)
            self.rejects.clear()


DDHead = WakeHead  # (older name)


class SoftJarvis:
    """The hey-jarvis model scores a plain 'jarvis' at only ~0.3-0.7 (it was trained on the full phrase). A score
    that rises above the soft level and falls back without reaching the hard threshold becomes a *candidate* that
    Whisper confirms — so 'jarvis' alone wakes dd, while a real 'hey jarvis' still wakes it instantly."""
    word = "jarvis"

    def __init__(self, soft: float | None = None, hard: float = 0.5):
        self.threshold = float(os.environ.get("DD_WAKE_SOFT", "0.2")) if soft is None else soft
        self.base_threshold, self.hard = self.threshold, hard
        self.peak, self.age = 0.0, 0
        self.hold = False  # inside a hump that reached the hard level: ignore it until it is over
        self.rejects: deque[float] = deque(maxlen=40)

    def step(self, s: float) -> float | None:
        if s >= self.hard:  # the instant path takes it
            self.peak, self.age, self.hold = 0.0, 0, True
            return None
        if self.hold:
            self.hold = s >= self.threshold
            return None
        if s >= self.threshold:
            self.peak, self.age = max(self.peak, s), self.age + 1
            if self.age < 6:  # wait for the peak (up to ~0.5 s)
                return None
        if self.peak > 0:
            p, self.peak, self.age = self.peak, 0.0, 0
            return p
        return None

    def reset(self) -> None:
        self.peak, self.age, self.hold = 0.0, 0, False

    def rejected(self, now: float) -> None:
        self.rejects.append(now)
        recent = [t for t in self.rejects if now - t < 600]
        if len(recent) >= 8 and self.threshold < self.hard - 0.05:
            self.threshold = min(self.hard - 0.05, self.threshold + 0.05)
            self.rejects.clear()

    def good_wake(self) -> None:
        self.threshold = max(self.base_threshold, self.threshold - 0.01)

    def false_wake(self, now: float) -> None:
        self.rejected(now)


NORM_FILE = str(ASSETS / "oww_norm.npz")


def voice_dir() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "voice"


def print_file(word: str = "jarvis") -> Path:
    return voice_dir() / f"{word}_print.npz"


def diag_dir() -> Path:
    """<repo>/logs/voice: near-miss / wake / setup clips for tuning the wake word to your voice (gitignored)."""
    return dd.ROOT / "logs" / "voice"


def save_clip(pcm: bytes, name: str, keep: int = 80) -> str | None:
    """16 kHz mono int16 -> logs/voice/<name>.wav, keeping only the newest `keep` clips. Best effort."""
    import wave
    try:
        d = diag_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.wav"
        with wave.open(str(p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RATE)
            wf.writeframes(pcm)
        old = sorted(d.glob("*.wav"), key=lambda f: f.stat().st_mtime)
        for f in old[:-keep]:
            f.unlink()
        return str(p)
    except Exception:
        return None


def mirror_setup_clips() -> int:
    """Copy the voice-setup recordings (~/.dd/voice/jarvis-*.wav) next to the logs, so they can be studied."""
    import shutil as _sh
    n = 0
    try:
        d = diag_dir()
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted(voice_dir().glob("jarvis-*.wav")):
            dst = d / f"setup-{f.name}"
            if not dst.exists():
                _sh.copy2(f, dst)
                n += 1
    except Exception:
        pass
    return n


def _speech_end(clip, rate: int = RATE) -> float:
    """Seconds where the spoken word ends in a clip (last 20 ms frame above 25% of the loudest)."""
    import numpy as np
    n = rate // 50
    frames = clip[: len(clip) // n * n].reshape(-1, n)
    e = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    if not len(e) or e.max() <= 0:
        return len(clip) / rate
    loud = np.nonzero(e >= 0.25 * e.max())[0]
    return (loud[-1] + 1) * n / rate


def stream_windows(clip, model=None, T: int = 16):
    """Run a float clip (16 kHz, -1..1) through a fresh openWakeWord feature pipeline the way the live detector
    sees audio (80 ms chunks); returns [(time_s, (T, 96) window)] after every chunk."""
    import numpy as np
    model = model or build_oww_model(oww_model_dir())
    pad = np.zeros(RATE // 2, dtype=np.float32)
    x = np.concatenate([pad, clip.astype(np.float32), pad])
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    out = []
    for i in range(0, len(pcm) - 1279, 1280):
        model.predict(pcm[i:i + 1280])
        f = np.asarray(model.preprocessor.get_features(T)).reshape(-1, 96)
        out.append(((i + 1280) / RATE - len(pad) / RATE, f[-T:].copy()))
    return out


class VoicePrint:
    """Your own 'Jarvis', Siri-style: a few recordings of you saying it (menu → Teach 'Jarvis') become templates in
    openWakeWord's embedding space. Live windows that look like them (cosine similarity after normalisation)
    propose a wake that Whisper confirms — so your own pronunciation ("jaravis") is caught even when the generic
    models score it low. ~0.1 ms per check."""
    word = "jarvis"

    def __init__(self, path: Path | str | None = None):
        import numpy as np
        z = np.load(str(path or print_file()))
        self.templates = z["templates"].astype(np.float32)
        self.mu, self.sd = z["mu"].astype(np.float32), z["sd"].astype(np.float32)
        self.T = int(z["T"])
        self.threshold = self.base_threshold = float(z["detect"])
        self.gate = float(z["gate"])
        self.clips = int(z["clips"])
        self.streak = 0
        self.last_score = 0.0
        self.recent: deque[float] = deque(maxlen=8)  # ~0.6 s of similarities
        self.rejects: deque[float] = deque(maxlen=40)
        self.false_wakes: deque[float] = deque(maxlen=20)

    def _vec(self, feats):
        import numpy as np
        v = (np.asarray(feats, dtype=np.float32).reshape(-1) - self.mu) / self.sd
        return v / (float(np.linalg.norm(v)) + 1e-6)

    def similarity(self, feats) -> float:
        return float((self.templates @ self._vec(feats)).max())

    def step(self, feats) -> float | None:
        s = self.similarity(feats)
        self.last_score = s
        self.recent.append(s)
        self.streak = self.streak + 1 if s >= self.threshold else 0
        if self.streak >= 2:
            self.streak = 0
            return s
        return None

    def recent_max(self) -> float:
        return max(self.recent, default=0.0)

    def rejected(self, now: float) -> None:
        self.rejects.append(now)
        if len([t for t in self.rejects if now - t < 600]) >= 8 and self.threshold < 0.95:
            self.threshold = min(0.95, self.threshold + 0.03)
            self.rejects.clear()

    def false_wake(self, now: float) -> None:
        self.rejected(now)

    def good_wake(self) -> None:
        self.threshold = max(self.base_threshold, self.threshold - 0.01)

    @staticmethod
    def build(clips: list, out: Path | str | None = None, model=None) -> dict:
        """clips: float arrays (16 kHz) of you saying the word -> templates + calibrated thresholds, saved to `out`.
        Templates: the windows ending 0-320 ms after the word ends (where the live detector decides)."""
        import numpy as np
        z = np.load(NORM_FILE)
        mu, sd, T = z["mu"], z["sd"], int(z["T"])
        per_clip = []
        for c in clips:
            end = _speech_end(c)
            # a fresh feature pipeline per clip (Model.reset() does not clear features in every openwakeword version)
            wins = [w for t, w in stream_windows(c, model() if callable(model) else None, T) if end - 0.02 <= t <= end + 0.34]
            if wins:
                vs = [((w.reshape(-1) - mu) / sd) for w in wins]
                per_clip.append(np.stack([v / (np.linalg.norm(v) + 1e-6) for v in vs]).astype(np.float32))
        if len(per_clip) < 2:
            raise ValueError("need at least 2 usable recordings")
        loo = []  # each clip against the others: how alike your own repetitions are
        for i, a in enumerate(per_clip):
            others = np.concatenate([b for j, b in enumerate(per_clip) if j != i])
            loo.append(float((a @ others.T).max()))
        # generous on purpose: every voice candidate is still confirmed by Whisper; random speech sits ~0.3-0.6
        detect = float(np.clip(np.percentile(loo, 25) - 0.15, 0.6, 0.75))
        gate = float(np.clip(detect - 0.12, 0.45, 0.6))
        templates = np.concatenate(per_clip)
        out = Path(out or print_file())
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out, templates=templates, mu=mu, sd=sd, T=T, detect=detect, gate=gate, clips=len(per_clip))
        return {"clips": len(per_clip), "templates": int(len(templates)), "self_similarity": [round(x, 2) for x in loo],
                "detect": round(detect, 2), "gate": round(gate, 2), "file": str(out)}


def decide(hard: float, threshold: float, hits: list, soft, voice_hit):
    """Which wake (if any) a chunk produces: (word, source, score) or None. Pure — tested headless.
    hard/threshold: the hey-jarvis model's score and level (instant, no confirmation); hits: [(score, head)];
    soft: (score, source) or None; voice_hit: (score, voiceprint) or None. Everything but the instant path is a
    candidate that Whisper confirms. (A voice-print *gate* on the other candidates was tried and dropped: under
    a playing video it cut recall to ~15% in the streaming tests.)"""
    if hard >= threshold:
        return ("hey jarvis", None, hard)
    if voice_hit is not None:
        return ("jarvis", voice_hit[1], voice_hit[0])
    if hits:
        r, h = max(hits, key=lambda x: x[0])
        return (h.word, h, r)
    if soft is not None:
        return ("jarvis", soft[1], soft[0])
    return None


class WakeDetector:
    """openWakeWord 'hey jarvis' (ONNX model) + custom heads ('dee dee', plain 'jarvis') when their weights exist +
    soft 'jarvis' candidates. 80 ms chunks, ~3% of one CPU core, ignores speech that isn't a wake phrase — so the
    mic can stay open next to a video without transcribing it. After a trigger: last_word names the phrase and
    last_source the head that proposed it (None = the instant hey-jarvis model, no confirmation needed)."""
    CHUNK = 1280 * 2  # bytes = 80 ms of 16 kHz int16

    def __init__(self, threshold: float | None = None, log=print, dd_head: WakeHead | None | bool = None,
                 jarvis_head: WakeHead | None | bool = None):
        import warnings
        warnings.filterwarnings("ignore", message=".*ExecutionProvider.*")
        folder = oww_model_dir()
        ensure_oww_models(folder, log=log)
        self.model = build_oww_model(folder)
        self.threshold = float(os.environ.get("DD_WAKE_THRESHOLD", "0.5")) if threshold is None else threshold
        self.buf = b""
        self.cooldown = 0
        self.last_score = 0.0
        self.last_word = ""
        self.last_source = None
        self.soft_enabled = True   # the listener turns this off when Whisper confirmation is off

        def load(given, word, path, default="1"):
            if given is not None:
                return given or None
            if os.environ.get(f"DD_WAKE_{word.upper()}", default) == "0" or not os.path.isfile(path):
                return None
            try:
                return WakeHead(path, word=word)
            except Exception as e:
                log(f"  ({word} wake head not loaded: {str(e)[:100]})")
                return None
        self.dd = load(dd_head, "dd", DD_HEAD_FILE, default="0")  # opt-in (DD_WAKE_DD=1): media audio fools it
        self.jarvis = load(jarvis_head, "jarvis", JARVIS_HEAD_FILE)
        self.soft = SoftJarvis(hard=self.threshold)
        self.voice = None
        self.gated = 0  # (kept for log compatibility; nothing is gated any more)
        self.reload_voice(log)

    def reload_voice(self, log=print) -> bool:
        """(Re)load your voice print (menu → Teach 'Jarvis'); True when one is active."""
        self.voice = None
        f = print_file()
        if os.environ.get("DD_WAKE_VOICE", "1") != "0" and f.is_file():
            try:
                self.voice = VoicePrint(f)
            except Exception as e:
                log(f"  (voice print not loaded: {str(e)[:100]})")
        return self.voice is not None

    @property
    def heads(self) -> list:
        return [h for h in (self.dd, self.jarvis) if h is not None]

    def _fire(self, word: str, source, score: float) -> float:
        self.last_word, self.last_source = word, source
        self.model.reset()
        for h in self.heads + ([self.voice] if self.voice is not None else []):
            h.streak = 0
        self.soft.reset()
        self.cooldown = 20  # 1.6 s: one trigger per wake
        return score

    def feed(self, frame: bytes) -> float | None:
        """Returns the score when a wake phrase was just heard, else None (last_word / last_source say which)."""
        import numpy as np
        self.buf += frame
        if len(self.buf) < self.CHUNK:
            return None
        chunk, self.buf = self.buf[:self.CHUNK], self.buf[self.CHUNK:]
        scores = self.model.predict(np.frombuffer(chunk, dtype=np.int16))
        s = max(scores.values()) if scores else 0.0
        self.last_score = s
        hits, vhit = [], None
        users = self.heads + ([self.voice] if self.voice is not None else [])
        if users:
            try:
                feats = np.asarray(self.model.preprocessor.get_features(max(u.T for u in users))).reshape(-1, 96)
                for h in self.heads:
                    r = h.step(feats[-h.T:])
                    if r is not None:
                        hits.append((r, h))
                if self.voice is not None:
                    v = self.voice.step(feats[-self.voice.T:])
                    vhit = (v, self.voice) if v is not None else None
            except Exception:
                hits, vhit = [], None
        soft = self.soft.step(s) if self.soft_enabled else None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        d = decide(s, self.threshold, hits, (soft, self.soft) if soft is not None else None,
                   vhit if self.soft_enabled else None)
        return None if d is None else self._fire(*d)


def make_wake_detector():
    """A WakeDetector when openwakeword is installed and DD_WAKE_MODEL != 0; else None (text-wake fallback)."""
    if os.environ.get("DD_WAKE_MODEL", "1") == "0":
        return None
    try:
        return WakeDetector()
    except Exception as e:
        print(f"(wake model unavailable: {type(e).__name__}: {str(e)[:160]} — pip install openwakeword; "
              "using Whisper text wake)", flush=True)
        return None


# ---------------- the listener ----------------
class Listener:
    def __init__(self, source=None, stt_fn=None, speak=None, handle=None, on_state=None, on_heard=None,
                 on_problem=None, followup_s: float | None = None, clock=time.monotonic, log=print,
                 inline: bool = False, wake=None, stop_speak=None):
        self.source = source or mic_source
        # wake: a WakeDetector (default: openwakeword if installed), False = none (Whisper text wake only)
        self.wake = make_wake_detector() if wake is None else (wake or None)
        self.armed_until = 0.0  # after a wake (or a reply) the next utterance is a command
        # Utterances are transcribed/answered on a worker thread, so the mic keeps streaming while Whisper or the
        # model is busy (a slow turn never backs up the audio pipe). inline=True runs them synchronously (tests).
        self.inline = inline
        self.queue: deque = deque(maxlen=2)   # utterances (and 'verify' jobs); older ones are stale and dropped
        self.work = threading.Event()
        self.worker: threading.Thread | None = None
        self.stt, self.speak_fn = stt_fn or stt, speak or default_speak
        self.verify_stt = stt_fn or stt_plain  # wake confirmation: no prompt that could put "Jarvis" in the text
        self.handle = handle  # built lazily (loads rules + skills) unless injected
        self.on_state = on_state or (lambda s: None)
        self.on_heard = on_heard or (lambda text, secs: None)
        self.on_problem = on_problem or (lambda msg: None)
        self.followup_s = float(os.environ.get("DD_FOLLOWUP", "8")) if followup_s is None else followup_s
        self.clock, self.log = clock, log
        self.seg = Segmenter()
        self.stop = threading.Event()
        self.paused = False
        self.state = "starting"
        self.followup_until = 0.0
        self.tail = 0            # frames still to drop after dd finished speaking (its own echo)
        self.speaking = False
        self.pending_wake = False  # a wake heard while dd spoke (barge-in): armed as soon as it is quiet
        self.wake_answered = True  # False between a wake and its command (false-wake learning for the dd head)
        self.recent: deque[bytes] = deque(maxlen=100)   # last 2 s of mic audio (Whisper confirms "dd" candidates)
        self.agc = AutoGain()      # quiet raw mics are brought up to speech level before anything else sees them
        self.wake_peak = 0.0       # highest hey-jarvis score since the last idle log line (sensitivity check)
        self.diag = os.environ.get("DD_WAKE_DIAG", "1") != "0"  # keep near-miss clips in logs/voice for tuning
        self._near_at = -1e9
        self._near_pending: list | None = None
        self.tentative_until = 0.0  # a "dd" candidate is being confirmed: keep capturing the command meanwhile
        self.verify_pending: dict | None = None  # a candidate waiting for the rest of the word before Whisper
        self.verify = os.environ.get("DD_WAKE_VERIFY", "1") != "0"
        self.wake_source = None    # the head that proposed the current wake (learns from what follows)
        if self.wake is not None and hasattr(self.wake, "soft_enabled"):
            self.wake.soft_enabled = self.verify  # soft 'jarvis' candidates always need Whisper's yes
        self.stop_speak_fn = stop_speak or default_stop_speaking
        self.zero_run = 0        # consecutive all-zero frames (mic blocked -> digital silence)
        self.problem_sent = False
        self.last_heard = ""
        self.last_command = ""   # what dd is acting on right now (HUD: shown while thinking)
        self.last_said = ""      # dd's latest spoken reply (HUD: shown while speaking + follow-up window)
        self.woke_at = 0.0       # clock() of the last wake (HUD: countdown)
        self.last_timing = ""    # "stt 0.8s · brain 1.4s" of the last answer (HUD / log)
        self.level = 0.0         # recent peak mic level (RMS), for the menu / log — "is the mic even hearing me?"
        self._level_win: deque[float] = deque(maxlen=25)
        self._frames = 0
        self._last_activity = clock()
        self.test: dict | None = None   # mic test in progress: {"frames": [...], "left": n, "done": callback}
        self.capture: dict | None = None  # voice setup: waiting for one recording {"event", "utt"}
        self.enrolling = False

    def enroll(self, n: int = 5, done=None, build=None) -> None:
        """Siri-style voice setup (call on its own thread): dd asks for 'Jarvis' n times, captures each with the
        segmenter, builds your voice print and switches it on. done(report) at the end ({"error": ...} on failure)."""
        build = build or VoicePrint.build
        prompts = ["Voice setup. Beep kettadhum, 'Jarvis' nu sollu."] + ["Innoru thadava."] * (n - 2) + ["Last time."]
        clips, misses = [], 0
        self.enrolling = True
        try:
            while len(clips) < n and misses < 4:
                self.say(prompts[min(len(clips), len(prompts) - 1)])
                self.ack(sound_only=True)
                time.sleep(0.35)  # let the beep pass: it must not become the recording
                utt = self._capture(7.0)
                if utt is None or not (0.25 <= len(utt) / (RATE * 2) <= 4.0):
                    misses += 1
                    self.say("Kekkala. Konjam sathama sollu.")
                    continue
                clips.append(utt)
                self.log(f"  voice setup: clip {len(clips)}/{n} ({len(utt) / (RATE * 2):.1f}s)")
            if len(clips) < 3:
                raise ValueError("not enough clear recordings")
            import numpy as np
            import wave
            vd = voice_dir()
            vd.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for i, c in enumerate(clips, 1):  # kept for later retraining (wakeword_train.py --my-voice)
                with wave.open(str(vd / f"jarvis-{stamp}-{i}.wav"), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(RATE)
                    wf.writeframes(c)
            floats = [np.frombuffer(c, dtype=np.int16).astype(np.float32) / 32768.0 for c in clips]
            for i, f in enumerate(floats, 1):
                clipped = float((np.abs(f) > 0.98).mean()) * 100
                self.log(f"  voice setup: clip {i} peak {float(np.abs(f).max()):.2f}, rms {float(np.sqrt((f ** 2).mean())):.3f}, "
                         f"clipped {clipped:.1f}%")
            mirror_setup_clips()
            report = build(floats)
            if self.wake is not None and hasattr(self.wake, "reload_voice"):
                self.wake.reload_voice(self.log)
            self.log(f"  voice setup: done {report}")
            self.say("Mudinjiduchu. Ini un 'Jarvis' mattum kekkum.")
        except Exception as e:
            report = {"error": f"{type(e).__name__}: {e}"}
            self.log(f"  voice setup failed: {report['error']}")
            self.say("Voice setup mudiyala. Menu la innoru thadava try pannu.")
        finally:
            self.enrolling = False
            self.capture = None
            self.set_state("listening")
        (done or (lambda r: None))(report)

    def _capture(self, timeout: float):
        """The next utterance from the mic (segmenter), or None after `timeout` seconds."""
        import threading as _t
        box = {"event": _t.Event(), "utt": None}
        self.seg.reset()
        self.seg.pre.clear()
        self.capture = box
        self.set_state("recording")
        box["event"].wait(timeout)
        self.capture = None
        return box["utt"]

    def mic_test(self, seconds: float = 3.0, done=None) -> None:
        """Capture the next N seconds from the live stream, then report level + what Whisper heard (via `done`)."""
        self.test = {"frames": [], "left": int(seconds * 1000 / FRAME_MS), "done": done or (lambda r: None)}

    def _finish_test(self) -> None:
        t, self.test = self.test, None
        pcm = b"".join(t["frames"])
        peak = max((rms(f) for f in t["frames"]), default=0.0)
        avg = sum(rms(f) for f in t["frames"]) / max(1, len(t["frames"]))
        try:
            text = self.stt(pcm).strip()
        except Exception as e:
            text = f"(stt error: {e})"
        res = {"heard": text, "peak": round(peak), "avg": round(avg), "threshold": round(self.seg.threshold()),
               "seconds": round(len(pcm) / (RATE * 2), 1)}
        self.log(f"  mic test: {res}")
        t["done"](res)

    def shutdown(self) -> None:
        """Stop listening and release the microphone (kills ffmpeg / dd-mic)."""
        self.stop.set()
        kill_children()

    def set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.on_state(s)

    def say(self, text: str) -> None:
        self.last_said = text
        self.set_state("speaking")
        self.speaking = True
        try:
            self.speak_fn(text)
        finally:
            self.speaking = False
            self.tail = 20  # 400 ms: let the room go quiet before listening again
        self.set_state("listening")

    def interrupt(self) -> None:
        """Stop the sentence being spoken (barge-in); say() then returns and the room goes back to listening."""
        try:
            self.stop_speak_fn()
        except Exception as e:
            self.log(f"  (could not stop speaking: {e})")

    def on_wake(self, score: float, word: str | None = None, source=None) -> None:
        """A wake phrase was detected: short sound (without pausing the mic), then the next utterance is the command.
        source: the head/soft detector that proposed it (learns from what follows), None for the instant model."""
        word = word or getattr(self.wake, "last_word", "") or "hey jarvis"
        self.log(f"  wake: {word} ({score:.2f})")
        self._last_activity = self.woke_at = self.clock()
        self.last_command = ""
        self.last_heard = f"{word} ✓"
        self.wake_answered = False
        self.wake_source = source
        if self.diag:  # real wakes are kept too: with the near misses they are the data to tune for your voice
            save_clip(b"".join(self.recent), time.strftime("wake-%H%M%S") + f"-{word.replace(' ', '_')}-{score:.2f}")
        self.on_heard(word, 0.0)
        self.ack(sound_only=True)
        if self.clock() >= self.tentative_until:  # (a confirmed candidate keeps what it captured meanwhile)
            self.seg.reset()
            self.seg.pre.clear()
            self.seg.pre.extend(list(self.recent)[-self.seg.pre.maxlen:])  # "hey jarvis chrome open pannu" in one go
        self.tentative_until = 0.0
        self.armed_until = self.clock() + self.followup_s
        self.set_state("recording")

    def ack(self, sound_only: bool = False) -> None:
        """Wake word heard. sound_only: a short system sound (command already understood, action follows).
        Otherwise a spoken 'Sollu.' — unmistakable: dd is waiting for the command."""
        tink = Path("/System/Library/Sounds/Tink.aiff")
        if sound_only:
            if MAC and tink.exists() and os.environ.get("DD_MUTE") != "1":  # async: the mic keeps listening
                subprocess.Popen(["afplay", str(tink)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        self.say("Sollu.")

    def process(self, utt: bytes) -> None:
        audio = len(utt) / (RATE * 2)
        if self.seg.last_forced and self.clock() >= self.followup_until:
            # non-stop audio with no pause (a video, music, people talking) — a command is 1-4 s with a pause after it.
            # Transcribing media would just burn CPU and never contains a wake word, so skip it while idle.
            self._last_activity = self.clock()
            self.last_heard = f"(continuous audio {audio:.0f}s — video/music? skipped)"
            self.log(f"  skipped: {audio:.0f}s of continuous audio (media?) — pause dd or use earphones while a video plays")
            self.set_state(self._idle_state())
            return
        t0 = self.clock()
        text = self.stt(utt).strip()
        secs = self.clock() - t0
        self._last_activity = self.clock()
        if not text or blocked(text):
            self.last_heard = f"(sound, no words — {audio:.1f}s)" if not text else f"(ignored: {text})"
            self.on_heard("", secs)
            self.log(f"  heard: {text!r} -> ignored  ({audio:.1f}s audio, stt {secs:.1f}s)")
            self.set_state(self._idle_state())
            return
        self.last_heard = text
        self.on_heard(text, secs)
        self.log(f"  heard: {text!r}  ({audio:.1f}s audio, stt {secs:.1f}s)")
        woke, cmd = parse_wake(text)
        armed = self.clock() < self.armed_until
        followup = self.clock() < self.followup_until
        if not woke and not followup and not armed:
            self.set_state(self._idle_state())
            return
        if woke and not cmd:  # just the wake word
            self.woke_at, self.last_command = self.clock(), ""
            self.ack()
            self.followup_until = self.clock() + self.followup_s
            self.set_state("recording")
            return
        command = cmd if woke else text
        self.armed_until = 0.0
        self.last_command = command
        if not self.wake_answered:
            self.wake_answered = True
            if self.wake_source is not None:
                self.wake_source.good_wake()
        if woke or armed:
            self.ack(sound_only=True)  # "heard you" — instant, before the action or the model
        self.set_state("thinking")
        t1 = self.clock()
        if self.handle is None:
            self.handle = make_handle()
        try:
            reply = self.handle(command) or "Done."
        except Exception as e:
            reply = f"Mudiyala: {type(e).__name__}: {str(e)[:120]}"
        brain_s = self.clock() - t1
        self.last_timing = f"stt {secs:.1f}s · brain {brain_s:.1f}s"
        self.log(f"  you> {command}\n  dd> {reply}  ({brain_s:.1f}s; {self.last_timing})")
        self.say(spoken_form(reply))
        self.followup_until = self.clock() + self.followup_s

    def feed(self, frame: bytes) -> None:
        """One 20 ms frame from the mic."""
        frame = self.agc.feed(frame, learn=not (self.speaking or self.tail > 0))  # dd's own voice must not set it
        e = rms(frame)
        self._level_win.append(e)
        self.level = max(self._level_win)
        self._frames += 1
        if self.wake is not None:
            self.wake_peak = max(self.wake_peak, getattr(self.wake, "last_score", 0.0))
        if self.test is not None:  # mic test: just collect
            self.test["frames"].append(frame)
            self.test["left"] -= 1
            if self.test["left"] <= 0:
                self._finish_test()
            return
        if self._frames % 750 == 0 and self.clock() - self._last_activity > 15:  # idle: a level line every 15 s
            self.log(f"  mic level: peak {self.level:.0f} (room floor {self.seg.floor:.0f}/p90 {self.seg.p90:.0f}; "
                     f"speech starts above {self.seg.threshold():.0f}, ends below {self.seg.end_threshold():.0f}; "
                     f"gain {self.agc.gain:.1f}x; wake max {self.wake_peak:.2f}) — nothing heard yet")
            self.wake_peak = 0.0
        if self.paused:
            self.set_state("paused")
            return
        if self.speaking or self.tail > 0:
            self.tail = max(0, self.tail - (0 if self.speaking else 1))
            if self.speaking and self.wake is not None and self.wake.feed(frame) \
                    and getattr(self.wake, "last_source", None) is None:  # barge-in: a sure "hey jarvis" mid-sentence
                self.log("  wake: hey jarvis while speaking — cutting the reply")
                self.interrupt()
                self.pending_wake = True
            return
        if self.capture is not None:  # voice setup: the segmenter alone decides what one recording is
            utt = self.seg.feed(frame)
            if utt:
                self.capture["utt"] = utt
                self.capture["event"].set()
            return
        if self.enrolling:
            return
        if self.pending_wake:  # the wake that interrupted dd: arm now that it is quiet
            self.pending_wake = False
            self.tail = 0
            self.on_wake(self.wake.last_score if self.wake is not None else 1.0)
            return
        if self.state in ("paused", "starting"):
            self.set_state("listening")
        if self.state == "recording" and self.clock() >= max(self.followup_until, self.armed_until):
            self.set_state("listening")
        if frame.count(b"\x00") == len(frame):  # digital silence = no mic at all (permission / device)
            self.zero_run += 1
            if self.zero_run * FRAME_MS >= 8000 and not self.problem_sent:
                self.problem_sent = True
                self.on_problem("Mic la onnum varala (digital silence). System Settings → Privacy & Security → "
                                "Microphone la dd/Terminal ku ON pannu.")
        else:
            self.zero_run = 0
        self.recent.append(frame)
        if self.verify_pending is not None:
            vp = self.verify_pending
            vp["left"] -= 1
            if vp["left"] <= 0:
                self.verify_pending = None
                self._send_verify(vp["score"], vp["word"], vp["source"])
        if self._near_pending is not None:
            self._near_pending[0] -= 1
            if self._near_pending[0] <= 0:
                _, hj, jh, vp = self._near_pending
                self._near_pending = None
                name = time.strftime("near-%H%M%S") + f"-hj{hj:.2f}-jh{jh:.2f}-vp{vp:.2f}"
                if save_clip(b"".join(self.recent), name):
                    self.log(f"  near miss (no wake): hey-jarvis {hj:.2f}, jarvis head {jh:.2f}, your voice {vp:.2f} "
                             f"-> logs/voice/{name}.wav")
        if self.wake is not None:
            score = self.wake.feed(frame)
            if self.diag and not score and self._near_pending is None and self.clock() - self._near_at > 8:
                hj = float(getattr(self.wake, "last_score", 0.0) or 0.0)
                jhh = getattr(self.wake, "jarvis", None)
                vpp = getattr(self.wake, "voice", None)
                jh = float(jhh.last_score) if jhh is not None else 0.0
                vp = float(vpp.last_score) if vpp is not None else 0.0
                if hj >= 0.05 or jh >= 0.3 or vp >= 0.45:
                    self._near_at = self.clock()
                    self._near_pending = [20, hj, jh, vp]  # save 400 ms later: the whole word
            if score:
                word, source = getattr(self.wake, "last_word", "") or "hey jarvis", getattr(self.wake, "last_source", None)
                if source is not None and self.verify:
                    self.confirm_wake(score, word, source)  # stage two: Whisper decides, on the worker thread
                else:
                    self.on_wake(score, word, source)
                return
            if self.clock() >= self.armed_until and self.clock() >= self.followup_until \
                    and self.clock() >= self.tentative_until:
                if self.state == "recording":
                    self.set_state("listening")
                if self.woke_at and not self.wake_answered:  # armed, but no command came
                    self.wake_answered = True
                    src = self.wake_source
                    if src is not None and not self.verify:  # unconfirmed wakes only; confirmed ones were real
                        src.false_wake(self.clock())
                        self.log(f"  ({src.word} wake with no command — threshold now {src.threshold:.2f})")
                if e < self.seg.threshold():  # learn the room's quiet level only (never from speech/media)
                    self.seg._idle_level(e)
                self.seg.reset()
                self.seg.pre.clear()
                return
        utt = self.seg.feed(frame)
        if utt:
            self.submit(utt)

    VERIFY_DELAY_FRAMES = 20  # 400 ms: detectors often fire mid-word; Whisper needs the whole word

    def confirm_wake(self, score: float, word: str = "dd", source=None) -> None:
        """A candidate from a head / the soft detector: 400 ms later the last 2 s go to Whisper (worker thread);
        meanwhile the segmenter keeps running so a command said in the same breath is not lost. on_wake() runs
        only if Whisper agrees."""
        self.tentative_until = self.clock() + 2.5 + self.VERIFY_DELAY_FRAMES * FRAME_MS / 1000
        self.seg.reset()
        self.seg.pre.clear()
        self.seg.pre.extend(list(self.recent)[-self.seg.pre.maxlen:])  # a command in the same breath starts right now
        self.log(f"  wake? {word} candidate ({score:.2f}) — confirming")
        self.verify_pending = {"left": self.VERIFY_DELAY_FRAMES, "score": score, "word": word, "source": source}

    def _send_verify(self, score: float, word: str, source) -> None:
        pcm = b"".join(self.recent)
        job = ("verify", pcm, score, word, source)
        if self.inline:
            self._verify(pcm, score, word, source)
            return
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._worker, daemon=True, name="dd-brain")
            self.worker.start()
        self.queue.append(job)
        self.work.set()

    def _verify(self, pcm: bytes, score: float, word: str = "dd", source=None) -> None:
        t0 = self.clock()
        try:
            text = self.verify_stt(pcm).strip()
        except Exception as e:
            text = ""
            self.log(f"  (wake confirmation failed: {e})")
        woke, _cmd = parse_wake(text) if text else (False, "")
        if not woke and text and word == "dd" and spelled_dd(text):
            woke = True
        if woke:
            self.log(f"  wake: {word} confirmed by whisper ({text!r}, {self.clock() - t0:.1f}s)")
            self.on_wake(score, word, source)
        else:
            self.tentative_until = 0.0
            self.seg.reset()
            self.seg.pre.clear()
            if self.wake is not None and hasattr(self.wake, "cooldown"):
                self.wake.cooldown = 0  # an early, wrong candidate must not block the real word that follows
            if source is not None:
                source.rejected(self.clock())
            thr = f"; threshold {source.threshold:.2f}" if source is not None else ""
            self.log(f"  wake: {word} candidate rejected ({text!r}, {self.clock() - t0:.1f}s{thr})")

    def _idle_state(self) -> str:
        """'recording' while a wake/follow-up window is open (the next words are a command), else 'listening'."""
        return "recording" if self.clock() < max(self.armed_until, self.followup_until) else "listening"

    def submit(self, utt: bytes) -> None:
        if self.clock() < max(self.armed_until, self.followup_until):
            self.set_state("thinking")  # a command just ended: transcribing it (the HUD/menu show it at once)
        if self.inline:
            self.process(utt)
            return
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._worker, daemon=True, name="dd-brain")
            self.worker.start()
        if len(self.queue) == self.queue.maxlen:
            self.log("  (busy — dropped an older utterance)")
        self.queue.append(utt)
        self.work.set()

    def _worker(self) -> None:
        while not self.stop.is_set():
            if not self.queue:
                self.work.wait(0.5)
                self.work.clear()
                continue
            utt = self.queue.popleft()
            try:
                if isinstance(utt, tuple) and utt[0] == "verify":
                    self._verify(*utt[1:])
                else:
                    self.process(utt)
            except Exception as e:  # a bad turn must not kill the brain thread
                self.log(f"  turn failed: {type(e).__name__}: {e}")
                self.set_state("listening")

    def run(self) -> None:
        """Blocking loop; restarts the mic stream after an error. Call stop.set() to end."""
        backoff = 3
        while not self.stop.is_set():
            try:
                for frame in self.source():
                    if self.stop.is_set():
                        break
                    self.feed(frame)
                    backoff = 3
                if self.stop.is_set():
                    break
                raise RuntimeError("mic stream ended")
            except Exception as e:
                self.set_state("error")
                msg = f"mic error: {e}"
                self.log(msg)
                if not self.problem_sent:
                    self.problem_sent = True
                    self.on_problem(msg + " — Microphone permission / device paaru (python3 examples/dd_listen.py --devices)")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30)
        self.set_state("stopped")


def warm_stt() -> None:
    try:
        stt(b"\x00\x00" * (RATE // 2))  # loads the Whisper model so the first command isn't slow
    except Exception as e:
        print(f"(whisper not ready: {e})")


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--devices" in argv:
        print(list_devices())
        return 0
    if not shutil.which("ffmpeg"):
        print("ffmpeg missing: brew install ffmpeg")
        return 1
    if "--check" in argv:  # 3-second mic + Whisper check: prints the level and what it heard, and says it back
        print("3 second pesu (e.g. 'jarvis chrome open pannu')...")
        frames, src = [], mic_source()
        try:
            for f in src:
                frames.append(f)
                if len(frames) >= 3000 // FRAME_MS:
                    break
        finally:
            src.close()
        pcm = b"".join(frames)
        peak = max((rms(f) for f in frames), default=0.0)
        text = stt(pcm) if frames else ""
        print(f"mic peak level {peak:.0f} (speech starts above {Segmenter().threshold():.0f}); heard: {text!r}")
        default_speak(f"Naan kettadhu: {text}" if text else "Onnum kekkala. Mic permission paaru.")
        return 0 if text else 1
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        print("pip3 install faster-whisper   (first run downloads the base model, ~150 MB)")
        return 1
    print(f"dd listening — brain {dd.MODEL}, whisper-{os.environ.get('DD_STT_MODEL', 'base')}. "
          f"Sollu: 'dd, chrome open pannu' / 'jarvis'. Ctrl-C to stop.")
    print("  loading model + voice (one-time)...")
    warm_stt()
    dd.warm()
    handle = make_handle()
    lis = Listener(handle=handle, on_problem=lambda m: (print("!!", m), open_mic_settings()))
    dd.start_reminder_watcher(lambda text: lis.say(text))
    lis.say("Vanakkam Dhina. dd ready.")
    try:
        lis.run()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


# ---------------- selftest (no mic, no Whisper, no Ollama) ----------------
def _tone(ms: int, amp: int = 3000) -> list[bytes]:
    n = RATE * ms // 1000
    a = array.array("h", [amp if (i // 20) % 2 else -amp for i in range(n)])
    raw = a.tobytes()
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES)]


def _quiet(ms: int, amp: int = 40) -> list[bytes]:
    import random
    rnd = random.Random(7)
    n = RATE * ms // 1000
    raw = array.array("h", [rnd.randint(-amp, amp) for _ in range(n)]).tobytes()
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES)]


def _zeros(ms: int) -> list[bytes]:
    return [b"\x00" * FRAME_BYTES] * (ms // FRAME_MS)


def selftest() -> int:
    # 1. segmenter: quiet -> nothing; 1 s of sound then 0.8 s quiet -> one utterance with pre-roll; a click is dropped
    seg = Segmenter()
    assert all(seg.feed(f) is None for f in _quiet(1000))
    got = [seg.feed(f) for f in _tone(1000) + _quiet(800)]
    utts = [u for u in got if u]
    assert len(utts) == 1, len(utts)
    dur = len(utts[0]) / (RATE * 2)
    assert 1.5 <= dur <= 2.3, dur  # ~0.3 pre-roll + 1.0 speech + 0.7 tail
    assert not any(seg.feed(f) for f in _tone(100) + _quiet(900)), "a 100 ms click must not be an utterance"
    long_utt = [u for u in (seg.feed(f) for f in _tone(9000)) if u]
    assert long_utt and len(long_utt[0]) / (RATE * 2) <= 8.5, "max utterance length"
    # a noisy room (fan/AC ~230 RMS): speech must still end ~0.6 s after the voice stops, not run to the max window
    noisy = Segmenter()
    for f in _quiet(3000, amp=400):
        noisy.feed(f)
    assert 150 < noisy.floor < 320 and noisy.threshold() > 600, (noisy.floor, noisy.threshold())
    got = [u for u in (noisy.feed(f) for f in _tone(1000) + _quiet(1500, amp=400)) if u]
    assert len(got) == 1 and 1.4 <= len(got[0]) / (RATE * 2) <= 2.3, [len(u) / (RATE * 2) for u in got]

    # 2. wake word parsing — the ways Whisper writes "dd"/"jarvis", and words that must NOT wake
    cases = {
        "ok dd, jarvis, gavis, Narvis": (True, ""), "jarvis jarvis jarvis": (True, ""), "Jarvis. Jarvi, chrome open pannu": (True, "chrome open pannu"),
        "dd narvis volume 30": (True, "volume 30"),
        "enna rvis, open chrome.": (True, "open chrome"), "dd, jarvis, ennaiku,": (True, "ennaiku"),
        "Hendemiz.": (False, "Hendemiz."), "na jan tharvisan tannaw": (False, "na jan tharvisan tannaw"),
        "D.D. open chrome": (True, "open chrome"), "Didi, volume 30.": (True, "volume 30"),
        "Dee dee screenshot edu": (True, "screenshot edu"), "dd": (True, ""), "DD.": (True, ""),
        "Hey Jarvis, what's the time?": (True, "what's the time"), "Jervis open safari": (True, "open safari"),
        "the dd mani enna": (True, "mani enna"), "okay dd news sollu": (True, "news sollu"),
        "address check pannu": (False, "address check pannu"), "add this file": (False, "add this file"),
        "I'd like tea": (False, "I'd like tea"), "deadline is tomorrow": (False, "deadline is tomorrow"),
        "Diti.": (True, ""), "Dedee, volume up": (True, "volume up"), "Titi.": (False, "Titi."),
        "Daddy, open chrome": (False, "Daddy, open chrome"), "the deed is done": (False, "the deed is done"),
        "": (False, ""),
    }
    for text, want in cases.items():
        assert parse_wake(text) == want, (text, parse_wake(text), want)
    os.environ["DD_WAKE"] = "computer"
    assert parse_wake("Computer, lights")[0] is True
    os.environ.pop("DD_WAKE")
    for t in ("Can. HDD.", "Oil there. D.G.", "Builder. D.T.", "Hey, D D. Chrome.", "Code. EDD."):
        assert spelled_dd(t), t
    for t in ("Code. ADD.", "the CD player", "Ed is here", "Thanks. GG.", "4. Hittiti.", "Andry or. TG."):
        assert not spelled_dd(t), t

    # 3. hallucination filter + mic picker
    assert blocked("Thank you.") and blocked("...") and blocked("you") and not blocked("chrome open pannu")
    assert blocked("Sollu.") and blocked("Bump. Bump. Bump. Bump.") and blocked("Jarvis. dd.") and not blocked("dd")
    assert blocked("Innaiku weather enna. Screenshot edu.") and blocked("Volume thirty. Innaiku weather enna.")
    assert not blocked("Innaiku weather enna?") and not blocked("Innaiku weather enna, screenshot edu, then chrome open pannu")
    assert not blocked("Jarvis. Chrome open pannu.") and not blocked("dd. Screenshot edu.")
    assert not blocked("open open sesame") and not blocked("Rude. Sollu.")
    listing = ("[AVFoundation indev @ 0x1] AVFoundation video devices:\n[AVFoundation indev @ 0x1] [0] FaceTime HD Camera\n"
               "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n[AVFoundation indev @ 0x1] [0] AirPods Pro\n"
               "[AVFoundation indev @ 0x1] [1] MacBook Air Microphone\n")
    assert pick_mic(listing) == "1", pick_mic(listing)
    assert pick_mic("garbage") == "0"
    os.environ["DD_MIC"] = "3"
    assert pick_mic(listing) == "3"
    os.environ.pop("DD_MIC")

    # 4. end-to-end with a fake mic / STT / voice / brain and a controllable clock
    now = {"t": 1000.0}
    heard = iter(["D.D. open chrome", "Thank you.", "jarvis", "volume 30", "random talk later", "dd"])
    spoken, handled, states, heard_log, problems = [], [], [], [], []

    def fake_stt(pcm):
        return next(heard, "")

    def fake_speak(text):
        spoken.append(text)
        now["t"] += 1.0  # speaking takes a second

    def fake_handle(cmd):
        handled.append(cmd)
        return f"ok {cmd}"

    lis = Listener(source=lambda: iter([]), stt_fn=fake_stt, speak=fake_speak, handle=fake_handle,
                   on_state=states.append, on_heard=lambda t, s: heard_log.append(t) if t else None,
                   on_problem=problems.append, followup_s=8.0, clock=lambda: now["t"], log=lambda *_: None, inline=True, wake=False)
    utterance = _tone(800) + _quiet(800)

    def speak_utt():
        for f in utterance:
            lis.feed(f)

    for f in _tone(200):             # a 200 ms blip: level meter sees it, but it is too short to be an utterance
        lis.feed(f)
    assert lis.level > 1000, lis.level
    for f in _quiet(800):
        lis.feed(f)
    assert handled == [] and lis.level < 200
    speak_utt()                      # "D.D. open chrome" -> command
    assert handled == ["open chrome"] and spoken[-1] == "ok open chrome", (handled, spoken)
    assert "thinking" in states and "speaking" in states and states[-1] == "listening", states
    speak_utt()                      # "Thank you." -> hallucination, ignored but visible
    assert handled == ["open chrome"] and lis.last_heard.startswith("(ignored: Thank you.)"), lis.last_heard
    speak_utt()                      # "jarvis" alone -> ack, waits for the command
    assert spoken[-1] == "Sollu." and lis.state == "recording"
    speak_utt()                      # "volume 30" inside the follow-up window -> command without wake word
    assert handled == ["open chrome", "volume 30"], handled
    now["t"] += 30                   # window expired
    speak_utt()                      # "random talk later" -> ignored
    assert handled == ["open chrome", "volume 30"], handled
    assert heard_log == ["D.D. open chrome", "jarvis", "volume 30", "random talk later"], heard_log

    # own voice: frames arriving while dd speaks (and 400 ms after) are dropped
    lis.speaking = True
    for f in _tone(500):
        lis.feed(f)
    lis.speaking, lis.tail = False, 20
    for f in _tone(400):
        lis.feed(f)
    assert lis.tail == 0 and not lis.seg.speaking, "echo of dd's own voice must not start an utterance"

    # paused: nothing is processed, state says so
    lis.paused = True
    speak_utt()
    assert lis.state == "paused" and handled == ["open chrome", "volume 30"]
    lis.paused = False

    # mic test: collects the next 3 s raw, reports level + transcript, normal listening resumes after
    results = []
    heard = iter(["jarvis chrome open pannu"])
    lis.mic_test(3.0, done=results.append)
    for f in _tone(1500) + _quiet(1600):
        lis.feed(f)
    assert results and results[0]["heard"] == "jarvis chrome open pannu" and results[0]["peak"] > 1000, results
    assert results[0]["seconds"] == 3.0 and lis.test is None and handled == ["open chrome", "volume 30"]

    # continuous audio (a video) while idle is skipped without calling Whisper; in a follow-up window it is read
    calls = {"n": 0}

    def counting_stt(pcm):
        calls["n"] += 1
        return "dictated long message"
    lis.stt = counting_stt
    for f in _tone(9000):
        lis.feed(f)
    assert calls["n"] == 0 and "continuous audio" in lis.last_heard, (calls, lis.last_heard)
    for f in _quiet(3200):  # the room goes quiet again -> the floor relaxes
        lis.feed(f)
    assert lis.seg.threshold() < 1000, lis.seg.threshold()
    lis.followup_until = now["t"] + 8
    for f in _tone(9000):
        lis.feed(f)
    assert calls["n"] == 1 and handled[-1] == "dictated long message", (calls, handled)
    lis.followup_until = 0
    lis.stt = fake_stt

    # an utterance Whisper can't read is still visible as "(sound, no words)"
    for f in _quiet(3200):  # let the floor relax after the long dictation
        lis.feed(f)
    heard = iter([""])
    speak_utt()
    assert lis.last_heard.startswith("(sound, no words"), lis.last_heard

    # blocked mic: 8 s of digital silence -> one problem report
    for f in _zeros(8200):
        lis.feed(f)
    assert len(problems) == 1 and "Microphone" in problems[0], problems
    for f in _zeros(9000):
        lis.feed(f)
    assert len(problems) == 1, "reported once"

    # 4a. Siri-style gating: with a wake detector, speech is NOT transcribed until "hey jarvis" fires;
    #     the next utterance is the command; after the reply an 8 s follow-up stays open
    class FakeWake:
        CHUNK = 2560

        def __init__(self):
            self.fire_in = None  # frames until a trigger
            self.last_score = 0.0
            self.last_source = None

        def feed(self, frame):
            if self.fire_in is None:
                return None
            self.fire_in -= 1
            if self.fire_in <= 0:
                self.fire_in = None
                return 0.97
            return None

    fw = FakeWake()
    gate_stt_calls = []
    g_handled, g_spoken = [], []
    lg = Listener(source=lambda: iter([]), stt_fn=lambda b: (gate_stt_calls.append(1), "open chrome")[1],
                  speak=g_spoken.append, handle=lambda c: (g_handled.append(c), "ok")[1],
                  followup_s=8.0, clock=lambda: now["t"], log=lambda *_: None, inline=True, wake=fw)
    speak_utt()  # (the old listener) no-op here; the gated one:
    for f in utterance:
        lg.feed(f)
    assert gate_stt_calls == [] and g_handled == [], "speech without the wake phrase must not reach Whisper"
    fw.fire_in = 3
    for f in _quiet(200):  # the detector fires during quiet
        lg.feed(f)
    assert lg.state == "recording" and lg.last_heard == "hey jarvis ✓"
    assert lg.woke_at == now["t"] and lg.last_command == "" and lg.armed_until > now["t"]  # what the HUD reads
    hud_states = []
    lg.on_state = hud_states.append
    for f in utterance:  # the command
        lg.feed(f)
    assert gate_stt_calls == [1] and g_handled == ["open chrome"] and g_spoken[-1] == "ok", (gate_stt_calls, g_handled, g_spoken)
    assert hud_states[:1] == ["thinking"] and lg.last_command == "open chrome" and lg.last_said == "ok", hud_states
    lg.on_state = lambda s: None
    for f in utterance:  # follow-up window: another command without the wake phrase
        lg.feed(f)
    assert g_handled == ["open chrome", "open chrome"]
    now["t"] += 30
    for f in utterance:  # window closed: ignored again
        lg.feed(f)
    assert len(gate_stt_calls) == 2 and lg.state == "listening"
    fw.fire_in = 2
    for f in _quiet(200):
        lg.feed(f)
    now["t"] += 30  # armed but said nothing: disarms quietly
    for f in _quiet(200):
        lg.feed(f)
    assert lg.state == "listening" and len(gate_stt_calls) == 2
    # the 'dee dee' head: numpy forward pass, 2-chunk agreement, false-wake learning; and the listener's bookkeeping
    import tempfile as _tf2
    import numpy as _np
    T = 16
    w1 = _np.zeros((T * 96, 4), dtype=_np.float32); w1[0, 0] = 40.0  # decides on feature[0] alone
    z = _tf2.mkdtemp(prefix="dd-head-") + "/wake_dd.npz"
    _np.savez(z, mu=_np.zeros(T * 96, _np.float32), sd=_np.ones(T * 96, _np.float32), w1=w1, b1=_np.zeros(4, _np.float32),
              w2=_np.array([[1.0], [0], [0], [0]], _np.float32), b2=_np.array([-8.0], _np.float32), T=T)
    head = DDHead(z, threshold=0.9)
    hot, cold = _np.zeros((T, 96), _np.float32), _np.zeros((T, 96), _np.float32)
    hot[0, 0] = 1.0
    assert head.score(cold) < 0.01 and head.score(hot) > 0.99
    assert head.step(hot) is None and head.step(cold) is None and head.step(hot) is None and head.step(hot) > 0.99
    assert head.step(hot) is None, "streak resets after a fire"
    for t in (0.0, 100.0, 200.0):
        head.false_wake(t)
    assert abs(head.threshold - 0.92) < 1e-9, head.threshold
    head.good_wake(); head.good_wake(); head.good_wake()
    assert head.threshold == 0.9, "never below the base threshold"

    class FakeHead:
        threshold, word = 0.9, "dd"

        def __init__(self): self.calls = []
        def false_wake(self, t): self.calls.append(("false", t))
        def good_wake(self): self.calls.append(("good",))
        def rejected(self, t): self.calls.append(("rejected", t))
    fw.dd, fw.last_word = FakeHead(), "dd"
    fw.last_source = fw.dd
    # stage two: a dd candidate is confirmed by Whisper on the last 2 s — rejected when it heard something else
    real_stt = lg.stt
    lg.stt = lg.verify_stt = lambda b: "the weather is nice"
    fw.fire_in = 1
    lg.feed(_quiet(20)[0])
    assert lg.verify_pending is not None and lg.tentative_until > now["t"], "Whisper waits for the rest of the word"
    for f in _quiet(Listener.VERIFY_DELAY_FRAMES * FRAME_MS):
        lg.feed(f)
    assert lg.state == "listening" and fw.dd.calls[-1][0] == "rejected" and lg.tentative_until == 0.0, (lg.state, fw.dd.calls)
    lg.stt = lg.verify_stt = lambda b: "D.D."
    fw.dd.calls.clear()
    fw.fire_in = 1
    for f in _quiet((Listener.VERIFY_DELAY_FRAMES + 1) * FRAME_MS):
        lg.feed(f)
    assert lg.state == "recording" and lg.last_heard == "dd ✓" and lg.wake_answered is False and lg.tentative_until == 0.0
    lg.stt = lg.verify_stt = real_stt
    lg.verify = False  # the rest of this block exercises the head's bookkeeping without the Whisper stage
    now["t"] += 30
    for f in _quiet(2000):
        lg.feed(f)
    assert fw.dd.calls and fw.dd.calls[-1][0] == "false", fw.dd.calls  # confirmed wake, then silence: a false wake
    fw.dd.calls.clear()
    fw.fire_in = 1
    lg.feed(_quiet(20)[0])
    assert lg.state == "recording" and lg.last_heard == "dd ✓" and lg.wake_answered is False
    for f in utterance:  # a command follows: good wake
        lg.feed(f)
    assert fw.dd.calls == [("good",)], fw.dd.calls
    now["t"] += 30
    for f in _quiet(2000):  # (drains the post-speech tail too)
        lg.feed(f)
    fw.fire_in = 1
    lg.feed(_quiet(20)[0])
    assert lg.wake_answered is False
    now["t"] += 30  # nothing said: false wake
    for f in _quiet(2000):
        lg.feed(f)
    assert fw.dd.calls[-1][0] == "false" and lg.wake_answered, fw.dd.calls
    fw.dd, fw.last_word, fw.last_source = None, "hey jarvis", None
    lg.verify = True
    # DDHead.rejected: eight rejections in ten minutes raise the threshold by 0.05
    for t in range(8):
        head.rejected(1000.0 + t)
    assert abs(head.threshold - 0.95) < 1e-9, head.threshold

    # decide(): instant hey-jarvis > your voice print > heads > soft; with a voice print, heads/soft must sound like you
    class _H:
        word = "jarvis"
    hh = _H()
    assert decide(0.9, 0.5, [], None, None) == ("hey jarvis", None, 0.9)
    assert decide(0.1, 0.5, [(0.95, hh)], None, None) == ("jarvis", hh, 0.95)
    assert decide(0.1, 0.5, [], (0.3, "soft"), None) == ("jarvis", "soft", 0.3)
    assert decide(0.1, 0.5, [(0.95, hh)], (0.3, "soft"), (0.8, "vp")) == ("jarvis", "vp", 0.8)
    assert decide(0.9, 0.5, [], None, (0.8, "vp"))[0] == "hey jarvis" and decide(0.1, 0.5, [], None, None) is None

    # voice setup: dd asks n times, each recording comes from the segmenter, the print is built and reported
    built = {}

    def fake_build(clips):
        built["n"] = len(clips)
        return {"clips": len(clips), "detect": 0.7, "gate": 0.58}
    said_vs = []
    lv = Listener(source=lambda: iter([]), stt_fn=lambda b: "", speak=said_vs.append, handle=lambda c: "ok",
                  followup_s=8, log=lambda *_: None, inline=True, wake=False)
    import tempfile as _tfv
    os.environ["DD_HOME_SAVED"] = os.environ.get("DD_HOME", "")
    os.environ["DD_HOME"] = _tfv.mkdtemp(prefix="dd-voice-")
    result = {}
    tv = threading.Thread(target=lv.enroll, kwargs={"n": 3, "done": result.update, "build": fake_build}, daemon=True)
    tv.start()
    t_end = time.time() + 20
    while tv.is_alive() and time.time() < t_end:
        if lv.capture is not None:
            for f in _tone(700) + _quiet(900):
                lv.feed(f)
        else:
            time.sleep(0.02)
    tv.join(timeout=2)
    assert result.get("clips") == 3 and built["n"] == 3, (result, built)
    assert said_vs[0].startswith("Voice setup") and said_vs[-1].startswith("Mudinjiduchu") and not lv.enrolling
    assert len(list((Path(os.environ["DD_HOME"]) / "voice").glob("jarvis-*.wav"))) == 3
    os.environ["DD_HOME"] = os.environ.pop("DD_HOME_SAVED") or os.environ["DD_HOME"]

    # soft 'jarvis': a score that peaks between the soft and the hard level becomes a candidate at the peak;
    # a real 'hey jarvis' (crossing the hard level) never produces a soft candidate
    sj = SoftJarvis(soft=0.2, hard=0.5)
    seq = [0.05, 0.25, 0.38, 0.31, 0.1]
    got = [sj.step(x) for x in seq]
    assert got == [None, None, None, None, 0.38], got
    assert [sj.step(x) for x in (0.1, 0.3, 0.7, 0.9, 0.2, 0.05)] == [None] * 6
    assert [sj.step(0.3) for _ in range(6)][-1] == 0.3, "steady mid score: candidate after ~0.5 s"
    for t in range(8):
        sj.rejected(500.0 + t)
    assert abs(sj.threshold - 0.25) < 1e-9 and sj.hard == 0.5
    # the real detector (when openwakeword is here): shipped heads load, silence never wakes
    try:
        import openwakeword  # noqa: F401
        have_oww = True
    except ImportError:
        have_oww = False
    if have_oww:
        # a voice print from three synthetic 'recordings': builds, loads, a template matches itself
        import numpy as _npv
        tones = [(_npv.sin(2 * _npv.pi * f0 * _npv.arange(RATE // 2) / RATE) * 0.4).astype(_npv.float32) for f0 in (300, 320, 340)]
        vfile = Path(_tfv.mkdtemp(prefix="dd-vp-")) / "jarvis_print.npz"
        rep = VoicePrint.build([_npv.concatenate([_npv.zeros(RATE // 3, _npv.float32), t, _npv.zeros(RATE // 2, _npv.float32)]) for t in tones], vfile)
        assert rep["clips"] == 3 and 0.6 <= rep["detect"] <= 0.75 and 0.45 <= rep["gate"] <= 0.6, rep
        vp = VoicePrint(vfile)
        assert vp.templates.shape[1] == 16 * 96 and abs(float(vp.templates[0] @ vp.templates[0]) - 1.0) < 1e-3
        real = WakeDetector(log=lambda *a: None)
        assert all(isinstance(h, WakeHead) for h in real.heads) and real.soft.hard == real.threshold
        assert real.dd is None or os.environ.get("DD_WAKE_DD") == "1", "dee dee is opt-in"
        assert not any(real.feed(b"\x00" * FRAME_BYTES) for _ in range(150))

    # barge-in: "hey jarvis" while dd is talking cuts the sentence, then arms as soon as it is quiet
    stops = []
    lg.stop_speak_fn = lambda: stops.append(1) or True
    lg.speaking, fw.fire_in = True, 1
    lg.feed(_quiet(20)[0])
    assert stops == [1] and lg.pending_wake and lg.state == "listening"
    lg.speaking = False  # say() returned
    lg.feed(_quiet(20)[0])
    assert not lg.pending_wake and lg.state == "recording" and lg.armed_until > now["t"], lg.state
    now["t"] += 30
    for f in _quiet(200):
        lg.feed(f)
    assert lg.state == "listening"

    # 3b. auto gain: a quiet mic (speech at ~200 RMS) is brought up; a loud one is left alone; a loud word after a
    #     quiet minute is never clipped (instant attack + limiter)
    ag = AutoGain(enabled=True)
    quiet_speech = _tone(2000, amp=200)
    for _ in range(4):
        out = [ag.feed(f) for f in quiet_speech]
    assert ag.gain > 4 and rms(out[-1]) > 800, (ag.gain, rms(out[-1]))  # (slow release: ~6 s to double)
    room = AutoGain(enabled=True)
    for f in _quiet(20000, amp=120):  # a quiet room: the gain climbs to its maximum
        room.feed(f)
    assert room.gain > 0.85 * AutoGain.MAX_GAIN, room.gain
    word = [room.feed(f) for f in _tone(400, amp=4000)]  # then a loud "Jarvis"
    assert max(max(abs(x) for x in array.array("h", f)) for f in word) <= AutoGain.PEAK + 1, "no clipping"
    assert room.gain < 1.5, room.gain
    loud = AutoGain(enabled=True)
    for f in _tone(1000, amp=6000):
        loud.feed(f)
    assert abs(loud.gain - 1.0) < 1e-6
    assert AutoGain(enabled=False).feed(quiet_speech[0]) == quiet_speech[0]
    assert all(len(f) == FRAME_BYTES for f in out) and max(abs(x) for x in array.array("h", out[-1])) <= 32767

    # 4a. whisper cache probe: only a snapshot that holds model.bin counts as "already downloaded"
    import stat as _stat
    import tempfile as _tf
    hub = Path(_tf.mkdtemp(prefix="dd-hf-"))
    (hub / "models--Systran--faster-whisper-base" / "snapshots" / "abc").mkdir(parents=True)
    (hub / "models--Systran--faster-whisper-base" / "snapshots" / "abc" / "model.bin").write_bytes(b"x")
    (hub / "models--Systran--faster-whisper-small" / "snapshots" / "def").mkdir(parents=True)  # partial: no model.bin
    assert whisper_cached("base", hub) and not whisper_cached("small", hub) and not whisper_cached("tiny", hub)

    # 4b. mic helper plumbing: frames stream from the helper's stdout; a dead helper counts as a failure and,
    #     after two, the ffmpeg path is chosen; DD_AEC=0 forces ffmpeg
    fake = Path(_tf.mkdtemp(prefix="dd-mic-")) / "dd-mic"
    fake.write_text("#!/bin/sh\nhead -c 12800 /dev/zero\necho 'dd-mic: 24000 Hz x1 -> 16000 Hz mono, voice processing ON' >&2\n")
    fake.chmod(fake.stat().st_mode | _stat.S_IEXEC)
    os.environ["DD_MIC_BIN"] = str(fake)
    global _helper_failures
    _helper_failures = 0
    assert choose_source()[0] == "helper"
    frames_got, ended = 0, ""
    try:
        for _f in helper_source(str(fake)):
            frames_got += 1
    except RuntimeError as e:
        ended = str(e)
    assert frames_got == 20 and "voice processing ON" in ended, (frames_got, ended)
    assert _helper_failures == 1  # it exited within 3 s
    _helper_failures = 2
    assert choose_source()[0] == "ffmpeg", "two early deaths must fall back to ffmpeg"
    # exit code 7 = "this voice-processing wiring is silent here": mic_source relaunches with the next mode and
    # remembers it; the mode in use comes from the helper's own "[mode N]" line
    _helper_failures = 0
    os.environ["DD_HOME_SAVED2"] = os.environ.get("DD_HOME", "")
    os.environ["DD_HOME"] = _tf.mkdtemp(prefix="dd-micmode-")
    modal = fake.parent / "dd-mic-modal"
    modal.write_text("#!/bin/sh\n"
                     "case \"$2\" in\n"
                     "  0) echo 'input x5 voice processing ON [mode 0]' >&2; echo 'silent in mode 0 — relaunch' >&2; exit 7;;\n"
                     "  *) echo \"input x1 [mode $2]\" >&2; head -c 1280 /dev/urandom;;\n"
                     "esac\n")
    modal.chmod(modal.stat().st_mode | _stat.S_IEXEC)
    os.environ["DD_MIC_BIN"] = str(modal)
    assert load_mic_mode() == 0
    seen: list[str] = []
    got_modal = []
    try:
        for f in mic_source(log=seen.append):
            got_modal.append(f)
    except RuntimeError:
        pass
    assert any("mode 0 silent on this Mac -> mode 1" in m for m in seen) and load_mic_mode() == 1, seen
    assert len(got_modal) == 2, len(got_modal)  # mode 1's two frames
    os.environ["DD_HOME"] = os.environ.pop("DD_HOME_SAVED2") or os.environ["DD_HOME"]
    # a helper that starts and then delivers nothing at all (stuck audio unit) is given up after HELPER_STALL_S
    hang = fake.parent / "dd-mic-hang"
    hang.write_text("#!/bin/sh\necho 'input x3 [mode 4]' >&2\nexec sleep 30\n")
    hang.chmod(hang.stat().st_mode | _stat.S_IEXEC)
    global HELPER_STALL_S
    real_stall, HELPER_STALL_S = HELPER_STALL_S, 0.5
    _helper_failures = 0
    t0h = time.monotonic()
    try:
        list(helper_source(str(hang), log=lambda *_: None))
        raise AssertionError("a stuck helper must raise")
    except RuntimeError as e:
        assert "no audio" in str(e) and _helper_failures == 2 and time.monotonic() - t0h < 5, (e, _helper_failures)
    HELPER_STALL_S = real_stall
    # a helper that streams nothing but digital zeros (wrong device / no permission) is given up within
    # SILENCE_FALLBACK_MS and mic_source() carries on with ffmpeg in the same run — no restart, no notification
    _helper_failures = 0
    silent = fake.parent / "dd-mic-silent"
    silent.write_text("#!/bin/sh\necho 'input: Virtual Cable 48000 Hz x1' >&2\nexec cat /dev/zero\n")
    silent.chmod(silent.stat().st_mode | _stat.S_IEXEC)
    os.environ["DD_MIC_BIN"] = str(silent)
    relayed: list[str] = []
    zero_frames, fell = 0, ""
    try:
        for _f in helper_source(str(silent), log=relayed.append):
            zero_frames += 1
    except RuntimeError as e:
        fell = str(e)
    assert "hears nothing" in fell and zero_frames == SILENCE_FALLBACK_MS // FRAME_MS - 1 and _helper_failures == 2, \
        (fell, zero_frames, _helper_failures)
    assert any("Virtual Cable" in r for r in relayed), relayed  # the helper's own diagnostics reach the log
    g = globals()
    real_ffmpeg = g["ffmpeg_source"]
    g["ffmpeg_source"] = lambda: iter([b"\x01\x00" * (FRAME_BYTES // 2)])
    try:
        _helper_failures = 0
        logged: list[str] = []
        out = list(mic_source(log=logged.append))
        assert out[-1] == b"\x01\x00" * (FRAME_BYTES // 2) and out[0] == b"\x00" * FRAME_BYTES \
            and len(out) == SILENCE_FALLBACK_MS // FRAME_MS, (len(out), logged)
        assert any("ffmpeg avfoundation instead" in m for m in logged), logged
    finally:
        g["ffmpeg_source"] = real_ffmpeg
    _helper_failures = 0
    os.environ["DD_AEC"] = "0"
    assert choose_source()[0] == "ffmpeg"
    os.environ.pop("DD_AEC")
    os.environ["DD_MIC_BIN"] = "/nonexistent/dd-mic"
    assert choose_source()[0] == "ffmpeg"
    os.environ.pop("DD_MIC_BIN")

    # 4c. worker mode: feed() returns at once; the brain thread transcribes and answers; the queue keeps the newest two
    slow_calls = []

    def slow_handle(cmd):
        time.sleep(0.3)
        slow_calls.append(cmd)
        return "ok"
    lw = Listener(source=lambda: iter([]), stt_fn=lambda b: "jarvis volume 30", speak=lambda t: None,
                  handle=slow_handle, followup_s=0.0, log=lambda *_: None, wake=False)
    t0 = time.monotonic()
    for f in utterance:
        lw.feed(f)
    assert time.monotonic() - t0 < 0.25, "feed must not block on the turn"
    deadline = time.monotonic() + 5
    while not slow_calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert slow_calls == ["volume 30"], slow_calls
    for _ in range(4):  # four quick utterances while busy -> at most the newest two are kept
        for f in utterance:
            lw.feed(f)
    deadline = time.monotonic() + 6
    while lw.queue and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.4)
    assert 2 <= len(slow_calls) <= 4, slow_calls
    lw.stop.set()

    # 4d. spoken form: short, no dialogue hallucinations, no code
    sp = spoken_form("Chrome open panniten. User: \"hi\"\ndd: \"hello\"\n```bash\nls\n```" + " x" * 300)
    assert "User" not in sp and "```" not in sp and len(sp) <= 310 and sp.startswith("Chrome open panniten."), sp
    assert spoken_form("Ok.") == "Ok."

    # 5. run(): a dying mic stream is reported and retried; stop ends the loop
    calls = {"n": 0}

    def flaky_source():
        calls["n"] += 1
        if calls["n"] == 2:
            lis2.stop.set()
        raise RuntimeError("Input/output error")

    probs2 = []
    lis2 = Listener(source=flaky_source, stt_fn=fake_stt, speak=fake_speak, handle=fake_handle,
                    on_problem=probs2.append, followup_s=8.0, clock=lambda: now["t"], log=lambda *_: None, inline=True, wake=False)
    lis2.stop.wait = lambda *_: None  # don't actually sleep in the test
    lis2.run()
    assert calls["n"] == 2 and probs2 and "mic error" in probs2[0] and lis2.state == "stopped", (calls, probs2)
    print("dd_listen selftest ok (segmenter, wake parsing, hallucination filter, mic pick, end-to-end turns, "
          "follow-up window, own-voice guard, pause, blocked-mic report, stream retry)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
