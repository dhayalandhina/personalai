#!/usr/bin/env python3
"""wakeword_train.py — train dd's own wake word ("dee dee") on openWakeWord embeddings, free and offline.

How it works: Piper TTS (hundreds of synthetic voices) says the wake phrase and thousands of other sentences /
confusable words; each clip is augmented (noise, reverb, gain, clipping) and turned into openWakeWord's 96-dim
speech embeddings (the same features the pre-trained "hey jarvis" model reads). A tiny classifier
(1 hidden layer) learns "the last 1.3 s was dee dee". Weights go to skills/voice/assets/wake_dd.npz (~1 MB);
examples/dd_listen.py loads them next to the hey-jarvis model (DDHead). CPU only; ~30-40 min on 2 cores.

  python3 wakeword_train.py voices  [--voices DIR]        # download 6 Piper voices from Hugging Face (~400 MB)
  python3 wakeword_train.py all     [--work DIR] [--out skills/voice/assets/wake_dd.npz]
  python3 wakeword_train.py gen|feats|train|export        # single stages (cached in --work)
  python3 wakeword_train.py --selftest                    # tiny synthetic run (no Piper, no downloads)

pip install piper-tts openwakeword scipy scikit-learn numpy   (all free)
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SR = 16000
WIN = 2 * SR  # 2.0 s training windows; the detector scores the last 16 embedding frames (~1.3 s)
T = 16
VOICE_URLS = {  # name: path inside rhasspy/piper-voices
    "en_US-libritts_r-medium": "en/en_US/libritts_r/medium/en_US-libritts_r-medium",  # 904 speakers
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    "en_US-ryan-medium": "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_GB-alan-medium": "en/en_GB/alan/medium/en_GB-alan-medium",
    "en_GB-jenny_dioco-medium": "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium",
}
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
POS_TEXTS = ["dee dee", "dee-dee", "hey dee dee", "D D", "D. D.", "ok dee dee", "hi dee dee", "deedee",
             "dee dee!", "hey, dee dee", "D.D.", "hey D D"]
HARD_NEG = ["daddy", "teddy", "ready", "steady", "TV", "CD", "DVD", "deep", "indeed", "needy", "speedy", "tidy",
            "duty", "data", "today", "detail", "daily", "did he", "D", "B B", "P P", "T T", "G G", "E E", "three three",
            "jarvis", "hey jarvis", "hey siri", "alexa", "ok google", "hey", "hey you", "the", "deed", "seedy", "did it",
            "dude", "dad", "did", "deal", "dear", "dean", "a d", "idea", "video", "media", "pdf", "d day", "db",
            "chrome open pannu", "volume thirty", "innaiku weather enna", "screenshot edu", "sollu", "enna", "pannu",
            "dee", "tee tee", "bee bee", "pee pee", "key key", "gee gee", "we we", "me me", "see see", "knee knee"]
VOCAB = ("the of and to in is you that it he was for on are as with his they I at be this have from or one had by word "
         "but not what all were we when your can said there use an each which she do how their if will up other about "
         "out many then them these so some her would make like him into time has look two more write go see number no "
         "way could people my than first water been call who oil its now find long down day did get come made may part "
         "open close play stop music volume weather today tomorrow morning evening night remind me later send message "
         "chrome safari file folder screen show tell give take run check email calendar meeting call phone home work "
         "coffee tea lunch dinner movie anime episode video game code deploy server database table price market news "
         "please thanks okay yes no maybe never always sometimes quickly slowly really very much little big small").split()
TAMIL = ("pannu sollu enna epdi ethu inga anga ippo appo innaiku naalaikku neram mani vanakkam nandri seri illa aama "
         "kaattu edu podu thaa kudu paaru kelu vaa po iru irukku iruken panren paakalam seekiram konjam romba nalla").split()

rng = random.Random(7)


def _np():
    import numpy as np
    return np


# ---------------- audio helpers ----------------
def sentence() -> str:
    return " ".join(rng.choice(VOCAB if rng.random() < 0.75 else TAMIL) for _ in range(rng.randint(2, 7)))


def noise(n: int, kind: str | None = None):
    np = _np()
    g = np.random.default_rng(rng.randrange(1 << 30))
    kind = kind or rng.choice(["white", "pink", "brown", "hum"])
    w = g.standard_normal(n).astype(np.float32)
    if kind == "white":
        x = w
    elif kind == "pink":
        X = np.fft.rfft(w)
        x = np.fft.irfft(X / np.sqrt(np.arange(len(X)) + 1), n)
    elif kind == "brown":
        x = np.cumsum(w)
        x = x - np.linspace(x[0], x[-1], n)
    else:
        t = np.arange(n) / SR
        x = np.sin(2 * np.pi * 50 * t) + 0.3 * np.sin(2 * np.pi * 150 * t) + 0.2 * w
    x = x.astype(np.float32)
    return x / (np.max(np.abs(x)) + 1e-6)


def reverb(a):
    np = _np()
    from scipy.signal import fftconvolve
    L = int(SR * rng.uniform(0.08, 0.4))
    ir = np.random.default_rng(rng.randrange(1 << 30)).standard_normal(L).astype(np.float32) * np.exp(-np.arange(L) / (L / 4))
    ir[0] = 1.0
    y = fftconvolve(a, ir)[: len(a)]
    return (y / (np.max(np.abs(y)) + 1e-6) * np.max(np.abs(a))).astype(np.float32)


def place(a, end_offset_s: float = 0.0, win: int = WIN):
    """Clip `a` inside a window so that it ENDS end_offset_s before the window end (how the detector sees it).
    A negative offset lets the clip run past the window end (its tail is cut); a long clip loses its head."""
    np = _np()
    out = np.zeros(win, dtype=np.float32)
    end = win - int(end_offset_s * SR)
    start = end - len(a)
    a0, a1 = max(0, -start), len(a) - max(0, end - win)
    start, end = max(0, start), min(end, win)
    out[start:end] = a[a0:a1]
    return out


def speed_perturb(a, lo: float = 0.85, hi: float = 1.15):
    """Resample by a random factor: faster/slower and higher/lower pitched at once (speaker variety)."""
    np = _np()
    from scipy.signal import resample_poly
    f = rng.uniform(lo, hi)
    up, down = int(round(100 / f)), 100
    y = resample_poly(a, up, down).astype(np.float32)
    return y


def espeak_synth(text: str):
    """espeak-ng (formant synthesis) — a second, very different 'speaker family' when it is installed."""
    import shutil
    import subprocess
    np = _np()
    if not shutil.which("espeak-ng") or not shutil.which("ffmpeg"):
        return None
    voice = rng.choice(["en-us", "en-gb", "en-in", "en-us+f3", "en-gb+m4", "en-in+f2", "en-us+m7", "en+f5"])
    try:
        wav = subprocess.run(["espeak-ng", "-v", voice, "-p", str(rng.randint(25, 75)), "-s", str(rng.randint(120, 200)),
                              "--stdout", text], check=True, capture_output=True, timeout=20).stdout
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                             input=wav, check=True, capture_output=True, timeout=20).stdout
    except Exception:
        return None
    a = np.frombuffer(pcm, dtype=np.float32).copy()
    if len(a) < 1600:
        return None
    a = a / (np.max(np.abs(a)) + 1e-6) * 0.7
    e = np.abs(a) > 0.01
    if e.any():
        i, j = int(np.argmax(e)), len(a) - int(np.argmax(e[::-1]))
        a = a[max(0, i - 400): j + 400]
    return a


def augment(x, strong: bool = True):
    np = _np()
    y = x.copy()
    if rng.random() < 0.5:
        y = reverb(y)
    y = y * 10 ** (rng.uniform(-14, 4) / 20)
    snr = rng.uniform(3, 30) if strong else rng.uniform(15, 40)
    sig = float(np.sqrt(np.mean(y ** 2))) + 1e-6
    y = y + noise(len(y)) * sig / (10 ** (snr / 20))
    if rng.random() < 0.3:
        y = np.tanh(y * 1.5)
    return np.clip(y, -1, 1).astype(np.float32)


# ---------------- Piper ----------------
def download_voices(folder: Path, names=tuple(VOICE_URLS)) -> list[str]:
    import urllib.request
    folder.mkdir(parents=True, exist_ok=True)
    got = []
    for name in names:
        for ext in (".onnx", ".onnx.json"):
            dst = folder / (name + ext)
            if dst.is_file():
                continue
            url = HF + VOICE_URLS[name] + ext
            print(f"downloading {dst.name}...", flush=True)
            with urllib.request.urlopen(url, timeout=120) as r, open(str(dst) + ".part", "wb") as out:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
            os.replace(str(dst) + ".part", dst)
        got.append(name)
    return got


def load_voices(folder: Path) -> dict:
    from piper import PiperVoice  # pip install piper-tts
    out = {}
    for f in sorted(glob.glob(str(folder / "*.onnx"))):
        out[os.path.basename(f)[:-5]] = PiperVoice.load(f)
    if not out:
        raise SystemExit(f"no Piper voices in {folder} — run: wakeword_train.py voices")
    return out


def synth(voice, text: str, spk=None):
    np = _np()
    from scipy.signal import resample_poly
    from piper.config import SynthesisConfig
    cfg = SynthesisConfig(speaker_id=spk, length_scale=rng.uniform(0.75, 1.35), noise_scale=rng.uniform(0.3, 0.9),
                          noise_w_scale=rng.uniform(0.4, 1.1))
    a = np.concatenate([c.audio_float_array for c in voice.synthesize(text, cfg)]).astype(np.float32)
    a = resample_poly(a, 320, 441).astype(np.float32)  # 22050 -> 16000
    a = a / (np.max(np.abs(a)) + 1e-6) * 0.7
    e = np.abs(a) > 0.01
    if e.any():
        i, j = int(np.argmax(e)), len(a) - int(np.argmax(e[::-1]))
        a = a[max(0, i - 400): j + 400]
    return a


# ---------------- stages ----------------
def stage_gen(work: Path, voices_dir: Path, n_pos=2400, n_neg=3000, n_hard=1500, n_noise=600, pos_only: bool = False) -> None:
    np = _np()
    voices = load_voices(voices_dir)
    names = list(voices)
    multi = [n for n in names if "libritts" in n]

    def pick():
        name = rng.choice(names + multi * 3)
        spk = rng.randrange(voices[name].config.num_speakers) if voices[name].config.num_speakers > 1 else None
        return name, voices[name], spk

    pos, posmeta, neg, negmeta = [], [], [], []
    t0 = time.time()
    use_espeak = espeak_synth("test") is not None

    def say(text):
        """A clip from Piper (85%) or espeak-ng (15%, when installed): two very different voice families."""
        if use_espeak and rng.random() < 0.15:
            a = espeak_synth(text)
            if a is not None:
                return "espeak", None, a
        name, v, spk = pick()
        return name, spk, synth(v, text, spk)

    for i in range(n_pos):
        text = rng.choice(POS_TEXTS)
        name, spk, a = say(text)
        tail = None
        if i % 3 == 2:  # one in three: the command follows in the same breath ("dee dee chrome open pannu")
            _n, _s, tail = say(sentence())
        for k in range(3):
            b = a if k == 0 else speed_perturb(a)
            if tail is not None:
                gap = np.zeros(int(SR * rng.uniform(0.03, 0.2)), np.float32)
                extra = np.concatenate([gap, tail[: int(SR * 0.9)]])
                # dee dee ends 0-0.5 s before the window end and the next words run on past it
                x = place(np.concatenate([b, extra]), rng.uniform(0.0, 0.5) - len(extra) / SR)
            else:
                x = place(b, rng.uniform(0.0, 0.35))
            pos.append(x if k == 0 else augment(x))
            posmeta.append({"voice": name, "spk": spk, "text": text, "aug": k, "tail": tail is not None})
        if i % 300 == 0:
            print(f"pos {i}/{n_pos} {time.time() - t0:.0f}s", flush=True)
    if pos_only and (work / "neg.npy").is_file() and (work / "meta.json").is_file():
        old_meta = json.loads((work / "meta.json").read_text())
        (work / "meta.json").write_text(json.dumps({"pos": posmeta, "neg": old_meta["neg"]}))
        np.save(work / "pos.npy", np.stack(pos))
        print(f"gen done (positives only): pos {len(pos)}, negatives kept ({len(old_meta['neg'])}) in {time.time() - t0:.0f}s", flush=True)
        return
    for i in range(n_neg):
        text = sentence()
        name, spk, a = say(text)
        for k in range(2):
            b = a if k == 0 else speed_perturb(a)
            x = place(b, rng.uniform(0.0, 0.6))
            if len(b) > WIN:
                s = rng.randrange(0, len(b) - WIN + 1)
                x = b[s: s + WIN].copy()
            neg.append(x if k == 0 else augment(x))
            negmeta.append({"voice": name, "spk": spk, "text": text, "aug": k})
        if i % 300 == 0:
            print(f"neg {i}/{n_neg} {time.time() - t0:.0f}s", flush=True)
    for i in range(n_hard):
        text = rng.choice(HARD_NEG)
        name, spk, a = say(text)
        for k in range(4):  # confusables get one more augmented copy: they are what the head must learn to refuse
            b = a if k == 0 else speed_perturb(a)
            x = place(b, rng.uniform(0.0, 0.35))
            neg.append(x if k == 0 else augment(x))
            negmeta.append({"voice": name, "spk": spk, "text": text, "aug": k, "hard": True})
    for i in range(n_noise):
        kind = rng.choice(["white", "pink", "brown", "hum", "silence"])
        x = np.zeros(WIN, dtype=np.float32) if kind == "silence" else noise(WIN, kind) * 10 ** (rng.uniform(-50, -6) / 20)
        neg.append(x.astype(np.float32))
        negmeta.append({"voice": "noise", "text": kind})
    work.mkdir(parents=True, exist_ok=True)
    np.save(work / "pos.npy", np.stack(pos))
    np.save(work / "neg.npy", np.stack(neg))
    (work / "meta.json").write_text(json.dumps({"pos": posmeta, "neg": negmeta}))
    print(f"gen done: pos {len(pos)} neg {len(neg)} in {time.time() - t0:.0f}s", flush=True)


def audio_features():
    """openWakeWord's feature extractor (melspectrogram + speech embedding ONNX), any installed version."""
    import importlib.metadata as md
    import openwakeword
    from openwakeword.utils import AudioFeatures
    sys.path.insert(0, str(SKILL.parents[1] / "examples"))
    import dd_listen  # the same model-file logic the app uses (bundled or ~/.dd/models/oww)
    folder = dd_listen.oww_model_dir()
    dd_listen.ensure_oww_models(folder)
    mel, emb = os.path.join(folder, "melspectrogram.onnx"), os.path.join(folder, "embedding_model.onnx")
    try:
        new = tuple(int(x) for x in md.version("openwakeword").split(".")[:2]) >= (0, 5)
    except Exception:
        new = True
    if new:
        return AudioFeatures(melspec_model_path=mel, embedding_model_path=emb, inference_framework="onnx", ncpu=2)
    return AudioFeatures(melspec_onnx_model_path=mel, embedding_onnx_model_path=emb, ncpu=2)


