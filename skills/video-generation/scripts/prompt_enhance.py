#!/usr/bin/env python3
"""prompt_enhance.py — turn a short idea into video-model prompts and a storyboard (stdlib only).

  enhance "idea" [--style cinematic|product|anime|documentary|vlog]
  storyboard "idea" [--seconds 30] [--clip 5] [--style S] [--narration] [--subject "exact descriptor"] [--out file.json]
  --selftest
The LLM in dd can rewrite any field afterwards; this script guarantees structure, continuity and sane defaults.
"""
from __future__ import annotations

import json
import re
import sys

NEGATIVE = ("blurry, low quality, deformed, distorted hands, extra limbs, extra fingers, text, watermark, logo, "
            "subtitles, flicker, jitter, static noise, oversaturated, duplicate, cropped")

STYLES = {
    "cinematic": {"look": "cinematic film look, 35mm, shallow depth of field, soft film grain, color graded",
                  "light": "golden hour backlight, volumetric haze", "cams": ["slow dolly-in", "slow orbit", "static wide shot",
                  "handheld tracking shot", "crane up reveal", "rack focus close-up"]},
    "product": {"look": "clean commercial product shot, studio quality, photorealistic, 4k detail",
                "light": "soft studio lighting with gentle rim light, seamless background", "cams": ["slow 360 orbit",
                "macro slide across the surface", "top-down slow push-in", "hero shot slow tilt up", "pull-back reveal", "static beauty shot"]},
    "anime": {"look": "high quality anime style, clean linework, vibrant colors, Makoto-Shinkai-like skies",
              "light": "bright afternoon light, soft shading", "cams": ["slow pan", "static shot", "zoom in", "tracking shot", "tilt down", "wide establishing"]},
    "documentary": {"look": "natural documentary footage, realistic, neutral grade", "light": "available natural light",
                    "cams": ["handheld medium shot", "static wide", "slow pan", "follow shot", "close-up detail", "interview framing"]},
    "vlog": {"look": "bright social-media vlog look, crisp, vertical 9:16 friendly framing", "light": "daylight, high key",
             "cams": ["selfie-style medium shot", "quick pan", "walking POV", "close-up", "static shot", "whip to reveal"]},
}
TANGLISH_HINTS = [(r"\bkadai\b", "shop"), (r"\bveedu\b", "house"), (r"\bthanni\b", "water"), (r"\bsamayal\b", "cooking"),
                  (r"\bvanam\b", "sky"), (r"\bmazhai\b", "rain"), (r"\bkadal\b", "sea"), (r"\bmalai\b", "mountain"),
                  (r"\bpayan\b", "boy"), (r"\bponnu\b", "girl"), (r"\bnaai\b", "dog"), (r"\bpoonai\b", "cat")]


def englishify(idea: str) -> str:
    s = idea
    for pat, rep in TANGLISH_HINTS:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"\b(oru|pannu|panna|pannunga|vendum|venum|la|le|ku|ah|aa)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,.")


def enhance(idea: str, style: str = "cinematic", camera: str | None = None, subject: str | None = None) -> dict:
    st = STYLES.get(style, STYLES["cinematic"])
    core = subject or englishify(idea)
    cam = camera or st["cams"][0]
    prompt = f"{core}. {cam}. {st['light']}. {st['look']}. smooth natural motion, consistent subject, high detail"
    return {"prompt": prompt, "negative": NEGATIVE, "style": style, "camera": cam, "subject": core}


def storyboard(idea: str, seconds: int = 30, clip: int = 5, style: str = "cinematic",
               narration: bool = False, subject: str | None = None, seed: int = 42) -> dict:
    n = max(1, round(seconds / clip))
    st = STYLES.get(style, STYLES["cinematic"])
    core = subject or englishify(idea)
    beats = ["establishing shot introducing", "closer look at", "the key action with", "a detail moment of",
             "emotional / payoff moment with", "closing hero shot of", "alternate angle on", "final lingering shot of"]
    scenes = []
    for i in range(n):
        beat = beats[i % len(beats)]
        cam = st["cams"][i % len(st["cams"])]
        e = enhance(f"{beat} {core}", style, cam, subject=f"{beat} {core}")
        scenes.append({"n": i + 1, "seconds": clip, "prompt": e["prompt"], "negative": NEGATIVE, "camera": cam,
                       "seed": seed, "narration": (f"Scene {i + 1}: {core}." if narration else "")})
    return {"idea": idea, "subject": core, "style": style, "total_seconds": n * clip, "clip_seconds": clip,
            "fps": 24, "width": 1280 if style != "vlog" else 704, "height": 704 if style != "vlog" else 1280,
            "seed": seed, "scenes": scenes}


def selftest() -> int:
    e = enhance("oru product bottle slow rotate pannu", "product")
    assert "product bottle slow rotate" in e["prompt"] and "studio" in e["prompt"], e
    sb = storyboard("mazhai la oru payan cycle ottura", seconds=30, clip=5, style="cinematic", narration=True)
    assert len(sb["scenes"]) == 6 and sb["total_seconds"] == 30, sb
    assert all(s["seed"] == 42 for s in sb["scenes"])
    assert "rain" in sb["subject"] and "boy" in sb["subject"], sb["subject"]
    assert all(sb["subject"] in s["prompt"] for s in sb["scenes"])  # continuity
    vl = storyboard("x", 10, 5, "vlog")
    assert vl["width"] == 704 and vl["height"] == 1280
    print("prompt_enhance selftest ok")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd, idea = argv[0], argv[1] if len(argv) > 1 else ""
    style = arg(argv, "--style", "cinematic")
    if cmd == "enhance":
        res = enhance(idea, style, arg(argv, "--camera"), arg(argv, "--subject"))
    elif cmd == "storyboard":
        res = storyboard(idea, int(arg(argv, "--seconds", 30)), int(arg(argv, "--clip", 5)), style,
                         "--narration" in argv, arg(argv, "--subject"), int(arg(argv, "--seed", 42)))
        if (out := arg(argv, "--out")):
            with open(out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
            res = {"ok": True, "file": out, "scenes": len(res["scenes"]), "total_seconds": res["total_seconds"]}
    else:
        print(__doc__)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
