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
HINT = "Jarvis. dd. Chrome open pannu. Volume thirty. Innaiku weather enna? Screenshot edu. Sollu."
VOICE_SUFFIX = ("\n\nVOICE MODE: your reply is spoken aloud. 1-2 short sentences, no tables, no lists; "
                "put code only when asked (it is shown, not read).")
# How Whisper tends to write a spoken "dd" / "jarvis". Compared after lower-casing and dropping punctuation/spaces.
WAKE_FORMS = {"dd", "ddd", "d", "didi", "deedee", "dede", "dedi", "didee", "teedee", "jarvis", "jarviz", "jarvi",
              "jarvice", "jarvies", "jervis", "jarves", "javis"}
LEAD_SKIP = {"hey", "ok", "okay", "hi", "hello", "so", "yo", "the", "a", "ey", "hey,"}
# Things Whisper invents from silence / noise — never treat as a command.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "subtitles by",
                  "the end", "so", "okay", "oh", "hmm", "uh", "um", "i", "a"}


def rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    a = array.array("h", frame[: len(frame) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


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


def _is_wake(cand: str, forms: set[str]) -> bool:
    c = re.sub(r"[^a-z0-9]", "", cand)
    if not c:
        return False
    return c in forms or (len(c) >= 4 and difflib.SequenceMatcher(None, c, "jarvis").ratio() >= 0.75)


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
            while rest and (_is_wake(rest[0], forms) or rest[0] in LEAD_SKIP):  # "dd jarvis ..." / "dd, hey ..."
                rest = rest[1:]
            return True, " ".join(rest).strip()
    return False, text.strip()


def blocked(text: str) -> bool:
    t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    return not re.search(r"[a-z]", t) or t in HALLUCINATIONS


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


def ffmpeg_source():
    """Yields 20 ms PCM frames from the microphone until ffmpeg stops (then raises with its last error)."""
    if MAC:
        inp = ["-f", "avfoundation", "-i", f":{pick_mic(list_devices())}"]
    else:
        inp = ["-f", "pulse", "-i", "default"] if shutil.which("pactl") else ["-f", "alsa", "-i", "default"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *inp, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
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


def choose_source() -> tuple[str, str | None]:
    """('helper', path) when the echo-cancelling dd-mic helper is built and allowed, else ('ffmpeg', None)."""
    b = helper_binary()
    if os.environ.get("DD_AEC", "1") != "0" and _helper_failures < 2 and os.access(b, os.X_OK):
        return "helper", b
    return "ffmpeg", None


def helper_source(binary: str):
    """Frames from dd-mic (AVAudioEngine voice processing: cancels the Mac's own playback). Same pipe protocol."""
    global _helper_failures
    started = time.monotonic()
    p = subprocess.Popen([binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    got = 0
    try:
        while True:
            frame = p.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                break
            got += 1
            yield frame
    finally:
        p.kill()
    err = (p.stderr.read() or b"").decode(errors="ignore").strip()
    if time.monotonic() - started < 3 or got == 0:  # died at once: count it; after two, fall back to ffmpeg
        _helper_failures += 1
    raise RuntimeError(f"dd-mic ended: {err[-300:] or 'no output'}")


def mic_source():
    kind, path = choose_source()
    if kind == "helper":
        print(f"  mic: dd-mic helper (echo cancellation) {path}", flush=True)
        yield from helper_source(path)
    else:
        print("  mic: ffmpeg avfoundation (no echo cancellation — build dd-mic for playback-proof listening)", flush=True)
        yield from ffmpeg_source()


# ---------------- speech to text ----------------
_whisper = None


def stt(pcm: bytes) -> str:
    """16 kHz mono PCM -> text (faster-whisper, int8). Short utterances are padded so Whisper hears them whole."""
    global _whisper
    import numpy as np
    if _whisper is None:
        from faster_whisper import WhisperModel  # pip install faster-whisper
        name = os.environ.get("DD_STT_MODEL", "small")  # small: clearly better for Thanglish; base: faster
        try:
            _whisper = WhisperModel(name, device="auto", compute_type="int8")
        except Exception as e:
            if name == "base":
                raise
            print(f"(whisper '{name}' unavailable: {str(e)[:80]} — using base)")
            _whisper = WhisperModel("base", device="auto", compute_type="int8")
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    pad = np.zeros(int(0.3 * RATE), dtype=np.float32)
    lang = os.environ.get("DD_STT_LANG", "en")  # English decoding reads romanised Tamil best; "auto" wanders
    segs, _ = _whisper.transcribe(np.concatenate([pad, a, pad]), language=None if lang == "auto" else lang,
                                  beam_size=1, vad_filter=False, condition_on_previous_text=False,
                                  initial_prompt=HINT, no_speech_threshold=0.6)
    return " ".join(s.text.strip() for s in segs).strip()


def default_speak(text: str) -> None:
    if os.environ.get("DD_MUTE") == "1":
        return
    import dd_jarvis
    dd_jarvis.speak(text, mute=False)


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


# ---------------- the listener ----------------
class Listener:
    def __init__(self, source=None, stt_fn=None, speak=None, handle=None, on_state=None, on_heard=None,
                 on_problem=None, followup_s: float | None = None, clock=time.monotonic, log=print,
                 inline: bool = False):
        self.source = source or mic_source
        # Utterances are transcribed/answered on a worker thread, so the mic keeps streaming while Whisper or the
        # model is busy (a slow turn never backs up the audio pipe). inline=True runs them synchronously (tests).
        self.inline = inline
        self.queue: deque[bytes] = deque(maxlen=2)   # newest two wait; anything older is stale and dropped
        self.work = threading.Event()
        self.worker: threading.Thread | None = None
        self.stt, self.speak_fn = stt_fn or stt, speak or default_speak
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
        self.zero_run = 0        # consecutive all-zero frames (mic blocked -> digital silence)
        self.problem_sent = False
        self.last_heard = ""
        self.level = 0.0         # recent peak mic level (RMS), for the menu / log — "is the mic even hearing me?"
        self._level_win: deque[float] = deque(maxlen=25)
        self._frames = 0
        self._last_activity = clock()
        self.test: dict | None = None   # mic test in progress: {"frames": [...], "left": n, "done": callback}

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

    def set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.on_state(s)

    def say(self, text: str) -> None:
        self.set_state("speaking")
        self.speaking = True
        try:
            self.speak_fn(text)
        finally:
            self.speaking = False
            self.tail = 20  # 400 ms: let the room go quiet before listening again
        self.set_state("listening")

    def ack(self) -> None:
        """Wake word heard, no command yet: a short system sound (instant) or a spoken 'Sollu.'"""
        tink = Path("/System/Library/Sounds/Tink.aiff")
        if MAC and tink.exists() and os.environ.get("DD_MUTE") != "1":
            self.set_state("speaking")
            self.speaking = True
            try:
                subprocess.run(["afplay", str(tink)], check=False, timeout=5)
            finally:
                self.speaking, self.tail = False, 10
            self.set_state("listening")
        else:
            self.say("Sollu.")

    def process(self, utt: bytes) -> None:
        audio = len(utt) / (RATE * 2)
        if self.seg.last_forced and self.clock() >= self.followup_until:
            # non-stop audio with no pause (a video, music, people talking) — a command is 1-4 s with a pause after it.
            # Transcribing media would just burn CPU and never contains a wake word, so skip it while idle.
            self._last_activity = self.clock()
            self.last_heard = f"(continuous audio {audio:.0f}s — video/music? skipped)"
            self.log(f"  skipped: {audio:.0f}s of continuous audio (media?) — pause dd or use earphones while a video plays")
            return
        t0 = self.clock()
        text = self.stt(utt).strip()
        secs = self.clock() - t0
        self._last_activity = self.clock()
        if not text or blocked(text):
            self.last_heard = f"(sound, no words — {audio:.1f}s)" if not text else f"(ignored: {text})"
            self.on_heard("", secs)
            self.log(f"  heard: {text!r} -> ignored  ({audio:.1f}s audio, stt {secs:.1f}s)")
            return
        self.last_heard = text
        self.on_heard(text, secs)
        self.log(f"  heard: {text!r}  ({audio:.1f}s audio, stt {secs:.1f}s)")
        woke, cmd = parse_wake(text)
        followup = self.clock() < self.followup_until
        if not woke and not followup:
            return
        if woke and not cmd:  # just the wake word
            self.ack()
            self.followup_until = self.clock() + self.followup_s
            self.set_state("recording")
            return
        command = cmd if woke else text
        self.set_state("thinking")
        t1 = self.clock()
        if self.handle is None:
            self.handle = make_handle()
        try:
            reply = self.handle(command) or "Done."
        except Exception as e:
            reply = f"Mudiyala: {type(e).__name__}: {str(e)[:120]}"
        self.log(f"  you> {command}\n  dd> {reply}  ({self.clock() - t1:.1f}s)")
        self.say(reply)
        self.followup_until = self.clock() + self.followup_s

    def feed(self, frame: bytes) -> None:
        """One 20 ms frame from the mic."""
        e = rms(frame)
        self._level_win.append(e)
        self.level = max(self._level_win)
        self._frames += 1
        if self.test is not None:  # mic test: just collect
            self.test["frames"].append(frame)
            self.test["left"] -= 1
            if self.test["left"] <= 0:
                self._finish_test()
            return
        if self._frames % 750 == 0 and self.clock() - self._last_activity > 15:  # idle: a level line every 15 s
            self.log(f"  mic level: peak {self.level:.0f} (room floor {self.seg.floor:.0f}/p90 {self.seg.p90:.0f}; "
                     f"speech starts above {self.seg.threshold():.0f}, ends below {self.seg.end_threshold():.0f}) — nothing heard yet")
        if self.paused:
            self.set_state("paused")
            return
        if self.speaking or self.tail > 0:
            self.tail = max(0, self.tail - (0 if self.speaking else 1))
            return
        if self.state in ("paused", "starting"):
            self.set_state("listening")
        if self.state == "recording" and self.clock() >= self.followup_until:
            self.set_state("listening")
        if frame.count(b"\x00") == len(frame):
            self.zero_run += 1
            if self.zero_run * FRAME_MS >= 8000 and not self.problem_sent:
                self.problem_sent = True
                self.on_problem("Mic la onnum varala (digital silence). System Settings → Privacy & Security → "
                                "Microphone la dd/Terminal ku ON pannu.")
        else:
            self.zero_run = 0
        utt = self.seg.feed(frame)
        if utt:
            self.submit(utt)

    def submit(self, utt: bytes) -> None:
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
        "enna rvis, open chrome.": (True, "open chrome"), "dd, jarvis, ennaiku,": (True, "ennaiku"),
        "Hendemiz.": (False, "Hendemiz."), "na jan tharvisan tannaw": (False, "na jan tharvisan tannaw"),
        "D.D. open chrome": (True, "open chrome"), "Didi, volume 30.": (True, "volume 30"),
        "Dee dee screenshot edu": (True, "screenshot edu"), "dd": (True, ""), "DD.": (True, ""),
        "Hey Jarvis, what's the time?": (True, "what's the time"), "Jervis open safari": (True, "open safari"),
        "the dd mani enna": (True, "mani enna"), "okay dd news sollu": (True, "news sollu"),
        "address check pannu": (False, "address check pannu"), "add this file": (False, "add this file"),
        "I'd like tea": (False, "I'd like tea"), "deadline is tomorrow": (False, "deadline is tomorrow"),
        "": (False, ""),
    }
    for text, want in cases.items():
        assert parse_wake(text) == want, (text, parse_wake(text), want)
    os.environ["DD_WAKE"] = "computer"
    assert parse_wake("Computer, lights")[0] is True
    os.environ.pop("DD_WAKE")

    # 3. hallucination filter + mic picker
    assert blocked("Thank you.") and blocked("...") and blocked("you") and not blocked("chrome open pannu")
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
                   on_problem=problems.append, followup_s=8.0, clock=lambda: now["t"], log=lambda *_: None, inline=True)
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

    # 4b. mic helper plumbing: frames stream from the helper's stdout; a dead helper counts as a failure and,
    #     after two, the ffmpeg path is chosen; DD_AEC=0 forces ffmpeg
    import stat as _stat
    import tempfile as _tf
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
                  handle=slow_handle, followup_s=0.0, log=lambda *_: None)
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

    # 5. run(): a dying mic stream is reported and retried; stop ends the loop
    calls = {"n": 0}

    def flaky_source():
        calls["n"] += 1
        if calls["n"] == 2:
            lis2.stop.set()
        raise RuntimeError("Input/output error")

    probs2 = []
    lis2 = Listener(source=flaky_source, stt_fn=fake_stt, speak=fake_speak, handle=fake_handle,
                    on_problem=probs2.append, followup_s=8.0, clock=lambda: now["t"], log=lambda *_: None, inline=True)
    lis2.stop.wait = lambda *_: None  # don't actually sleep in the test
    lis2.run()
    assert calls["n"] == 2 and probs2 and "mic error" in probs2[0] and lis2.state == "stopped", (calls, probs2)
    print("dd_listen selftest ok (segmenter, wake parsing, hallucination filter, mic pick, end-to-end turns, "
          "follow-up window, own-voice guard, pause, blocked-mic report, stream retry)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