def stage_feats(work: Path, chunk: int = 1000) -> None:
    """Embeddings in chunks of `chunk` clips (memory-mapped input) — the clip arrays are GBs, RAM is not."""
    np = _np()
    af = audio_features()
    for name in ("pos", "neg"):
        x = np.load(work / f"{name}.npy", mmap_mode="r")
        t0 = time.time()
        parts = []
        for i in range(0, len(x), chunk):
            xi = (np.asarray(x[i:i + chunk]) * 32767).astype(np.int16)
            parts.append(af.embed_clips(xi, batch_size=64).astype(np.float32))
            del xi
            print(f"{name} {min(i + chunk, len(x))}/{len(x)} {time.time() - t0:.0f}s", flush=True)
        emb = np.concatenate(parts)
        print(f"{name} embeddings {emb.shape} in {time.time() - t0:.0f}s", flush=True)
        np.save(work / f"{name}_emb.npy", emb)


def fit_head(X, y, hidden: int = 96, seed: int = 0):
    """Standardise + one-hidden-layer MLP (sklearn). Returns (clf, mu, sd)."""
    from sklearn.neural_network import MLPClassifier
    np = _np()
    mu, sd = X.mean(0), X.std(0) + 1e-5
    clf = MLPClassifier(hidden_layer_sizes=(hidden,), alpha=1e-3, max_iter=300, early_stopping=True, random_state=seed)
    clf.fit((X - mu) / sd, y)
    return clf, mu, sd


