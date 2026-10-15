#!/usr/bin/env python3
"""imagegen.py — local image generation for dd (ComfyUI / diffusers / mock) + small edits via ffmpeg/Pillow.

  gen "prompt" --out img.png [--backend comfyui|diffusers|mock] [--w 1024 --h 1024] [--seed 42] [--negative N]
      [--workflow api.json] [--model black-forest-labs/FLUX.1-schnell] [--steps 4]
  batch prompts.txt --out-dir DIR [same options]
  resize IN --w W --h H --out OUT          (ffmpeg; pads to exact size)
  text IN --text "..." --out OUT [--size 48] [--pos bottom|top|center]   (Pillow)
  info IMG
  --selftest
Env: COMFYUI_URL (default http://127.0.0.1:8188)
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
COMFY = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")


def write_png(path: str, w: int, h: int, rgb=(43, 108, 176)) -> None:
    """stdlib PNG writer (mock backend)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def info(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    data = p.read_bytes()[:32]
    res = {"path": str(p), "bytes": p.stat().st_size}
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        res.update({"format": "png", "width": struct.unpack(">I", data[16:20])[0], "height": struct.unpack(">I", data[20:24])[0]})
    elif data[:2] == b"\xff\xd8":
        res["format"] = "jpeg"
    if shutil.which("ffprobe") and "width" not in res:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        if r.returncode == 0 and "," in r.stdout:
            w, h = r.stdout.strip().split(",")[:2]
            res.update({"width": int(w), "height": int(h)})
    return res


def http_json(url: str, data: dict | None = None, timeout: int = 60) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fill(wf: dict, prompt: str, negative: str, seed: int, w: int, h: int, steps: int | None) -> dict:
    wf = json.loads(json.dumps(wf))
    wf.pop("_comment", None)
    for node in wf.values():
        ct, title = node.get("class_type", ""), (node.get("_meta", {}).get("title", "") or "").lower()
        inp = node.setdefault("inputs", {})
        if ct == "CLIPTextEncode":
            if "negative" in title:
                inp["text"] = negative
            elif "positive" in title or inp.get("text") == "POSITIVE_PROMPT":
                inp["text"] = prompt
        elif ct in ("KSampler", "KSamplerAdvanced", "RandomNoise"):
            for k in ("seed", "noise_seed"):
                if k in inp:
                    inp[k] = seed
            if steps and "steps" in inp:
                inp["steps"] = steps
        elif ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inp["width"], inp["height"] = w, h
        elif ct == "SaveImage":
            inp["filename_prefix"] = f"dd/img_{int(time.time())}"
    return wf


def gen_comfy(prompt: str, out: str, w: int, h: int, seed: int, negative: str, workflow: str | None, steps: int | None) -> None:
    wf = json.loads(Path(workflow or SKILL / "assets" / "sdxl_workflow.json").read_text(encoding="utf-8"))
    pid = http_json(f"{COMFY}/prompt", {"prompt": fill(wf, prompt, negative, seed, w, h, steps), "client_id": "dd-img"})["prompt_id"]
    deadline = time.time() + 1800
    while time.time() < deadline:
        hist = http_json(f"{COMFY}/history/{pid}")
        if pid in hist:
            if hist[pid].get("status", {}).get("status_str") == "error":
                raise RuntimeError(json.dumps(hist[pid]["status"])[:300])
            for node_out in hist[pid].get("outputs", {}).values():
                for f in node_out.get("images", []):
                    q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
                    with urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=300) as r, open(out, "wb") as fh:
                        shutil.copyfileobj(r, fh)
                    return
        time.sleep(2)
    raise TimeoutError("ComfyUI image job timed out")


def gen_diffusers(prompt: str, out: str, w: int, h: int, seed: int, negative: str, model: str | None, steps: int | None) -> None:
    import torch  # pip install torch diffusers transformers accelerate
    from diffusers import AutoPipelineForText2Image
    model = model or "black-forest-labs/FLUX.1-schnell"
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    pipe = AutoPipelineForText2Image.from_pretrained(model, torch_dtype=dtype)
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()
    gen = torch.Generator("cpu").manual_seed(seed)
    kwargs = {"prompt": prompt, "width": w, "height": h, "generator": gen, "num_inference_steps": steps or (4 if "schnell" in model else 25)}
    if "flux" in model.lower():
        kwargs["guidance_scale"] = 0.0
    else:
        kwargs["negative_prompt"] = negative
    pipe(**kwargs).images[0].save(out)


