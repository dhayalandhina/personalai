#!/usr/bin/env python3
"""mac.py — JARVIS-style Mac control with built-in macOS tools (no installs).

  open-app NAME | quit-app NAME | open-url URL | search-web QUERY
  volume N|up|down|mute|unmute | media play|pause|next|previous
  notify TEXT | time | battery | wifi | apps | screenshot [--out FILE] | lock
  read-screen                       screenshot + Apple Vision OCR (built-in) -> the text on screen
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
import shutil
import subprocess
import sys
import tempfile
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
    # how Whisper writes them when the command was said in Tamil ("குரோம் திற" -> "Gurum tire")
    "kurom": "Google Chrome", "gurum": "Google Chrome", "krome": "Google Chrome", "crome": "Google Chrome",
    "kroam": "Google Chrome", "spotifai": "Spotify", "whatsup": "WhatsApp", "watsapp": "WhatsApp",
}
SPOKEN = {"kurom": "chrome", "gurum": "chrome", "krome": "chrome", "crome": "chrome", "kroam": "chrome",
          "spotifai": "spotify", "whatsup": "whatsapp", "watsapp": "whatsapp"}  # (for the spoken reply)
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
    if action == "youtube-search":
        return [["open", "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(" ".join(args).strip())]]
    if action == "wifi-power":
        return [["networksetup", "-setairportpower", "en0", "on" if a0 == "on" else "off"]]
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
    if action == "type-text":  # types into whatever has the keyboard focus (form fields, editors, chat boxes)
        txt = " ".join(args).replace("\\", "\\\\").replace('"', '\\"')
        return [osa(f'tell application "System Events" to keystroke "{txt}"')]
    if action == "press-key":  # "enter", "tab", "cmd+s", "shift+tab", "cmd+shift+t"
        parts = [p.strip().lower() for p in a0.replace("command", "cmd").replace("-", "+").split("+") if p.strip()]
        key, mods = (parts[-1] if parts else "return"), parts[:-1]
        using = ", ".join(KEY_MODS[m] for m in mods if m in KEY_MODS)
        suffix = f" using {{{using}}}" if using else ""
        if key in KEY_CODES:
            return [osa(f'tell application "System Events" to key code {KEY_CODES[key]}{suffix}')]
        return [osa(f'tell application "System Events" to keystroke "{key[:1]}"{suffix}')]
    raise ValueError(f"unknown action '{action}'")


KEY_CODES = {"enter": 36, "return": 36, "tab": 48, "escape": 53, "esc": 53, "space": 49, "delete": 51, "backspace": 51,
             "up": 126, "down": 125, "left": 123, "right": 124, "home": 115, "end": 119, "pageup": 116, "pagedown": 121}
KEY_MODS = {"cmd": "command down", "shift": "shift down", "alt": "option down", "option": "option down", "ctrl": "control down",
            "control": "control down"}
# typing keeps the original text (case, punctuation) — matched on the raw message, before the router normalises it
TYPE_RE = re.compile(r"(?is)^\s*(?:(?:hey|ok|dd|jarvis)[,\s]+)*(?:idha|idhai|this|intha)?\s*type(?:\s+pannu|\s+this|\s+in|\s+it)?\s*[:,\-]?\s+(.+?)\s*$")
TYPE_TA_RE = re.compile(r"(?is)^\s*(?:(?:hey|ok|dd|jarvis)[,\s]+)*(.+?)\s+(?:nu|nnu|-nu|endru)\s+type\s+pann\w*\s*$")
COMBO = r"(?:cmd|command|ctrl|control|shift|alt|option)(?:\s*\+\s*\w+)+"
PRESS_RE = re.compile(rf"(?i)\b(?:press|adi|thattu|hit)\s+(enter|return|tab|escape|esc|space|delete|backspace|up|down|left|right|{COMBO})\b"
                      rf"|\b(enter|return|tab|escape|esc|space|delete|backspace|{COMBO})\s+(?:key\s+)?(?:press|adi|thattu|podu)\w*")


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


OCR_JXA = r"""
ObjC.import('Foundation'); ObjC.import('AppKit'); ObjC.import('Vision');
function run(argv) {
  const img = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
  if (!img || img.isNil()) return 'ERR: cannot read image';
  const cg = img.CGImageForProposedRectContextHints(null, null, null);
  const req = $.VNRecognizeTextRequest.alloc.init;
  req.recognitionLevel = $.VNRequestTextRecognitionLevelAccurate;
  req.usesLanguageCorrection = true;
  const handler = $.VNImageRequestHandler.alloc.initWithCGImageOptions(cg, $({}));
  const err = Ref();
  if (!handler.performRequestsError($([req]), err)) return 'ERR: ' + (err[0] ? ObjC.unwrap(err[0].localizedDescription) : 'vision failed');
  const res = req.results, lines = [];
  for (let i = 0; i < res.count; i++) {
    const c = res.objectAtIndex(i).topCandidatesWithMaximumCandidates(1);
    if (c.count) lines.push(ObjC.unwrap(c.objectAtIndex(0).string));
  }
  return lines.join('\n');
}
"""


def ocr(image: str) -> tuple[bool, str]:
    """Text on an image: Apple Vision through built-in JXA (no install), else tesseract if present."""
    js = Path(tempfile.gettempdir()) / "dd_ocr.js"
    js.write_text(OCR_JXA, encoding="utf-8")
    r = subprocess.run(["osascript", "-l", "JavaScript", str(js), image], capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out and not out.startswith("ERR:"):
        return True, out
    if shutil.which("tesseract"):
        t = subprocess.run(["tesseract", image, "-", "--psm", "3"], capture_output=True, text=True, timeout=120)
        if t.returncode == 0 and t.stdout.strip():
            return True, t.stdout.strip()
    return False, (out or r.stderr.strip() or "OCR failed")[:300]


def read_screen() -> dict:
    """Screenshot the screen and return its text (for 'screen la enna irukku', 'read my screen')."""
    shot = str(Path(tempfile.gettempdir()) / f"dd_screen_{int(time.time())}.png")
    cmds = [["screencapture", "-x", shot]]
    if DRY:
        return {"ok": True, "action": "read-screen", "dry_run": True, "would_run": cmds + [["osascript", "-l", "JavaScript", "<vision-ocr>", shot]]}
    r = subprocess.run(cmds[0], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not Path(shot).exists():
        return {"ok": False, "action": "read-screen", "error": "screenshot aagala: " + (r.stderr or "").strip()[:200]
                + " (System Settings → Privacy & Security → Screen Recording la dd ku ON pannu)"}
    ok, text = ocr(shot)
    try:
        Path(shot).unlink()
    except OSError:
        pass
    if not ok:
        return {"ok": False, "action": "read-screen", "error": text}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"ok": True, "action": "read-screen", "lines": len(lines), "text": text[:6000],
            "say": f"Screen la {len(lines)} lines text irukku. " + (" ".join(lines[:6])[:220] if lines else "")}


def run(action: str, args: list[str]) -> dict:
    if action == "read-screen":
        return read_screen()
    if action == "time":
        return {"ok": True, "action": "time", "say": time.strftime("Ippo mani %I:%M %p, %A %d %B")}
    if action == "date":
        return {"ok": True, "action": "date", "say": time.strftime("Innaiku %A, %d %B %Y.").replace(" 0", " ")}
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
OPEN = r"(?:open|thira|thera|tira|tire|tera|thirakk|start|launch|run|kaattu)\w*"
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
    (r"\b(?:volume|sound|savun|saund|soundu)\b\D{0,12}(\d{1,3})", "volume", "num"),
    (r"\bunmute\b|\bsound\s+(?:on|podu)\b", "volume", ["unmute"]),
    (r"\bmute\b|\bsound\s+(?:off|vendam|niruthu)\b", "volume", ["mute"]),
    (r"\b(?:volume|sound|savun|saund|soundu)\s+(?:up|increase|jaasthi|jasthi|athigam|koodu|eathu|ethu)\w*", "volume", ["up"]),
    (r"\b(?:volume|sound|savun|saund|soundu)\s+(?:down|decrease|kammi|kambi|korai|kurai|eraku|iraku)\w*", "volume", ["down"]),
    (r"\b(?:youtube|yt)\s+(?:la|le|il|in|on)\s+(.+?)\s+(?:podu|pottu|play|paadu|search|thedu|kaattu|open|vai|vei)\w*\s*$"
     r"|\bplay\s+(.+?)\s+(?:on|in)\s+youtube\b", "youtube-search", "grp"),
    (r"\b(?:next|adutha)\s+(?:song|track|paattu|pattu)\b", "media", ["next"]),
    (r"\b(?:previous|prev|munnadi)\s+(?:song|track|paattu|pattu)\b", "media", ["previous"]),
    (r"\b(?:pause|stop)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:pause|stop|niruthu)\w*", "media", ["pause"]),
    (r"\b(?:play|podu)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:play|podu)\w*", "media", ["play"]),
    (r"\b(?:read|padi|padikka|paaru|paar|sollu)\b.{0,12}\bscreen\b|\bscreen\b.{0,16}\b(?:padi|padikka|read|enna irukku|enna iruku|la enna)\b"
     r"|\bwhat(?:'s| is) on (?:my |the )?screen\b|\bidha padi\b|\bidhai padi\b", "read-screen", []),
    (r"\bscreen\s*shot\b|\bscreenshot\b", "screenshot", []),
    (r"\block\s+(?:the\s+)?(?:screen|mac|laptop|system)\b|\b(?:screen|mac|laptop)\s+lock\b|^lock(?:\s+(?:pannu|panniru|it))?$",
     "lock", []),
    (r"\bbattery\b|\bcharge\s+(?:evlo|enna|how much)\b", "battery", []),
    (r"\bwi-?fi\s+(on|off)\b|\bwi-?fi\s+(?:ah\s+)?(?:anachu|anaichu|niruthu|stop)\w*", "wifi-power", "wifi"),
    (r"\bwi-?fi\b", "wifi", []),
    (r"\b(?:time|mani|manu|maani)\s+(?:enna|ennai|evlo|aachu|what)\b|\bwhat(?:'s| is) the time\b|\bwhat time is it\b"
     r"|\bippo\w*\s+(?:mani|manu)\b", "time", []),
    (r"\b(?:date|thethi|thedhi|tarikh)\s+(?:enna|what|sollu)\b|\bwhat(?:'s| is)\s+(?:the\s+|today'?s\s+)?date\b"
     r"|\b(?:innaiku|today|inniki)\s+(?:enna\s+)?(?:date|thethi|kizhamai|day)\b|^(?:date|today'?s date)$", "date", []),
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
_NUM_WORDS.update({"seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "zero": 0})
for _tens, _tv in (("sixty", 60), ("seventy", 70), ("eighty", 80), ("ninety", 90)):
    for _o, _ov in _ONES.items():
        _NUM_WORDS[f"{_tens} {_o}"] = _NUM_WORDS[f"{_tens}-{_o}"] = _tv + _ov
_VOL_NUM_RE = re.compile(r"\b((?:volume|sound)\s+(?:ah\s+|a\s+|to\s+|ku\s+)?)("
                         + "|".join(sorted((w for w in _NUM_WORDS if w not in ("a", "an")), key=len, reverse=True)) + r")\b")
_NUM_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")"
                     r"(?=\s+(?:min|mins|minutes?|h|hr|hrs|hours?|nimisham|nimidam|nimisam)\b)")


def route(text: str) -> dict:
    m = TYPE_RE.match(text) or TYPE_TA_RE.match(text)
    if m and len(m.group(1)) <= 2000:
        return {"action": "type-text", "args": [m.group(1)]}
    m = PRESS_RE.search(text)
    if m:
        key = (m.group(1) or m.group(2)).lower().replace(" ", "")
        return {"action": "press-key", "args": [key]}
    t = re.sub(r"[^\w\s\-.']", " ", text.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    t = _NUM_RE.sub(lambda m: str(_NUM_WORDS[m.group(1)]), t)
    t = _VOL_NUM_RE.sub(lambda m: m.group(1) + str(_NUM_WORDS.get(m.group(2), 100)), t)  # "volume thirty" -> 30
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
        elif kind == "wifi":
            args = [groups[0] if groups else "off"]
        elif kind == "rest":
            q = re.sub(r"\b(pannu|panni|kaattu|please)\b", "", groups[0]).strip()
            if not q:
                continue
            args = [q]
        else:
            args = list(kind)
        return {"action": action, "args": args}
    return {"action": None}


def battery_say(out: str) -> str:
    """pmset -g batt -> one spoken line ("Battery 85%, charge aagudhu.") instead of reading the raw output aloud."""
    m = re.search(r"(\d{1,3})%", out)
    if not m:
        return out.strip()[:120]
    pct = m.group(1)
    low = out.lower()
    if "charged" in low and "discharg" not in low:
        state = "full charge"
    elif "discharging" in low:
        rem = re.search(r"(\d+):(\d{2}) remaining", out)
        h, mins = (int(rem.group(1)), int(rem.group(2))) if rem else (0, 0)
        left = (f"{h} mani {mins} nimisham" if h else f"{mins} nimisham") if (h or mins) else ""
        state = "battery la odudhu" + (f", innum {left} varum" if left else "")
    elif "charging" in low or "ac power" in low:
        state = "charge aagudhu"
    else:
        state = ""
    return f"Battery {pct}%" + (f", {state}." if state else ".")


def wifi_say(out: str) -> str:
    m = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
    if m:
        return f"Wi-Fi: {m.group(1).strip()} la connect aagi irukku."
    if "not associated" in out.lower():
        return "Wi-Fi edhulayum connect aagala."
    return out.strip()[:120]


REPLIES = {"open-folder": "{0} folder open panniten.", "open-app": "{0} open panniten.", "quit-app": "{0} close panniten.", "open-url": "{0} open panniten.",
           "search-web": "Google la '{0}' search panniten.", "youtube-search": "YouTube la '{0}' search panniten.",
           "wifi-power": "Wi-Fi {0} panniten.", "volume": "Volume {0}.", "media": "Music {0}.",
           "screenshot": "Screenshot eduthuten, Desktop la irukku.", "lock": "Screen lock panniten.",
           "read-screen": "Screen padichiten.",
           "dark-mode": "Dark mode {0}.", "notify": "Notification potten.", "type-text": "Type panniten.",
           "press-key": "{0} press panniten."}


def do(text: str) -> dict:
    r = route(text)
    if not r["action"] or (on_server() and r["action"] not in SERVER_OK):
        return {"handled": False}
    res = run(r["action"], r["args"])
    if not res.get("ok"):
        return {"handled": True, "ok": False, "say": f"Mudiyala: {res.get('error', 'error')}", **res}
    if "say" in res:
        say = res["say"]
    elif res.get("output") and r["action"] == "battery":
        say = battery_say(res["output"])
    elif res.get("output") and r["action"] == "wifi":
        say = wifi_say(res["output"])
    elif res.get("output") and r["action"] in ("apps", "clipboard"):
        say = res["output"][:300]
    else:
        args = [SPOKEN.get(a.lower(), a) if r["action"] in ("open-app", "quit-app") else a for a in (r["args"] or [""])]
        say = REPLIES.get(r["action"], "Done.").format(*args)
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
        "screen la enna irukku": ("read-screen", []), "read my screen": ("read-screen", []), "idha padi": ("read-screen", []),
        "screen padi": ("read-screen", []), "what's on my screen": ("read-screen", []),
        "screen recording app ku api design pannu": (None, None),
        "remind me in 10 minutes to call amma": ("remind", ["10m", "call amma"]),
        "20 nimisham la tea kudikka remind pannu": ("remind", ["20m", "tea kudikka"]),
        "2 mani neram kalichu client ku call pannanum nu nyabagapaduthu": ("remind", ["2h", "client ku call pannanum"]),
        "Remind me in ten minutes to call the client.": ("remind", ["10m", "call the client"]),
        "remind me in twenty five minutes to stretch": ("remind", ["25m", "stretch"]),
        "remind me in an hour to call a friend": ("remind", ["1h", "call a friend"]),
        "type pannu: Hello Dhina, invoice attached.": ("type-text", ["Hello Dhina, invoice attached."]),
        "Jarvis, type this: dhayalandhina355@gmail.com": ("type-text", ["dhayalandhina355@gmail.com"]),
        "Vanakkam sir nu type pannu": ("type-text", ["Vanakkam sir"]),
        "press enter": ("press-key", ["enter"]), "tab press pannu": ("press-key", ["tab"]), "cmd + s adi": ("press-key", ["cmd+s"]),
        "what type of database is best": (None, None),
        "volume thirty vai": ("volume", ["30"]), "Volume twenty five.": ("volume", ["25"]), "sound ah fifty vai": ("volume", ["50"]),
        "date enna": ("date", []), "innaiku date enna": ("date", []), "what's the date today": ("date", []),
        "innaiku enna kizhamai": ("date", []),
        "youtube la ilayaraja song podu": ("youtube-search", ["ilayaraja song"]),
        "play arijit singh songs on youtube": ("youtube-search", ["arijit singh songs"]),
        "lock pannu": ("lock", []), "wifi off pannu": ("wifi-power", ["off"]), "wifi on pannu": ("wifi-power", ["on"]),
        "wifi connect aagi irukka": ("wifi", []), "paattu podu youtube la": ("media", ["play"]),
        "Gurum tire.": ("open-app", ["gurum"]), "Savun kammi pannu.": ("volume", ["down"]),
        "Ippon manu enna?": ("time", []), "What time is it now?": ("time", []),
    }
    for text, (action, args) in cases.items():
        r = route(text)
        assert r["action"] == action, (text, r)
        if action:
            assert r["args"] == args, (text, r)
    assert build("open-app", ["chrome"]) == [["open", "-a", "Google Chrome"]]
    assert build("volume", ["150"])[0][-1].endswith("volume 100")
    assert build("open-url", ["github.com"]) == [["open", "https://github.com"]]
    assert build("type-text", ['say "hi"'])[0][-1] == 'tell application "System Events" to keystroke "say \\"hi\\""'
    assert build("press-key", ["enter"])[0][-1].endswith("key code 36")
    assert build("press-key", ["cmd+shift+t"])[0][-1].endswith('keystroke "t" using {command down, shift down}')
    assert do("tab press pannu")["say"] == "tab press panniten." and do("press enter")["would_run"][0][-1].endswith("key code 36")
    d = do("chrome open pannu")
    assert d["handled"] and d["say"] == "chrome open panniten." and d["dry_run"], d
    assert do("Gurum tire.")["say"] == "chrome open panniten."
    assert do("time enna")["say"].startswith("Ippo mani") and do("date enna")["say"].startswith("Innaiku ")
    assert battery_say("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t85%; discharging; 4:12 remaining present: true") \
        == "Battery 85%, battery la odudhu, innum 4 mani 12 nimisham varum."
    assert battery_say(" -InternalBattery-0 (id=1)\t9%; discharging; 0:25 remaining") == "Battery 9%, battery la odudhu, innum 25 nimisham varum."
    assert battery_say(" -InternalBattery-0 (id=1)\t80%; discharging; (no estimate)") == "Battery 80%, battery la odudhu."
    assert battery_say("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t62%; charging; 1:05 remaining") == "Battery 62%, charge aagudhu."
    assert battery_say("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining") == "Battery 100%, full charge."
    assert wifi_say("Current Wi-Fi Network: Dhina-5G") == "Wi-Fi: Dhina-5G la connect aagi irukku."
    assert do("youtube la ilayaraja song podu")["would_run"] == [["open", "https://www.youtube.com/results?search_query=ilayaraja+song"]]
    assert do("write a poem") == {"handled": False}
    rs = do("screen la enna irukku")
    assert rs["handled"] and rs["dry_run"] and rs["would_run"][0][0] == "screencapture", rs
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
