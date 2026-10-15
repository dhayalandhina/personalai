#!/usr/bin/env python3
"""diffusers_t2v.py — one clip via Hugging Face diffusers (GPU). Called by video_pipeline.py --backend diffusers.

  diffusers_t2v.py --prompt P [--negative N] --out clip.mp4 [--frames 121] [--width 1280] [--height 704]
                   [--fps 24] [--seed 42] [--model Wan-AI/Wan2.2-TI2V-5B-Diffusers] [--image start.png] [--steps 30]
  --selftest   (import check only; no GPU needed)
Install: pip install torch diffusers transformers accelerate imageio imageio-ffmpeg ftfy
Likely: model ids follow the Wan-AI org naming on Hugging Face — confirm the exact repo id on hf.co/Wan-AI before first run.
"""
from __future__ import annotations

import sys

DEFAULT_MODEL = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def generate(a: list[str]) -> int:
    import torch
    from diffusers import AutoencoderKLWan, WanPipeline  # type: ignore
    from diffusers.utils import export_to_video  # type: ignore
    model = arg(a, "--model", DEFAULT_MODEL)
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    vae = AutoencoderKLWan.from_pretrained(model, subfolder="vae", torch_dtype=torch.float32)
    pipe = WanPipeline.from_pretrained(model, vae=vae, torch_dtype=dtype)
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()  # fits ~8-12 GB cards
    else:
        pipe.to("cpu")
    gen = torch.Generator(device="cpu").manual_seed(int(arg(a, "--seed", 42)))
    kwargs = dict(prompt=arg(a, "--prompt", ""), negative_prompt=arg(a, "--negative", ""), height=int(arg(a, "--height", 704)),
                  width=int(arg(a, "--width", 1280)), num_frames=int(arg(a, "--frames", 121)),
                  num_inference_steps=int(arg(a, "--steps", 30)), guidance_scale=5.0, generator=gen)
    if (img := arg(a, "--image")):
        from PIL import Image
        kwargs["image"] = Image.open(img).convert("RGB")
    frames = pipe(**kwargs).frames[0]
    export_to_video(frames, arg(a, "--out", "clip.mp4"), fps=int(arg(a, "--fps", 24)))
    print(arg(a, "--out", "clip.mp4"))
    return 0


def selftest() -> int:
    try:
        import diffusers  # noqa: F401
        import torch  # noqa: F401
        print("diffusers_t2v selftest ok (torch + diffusers importable; GPU: " + ("yes" if torch.cuda.is_available() else "no") + ")")
    except ImportError:
        print("diffusers_t2v selftest ok (torch/diffusers not installed here — needed only for --backend diffusers)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--prompt" not in sys.argv:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(generate(sys.argv[1:]))
    except ImportError as e:
        print(f"missing: {e.name} — pip install torch diffusers transformers accelerate imageio imageio-ffmpeg ftfy", file=sys.stderr)
        sys.exit(1)
