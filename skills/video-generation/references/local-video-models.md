# Local open-weight video models — what fits what hardware (checked 15 Oct 2026)

Source for the VRAM figures: insiderllm.com local AI video guide (Oct 2026). Numbers are approximate; test on your card.

| Model | Licence | VRAM | Notes |
|---|---|---|---|
| **Wan 2.2 TI2V-5B** (Alibaba) | Apache 2.0 | ~8 GB | 720p 24 fps ~5 s; text- and image-to-video in one model. Default for dd. |
| Wan 2.2 A14B (MoE) | Apache 2.0 | 6-8 GB at 480p with GGUF Q3/Q4 + T5 offload; 11-16 GB with Q5; 50 GB+ FP16 | Better quality, slower. |
| **LTX-2.3** (Lightricks) distilled/dev | community licence (free < $10M revenue) | ~16 GB FP8 at 720p; ~10 GB Q4 GGUF at 960x544 | Generates synced audio. |
| LTX-Video (earlier) | same | ~12 GB FP8 at 768x512 | Fastest — seconds per clip. Good for drafts. |
| HunyuanVideo 1.5 (Tencent) 8.3B | Tencent licence | ~24 GB; ~14 GB with offload; 6-8 GB heavily quantised | Best faces. |
| CogVideoX 2B / 5B | Apache 2.0 / CogVideoX licence | 8-12 GB / 12-18 GB | Older; 5B has strong image-to-video. |
| AnimateDiff (SD1.5) | CreativeML | ~8 GB | Motion for existing SD checkpoints. Stylised only. |

Wan 2.5 / 2.6 are API-only; for downloadable weights use Wan 2.1 / 2.2 from https://huggingface.co/Wan-AI.

## Install (ComfyUI route — recommended)
```bash
git clone https://github.com/comfyanonymous/ComfyUI && cd ComfyUI
python3 -m venv .venv && source .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124   # NVIDIA; Mac: plain pip install torch
pip install -r requirements.txt
python main.py --listen 127.0.0.1 --port 8188
```
Model files for Wan 2.2 5B (place under `ComfyUI/models/...`):
- `diffusion_models/wan2.2_ti2v_5B_fp16.safetensors`
- `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `vae/wan2.2_vae.safetensors`
Likely source: the Comfy-Org "Wan_2.2_ComfyUI_Repackaged" repo on Hugging Face — verify the exact file names on that page before downloading; ComfyUI's built-in template "Wan 2.2 5B video generation" lists them.

Alternative one-click: **Pinokio** (installs ComfyUI / Wan2GP), or **Wan2GP** (deepbeepmeep) standalone web UI with low-VRAM profiles.

## Workflow JSON
`assets/wan22_t2v_workflow.json` follows ComfyUI's Wan 2.2 5B template in *API format*. If your ComfyUI version names a node differently: open the template in ComfyUI → Workflow → Export (API) → pass `--workflow that.json`. The pipeline finds prompt nodes by `class_type == CLIPTextEncode` and `_meta.title` containing "positive"/"negative", the latent node by `Wan22ImageToVideoLatent`/`EmptyHunyuanLatentVideo`, KSampler for the seed.

## Free GPU when the Mac/PC has none {#colab}
- Google Colab (free T4 16 GB): run ComfyUI with `--listen 0.0.0.0` + a cloudflared tunnel, then set `COMFYUI_URL=https://<tunnel>` for `video_pipeline.py`.
- Kaggle (free T4 x2, 30 h/week) — same recipe.
- Both are free tiers; sessions reset, so download outputs immediately (the pipeline already saves each clip as it finishes).

## Quality checklist before sending
- 24 fps constant, yuv420p (plays on phones), audio AAC 48 kHz if narration.
- Watch for frame flicker at clip joins → use `--crossfade 0.3`.
- Hands/text artefacts → shorten clip, add to negative prompt, raise steps to 30-40.
