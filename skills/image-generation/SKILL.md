---
name: image-generation
description: Local, free image generation and editing for dd — FLUX.1-schnell / SDXL via ComfyUI or diffusers, plus mock backend; product shots, posters, thumbnails, start frames for video, simple edits (resize, crop, background removal with rembg). Use when Dhina asks for an image, poster, logo draft, thumbnail, product mockup or a start frame for a video scene.
triggers: [image, picture, poster, thumbnail, logo, mockup, product shot, generate image, flux, sdxl, stable diffusion, photo pannu]
metadata:
  version: "1.0"
  owner: dhina
---

# image-generation

Models (free, local): **FLUX.1-schnell** (Apache 2.0, 4 steps, ~12 GB fp8 / ~8 GB GGUF), **SDXL 1.0** (~6-8 GB), **SD 1.5** (4 GB, low-end). Same ComfyUI server as `video-generation` (http://127.0.0.1:8188).

## Commands
```bash
python3 scripts/imagegen.py gen "prompt" --out img.png [--backend comfyui|diffusers|mock] [--w 1024 --h 1024] [--seed 42] [--negative "..."] [--workflow api.json] [--model black-forest-labs/FLUX.1-schnell]
python3 scripts/imagegen.py batch prompts.txt --out-dir shots/        # one line = one image, same seed
python3 scripts/imagegen.py resize in.png --w 1280 --h 704 --out start.png   # exact size for a video start frame (ffmpeg)
python3 scripts/imagegen.py info img.png
```
Background removal: `pip install rembg` then `python3 -m rembg i in.png out.png` (free, local).

## Prompting
- Structure: subject → details → setting → lighting → style → quality tags. English only.
- Product: "studio product photo of <item>, centered, soft diffused light, seamless white background, 85mm, sharp focus".
- Poster/thumbnail: say the aspect (`--w 1080 --h 1920` story, `1280x720` thumbnail) and "bold composition, high contrast"; add text later with `scripts/imagegen.py text` (Pillow) — models spell badly.
- Negative (SDXL): "blurry, deformed, extra fingers, watermark, text, low quality". FLUX ignores negatives.
- Keep the seed fixed across variations; change one thing at a time.

## Workflow files
`assets/flux_schnell_workflow.json` and `assets/sdxl_workflow.json` are ComfyUI API-format templates. Prompt, seed and size are filled by class/title like the video skill; export your own from ComfyUI if nodes differ.

## Rules
- Verify the file exists and has sane size (`info`) before claiming success; send via `telegram-interface` as a photo.
- No real people's faces, no brand logos/characters — originals only (also keeps Dhina's client work safe).
- Image is the start frame for a video → generate at the video size (1280x704 for Wan 2.2 5B) and pass `"image"` in the storyboard scene.
