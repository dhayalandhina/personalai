#!/usr/bin/env python3
"""wakeword_real.py — retrain the plain-"jarvis" head on REAL audio: what the shipped assets/wake_jarvis.npz is.

Why: the head trained on synthetic voices alone fired on real Japanese anime speech ~90 times an hour and missed
some of Dhina's own quiet "Jarvis" (its score swung with the 80 ms chunk alignment). This adds:
  * real negatives — Japanese anime speech, JSUT, Tamil (FLEURS ta_in), English (LibriSpeech), hard-mined twice
    with the head itself (the windows it still fires on);
  * his voice-setup recordings as positives — 300 augmented copies each (speed, reverb, room noise, level), a third
    of them mixed into real media; plus synthetic positives mixed into media (a "jarvis" said over a video) and the
    same media without the word as negatives.
Result (21 Oct 2026): held-out media 112 -> 31.5 false candidates/hour at 0.7, his recordings 1.0 at every chunk
alignment in leave-one-out, listener replay 18/18 in a quiet room, 0 false wakes (Whisper confirms each candidate).

  python3 wakeword_real.py media  --real DIR                    # download + decode the speech sets (~3 GB, HF)
  python3 wakeword_real.py all    --real DIR --work DIR --base WORKJ --voice DIR [--out FILE]
     --base:  a `wakeword_train.py all --word jarvis` work dir (pos_emb.npy, neg_emb.npy, pos.npy ...)
     --voice: folder with setup-jarvis-*.wav (menu → Teach 'Jarvis'; mirrored to logs/voice)
Needs: pip install openwakeword scikit-learn scipy huggingface_hub pyarrow soundfile librosa
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import math
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wakeword_train as W  # noqa: E402

SR, T = W.SR, W.T
MEDIA = [  # name, HF dataset, parquet file
    ("anime0", "joujiboi/japanese-anime-speech", "data/train-00000-of-00021.parquet"),
    ("anime1", "joujiboi/japanese-anime-speech", "data/train-00001-of-00021.parquet"),
    ("fleurs_ta_val", "google/fleurs", "parquet-data/ta_in/validation-00000-of-00001.parquet"),
    ("libri_test_clean", "openslr/librispeech_asr", "all/test.clean/0000.parquet"),
    ("libri_test_other", "openslr/librispeech_asr", "all/test.other/0000.parquet"),
]
TRAIN = [("anime0", 2.6), ("fleurs_ta_val", 2.0), ("libri_test_clean", 2.0)]
TEST = [("anime1", 1.0), ("libri_test_other", 1.0)]
rng = np.random.default_rng(11)
_af = None


def af():
    global _af
    if _af is None:
        _af = W.audio_features()
    return _af


def stage_media(real: Path, hours: float = 3.0) -> None:
    import pyarrow.parquet as pq
    import soundfile as sf
    from huggingface_hub import hf_hub_download
    real.mkdir(parents=True, exist_ok=True)
    for name, repo, path in MEDIA:
        if (real / f"{name}.npy").is_file():
            continue
        p = hf_hub_download(repo, path, repo_type="dataset")
        pf = pq.ParquetFile(p)
        col = [c for c in pf.schema_arrow.names if "audio" in c.lower()][0]
        out, secs = [], 0.0
        for batch in pf.iter_batches(batch_size=64, columns=[col]):
            for row in batch.column(0).to_pylist():
                try:
                    a, sr = sf.read(io.BytesIO(row["bytes"]), dtype="float32")
                except Exception:
                    continue
                if a.ndim > 1:
                    a = a.mean(1)
                if sr != SR:
                    import librosa
                    a = librosa.resample(a, orig_sr=sr, target_sr=SR)
                out.append(a.astype(np.float32))
                secs += len(a) / SR
            if secs > hours * 3600:
                break
        np.save(real / f"{name}.npy", np.concatenate(out))
        print(f"{name}: {secs / 3600:.2f} h", flush=True)


def load_head(path) -> dict:
    z = np.load(path)
    return {k: z[k] for k in z.files}


def head_scores(z, X) -> np.ndarray:
    out = np.empty(len(X), np.float32)
    for i in range(0, len(X), 4096):
        x = (X[i:i + 4096].astype(np.float32) - z["mu"]) / z["sd"]
        h = np.maximum(0.0, x @ z["w1"] + z["b1"])
        o = (h @ z["w2"] + z["b2"])[:, 0]
        out[i:i + 4096] = 1.0 / (1.0 + np.exp(-np.clip(o, -30, 30)))
    return out


def embed_stream(x, chunk_s: float = 30.0):
    n = int(chunk_s * SR)
    for i in range(0, len(x) - SR, n):
        c = np.asarray(x[i:i + n], dtype=np.float32)
        if len(c) < 2 * SR:
            continue
        c = c / (np.abs(c).max() + 1e-6) * rng.uniform(0.05, 0.6)  # media at many levels
        yield af().embed_clips((c * 32767).astype(np.int16)[None, :], batch_size=1)[0].astype(np.float32)


def windows(e, stride: int = 2) -> np.ndarray:
    if e.shape[0] < T:
        return np.zeros((0, T * 96), np.float32)
    return np.stack([e[f - T:f].reshape(-1) for f in range(T, e.shape[0] + 1, stride)])


def mine(real: Path, srcs, head, keep_score: float, keep_random: float, stride: int) -> np.ndarray:
    z = load_head(head) if head else None
    kept = []
    for name, hours in srcs:
        x = np.load(real / f"{name}.npy", mmap_mode="r")[: int(hours * 3600 * SR)]
        t0, n = time.time(), 0
        for e in embed_stream(x):
            Xw = windows(e, stride)
            if not len(Xw):
                continue
            if z is not None:
                m = (head_scores(z, Xw) >= keep_score) | (rng.random(len(Xw)) < keep_random)
                Xw = Xw[m]
            kept.append(Xw.astype(np.float16))
            n += len(Xw)
        print(f"  {name}: kept {n} windows ({time.time() - t0:.0f}s)", flush=True)
    return np.concatenate(kept) if kept else np.zeros((0, T * 96), np.float16)


def load_wav(p) -> np.ndarray:
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def trim_word(a):
    fr = a[: len(a) // 160 * 160].reshape(-1, 160)
    e = np.sqrt((fr ** 2).mean(1))
    idx = np.where(e > max(np.percentile(e, 20) * 3, e.max() * 0.08))[0]
    return a[max(0, idx[0] - 8) * 160: min(len(e), idx[-1] + 12) * 160]


def media_segment(real: Path, n: int) -> np.ndarray:
    name = TRAIN[int(rng.integers(len(TRAIN)))][0]
    x = np.load(real / f"{name}.npy", mmap_mode="r")
    s = int(rng.integers(0, len(x) - n - 1))
    seg = np.asarray(x[s:s + n], dtype=np.float32)
    return seg / (np.abs(seg).max() + 1e-6)


def mix(word_win, media, snr_db):
    nz = word_win[word_win != 0]
    sig = math.sqrt(float(np.mean(nz ** 2)) + 1e-9) if nz.size else 1e-3
    med = math.sqrt(float(np.mean(media ** 2)) + 1e-9)
    y = word_win + media * (sig / med) / (10 ** (snr_db / 20))
    return y / (np.abs(y).max() + 1e-6) * rng.uniform(0.05, 0.7)


def embed_last(clips) -> np.ndarray:
    out = []
    for i in range(0, len(clips), 200):
        e = af().embed_clips((np.stack(clips[i:i + 200]) * 32767).astype(np.int16), batch_size=64)
        out.append(e[:, -T:, :].reshape(len(e), -1).astype(np.float16))
    return np.concatenate(out)


def positives(real: Path, base: Path, voice: Path, n_his: int = 300, n_mix: int = 3000):
    his = [trim_word(load_wav(p)) for p in sorted(voice.glob("setup-jarvis-*.wav"))]
    beds = [load_wav(p) for p in sorted(voice.glob("near-*.wav"))] or [np.zeros(SR, np.float32)]
    clips, owner = [], []
    for ci, a in enumerate(his):
        for k in range(n_his):
            b = W.speed_perturb(a, 0.92, 1.08) if k % 4 else a
            if rng.random() < 0.4:
                b = W.reverb(b)
            x = W.place(b, rng.uniform(0.0, 0.45) + rng.integers(0, 1280) / SR)  # every chunk alignment
            r = rng.random()
            if r < 0.35:
                x = mix(x, media_segment(real, len(x)), rng.uniform(0, 15))
            elif r < 0.6:
                x = mix(x, np.resize(beds[int(rng.integers(len(beds)))], len(x)), rng.uniform(5, 25))
            else:
                x = W.augment(x, strong=rng.random() < 0.5)
            clips.append(x.astype(np.float32))
            owner.append(ci)
    his_X = embed_last(clips)
    P = np.load(base / "pos.npy", mmap_mode="r")
    sel = rng.choice(len(P), size=min(n_mix, len(P)), replace=False)
    mix_pos = embed_last([mix(np.asarray(P[j], np.float32), media_segment(real, P.shape[1]), rng.uniform(0, 15))
                          for j in sel])
    mix_neg = embed_last([media_segment(real, 2 * SR) * rng.uniform(0.05, 0.6) for _ in range(n_mix)])
    return his_X, np.array(owner), mix_pos, mix_neg


def fit(base: Path, his_X, mix_pos, mix_neg, real_neg, out: Path, hidden: int = 96) -> Path:
    from sklearn.neural_network import MLPClassifier
    last = lambda e: np.asarray(e[:, -T:, :]).reshape(len(e), -1).astype(np.float32)  # noqa: E731
    Xp = [last(np.load(base / "pos_emb.npy", mmap_mode="r")), his_X.astype(np.float32), his_X.astype(np.float32),
          mix_pos.astype(np.float32)]  # his voice counts double
    Xn = [last(np.load(base / "neg_emb.npy", mmap_mode="r")), mix_neg.astype(np.float32), real_neg.astype(np.float32)]
    if (base / "neg_reuse_emb.npy").is_file():
        Xn.append(last(np.load(base / "neg_reuse_emb.npy", mmap_mode="r")))
    Xp, Xn = np.concatenate(Xp), np.concatenate(Xn)
    print(f"  fit: positives {len(Xp)}, negatives {len(Xn)}", flush=True)
    X = np.concatenate([Xp, Xn])
    y = np.concatenate([np.ones(len(Xp), np.float32), np.zeros(len(Xn), np.float32)])
    del Xp, Xn
    mu, sd = X.mean(0), X.std(0) + 1e-5
    X -= mu
    X /= sd
    clf = MLPClassifier(hidden_layer_sizes=(hidden,), alpha=1e-3, max_iter=200, early_stopping=True, random_state=0)
    clf.fit(X, y)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, mu=mu.astype(np.float32), sd=sd.astype(np.float32),
                        w1=clf.coefs_[0].astype(np.float32), b1=clf.intercepts_[0].astype(np.float32),
                        w2=clf.coefs_[1].astype(np.float32), b2=clf.intercepts_[1].astype(np.float32), T=T)
    return out


def false_per_hour(head: Path, test_X, hours: float, thr: float = 0.7) -> float:
    s = head_scores(load_head(head), test_X)
    n, last = 0, -999
    for i in np.where(s >= thr)[0]:
        if i - last > 10:
            n += 1
        last = i
    return n / hours


def stage_all(real: Path, work: Path, base: Path, voice: Path, out: Path, start_head: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    print("mining real negatives (round 1)...", flush=True)
    neg1 = mine(real, TRAIN, start_head, 0.1, 0.12, 2)
    test_X = mine(real, TEST, None, 0, 0, 2)
    print("positives...", flush=True)
    his_X, owner, mix_pos, mix_neg = positives(real, base, voice)
    h1 = fit(base, his_X, mix_pos, mix_neg, neg1, work / "round1.npz")
    print("mining again with the round-1 head (round 2)...", flush=True)
    neg2 = mine(real, TRAIN, h1, 0.05, 0.04, 1)
    h2 = fit(base, his_X, mix_pos, mix_neg, np.concatenate([neg1, neg2]), out)
    hours = sum(h for _, h in TEST)
    for name, h in (("start", start_head), ("round 1", h1), ("round 2", h2)):
        print(f"{name}: {false_per_hour(h, test_X, hours):.1f} false candidates/hour on held-out media at 0.7",
              flush=True)
    print(f"exported {out}", flush=True)


def selftest() -> int:
    """The data plumbing on synthetic arrays (no downloads, no openWakeWord): windows, mixing, trimming, the head's
    forward pass and the false-candidate counter."""
    e = rng.normal(size=(40, 96)).astype(np.float32)
    w = windows(e, stride=2)
    assert w.shape == ((40 - T) // 2 + 1, T * 96) and np.array_equal(w[0], e[:T].reshape(-1))
    assert windows(e[:5]).shape == (0, T * 96)
    word = W.place(np.sin(np.arange(SR // 2) / 9.0).astype(np.float32) * 0.5, 0.2)
    m = mix(word, rng.normal(size=word.shape).astype(np.float32), 10.0)
    assert m.shape == word.shape and 0.04 < float(np.abs(m).max()) <= 0.71
    a = np.concatenate([np.zeros(SR), np.sin(np.arange(SR // 2) / 7.0) * 0.3, np.zeros(SR)]).astype(np.float32)
    t = trim_word(a)
    assert SR // 2 <= len(t) <= SR // 2 + 20 * 160, len(t)
    z = {"mu": np.zeros(T * 96, np.float32), "sd": np.ones(T * 96, np.float32),
         "w1": np.zeros((T * 96, 4), np.float32), "b1": np.ones(4, np.float32),
         "w2": np.ones((4, 1), np.float32), "b2": np.array([-3.0], np.float32)}
    s = head_scores(z, w)
    assert np.allclose(s, 1 / (1 + math.exp(-1.0)), atol=1e-5)
    import tempfile
    f = Path(tempfile.mkdtemp(prefix="dd-wake-real-")) / "h.npz"
    np.savez(f, **z)
    assert false_per_hour(f, w, 1.0, thr=0.5) == 1.0 and false_per_hour(f, w, 1.0, thr=0.9) == 0.0
    print("wakeword_real selftest ok (windows, media mixing, word trim, head forward, false-candidate count)")
    return 0


def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["media", "all"])
    home = Path(os.environ.get("DD_HOME", Path.home() / ".dd"))
    ap.add_argument("--real", default=str(home / "wake-real"))
    ap.add_argument("--work", default=str(home / "wake-real-work"))
    ap.add_argument("--base", default=str(home / "wake-train-jarvis"))
    ap.add_argument("--voice", default=str(HERE.parents[2] / "logs" / "voice"))
    ap.add_argument("--start", default=str(HERE.parent / "assets" / "wake_jarvis.npz"))
    ap.add_argument("--out", default=str(HERE.parent / "assets" / "wake_jarvis.npz"))
    a = ap.parse_args(argv)
    if a.stage == "media":
        stage_media(Path(a.real))
        return 0
    stage_all(Path(a.real), Path(a.work), Path(a.base), Path(a.voice), Path(a.out), Path(a.start))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
