#!/usr/bin/env python3
"""briefing.py — live daily briefing: RSS headlines + Open-Meteo weather + reminders due (stdlib only).

  briefing.py [--news-only|--weather-only] [--max N] [--json] [--init-config]
  briefing.py --selftest
Config: $DD_HOME/briefing.json (created with --init-config). Reminders: $DD_HOME/reminders.db (if present).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 dd-assistant/1.0"
DEFAULT_CONFIG = {
    "location": {"name": "Coimbatore", "lat": 11.0168, "lon": 76.9558, "tz": "Asia/Kolkata"},
    "feeds": [
        {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=100"},
        {"name": "Hugging Face blog", "url": "https://huggingface.co/blog/feed.xml"},
        {"name": "Ollama blog", "url": "https://ollama.com/blog/rss"},
        {"name": "The Hindu Tamil Nadu", "url": "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss"},
        {"name": "Google News India AI", "url": "https://news.google.com/rss/search?q=AI+India&hl=en-IN&gl=IN&ceid=IN:en"},
    ],
    "max_per_feed": 5,
}
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 80: "showers",
       81: "showers", 82: "heavy showers", 95: "thunderstorm", 96: "thunderstorm+hail", 99: "thunderstorm+hail"}


def home() -> Path:
    h = Path(os.environ.get("DD_HOME", Path.home() / ".dd"))
    h.mkdir(parents=True, exist_ok=True)
    return h


def load_config() -> dict:
    p = home() / "briefing.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return DEFAULT_CONFIG


def init_config() -> str:
    p = home() / "briefing.json"
    if not p.exists():
        p.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return str(p)


def http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_feed(xml_text: str, limit: int = 5) -> list[dict]:
    """RSS 2.0 and Atom."""
    root = ET.fromstring(xml_text.strip())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for it in root.iter("item"):  # RSS
        items.append({"title": strip_html(it.findtext("title") or ""), "link": (it.findtext("link") or "").strip(),
                      "published": (it.findtext("pubDate") or "").strip(),
                      "summary": strip_html(it.findtext("description") or "")[:200]})
    if not items:  # Atom
        for e in root.findall("a:entry", ns):
            link_el = e.find("a:link", ns)
            items.append({"title": strip_html(e.findtext("a:title", default="", namespaces=ns)),
                          "link": (link_el.get("href") if link_el is not None else "") or "",
                          "published": (e.findtext("a:updated", default="", namespaces=ns) or "").strip(),
                          "summary": strip_html(e.findtext("a:summary", default="", namespaces=ns))[:200]})
    return [i for i in items if i["title"]][:limit]


def fetch_news(cfg: dict, max_items: int | None = None) -> list[dict]:
    out = []
    per = max_items or cfg.get("max_per_feed", 5)
    for f in cfg.get("feeds", []):
        try:
            items = parse_feed(http_get(f["url"]), per)
            out.append({"source": f["name"], "ok": True, "items": items})
        except Exception as e:
            out.append({"source": f["name"], "ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}", "items": []})
    return out


def weather_from_payload(d: dict, name: str) -> dict:
    cur, daily = d.get("current", {}), d.get("daily", {})
    return {"location": name, "now_c": cur.get("temperature_2m"), "feels_c": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"), "sky": WMO.get(cur.get("weather_code"), "n/a"),
            "today_max_c": (daily.get("temperature_2m_max") or [None])[0],
            "today_min_c": (daily.get("temperature_2m_min") or [None])[0],
            "rain_chance_pct": (daily.get("precipitation_probability_max") or [None])[0]}


def fetch_weather(cfg: dict) -> dict:
    loc = cfg["location"]
    q = urllib.parse.urlencode({"latitude": loc["lat"], "longitude": loc["lon"], "timezone": loc.get("tz", "auto"),
                                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
                                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                                "forecast_days": 1})
    try:
        return weather_from_payload(json.loads(http_get(f"https://api.open-meteo.com/v1/forecast?{q}")), loc["name"])
    except Exception as e:
        return {"location": loc["name"], "error": f"{type(e).__name__}: {str(e)[:80]}"}


def reminders_due() -> list[dict]:
    db = home() / "reminders.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(db)
        end = int(time.time()) + 86400
        rows = con.execute("SELECT id, text, due FROM reminders WHERE done=0 AND due<=? ORDER BY due", (end,)).fetchall()
        return [{"id": r[0], "text": r[1], "due": datetime.fromtimestamp(r[2]).strftime("%H:%M %d-%b")} for r in rows]
    except sqlite3.Error:
        return []


def learning_stats() -> dict:
    p = home() / "learning" / "interactions.jsonl"
    if not p.exists():
        return {}
    since = time.time() - 86400
    n = good = bad = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            it = json.loads(line)
        except json.JSONDecodeError:
            continue
        if it.get("ts", 0) >= since:
            n += 1
            good += it.get("rating") == "good"
            bad += it.get("rating") == "bad"
    return {"last_24h": n, "good": good, "bad": bad}


def build(news: bool = True, weather: bool = True, max_items: int | None = None) -> dict:
    cfg = load_config()
    now = datetime.now(timezone.utc).astimezone()
    return {"date": now.strftime("%a %d %b %Y"), "fetched_at": now.strftime("%H:%M %Z"),
            "weather": fetch_weather(cfg) if weather else None,
            "news": fetch_news(cfg, max_items) if news else None,
            "reminders": reminders_due(), "learning": learning_stats()}


def render(b: dict) -> str:
    lines = [f"Innaiku {b['date']} briefing (fetched {b['fetched_at']})"]
    w = b.get("weather")
    if w:
        if "error" in w:
            lines.append(f"Weather: {w['location']} — fetch failed ({w['error']})")
        else:
            lines.append(f"Weather {w['location']}: {w['now_c']}°C ({w['sky']}), feels {w['feels_c']}°C, "
                         f"max {w['today_max_c']}/min {w['today_min_c']}, rain {w['rain_chance_pct']}%")
    if b.get("news"):
        lines.append("\nNews:")
        for src in b["news"]:
            if not src["ok"]:
                lines.append(f"- {src['source']}: FAILED ({src['error']})")
                continue
            for it in src["items"]:
                lines.append(f"- [{src['source']}] {it['title']} — {it['link']}")
    if b.get("reminders"):
        lines.append("\nReminders due:")
        lines += [f"- {r['due']}: {r['text']} (#{r['id']})" for r in b["reminders"]]
    ls = b.get("learning")
    if ls:
        lines.append(f"\nLearning: {ls['last_24h']} interactions last 24h, good {ls['good']}, bad {ls['bad']}")
    return "\n".join(lines)


RSS_FIXTURE = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Ollama &amp; tools</title><link>https://a.example/1</link><pubDate>Wed, 15 Oct 2026 01:00:00 GMT</pubDate><description>&lt;p&gt;hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://a.example/2</link></item></channel></rss>"""
ATOM_FIXTURE = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom one</title><link href="https://b.example/1"/><updated>2026-10-15T00:00:00Z</updated><summary>s</summary></entry></feed>"""
WX_FIXTURE = {"current": {"temperature_2m": 27.4, "apparent_temperature": 29.1, "relative_humidity_2m": 70, "weather_code": 2},
              "daily": {"temperature_2m_max": [31.0], "temperature_2m_min": [22.5], "precipitation_probability_max": [40]}}


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-brief-")
    rss = parse_feed(RSS_FIXTURE)
    assert len(rss) == 2 and rss[0]["title"] == "Ollama & tools" and rss[0]["summary"] == "hello world", rss
    atom = parse_feed(ATOM_FIXTURE)
    assert atom[0]["link"] == "https://b.example/1", atom
    w = weather_from_payload(WX_FIXTURE, "Coimbatore")
    assert w["sky"] == "partly cloudy" and w["today_max_c"] == 31.0
    assert Path(init_config()).exists()
    text = render({"date": "Thu 15 Oct 2026", "fetched_at": "07:30 IST", "weather": w,
                   "news": [{"source": "HN", "ok": True, "items": rss}], "reminders": [], "learning": {}})
    assert "Weather Coimbatore: 27.4°C" in text and "[HN] Ollama & tools" in text, text
    print("briefing selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--init-config" in argv:
        print(init_config())
        return 0
    mx = int(argv[argv.index("--max") + 1]) if "--max" in argv else None
    b = build(news="--weather-only" not in argv, weather="--news-only" not in argv, max_items=mx)
    print(json.dumps(b, ensure_ascii=False, indent=1) if "--json" in argv else render(b))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