def export(clf, mu, sd, out: Path) -> None:
    np = _np()
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, mu=mu.astype(np.float32), sd=sd.astype(np.float32),
                        w1=clf.coefs_[0].astype(np.float32), b1=clf.intercepts_[0].astype(np.float32),
                        w2=clf.coefs_[1].astype(np.float32), b2=clf.intercepts_[1].astype(np.float32), T=T)


def stage_train(work: Path, out: Path) -> dict:
    np = _np()
    pe, ne = np.load(work / "pos_emb.npy"), np.load(work / "neg_emb.npy")
    meta = json.loads((work / "meta.json").read_text())

    def last(e):
        return e[:, -T:, :].reshape(len(e), -1)

    Xp = last(pe)
    Xn = np.concatenate([last(ne), ne[:, :T, :].reshape(len(ne), -1)])  # end window + start window
    X = np.concatenate([Xp, Xn])
    y = np.concatenate([np.ones(len(Xp)), np.zeros(len(Xn))])
    hold_spk = set(range(0, 904, 7))

    def held(m):
        return m["voice"] == "en_GB-jenny_dioco-medium" or (m.get("spk") in hold_spk)

    hp = np.array([held(m) for m in meta["pos"]])
    hn = np.array([held(m) for m in meta["neg"]])
    hold = np.concatenate([hp, hn, hn])
    clf, mu, sd = fit_head(X[~hold], y[~hold])
    p = clf.predict_proba((X[hold] - mu) / sd)[:, 1]
    yt = y[hold]
    report = {}
    for thr in (0.5, 0.8, 0.9, 0.95):
        report[str(thr)] = {"recall": round(float(np.mean(p[yt == 1] >= thr)), 4),
                            "fp_rate": round(float(np.mean(p[yt == 0] >= thr)), 5)}
        print(f"held-out voices @ {thr}: recall {report[str(thr)]['recall']}  fp-rate {report[str(thr)]['fp_rate']}")
    clf, mu, sd = fit_head(X, y)  # final weights on everything
    export(clf, mu, sd, out)
    print(f"exported {out} ({out.stat().st_size} bytes)")
    (work / "report.json").write_text(json.dumps(report, indent=1))
    return report


