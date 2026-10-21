#!/usr/bin/env python3
"""dd_aec.py — removes what the Mac itself plays (a film, music, dd's own voice) from the microphone signal.

dd-ear (macapp/dd-ear.swift) delivers the mic and the Mac's sound output in one clock; this module subtracts
an adaptive model of the speaker -> mic echo from the mic. The canceller is a partitioned-block frequency-domain
Kalman filter (PFDKF, Kuech/Mabande/Enzner 2014): linear and purely subtractive — Dhina's voice passes through
untouched while the film is taken out — and its step size comes from the Kalman gain, so it slows itself down
while he talks (no double-talk detector to tune) and converges within ~1 s of the film starting.

  python3 examples/dd_aec.py --selftest      synthetic film echo + a voice: echo removed, voice kept
"""
from __future__ import annotations

import sys

import numpy as np


class EchoCanceller:
    """mic, ref (float32 arrays of `block` samples, same clock) -> mic with the echo of ref removed.
    block 160 = 10 ms at 16 kHz; partitions x block = the echo tail it can model (default 24 -> 240 ms: the
    output/input latency difference plus a laptop's short room reverb)."""

    def __init__(self, block: int = 160, partitions: int = 24, A: float = 0.9995, P0: float = 1.0,
                 smooth: float = 0.5):
        M = block
        self.M, self.N = M, partitions
        self.A, self.A2 = A, A * A
        self.P0 = P0
        self.smooth = smooth
        self.reset()

    def reset(self) -> None:
        M, N = self.M, self.N
        self.x = np.zeros(2 * M, np.float64)
        self.X = np.zeros((N, M + 1), np.complex128)
        self.H = np.zeros((N, M + 1), np.complex128)
        self.P = np.full((N, M + 1), self.P0, np.float64)
        self.m = np.zeros(M + 1, np.float64)
        self.k = 0
        self.ref_power = 0.0
        self.erle = 0.0  # smoothed echo removed (dB) while the Mac plays something

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        M = self.M
        d = np.asarray(mic, np.float64) / 32768.0
        r = np.asarray(ref, np.float64) / 32768.0
        self.x = np.concatenate([self.x[M:], r])
        self.X[1:] = self.X[:-1]
        self.X[0] = np.fft.rfft(self.x)
        y = np.fft.irfft((self.H * self.X).sum(0), 2 * M)[M:]
        e = d - y
        E = np.fft.rfft(np.concatenate([np.zeros(M), e]))
        self.m = self.smooth * self.m + (1 - self.smooth) * (E.real ** 2 + E.imag ** 2)
        Xp = self.X.real ** 2 + self.X.imag ** 2
        R = (self.P * Xp).sum(0) + 2.0 * self.m / self.N
        mu = self.P / (R + 1e-10)
        G = mu * self.X.conj()
        self.P = self.A2 * (1.0 - 0.5 * mu * Xp) * self.P + (1.0 - self.A2) * (self.H.real ** 2 + self.H.imag ** 2)
        self.H = self.A * (self.H + G * E)
        p = self.k % self.N  # gradient constraint, one partition per block (keeps it a linear convolution)
        h = np.fft.irfft(self.H[p], 2 * M)
        h[M:] = 0.0
        self.H[p] = np.fft.rfft(h)
        self.k += 1
        pr = float((r ** 2).mean())
        self.ref_power = 0.95 * self.ref_power + 0.05 * pr
        if pr > 1e-6:
            de, ee = float((d ** 2).mean()) + 1e-12, float((e ** 2).mean()) + 1e-12
            self.erle = 0.98 * self.erle + 0.02 * 10 * np.log10(de / ee)
        return np.clip(e * 32768.0, -32768, 32767).astype(np.float32)


def _room_ir(rng, delay_ms: float, rt_ms: float, sr: int = 16000) -> np.ndarray:
    n = int(0.2 * sr)
    ir = np.zeros(n, np.float32)
    d = int(delay_ms * sr / 1000)
    ir[d] = 1.0
    tail = rng.normal(size=n - d - 1).astype(np.float32) * np.exp(-np.arange(n - d - 1) / (rt_ms * sr / 1000 / 6.9))
    ir[d + 1:] = 0.25 * tail
    return ir


def selftest() -> int:
    rng = np.random.default_rng(5)
    sr = 16000
    # "film": band-limited noise with a speech-like envelope, 6 s; "voice": a 0.6 s harmonic burst at 4.5 s
    far = rng.normal(size=6 * sr).astype(np.float32)
    far = np.convolve(far, np.hanning(9) / np.hanning(9).sum(), mode="same")
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * np.arange(len(far)) / sr) ** 2
    far = (far * env / np.abs(far).max() * 12000).astype(np.float32)
    echo = np.convolve(far, _room_ir(rng, 35.0, 120.0))[: len(far)] * 0.6
    t = np.arange(int(0.6 * sr)) / sr
    voice = sum(np.sin(2 * np.pi * f * t) / (i + 1) for i, f in enumerate((180, 360, 540, 720))) * np.hanning(len(t))
    near = np.zeros_like(far)
    s0 = int(4.5 * sr)
    near[s0:s0 + len(t)] = voice / np.abs(voice).max() * 5000
    mic = echo + near + rng.normal(size=len(far)).astype(np.float32) * 20
    aec = EchoCanceller()
    out = np.concatenate([aec.process(mic[i:i + 160], far[i:i + 160]) for i in range(0, len(mic) - 159, 160)])
    a, b = 2 * sr, s0 - sr // 10  # film only, after 2 s of convergence
    erle = 10 * np.log10((echo[a:b] ** 2).mean() / (out[a:b] ** 2).mean())
    kept = (out[s0:s0 + len(t)] * near[s0:s0 + len(t)]).sum() / (near[s0:s0 + len(t)] ** 2).sum()
    assert erle > 15, f"echo removed only {erle:.1f} dB"
    assert 0.8 < kept < 1.2, f"voice kept x{kept:.2f}"
    assert aec.erle > 10, aec.erle
    silent = EchoCanceller()  # nothing playing: the mic passes through unchanged
    m = rng.normal(size=160).astype(np.float32) * 3000
    assert np.allclose(silent.process(m, np.zeros(160, np.float32)), m, atol=1.0)
    print(f"dd_aec selftest ok (film echo removed {erle:.1f} dB, voice kept x{kept:.2f}, pass-through when silent)")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else 0)