def gen(prompt: str, out: str, backend: str = "comfyui", w: int = 1024, h: int = 1024, seed: int = 42, negative: str = "",
        workflow: str | None = None, model: str | None = None, steps: int | None = None) -> dict:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if backend == "mock":
        write_png(out, w, h, ((seed * 37) % 200 + 30, (seed * 91) % 200 + 30, (seed * 53) % 200 + 30))
    elif backend == "comfyui":
        gen_comfy(prompt, out, w, h, seed, negative, workflow, steps)
    elif backend == "diffusers":
        gen_diffusers(prompt, out, w, h, seed, negative, model, steps)
    else:
        return {"error": f"unknown backend {backend}"}
    return {"ok": True, "backend": backend, "secs": round(time.time() - t0, 1), **info(out)}


def resize(src: str, w: int, h: int, out: str) -> dict:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf",
                        f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", out],
                       capture_output=True, text=True)
    return {"error": r.stderr[-200:]} if r.returncode else {"ok": True, **info(out)}


def add_text(src: str, text: str, out: str, size: int = 48, pos: str = "bottom") -> dict:
    try:
        from PIL import Image, ImageDraw, ImageFont  # pip install pillow
    except ImportError:
        return {"error": "pip install pillow"}
    im = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (im.width - tw) // 2
    y = {"top": 20, "center": (im.height - th) // 2}.get(pos, im.height - th - 30)
    d.rectangle([x - 16, y - 10, x + tw + 16, y + th + 14], fill=(0, 0, 0))
    d.text((x, y), text, font=font, fill=(255, 255, 255))
    im.save(out)
    return {"ok": True, **info(out)}


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dd-img-"))
    r = gen("test", str(tmp / "a.png"), "mock", 64, 32, 3)
    assert r["ok"] and r["width"] == 64 and r["height"] == 32, r
    wf = json.loads((SKILL / "assets" / "sdxl_workflow.json").read_text())
    f = fill(wf, "P", "N", 5, 512, 768, 20)
    assert f["2"]["inputs"]["text"] == "P" and f["3"]["inputs"]["text"] == "N" and f["5"]["inputs"]["seed"] == 5
    assert f["4"]["inputs"]["width"] == 512 and f["5"]["inputs"]["steps"] == 20
    if shutil.which("ffmpeg"):
        rs = resize(str(tmp / "a.png"), 128, 128, str(tmp / "b.png"))
        assert rs.get("ok") and rs["width"] == 128, rs
    shutil.rmtree(tmp, ignore_errors=True)
    print("imagegen selftest ok (mock png, workflow fill, resize)")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd = argv[0]
    w, h, seed = int(arg(argv, "--w", 1024)), int(arg(argv, "--h", 1024)), int(arg(argv, "--seed", 42))
    steps = int(arg(argv, "--steps")) if "--steps" in argv else None
    try:
        if cmd == "gen":
            res = gen(argv[1], arg(argv, "--out", "img.png"), arg(argv, "--backend", "comfyui"), w, h, seed,
                      arg(argv, "--negative", ""), arg(argv, "--workflow"), arg(argv, "--model"), steps)
        elif cmd == "batch":
            od = Path(arg(argv, "--out-dir", "shots"))
            res = [gen(line.strip(), str(od / f"img_{i + 1:02d}.png"), arg(argv, "--backend", "comfyui"), w, h, seed,
                       arg(argv, "--negative", ""), arg(argv, "--workflow"), arg(argv, "--model"), steps)
                   for i, line in enumerate(Path(argv[1]).read_text(encoding="utf-8").splitlines()) if line.strip()]
        elif cmd == "resize":
            res = resize(argv[1], w, h, arg(argv, "--out", "resized.png"))
        elif cmd == "text":
            res = add_text(argv[1], arg(argv, "--text", ""), arg(argv, "--out", "text.png"), int(arg(argv, "--size", 48)), arg(argv, "--pos", "bottom"))
        elif cmd == "info":
            res = info(argv[1])
        else:
            print(__doc__)
            return 2
    except ImportError as e:
        res = {"error": f"missing {e.name}: pip install torch diffusers transformers accelerate pillow"}
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