# ---------------- selftest ----------------
def selftest() -> int:
    """Pipeline mechanics on synthetic tones (no Piper, no network, no model download): helpers, head fit, export
    + a numpy forward pass identical to DDHead's. openWakeWord features are only used when already installed."""
    import tempfile
    np = _np()
    work = Path(tempfile.mkdtemp(prefix="dd-wake-test-"))
    a = np.sin(2 * np.pi * 440 * np.arange(SR // 2) / SR).astype(np.float32) * 0.5
    w = place(a, 0.1)
    assert w.shape == (WIN,) and w[-int(0.1 * SR) - 10] != 0 and w[-1] == 0 and w[0] == 0
    over = place(a, -0.2)  # runs 0.2 s past the end: the last sample is still signal, nothing crashes
    assert over.shape == (WIN,) and over[-1] != 0 and over[-int(0.5 * SR) - 10] == 0
    aug = augment(w)
    assert aug.shape == (WIN,) and float(np.max(np.abs(aug))) <= 1.0 and not np.array_equal(aug, w)
    assert noise(1000, "pink").shape == (1000,) and reverb(w).shape == (WIN,)
    sp = speed_perturb(a)
    assert abs(len(sp) / len(a) - 1.0) < 0.2 and sp.dtype == np.float32
    es = espeak_synth("dee dee")
    assert es is None or (es.ndim == 1 and len(es) > 1600 and float(np.max(np.abs(es))) <= 0.71)
    assert 2 <= len(sentence().split()) <= 7
    # a toy embedding problem: positives have a bump in feature 0 of the last frames
    g = np.random.default_rng(0)
    pe = g.normal(size=(120, 20, 96)).astype(np.float32)
    ne = g.normal(size=(200, 20, 96)).astype(np.float32)
    pe[:, -T:, 0] += 3.0
    X = np.concatenate([pe[:, -T:, :].reshape(120, -1), ne[:, -T:, :].reshape(200, -1)])
    y = np.concatenate([np.ones(120), np.zeros(200)])
    clf, mu, sd = fit_head(X, y, hidden=8)
    out = work / "wake_dd.npz"
    export(clf, mu, sd, out)
    z = np.load(out)
    assert z["w1"].shape == (T * 96, 8) and z["w2"].shape == (8, 1) and int(z["T"]) == T

    def forward(feats):  # DDHead.score, verbatim maths
        x = (feats.reshape(1, -1) - z["mu"]) / z["sd"]
        h = np.maximum(0.0, x @ z["w1"] + z["b1"])
        o = float((h @ z["w2"] + z["b2"])[0, 0])
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, o))))

    pp = np.array([forward(e[-T:, :]) for e in pe]); pn = np.array([forward(e[-T:, :]) for e in ne])
    assert np.mean(pp > 0.5) > 0.9 and np.mean(pn < 0.5) > 0.9 and pp.mean() > pn.mean() + 0.3, (pp.mean(), pn.mean())
    assert abs(forward(pe[1, -T:, :]) - clf.predict_proba(((pe[1, -T:, :].reshape(1, -1)) - mu) / sd)[0, 1]) < 1e-4
    try:
        import openwakeword  # noqa: F401
        have_oww = True
    except ImportError:
        have_oww = False
    if have_oww and os.environ.get("DD_WAKE_TEST_FEATS") == "1":  # opt-in: needs the model files
        af = audio_features()
        emb = af.embed_clips((np.stack([w, aug]) * 32767).astype(np.int16), batch_size=2)
        assert emb.shape[0] == 2 and emb.shape[2] == 96 and emb.shape[1] >= T, emb.shape
    print("wakeword_train selftest ok (placement, augmentation, noise/reverb, head fit + export, numpy forward == sklearn)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["voices", "gen", "feats", "train", "all"])
    ap.add_argument("--work", default=str(Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "wake-train"))
    ap.add_argument("--voices", default=str(Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "piper-voices"))
    ap.add_argument("--out", default=str(SKILL / "assets" / "wake_dd.npz"))
    ap.add_argument("--pos", type=int, default=2400)
    ap.add_argument("--neg", type=int, default=3000)
    ap.add_argument("--pos-only", action="store_true", help="regenerate positives only, keep the work dir's negatives")
    a = ap.parse_args(argv)
    work, voices, out = Path(a.work), Path(a.voices), Path(a.out)
    if a.stage == "voices":
        print(json.dumps({"voices": download_voices(voices), "dir": str(voices)}))
        return 0
    if a.stage in ("gen", "all"):
        if not glob.glob(str(voices / "*.onnx")):
            download_voices(voices)
        stage_gen(work, voices, n_pos=a.pos, n_neg=a.neg, pos_only=a.pos_only)
    if a.stage in ("feats", "all"):
        stage_feats(work)
    if a.stage in ("train", "all"):
        stage_train(work, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
