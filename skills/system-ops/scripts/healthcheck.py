#!/usr/bin/env python3
"""healthcheck.py — machine + services + dd data status (stdlib only).

  healthcheck.py [--brief]
  healthcheck.py --selftest
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd"))


def load_env() -> None:
    p = home() / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def http_ok(url: str, timeout: float = 3) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 400, f"HTTP {r.status}"
    except Exception as e:
        return False, type(e).__name__


def ram() -> dict:
    if platform.system() == "Linux" and Path("/proc/meminfo").exists():
        m = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
        total, avail = int(m.get("MemTotal", 0)), int(m.get("MemAvailable", 0))
        return {"total_gb": round(total / 1e6, 1), "free_gb": round(avail / 1e6, 1), "used_pct": round(100 * (1 - avail / total)) if total else None}
    if platform.system() == "Darwin":
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            free = sum(int(re.search(rf"{k}:\s+(\d+)", vm).group(1)) for k in ("Pages free", "Pages inactive") if re.search(rf"{k}:\s+(\d+)", vm)) * page
            return {"total_gb": round(total / 1e9, 1), "free_gb": round(free / 1e9, 1), "used_pct": round(100 * (1 - free / total))}
        except Exception:
            pass
    return {}


def gpu() -> dict | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            n, used, total, util = [x.strip() for x in out.splitlines()[0].split(",")]
            return {"name": n, "vram_used_gb": round(int(used) / 1024, 1), "vram_total_gb": round(int(total) / 1024, 1), "util_pct": int(util)}
    except Exception:
        pass
    return None


def dir_size(p: Path) -> float:
    if not p.exists():
        return 0.0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return round(total / 1e6, 1)


def check() -> dict:
    load_env()
    du = shutil.disk_usage(str(Path.home()))
    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    services = {}
    for name, url in (("ollama", os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags"),
                      ("comfyui", os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188") + "/system_stats"),
                      ("n8n", (os.environ.get("N8N_URL", "http://127.0.0.1:5678").rstrip("/")) + "/healthz")):
        ok, why = http_ok(url)
        services[name] = {"up": ok, "detail": why}
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    services["telegram"] = {"up": http_ok(f"https://api.telegram.org/bot{tok}/getMe", 5)[0], "detail": "getMe"} if tok else {"up": False, "detail": "no token"}
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        internet = True
    except OSError:
        internet = False
    h = home()
    return {"host": platform.node(), "os": f"{platform.system()} {platform.release()}", "time": time.strftime("%Y-%m-%d %H:%M"),
            "disk": {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1), "free_pct": round(100 * du.free / du.total)},
            "ram": ram(), "load1": load1, "cpu_count": os.cpu_count(), "gpu": gpu(), "internet": internet, "services": services,
            "dd_data_mb": {"memory": dir_size(h / "memory.db") if (h / "memory.db").is_file() else round((h / "memory.db").stat().st_size / 1e6, 2) if (h / "memory.db").exists() else 0,
                           "learning": dir_size(h / "learning"), "video": dir_size(h / "video"), "plans": dir_size(h / "plans"),
                           "rules_locked": (h / "rules" / "RULES.lock").exists()}}


def brief(c: dict) -> str:
    red = []
    if c["disk"]["free_pct"] < 10:
        red.append(f"disk {c['disk']['free_pct']}% free")
    if c["ram"].get("used_pct", 0) > 90:
        red.append(f"ram {c['ram']['used_pct']}% used")
    for k, v in c["services"].items():
        if not v["up"]:
            red.append(f"{k} down")
    if not c["dd_data_mb"]["rules_locked"]:
        red.append("rules not locked")
    g = c.get("gpu")
    gtxt = f", gpu {g['vram_used_gb']}/{g['vram_total_gb']}GB" if g else ""
    status = "ALL OK" if not red else "ISSUES: " + ", ".join(red)
    return (f"{status} | disk {c['disk']['free_gb']}GB free, ram {c['ram'].get('free_gb', '?')}GB free{gtxt}, "
            f"load {c['load1']}, internet {'yes' if c['internet'] else 'NO'} | {c['time']}")


def selftest() -> int:
    import tempfile
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-hc-")
    c = check()
    assert "disk" in c and "services" in c and "dd_data_mb" in c, c
    b = brief(c)
    assert "disk" in b and ("ALL OK" in b or "ISSUES" in b), b
    print(f"healthcheck selftest ok ({b[:90]}...)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    c = check()
    print(brief(c) if "--brief" in argv else json.dumps(c, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
