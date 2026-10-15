---
name: daily-briefing
description: Morning/daily "current updates" for dd — RSS headlines (tech, AI, local), Open-Meteo weather (no key), reminders due today, yesterday's learning stats — all fetched live and date-stamped so answers never come from stale memory. Use when Dhina asks "innaiku news / updates / weather / enna irukku" or in the scheduled morning job.
triggers: [news, updates, today, innaiku, briefing, weather, morning, headlines, what's new, kaalai]
metadata:
  version: "1.0"
  owner: dhina
---

# daily-briefing

Live only. Every item carries the fetch time. If a feed fails, say which one failed — never fill from memory.

## Run
```bash
python3 scripts/briefing.py                 # full markdown briefing (news + weather + reminders)
python3 scripts/briefing.py --news-only --max 8
python3 scripts/briefing.py --weather-only
python3 scripts/briefing.py --json          # structured, for Telegram formatting
python3 scripts/briefing.py --init-config   # writes $DD_HOME/briefing.json with defaults (edit feeds there)
```

## Config (`$DD_HOME/briefing.json`)
```json
{
  "location": {"name": "Coimbatore", "lat": 11.0168, "lon": 76.9558, "tz": "Asia/Kolkata"},
  "feeds": [
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=100"},
    {"name": "Hugging Face blog", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "Ollama blog", "url": "https://ollama.com/blog/rss"},
    {"name": "The Hindu Tamil Nadu", "url": "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss"},
    {"name": "Google News India tech", "url": "https://news.google.com/rss/search?q=AI+India&hl=en-IN&gl=IN&ceid=IN:en"}
  ],
  "max_per_feed": 5
}
```
Add/remove feeds freely; any RSS/Atom URL works. Weather = Open-Meteo (free, no key, no signup).

## Delivery
- Telegram: send via `telegram-interface` (chunk at 4000 chars, plain text or MarkdownV2-escaped).
- Schedule: `scheduler-reminders` → `briefing.py --json` every day 07:30 IST (cron `30 7 * * *`).
- Reply shape: `Innaiku <date> briefing (fetched <HH:MM IST>)` → weather one line → top headlines with source → reminders due → learning stats one line.
- Follow-up questions on a headline → `web-search/scripts/fetch.py <link>` then answer; don't guess from the title.
