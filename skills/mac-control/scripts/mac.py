#!/usr/bin/env python3
"""mac.py — JARVIS-style Mac control with built-in macOS tools (no installs).

  open-app NAME | quit-app NAME | open-url URL | search-web QUERY
  volume N|up|down|mute|unmute | media play|pause|next|previous
  notify TEXT | time | battery | wifi | apps | screenshot [--out FILE] | lock
  dark-mode on|off|toggle | clipboard get|set TEXT
  route "spoken or typed text"      fast intent router -> JSON {"action", "args"} or {"action": null}
  do "text"                         route + run in one step (what the dd loop calls)
  --selftest
Off macOS (or with DD_MAC_DRY=1) commands are printed, not run.
On the cloud server (DD_HOST=server) `do` only handles time / weather / news / remind; Mac actions go to the LLM,
which says it can't control the Mac from there.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

DRY = platform.system() != "Darwin" or os.environ.get("DD_MAC_DRY") == "1"
SERVER_OK = {"time", "weather", "news", "remind"}  # router actions that make sense on the cloud server


def on_server() -> bool:
    return os.environ.get("DD_HOST") == "server"

# Spoken names -> real app names
APPS = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "safari": "Safari", "firefox": "Firefox",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "terminal": "Terminal", "finder": "Finder", "spotify": "Spotify", "music": "Music", "whatsapp": "WhatsApp",
    "telegram": "Telegram", "slack": "Slack", "notes": "Notes", "calendar": "Calendar", "mail": "Mail",
    "settings": "System Settings", "system settings": "System Settings", "android studio": "Android Studio",
    "xcode": "Xcode", "figma": "Figma", "zoom": "zoom.us", "cursor": "Cursor", "postman": "Postman",
    "calculator": "Calculator", "photos": "Photos", "preview": "Preview", "activity monitor": "Activity Monitor",
}
SITES = {"youtube": "https://youtube.com", "github": "https://github.com", "gmail": "https://mail.google.com",
         "google": "https://google.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
         "linkedin": "https://linkedin.com", "upwork": "https://upwork.com"}


def osa(script: str) -> list[str]:
    return ["osascript", "-e", script]


def build(action: str, args: list[str]) -> list[list[str]]:
    """Return the command(s) for an action. Pure — no side effects (so it can be tested anywhere)."""
    a0 = args[0] if args else ""
    if action == "open-app":
        return [["open", "-a", APPS.get(a0.lower(), a0)]]
    if action == "quit-app":
        return [osa(f'tell application "{APPS.get(a0.lower(), a0)}" to quit')]
    if action == "open-url":
        url = SITES.get(a0.lower(), a0)
        return [["open", url if re.match(r"^[a-z]+://", url) else f"https://{url}"]]
    if action == "search-web":
        return [["open", "https://www.google.com/search?q=" + urllib.parse.quote_plus(" ".join(args))]]
    if action == "volume":
        if a0 == "mute":
            return [osa("set volume output muted true")]
        if a0 == "unmute":
            return [osa("set volume output muted false")]
        if a0 in ("up", "down"):
            step = 15 if a0 == "up" else -15
            return [osa(f"set volume output volume ((output volume of (get volume settings)) + {step})")]
        return [osa(f"set volume output volume {max(0, min(100, int(a0)))}")]
    if action == "media":
        verb = {"play": "play", "pause": "pause", "next": "next track", "previous": "previous track",
                "prev": "previous track", "toggle": "playpause"}.get(a0, "playpause")
        return [osa(f'if application "Spotify" is running then\n tell application "Spotify" to {verb}\n'
                    f'else\n tell application "Music" to {verb}\nend if')]
    if action == "notify":
        msg = " ".join(args).replace('"', "'")
        return [osa(f'display notification "{msg}" with title "dd"')]
    if action == "battery":
        return [["pmset", "-g", "batt"]]
    if action == "wifi":
        return [["networksetup", "-getairportnetwork", "en0"]]
    if action == "apps":
        return [osa('tell application "System Events" to get name of (processes where background only is false)')]
    if action == "screenshot":
        out = os.path.expanduser(args[args.index("--out") + 1]) if "--out" in args else \
            os.path.expanduser(f"~/Desktop/dd-shot-{time.strftime('%Y%m%d-%H%M%S')}.png")
        return [["screencapture", "-x", out]]
    if action == "lock":
        return [osa('tell application "System Events" to keystroke "q" using {control down, command down}')]
    if action == "dark-mode":
        val = {"on": "true", "off": "false"}.get(a0, "not dark mode")
        return [osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {val}')]
    if action == "clipboard":
        return [["pbpaste"]] if a0 != "set" else [["pbcopy"]]
    raise ValueError(f"unknown action '{action}'")


SKILLS = Path(__file__).resolve().parents[2]
FOLDERS = {"downloads": "~/Downloads", "download": "~/Downloads", "desktop": "~/Desktop", "documents": "~/Documents",
           "home": "~", "personalai": "~/personalai", "applications": "/Applications", "pictures": "~/Pictures"}


def skill_script(skill: str, script: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SKILLS / skill / "scripts" / script), *args], capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL, env={**os.environ, "DD_AGENT": "1"})


def run_skill_action(action: str, args: list[str]) -> dict:
    """Router actions served by other skills (weather/news via daily-briefing, remind via scheduler-reminders)."""
    if action == "weather":
        r = skill_script("daily-briefing", "briefing.py", ["--json", "--weather-only"])
        try:
            w = json.loads(r.stdout)["weather"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return {"ok": False, "action": action, "error": "weather fetch failed"}
        if "error" in w:
            return {"ok": False, "action": action, "error": w["error"]}
        return {"ok": True, "action": action, "say": f"{w['location']}: ippo {w['now_c']}°C, {w['sky']}, max {w['today_max_c']}, "
                                                       f"mazhai chance {w['rain_chance_pct']}%."}
    if action == "news":
        r = skill_script("daily-briefing", "briefing.py", ["--json", "--news-only", "--max", "2"])
        try:
            items = [f"{i['title']}" for s in json.loads(r.stdout)["news"] if s["ok"] for i in s["items"]][:5]
        except (json.JSONDecodeError, KeyError, TypeError):
            items = []
        if not items:
            return {"ok": False, "action": action, "error": "news feeds fetch aagala"}
        return {"ok": True, "action": action, "say": "Top news: " + " | ".join(items)}
    if action == "remind":
        when, text = args[0], args[1]
        r = skill_script("scheduler-reminders", "reminders.py", ["add", text, "--in", when])
        try:
            res = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "action": action, "error": r.stderr[-200:] or "reminder add failed"}
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error", "reminder add failed")}
        return {"ok": True, "action": action, "say": f"Ok, {res['due']} ku '{text}' remind panren."}
    raise ValueError(action)


def run(action: str, args: list[str]) -> dict:
    if action == "time":
        return {"ok": True, "action": "time", "say": time.strftime("Ippo mani %I:%M %p, %A %d %B")}
    if action in ("weather", "news", "remind"):
        return run_skill_action(action, args)
    if action == "open-folder":
        path = os.path.expanduser(FOLDERS.get(args[0].lower(), args[0]) if args else "~")
        cmds = [["open", path]]
    else:
        cmds = build(action, args)
    if DRY:
        return {"ok": True, "action": action, "dry_run": True, "would_run": cmds}
    out = []
    for c in cmds:
        stdin = " ".join(args[1:]) if (action == "clipboard" and args[:1] == ["set"]) else None
        r = subprocess.run(c, capture_output=True, text=True, timeout=30, input=stdin)
        if r.returncode != 0:
            return {"ok": False, "action": action, "error": (r.stderr or r.stdout).strip()[:300]}
        out.append(r.stdout.strip())
    return {"ok": True, "action": action, "output": "\n".join(o for o in out if o)}


# ---------------- fast intent router (English + Thanglish) ----------------
APP_WORDS = "|".join(sorted(map(re.escape, APPS), key=len, reverse=True))
SITE_WORDS = "|".join(sorted(map(re.escape, SITES), key=len, reverse=True))
OPEN = r"(?:open|thira|start|launch|run|kaattu)\w*"
CLOSE = r"(?:close|quit|mooodu|moodu|kill|stop)\w*"
FOLDER_WORDS = "|".join(sorted(map(re.escape, FOLDERS), key=len, reverse=True))
DUR = r"(\d+)\s*(min|mins|minutes?|nimisham|nimidam|nimisam|h|hr|hrs|hours?|mani\s+neram|mani\s+nearam)"
ROUTES: list[tuple[str, str, object]] = [
    (rf"\bremind\s+me\s+in\s+{DUR}\s+(?:to\s+)?(.+)$", "remind", "remind_en"),
    (rf"\b{DUR}\s+(?:la|le|kalichu|kazhichu|aprom|apram)\s+(.+?)\s+(?:nu\s+|ah\s+|a\s+)?(?:remind|nyabagapaduthu|sollu)\w*", "remind", "remind_ta"),
    (rf"\b(?:{OPEN})\s+({FOLDER_WORDS})\s+folder\b|\b({FOLDER_WORDS})\s+folder\s+(?:ah\s+|a\s+)?(?:{OPEN})|\b({FOLDER_WORDS})\s+folder\b", "open-folder", "grp"),
    (r"\b(?:innaiku|today|ippo|now)\s+(?:weather|temperature)\b|\b(?:weather|temperature)\s+(?:enna|eppadi|evlo|sollu|how|today|innaiku)\b"
     r"|^(?:weather|temperature)$|\bmazhai\s+(?:varuma|varumaa|irukka|irukuma|varum)\b|\bveyil\s+(?:evlo|enna)\b", "weather", []),
    (r"\b(?:innaiku|today|latest|top|indha)\s+(?:news|seithi|headlines)\b|\b(?:news|seithi|headlines)\s+(?:enna|sollu|kaattu|what|today|innaiku)\b"
     r"|^(?:news|headlines)$", "news", []),
    (rf"\b(?:{OPEN})\s+({SITE_WORDS})\b|\b({SITE_WORDS})\s+(?:ah\s+|a\s+)?(?:{OPEN})", "open-url", "site"),
    (rf"\b(?:{OPEN})\s+({APP_WORDS})\b|\b({APP_WORDS})\s+(?:ah\s+|a\s+|app\s+)?(?:{OPEN})", "open-app", "app"),
    (rf"\b(?:{CLOSE})\s+({APP_WORDS})\b|\b({APP_WORDS})\s+(?:ah\s+|a\s+|app\s+)?(?:{CLOSE})", "quit-app", "app"),
    (r"\b(?:volume|sound)\b\D{0,12}(\d{1,3})", "volume", "num"),
    (r"\bunmute\b|\bsound\s+(?:on|podu)\b", "volume", ["unmute"]),
    (r"\bmute\b|\bsound\s+(?:off|vendam|niruthu)\b", "volume", ["mute"]),
    (r"\b(?:volume|sound)\s+(?:up|increase|jaasthi|athigam|koodu|eathu|ethu)\w*", "volume", ["up"]),
    (r"\b(?:volume|sound)\s+(?:down|decrease|kammi|korai|kurai|eraku|iraku)\w*", "volume", ["down"]),
    (r"\b(?:next|adutha)\s+(?:song|track|paattu|pattu)\b", "media", ["next"]),
    (r"\b(?:previous|prev|munnadi)\s+(?:song|track|paattu|pattu)\b", "media", ["previous"]),
    (r"\b(?:pause|stop)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:pause|stop|niruthu)\w*", "media", ["pause"]),
    (r"\b(?:play|podu)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:play|podu)\w*", "media", ["play"]),
    (r"\bscreen\s*shot\b|\bscreenshot\b", "screenshot", []),
    (r"\block\s+(?:the\s+)?(?:screen|mac|laptop|system)\b|\b(?:screen|mac|laptop)\s+lock\b", "lock", []),
    (r"\bbattery\b|\bcharge\s+(?:evlo|enna|how much)\b", "battery", []),
    (r"\bwi-?fi\b", "wifi", []),
    (r"\b(?:time|mani)\s+(?:enna|evlo|aachu|what)\b|\bwhat(?:'s| is) the time\b|\bippo\s+mani\b", "time", []),
    (r"\bdark\s*mode\s+(on|off)\b", "dark-mode", "grp"),
    (r"\bdark\s*mode\b", "dark-mode", ["toggle"]),
    (r"\b(?:google|search)\s+(?:pannu\s+|for\s+)?(.+)$", "search-web", "rest"),
]


# voice transcripts spell numbers out ("remind me in ten minutes") — turn them into digits before a time unit
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_NUM_WORDS = {**_ONES, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "an": 1, "a": 1,
              "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
for _tens, _tv in (("twenty", 20), ("thirty", 30), ("forty", 40), ("fifty", 50)):
    for _o, _ov in _ONES.items():
        _NUM_WORDS[f"{_tens} {_o}"] = _NUM_WORDS[f"{_tens}-{_o}"] = _tv + _ov
_NUM_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")"
                     r"(?=\s+(?:min|mins|minutes?|h|hr|hrs|hours?|nimisham|nimidam|nimisam)\b)")


def route(text: str) -> dict:
    t = re.sub(r"[^\w\s\-.']", " ", text.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    t = _NUM_RE.sub(lambda m: str(_NUM_WORDS[m.group(1)]), t)
    for pat, action, kind in ROUTES:
        m = re.search(pat, t)
        if not m:
            continue
        groups = [g for g in m.groups() if g] if m.groups() else []
        if kind in ("remind_en", "remind_ta"):
            n, unit, what = groups[0], groups[1], groups[2].strip(" .")
            u = "m" if unit.startswith(("m", "n")) and "mani" not in unit else "h"
            args = [f"{n}{u}", what]
        elif kind == "num":
            args = [groups[0]]
        elif kind in ("app", "site", "grp"):
            args = [groups[0]]
        elif kind == "rest":
            q = re.sub(r"\b(pannu|panni|kaattu|please)\b", "", groups[0]).strip()
            if not q:
                continue
            args = [q]
        else:
            args = list(kind)
        return {"action": action, "args": args}
    return {"action": None}


REPLIES = {"open-folder": "{0} folder open panniten.", "open-app": "{0} open panniten.", "quit-app": "{0} close panniten.", "open-url": "{0} open panniten.",
           "search-web": "Google la '{0}' search panniten.", "volume": "Volume {0}.", "media": "Music {0}.",
           "screenshot": "Screenshot eduthuten, Desktop la irukku.", "lock": "Screen lock panniten.",
           "dark-mode": "Dark mode {0}.", "notify": "Notification potten."}


def do(text: str) -> dict:
    r = route(text)
    if not r["action"] or (on_server() and r["action"] not in SERVER_OK):
        return {"handled": False}
    res = run(r["action"], r["args"])
    if not res.get("ok"):
        return {"handled": True, "ok": False, "say": f"Mudiyala: {res.get('error', 'error')}", **res}
    if "say" in res:
        say = res["say"]
    elif res.get("output") and r["action"] in ("battery", "wifi", "apps", "clipboard"):
        say = res["output"][:300]
    else:
        say = REPLIES.get(r["action"], "Done.").format(*(r["args"] or [""]))
    return {"handled": True, "ok": True, "say": say, **res}


def selftest() -> int:
    os.environ["DD_MAC_DRY"] = "1"
    global DRY
    DRY = True
    cases = {
        "chrome open pannu": ("open-app", ["chrome"]), "open vs code": ("open-app", ["vs code"]),
        "Jarvis, spotify ah close pannu": ("quit-app", ["spotify"]), "youtube open pannu": ("open-url", ["youtube"]),
        "volume 30 vei": ("volume", ["30"]), "sound kammi pannu": ("volume", ["down"]), "mute pannu": ("volume", ["mute"]),
        "adutha paattu podu": ("media", ["next"]), "paattu podu": ("media", ["play"]), "screenshot edu": ("screenshot", []),
        "mani enna?": ("time", []), "battery evlo irukku": ("battery", []), "mac lock pannu": ("lock", []),
        "dark mode on pannu": ("dark-mode", ["on"]), "google pannu ollama latest version": ("search-web", ["ollama latest version"]),
        "aguaclan erp la invoice bug fix pannu": (None, None), "write a poem about rain": (None, None),
        "news feed component la bug fix pannu": (None, None), "weather app ku api design pannu": (None, None),
        "innaiku news enna": ("news", []), "weather eppadi": ("weather", []), "mazhai varuma": ("weather", []),
        "downloads folder open pannu": ("open-folder", ["downloads"]), "desktop folder": ("open-folder", ["desktop"]),
        "remind me in 10 minutes to call amma": ("remind", ["10m", "call amma"]),
        "20 nimisham la tea kudikka remind pannu": ("remind", ["20m", "tea kudikka"]),
        "2 mani neram kalichu client ku call pannanum nu nyabagapaduthu": ("remind", ["2h", "client ku call pannanum"]),
        "Remind me in ten minutes to call the client.": ("remind", ["10m", "call the client"]),
        "remind me in twenty five minutes to stretch": ("remind", ["25m", "stretch"]),
        "remind me in an hour to call a friend": ("remind", ["1h", "call a friend"]),
    }
    for text, (action, args) in cases.items():
        r = route(text)
        assert r["action"] == action, (text, r)
        if action:
            assert r["args"] == args, (text, r)
    assert build("open-app", ["chrome"]) == [["open", "-a", "Google Chrome"]]
    assert build("volume", ["150"])[0][-1].endswith("volume 100")
    assert build("open-url", ["github.com"]) == [["open", "https://github.com"]]
    d = do("chrome open pannu")
    assert d["handled"] and d["say"] == "chrome open panniten." and d["dry_run"], d
    assert do("time enna")["say"].startswith("Ippo mani")
    assert do("write a poem") == {"handled": False}
    assert do("downloads folder open pannu")["would_run"] == [["open", os.path.expanduser("~/Downloads")]]
    import tempfile
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-mac-")
    rm = do("remind me in 10 minutes to call amma")
    assert rm["handled"] and rm["ok"] and "call amma" in rm["say"], rm
    os.environ["DD_HOST"] = "server"  # cloud server: Mac actions are not handled, reminders/time still are
    assert do("chrome open pannu") == {"handled": False}
    assert do("volume 30 vei") == {"handled": False}
    assert do("mani enna?")["handled"]
    assert do("20 nimisham la tea kudikka remind pannu")["ok"]
    os.environ.pop("DD_HOST")
    print(f"mac selftest ok ({len(cases)} router phrases, dry-run builds, server mode)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "route":
            res = route(" ".join(rest))
        elif cmd == "do":
            res = do(" ".join(rest))
        else:
            res = run(cmd, rest)
    except (ValueError, IndexError) as e:
        res = {"ok": False, "error": str(e)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
