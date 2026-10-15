---
name: local-models
description: Manage dd's own brain — Ollama / llama.cpp models: list, pull, switch, warm up, check VRAM/RAM, pick the right quant and context size for the machine, build a custom "dd" Modelfile with the locked rules as system prompt, and run a quick quality/latency benchmark. Use when a model is slow, missing, or Dhina asks to change/upgrade/benchmark the model or build the dd model.
triggers: [ollama, llama.cpp, model, gguf, quant, vram, gpu, slow model, which model, upgrade model, modelfile, benchmark, context size, num_ctx]
metadata:
  version: "1.0"
  owner: dhina
---

# local-models

Zero-cost brain: **Ollama** (http://127.0.0.1:11434) serving open-weight GGUF models. dd's default task model is whatever `DD_MODEL` says (recommended start: `qwen2.5-coder:14b-instruct-q4_K_M` with 16 GB+, `qwen2.5-coder:7b-instruct-q4_K_M` with 8-12 GB). Check current best with `web-search` before upgrading — model names change monthly.

## Commands
```bash
python3 scripts/models.py hw                          # RAM, GPU/VRAM (nvidia-smi / Apple silicon), disk, ollama up?
python3 scripts/models.py recommend                   # quant + context + model tier for THIS machine
python3 scripts/models.py list | ps | pull NAME | rm NAME
python3 scripts/models.py warm NAME                   # load into memory (keep_alive) so first reply is fast
python3 scripts/models.py bench NAME [--prompt "..."]  # tokens/s, first-token latency, tool-call support check
python3 scripts/models.py modelfile --base NAME --out Modelfile   # dd persona + RULES.md as SYSTEM, num_ctx, temperature
ollama create dd -f Modelfile && ollama run dd        # builds the "dd" model
```

## Decision rules
- VRAM ≤ 6 GB → 7B Q4_K_M, `num_ctx 4096`. 8-12 GB → 7-8B Q5/Q6 or 14B Q4, `num_ctx 8192`. 16-24 GB → 14B Q6 / 32B Q4, `num_ctx 16384`. Apple silicon: unified memory ≈ VRAM, keep 4 GB for the OS.
- Slow → smaller quant or lower `num_ctx` before anything else; check `ps` for two models loaded at once.
- Need tool calling (dd's agent loop) → model must pass `bench --tools`; Qwen 2.5/3, Llama 3.1+, Mistral/Devstral families do.
- Video/image models are separate (ComfyUI) — don't load them and the LLM on the same ≤ 12 GB GPU at once; `ollama stop NAME` first.
- Fine-tuned dd model (from `self-learning` exports) → convert to GGUF, then `modelfile --base ./dd-ft.gguf`.

## Modelfile template (what `modelfile` generates)
```
FROM qwen2.5-coder:14b-instruct-q4_K_M
PARAMETER num_ctx 8192
PARAMETER temperature 0.3
PARAMETER repeat_penalty 1.05
SYSTEM """<dd-core-identity summary + RULES.md text>"""
```
