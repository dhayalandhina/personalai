#!/usr/bin/env python3
"""status_icons.py — draws dd's menu-bar icons (macapp/status/*.png).

A small voice-wave glyph instead of an emoji: five rounded bars, monochrome "template" images that macOS tints
for light/dark menu bars, and one accent (blue) image while dd is listening to you — the same blue as the
on-screen pill. 60x60 px, shown at 20x20 pt (crisp on Retina). Needs Pillow; the PNGs are committed, so the
Mac app does not need Pillow at all.

  python3 examples/status_icons.py            writes macapp/status/*.png
  python3 examples/status_icons.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "macapp" / "status"
PX = 60          # canvas (3x of 20 pt)
SS = 4           # supersampling for smooth edges
BLUE = (59, 139, 255, 255)
RED = (255, 107, 90, 255)
INK = (0, 0, 0, 255)  # template images: only alpha matters

# state -> (kind, heights or None, colour, template?)
BAR_STATES = {
    "listening": ([0.22, 0.42, 0.62, 0.42, 0.22], INK, True),   # calm wave: dd is there, waiting for "Jarvis"
    "recording": ([0.42, 0.78, 1.00, 0.78, 0.42], BLUE, False),  # tall + blue: dd is listening to you now
    "speaking": ([0.36, 0.88, 0.56, 0.96, 0.40], INK, True),    # speech-shaped wave
    "starting": ([0.18, 0.18, 0.18, 0.18, 0.18], INK, True),    # flat line: warming up
}


def _canvas():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (PX * SS, PX * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img):
    from PIL import Image
    return img.resize((PX, PX), Image.LANCZOS)


def bars(heights, colour, alpha_scale: float = 1.0):
    img, d = _canvas()
    n = len(heights)
    box = 0.80 * PX * SS               # the glyph occupies ~16 pt of the 20 pt square
    bw = box / (n * 1.55)              # bar width (~2 pt at menu-bar size)
    gap = (box - n * bw) / (n - 1)
    x0 = (PX * SS - box) / 2
    cy = PX * SS / 2
    col = colour[:3] + (int(colour[3] * alpha_scale),)
    for i, h in enumerate(heights):
        hh = max(bw, h * box)
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, cy - hh / 2, x + bw, cy + hh / 2], radius=bw / 2, fill=col)
    return _finish(img)


def dots(colour):
    img, d = _canvas()
    r = PX * SS * 0.075
    cy = PX * SS / 2
    for k in (-1, 0, 1):
        cx = PX * SS / 2 + k * r * 3.2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    return _finish(img)


def paused(colour):
    img, d = _canvas()
    s = PX * SS
    bw, bh = s * 0.13, s * 0.52
    for x in (s * 0.5 - bw * 1.35, s * 0.5 + bw * 0.35):
        d.rounded_rectangle([x, s / 2 - bh / 2, x + bw, s / 2 + bh / 2], radius=bw / 2.4, fill=colour)
    return _finish(img)


def error(colour):
    """the calm wave, dimmed, with a small exclamation dot-and-bar on the right: 'mic problem'"""
    img = bars([0.22, 0.42, 0.62, 0.22, 0.0], colour, alpha_scale=0.45)
    from PIL import ImageDraw
    big, d = _canvas()
    s = PX * SS
    x = s * 0.80
    w = s * 0.085
    d.rounded_rectangle([x - w / 2, s * 0.22, x + w / 2, s * 0.62], radius=w / 2, fill=colour)
    d.ellipse([x - w * 0.62, s * 0.70, x + w * 0.62, s * 0.70 + w * 1.24], fill=colour)
    over = _finish(big)
    img.alpha_composite(over)
    return img


def render_all(out: Path = OUT) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    for state, (hs, col, tmpl) in BAR_STATES.items():
        p = out / f"{state}.png"
        bars(hs, col).save(p)
        made[state] = (str(p), tmpl)
    for state, img, tmpl in (("thinking", dots(INK), True), ("paused", paused(INK), True), ("error", error(RED), False)):
        p = out / f"{state}.png"
        img.save(p)
        made[state] = (str(p), tmpl)
    return made


def selftest() -> int:
    import tempfile
    from PIL import Image
    made = render_all(Path(tempfile.mkdtemp(prefix="dd-icons-")))
    assert set(made) == {"listening", "recording", "speaking", "starting", "thinking", "paused", "error"}
    for state, (path, tmpl) in made.items():
        im = Image.open(path)
        assert im.size == (PX, PX) and im.mode == "RGBA", (state, im.size, im.mode)
        a = im.getchannel("A")
        bbox = a.getbbox()
        assert bbox and bbox[0] >= 2 and bbox[2] <= PX - 2, (state, bbox)  # padding inside the 20 pt square
        if tmpl:  # template images are pure ink: colour carries no information
            assert all(im.getchannel(c).getextrema() == (0, 0) for c in "RGB"), state
    assert made["recording"][1] is False and Image.open(made["recording"][0]).getpixel((PX // 2, PX // 2))[2] > 200
    print(f"status_icons selftest ok ({len(made)} states, template + accent images, padding)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    for k, (p, t) in render_all().items():
        print(f"{k:10s} {'template' if t else 'colour  '} {p}")
