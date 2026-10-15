#!/usr/bin/env python3
"""search.py — key-less web search for dd.

  search.py "query" [--max N] [--news] [--backend searxng|ddgs|html]
  search.py --selftest
Backend order: $SEARXNG_URL (self-hosted) -> ddgs package -> DuckDuckGo HTML fallback (stdlib).
Output: JSON {"query","date","backend","results":[{"title","url","snippet"}]}
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

UA = "Mozilla/5.0 (X11; Linux x86_64) dd-assistant/1.0"


def http_get(url: str, timeout: int = 20, data: bytes | None = None) -> str:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------- backend 1: SearXNG (JSON API) ----------
def searxng(q: str, n: int, news: bool) -> list[dict]:
    base = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("SEARXNG_URL not set")
    params = {"q": q, "format": "json", "language": "en"}
    if news:
        params["categories"] = "news"
    data = json.loads(http_get(f"{base}/search?{urllib.parse.urlencode(params)}"))
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])[:n]]


# ---------- backend 2: ddgs package ----------
def ddgs_search(q: str, n: int, news: bool) -> list[dict]:
    from ddgs import DDGS  # pip install ddgs
    with DDGS() as d:
        rows = d.news(q, max_results=n) if news else d.text(q, max_results=n)
    out = []
    for r in rows or []:
        out.append({"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                    "snippet": r.get("body") or r.get("excerpt", "")})
    return out


# ---------- backend 3: DuckDuckGo HTML (stdlib) ----------
class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._cur: dict | None = None
        self._in_title = False
        self._in_snip = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": self._clean_url(a.get("href", "")), "snippet": ""}
            self._in_title = True
        elif tag == "a" and "result__snippet" in cls and self._cur is not None:
            self._in_snip = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            self._in_title = False
        elif tag == "a" and self._in_snip:
            self._in_snip = False
            if self._cur:
                self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._in_title and self._cur is not None:
            self._cur["title"] += data
        elif self._in_snip and self._cur is not None:
            self._cur["snippet"] += data

    @staticmethod
    def _clean_url(href: str) -> str:
        if "uddg=" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return qs.get("uddg", [href])[0]
        return href


def parse_ddg_html(page: str) -> list[dict]:
    p = _DDGParser()
    p.feed(page)
    return [{k: html.unescape(re.sub(r"\s+", " ", v)).strip() for k, v in r.items()} for r in p.results]


def ddg_html(q: str, n: int, news: bool) -> list[dict]:
    body = urllib.parse.urlencode({"q": q + (" news" if news else ""), "kl": "in-en"}).encode()
    page = http_get("https://html.duckduckgo.com/html/", data=body)
    return parse_ddg_html(page)[:n]


BACKENDS = {"searxng": searxng, "ddgs": ddgs_search, "html": ddg_html}


def search(q: str, n: int = 5, news: bool = False, backend: str | None = None) -> dict:
    order = [backend] if backend else ["searxng", "ddgs", "html"]
    errors = []
    for name in order:
        try:
            res = BACKENDS[name](q, n, news)
            if res:
                return {"query": q, "date": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                        "backend": name, "results": res}
            errors.append(f"{name}: empty")
        except Exception as e:  # move on to the next backend
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:80]}")
        time.sleep(1)
    return {"query": q, "date": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "backend": None, "results": [], "errors": errors}


FIXTURE = """<div class="result"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Follama%2Follama&amp;rut=1">Ollama on GitHub</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Follama%2Follama">Get up &amp; running with large language models.</a></div>
<div class="result"><h2 class="result__title"><a class="result__a" href="https://ollama.com/">Ollama</a></h2>
<a class="result__snippet" href="https://ollama.com/">Run   Llama, Qwen locally.</a></div>"""


def selftest() -> int:
    r = parse_ddg_html(FIXTURE)
    assert len(r) == 2, r
    assert r[0]["url"] == "https://github.com/ollama/ollama" and r[0]["title"] == "Ollama on GitHub", r[0]
    assert r[0]["snippet"] == "Get up & running with large language models."
    assert r[1]["snippet"] == "Run Llama, Qwen locally."
    out = search("x", backend="searxng")  # SEARXNG_URL unset -> graceful error, no crash
    assert out["results"] == [] and out["errors"], out
    print("search selftest ok (parser + fallback)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    q = argv[0]
    n = int(argv[argv.index("--max") + 1]) if "--max" in argv else 5
    backend = argv[argv.index("--backend") + 1] if "--backend" in argv else None
    out = search(q, n, "--news" in argv, backend)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["results"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
