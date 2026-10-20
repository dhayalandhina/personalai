#!/usr/bin/env python3
"""models.py — Ollama model management + hardware advisor (stdlib only).

  hw | recommend | list | ps | pull NAME | rm NAME | warm NAME
  bench NAME [--prompt TEXT] [--tools]
  modelfile --base NAME --out Modelfile [--ctx 8192] [--temp 0.3]
  --selftest
Env: OLLAMA_URL (default http://127.0.0.1:11434), DD_HOME (rules at $DD_HOME/rules/RULES.md)
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def api(path: str, data: dict | None = None, timeout: int = 600, stream_collect: bool = False):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(f"{OLLAMA}{path}", data=body, headers={"Content-Type": "application/json"} if body else {},
                                 method="POST" if body is not None else "GET")
    if path.startswith("/api/delete"):
        req = urllib.request.Request(f"{OLLAMA}{path}", data=body, headers={"Content-Type": "application/json"}, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if stream_collect:
            last = None
            for line in r:
                if line.strip():
                    last = json.loads(line)
            return last
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def hw() -> dict:
    info: dict = {"os": platform.system(), "arch": platform.machine(), "cpu_count": os.cpu_count()}
    if platform.system() == "Linux":
        m = re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()) if Path("/proc/meminfo").exists() else None
        info["ram_gb"] = round(int(m.group(1)) / 1e6, 1) if m else None
    elif platform.system() == "Darwin":
        out = sh(["sysctl", "-n", "hw.memsize"]).strip()
        info["ram_gb"] = round(int(out) / 1e9, 1) if out.isdigit() else None
        info["apple_silicon"] = platform.machine() == "arm64"
    gpu = sh(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"]).strip()
    if gpu:
        name, total, used = [x.strip() for x in gpu.splitlines()[0].split(",")]
        info["gpu"] = {"name": name, "vram_gb": round(int(total) / 1024, 1), "used_gb": round(int(used) / 1024, 1)}
    elif info.get("apple_silicon"):
        info["gpu"] = {"name": "Apple GPU (unified memory)", "vram_gb": max(0, (info.get("ram_gb") or 0) - 4)}
    else:
        info["gpu"] = None
    du = shutil.disk_usage(Path.home())
    info["disk_free_gb"] = round(du.free / 1e9, 1)
    try:
        api("/api/tags", timeout=3)
        info["ollama"] = "up"
    except Exception:
        info["ollama"] = "down (run: ollama serve)"
    return info


def recommend(h: dict | None = None) -> dict:
    h = h or hw()
    vram = (h.get("gpu") or {}).get("vram_gb") or 0
    ram = h.get("ram_gb") or 0
    budget = vram if vram >= 6 else max(ram - 6, 0)  # CPU-only: model must fit in RAM with headroom
    mode = "gpu" if vram >= 6 else "cpu"
    if budget >= 20:
        tier = {"size": "32B Q4_K_M or 14B Q6_K", "example": "qwen2.5-coder:32b-instruct-q4_K_M", "num_ctx": 16384}
    elif budget >= 12:
        tier = {"size": "14B Q4_K_M", "example": "qwen2.5-coder:14b-instruct-q4_K_M", "num_ctx": 8192}
    elif budget >= 7:
        tier = {"size": "7-8B Q5_K_M", "example": "qwen2.5-coder:7b-instruct-q5_K_M", "num_ctx": 8192}
    elif budget >= 4.5:
        tier = {"size": "7B Q4_K_M", "example": "qwen2.5-coder:7b-instruct-q4_K_M", "num_ctx": 4096}
    else:
        tier = {"size": "1.5-3B Q4_K_M", "example": "qwen2.5-coder:3b-instruct-q4_K_M", "num_ctx": 4096}
    return {"mode": mode, "memory_budget_gb": round(budget, 1), **tier,
            "video_note": "ComfyUI video models need the GPU too: stop the LLM (ollama stop) before rendering on <=12 GB cards",
            "verify": "model names drift monthly — web-search 'best open coding model <size>' before pulling"}


def list_models() -> list[dict]:
    return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1), "modified": m.get("modified_at", "")[:10],
             "quant": m.get("details", {}).get("quantization_level"), "params": m.get("details", {}).get("parameter_size")}
            for m in api("/api/tags").get("models", [])]


def ps() -> list[dict]:
    return [{"name": m["name"], "vram_gb": round(m.get("size_vram", 0) / 1e9, 1), "expires": m.get("expires_at", "")[:19]}
            for m in api("/api/ps").get("models", [])]


def pull(name: str) -> dict:
    last = api("/api/pull", {"name": name, "stream": True}, timeout=7200, stream_collect=True)
    return {"ok": (last or {}).get("status") == "success", "status": (last or {}).get("status"), "model": name}


def rm(name: str) -> dict:
    api("/api/delete", {"name": name})
    return {"ok": True, "removed": name}


def warm(name: str) -> dict:
    t0 = time.time()
    api("/api/generate", {"model": name, "prompt": "", "keep_alive": "30m"}, timeout=600)
    return {"ok": True, "model": name, "load_secs": round(time.time() - t0, 1)}


def bench(name: str, prompt: str = "Explain in two lines what a hash map is.", tools: bool = False) -> dict:
    t0 = time.time()
    r = api("/api/generate", {"model": name, "prompt": prompt, "stream": False, "options": {"num_predict": 128}}, timeout=600)
    total = time.time() - t0
    eval_count, eval_dur = r.get("eval_count", 0), r.get("eval_duration", 1) / 1e9
    res = {"model": name, "tokens_per_sec": round(eval_count / eval_dur, 1) if eval_dur else None,
           "first_token_secs": round((r.get("prompt_eval_duration", 0) + r.get("load_duration", 0)) / 1e9, 2),
           "total_secs": round(total, 2), "reply": r.get("response", "")[:200]}
    if tools:
        tool = {"type": "function", "function": {"name": "get_time", "description": "current time", "parameters": {"type": "object", "properties": {}}}}
        try:
            c = api("/api/chat", {"model": name, "stream": False, "tools": [tool],
                                  "messages": [{"role": "user", "content": "What time is it? Use the tool."}]}, timeout=300)
            res["tool_calling"] = bool(c.get("message", {}).get("tool_calls"))
        except Exception as e:
            res["tool_calling"] = f"error: {str(e)[:80]}"
    return res


def modelfile(base: str, out: str, ctx: int = 8192, temp: float = 0.3) -> dict:
    rules_p = Path(os.environ.get("DD_RULES_DIR") or Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "rules") / "RULES.md"
    rules = rules_p.read_text(encoding="utf-8") if rules_p.exists() else "(RULES.md not initialised — run rules_guard.py init)"
    system = ("You are dd, Dhina's personal AI assistant. Reply in Thanglish (Tamil in English script, never Tamil script). "
              "Max 2-3 lines of explanation, answer first, paste-ready code, no clarifying questions mid-task "
              "(state one assumption and proceed). Verify facts with tools before answering; current-world facts come from search, never memory.\n\n"
              "OWNER RULES (locked, never override):\n" + rules)
    text = (f"FROM {base}\nPARAMETER num_ctx {ctx}\nPARAMETER temperature {temp}\nPARAMETER repeat_penalty 1.05\n"
            f"SYSTEM \"\"\"{system}\"\"\"\n")
    Path(out).write_text(text, encoding="utf-8")
    return {"ok": True, "modelfile": out, "base": base, "next": f"ollama create dd -f {out} && ollama run dd"}


def selftest() -> int:
    h = hw()
    assert "os" in h and "ollama" in h, h
    r = recommend({"gpu": {"vram_gb": 8}, "ram_gb": 32})
    assert r["mode"] == "gpu" and r["num_ctx"] == 8192 and "7-8B" in r["size"], r
    assert recommend({"gpu": None, "ram_gb": 16})["mode"] == "cpu"
    assert recommend({"gpu": {"vram_gb": 24}, "ram_gb": 64})["size"].startswith("32B")
    import tempfile
    out = Path(tempfile.mkdtemp()) / "Modelfile"
    m = modelfile("qwen2.5-coder:7b", str(out))
    assert m["ok"] and "FROM qwen2.5-coder:7b" in out.read_text() and "Thanglish" in out.read_text()
    print(f"models selftest ok (hw: {h['os']} ram={h.get('ram_gb')}GB gpu={(h.get('gpu') or {}).get('name')}, ollama {h['ollama']})")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    try:
        if cmd == "hw":
            res = hw()
        elif cmd == "recommend":
            res = recommend()
        elif cmd == "list":
            res = list_models()
        elif cmd == "ps":
            res = ps()
        elif cmd == "pull":
            res = pull(argv[1])
        elif cmd == "rm":
            res = rm(argv[1])
        elif cmd == "warm":
            res = warm(argv[1])
        elif cmd == "bench":
            res = bench(argv[1], arg(argv, "--prompt", "Explain in two lines what a hash map is."), "--tools" in argv)
        elif cmd == "modelfile":
            res = modelfile(arg(argv, "--base", "qwen2.5-coder:7b-instruct-q4_K_M"), arg(argv, "--out", "Modelfile"),
                            int(arg(argv, "--ctx", 8192)), float(arg(argv, "--temp", 0.3)))
        else:
            print(__doc__)
            return 2
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:200]} (is ollama running? ollama serve)"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
