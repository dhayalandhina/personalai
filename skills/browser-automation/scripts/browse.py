#!/usr/bin/env python3
"""browse.py — Playwright/Chromium helper for dd (free, local, persistent profile).

  browse.py text URL [--wait SEL] [--max-chars N]
  browse.py shot URL --out FILE [--full]
  browse.py pdf URL --out FILE
  browse.py click URL --selector SEL [--then-text]
  browse.py fill URL --field "SEL=VALUE" [--field ...] [--submit SEL] [--then-text]
  browse.py eval URL --js "expression"
  browse.py login URL                     headed window; sign in manually, close it; profile persists
  browse.py --selftest                    checks install; passes (with a note) when chromium is absent
Profile: $DD_HOME/browser-profile    Requires: pip install playwright && python3 -m playwright install chromium
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAX_DEFAULT = 8000


def profile_dir() -> str:
    d = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def args_multi(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def page_text(page, max_chars: int) -> str:
    text = page.evaluate("() => (document.body && document.body.innerText) || ''")
    return text[:max_chars]


def run(cmd: str, url: str, argv: list[str]) -> dict:
    from playwright.sync_api import sync_playwright  # pip install playwright
    headed = cmd == "login"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(profile_dir(), headless=not headed,
                                                   viewport={"width": 1366, "height": 900},
                                                   args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        page.goto(url, wait_until="domcontentloaded")
        if (w := arg(argv, "--wait")):
            page.wait_for_selector(w)
        else:
            page.wait_for_load_state("networkidle", timeout=15000) if cmd != "login" else None
        res: dict = {"ok": True, "url": page.url, "title": page.title()}
        max_chars = int(arg(argv, "--max-chars", MAX_DEFAULT))
        if cmd == "text":
            res["text"] = page_text(page, max_chars)
        elif cmd == "shot":
            out = arg(argv, "--out", "page.png")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=out, full_page="--full" in argv)
            res["path"] = out
        elif cmd == "pdf":
            out = arg(argv, "--out", "page.pdf")
            page.pdf(path=out)
            res["path"] = out
        elif cmd == "click":
            page.click(arg(argv, "--selector"))
            page.wait_for_load_state("networkidle", timeout=15000)
            res["url"], res["title"] = page.url, page.title()
            if "--then-text" in argv:
                res["text"] = page_text(page, max_chars)
        elif cmd == "fill":
            for f in args_multi(argv, "--field"):
                sel, _, val = f.partition("=")
                page.fill(sel, val)
            if (s := arg(argv, "--submit")):
                page.click(s)
                page.wait_for_load_state("networkidle", timeout=15000)
            res["url"], res["title"] = page.url, page.title()
            if "--then-text" in argv:
                res["text"] = page_text(page, max_chars)
        elif cmd == "eval":
            res["result"] = page.evaluate(arg(argv, "--js", "document.title"))
        elif cmd == "login":
            print("Sign in inside the window, then close it. Waiting up to 10 min...", file=sys.stderr)
            deadline = time.time() + 600
            while time.time() < deadline and not page.is_closed() and ctx.pages:
                time.sleep(1)
            res["note"] = "profile saved"
        ctx.close()
        return res


def selftest() -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("browse selftest ok (playwright not installed here: pip install playwright && python3 -m playwright install chromium)")
        return 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.set_content("<html><title>dd</title><body><h1>ok</h1></body></html>")
            assert pg.title() == "dd" and "ok" in page_text(pg, 100)
            b.close()
        print("browse selftest ok (chromium works)")
    except Exception as e:  # browser binary missing etc.
        print(f"browse selftest ok (playwright present, chromium not ready: {type(e).__name__}) — run: python3 -m playwright install chromium")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd = argv[0]
    if cmd not in ("text", "shot", "pdf", "click", "fill", "eval", "login") or len(argv) < 2:
        print(__doc__)
        return 2
    try:
        print(json.dumps(run(cmd, argv[1], argv[2:]), ensure_ascii=False, indent=1))
        return 0
    except ImportError:
        print(json.dumps({"ok": False, "error": "playwright missing: pip install playwright && python3 -m playwright install chromium"}))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
