---
name: video-generation
description: dd's video mode — turn an idea into a finished 5-30s MP4 on local open-weight models (Wan 2.2 5B / LTX via ComfyUI, or diffusers), zero API cost: prompt enhancement, storyboard into 5s scenes, clip generation, ffmpeg stitch, optional Piper narration and subtitles. Use when Dhina asks for a video, reel, ad clip, animation, "video pannu", text-to-video or image-to-video.
triggers: [video, reel, clip, animation, text to video, image to video, shorts, ad video, video pannu, seedance, wan, ltx, comfyui]
metadata:
  version: "1.0"
  owner: dhina
---

# video-generation

Stack (all free, local): **ComfyUI** (http://127.0.0.1:8188) running **Wan 2.2 TI2V-5B** (720p, 24 fps, ~5 s/clip, ~8 GB VRAM) or **LTX-Video** (faster, ~12 GB), **ffmpeg** for assembly, **Piper** for narration (see `voice`). Read `references/local-video-models.md` for model choice, VRAM table and install commands.

## Pipeline (always this order)
1. **Understand** → `scripts/prompt_enhance.py enhance "<idea>"` → cinematic prompt + negative prompt (subject · action · setting · camera · light · style · motion). Thanglish idea in → English prompt out (video models want English).
2. **Storyboard** → `scripts/prompt_enhance.py storyboard "<idea>" --seconds 30 --clip 5 [--style cinematic|product|anime|documentary] [--narration]` → `storyboard.json` with N scenes, consistent character/product descriptor in every scene, narration line per scene.
3. **Generate + assemble** → `scripts/video_pipeline.py run storyboard.json --backend comfyui --out final.mp4 [--narrate] [--subtitles] [--music bg.mp3]`
   - per scene: ComfyUI `/prompt` → poll `/history` → fetch mp4 → `$DD_HOME/video/<job>/scene_01.mp4`
   - stitch: ffmpeg concat (optional 0.3 s crossfade) → narration mix → subtitles burn-in → `final.mp4` (H.264, yuv420p, 24 fps, AAC)
4. **Verify** → `video_pipeline.py probe final.mp4` (duration, fps, size). Duration must equal scenes × clip length ± 0.5 s; otherwise fix and re-stitch. Send via `telegram-interface` (video ≤ 50 MB; else send link/compress with `--compress`).
5. Reply: path + duration + model used + one line on what to change for a v2. Never claim a clip exists without `probe`.

## Backends
- `--backend comfyui` (default): uses `assets/wan22_t2v_workflow.json` (ComfyUI API format). Export your own workflow as API JSON and pass `--workflow my.json`; the script fills positive/negative prompt, seed, frames, size by node title/class.
- `--backend diffusers`: runs `scripts/diffusers_t2v.py` (torch + diffusers, GPU). Model id via `--model` (default `Wan-AI/Wan2.2-TI2V-5B-Diffusers`).
- `--backend mock`: ffmpeg colour clips with text — pipeline test without a GPU. Always run this first on a new machine.
- Image-to-video: add `"image": "path.png"` to a scene (Wan 2.2 5B is TI2V) — product shots stay consistent.

## Prompting rules (learned from Wan/LTX behaviour)
- One subject, one action, one camera move per 5 s clip. No lists of events.
- Say the camera: "slow dolly-in", "handheld tracking", "static wide", "orbit".
- Light + mood explicitly: "golden hour backlight", "soft studio light", "neon night rain".
- Negative prompt always: blurry, deformed hands, extra limbs, text, watermark, flicker, low quality.
- Keep character/product description identical across scenes (copy the same descriptor string).
- Clip length ≤ 5 s on 5B models; 30 s video = 6 clips. Longer single clips drift.
- Seed fixed per project (`--seed 42`) so a re-run with one changed scene stays consistent.

## Hardware fallbacks
No GPU → `--backend mock` locally, and say "GPU illa, real render ku free Colab T4 / Kaggle T4 use pannalam" with `references/local-video-models.md#colab`. Low VRAM (≤ 8 GB) → Wan 2.2 5B at 480p, `--frames 81`, or LTX-Video GGUF.
