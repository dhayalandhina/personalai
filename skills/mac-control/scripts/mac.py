#!/usr/bin/env python3
"""mac.py — JARVIS-style Mac control with built-in macOS tools (no installs).

  open-app NAME | quit-app NAME | open-url URL | search-web QUERY
  open-any NAME                     any installed app (fuzzy: "word" -> Microsoft Word), else a website
  close-any NAME | open-file QUERY [KIND] | find-file QUERY [KIND]   (Spotlight, newest match)
  youtube-play QUERY | youtube-search QUERY | open-settings PANE | brightness up|down|max|min
  whatsapp-read                     latest WhatsApp messages (the app's own database, else its window, read)
  sysinfo disk|ram|ip | calc EXPR
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

import ast
import difflib
import json
import operator
import os
import platform
import re
import shutil
import sqlite3
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
MORE_SITES = {  # (open-any only: a phrase the generic "open X" route caught)
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com", "twitter": "https://x.com",
    "x": "https://x.com", "whatsapp web": "https://web.whatsapp.com", "google maps": "https://maps.google.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com", "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com", "hotstar": "https://www.hotstar.com", "jiohotstar": "https://www.hotstar.com",
    "prime video": "https://www.primevideo.com", "amazon prime": "https://www.primevideo.com", "swiggy": "https://www.swiggy.com",
    "zomato": "https://www.zomato.com", "irctc": "https://www.irctc.co.in", "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com", "reddit": "https://www.reddit.com", "wikipedia": "https://www.wikipedia.org",
    "gemini": "https://gemini.google.com", "perplexity": "https://www.perplexity.ai", "outlook": "https://outlook.live.com",
    "supabase": "https://supabase.com/dashboard", "vercel": "https://vercel.com/dashboard", "n8n": "https://app.n8n.cloud",
    "canva": "https://www.canva.com", "figma website": "https://www.figma.com", "notion website": "https://www.notion.so",
}
SETTINGS = {  # System Settings panes (macOS 13+) — Bluetooth cannot be switched without a third-party tool
    "bluetooth": "com.apple.BluetoothSettings", "wifi": "com.apple.wifi-settings-extension",
    "wi-fi": "com.apple.wifi-settings-extension", "sound": "com.apple.Sound-Settings.extension",
    "display": "com.apple.Displays-Settings.extension", "battery": "com.apple.Battery-Settings.extension",
    "notification": "com.apple.Notifications-Settings.extension", "privacy": "com.apple.settings.PrivacySecurity.extension",
    "keyboard": "com.apple.Keyboard-Settings.extension", "network": "com.apple.Network-Settings.extension",
}
APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
            "~/Applications", "~/Applications/Chrome Apps.localized"]
_APP_CACHE: tuple[float, dict] | None = None


def installed_apps() -> dict[str, str]:
    """Every app on this Mac: name -> bundle path (/Applications, its vendor folders, the system apps, ~/Applications
    with Chrome's web apps). Cached 5 min — a scan is a few ms."""
    global _APP_CACHE
    if _APP_CACHE is not None and time.time() - _APP_CACHE[0] < 300:
        return _APP_CACHE[1]
    names: dict[str, str] = {}
    for d in APP_DIRS:
        try:
            entries = list(os.scandir(os.path.expanduser(d)))
        except OSError:
            continue
        for e in entries:
            if e.name.endswith(".app"):
                names.setdefault(e.name[:-4], e.path)
            elif d == "/Applications" and not e.name.startswith(".") and e.is_dir():
                try:  # vendor folders: /Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app
                    for e2 in os.scandir(e.path):
                        if e2.name.endswith(".app"):
                            names.setdefault(e2.name[:-4], e2.path)
                except OSError:
                    pass
    _APP_CACHE = (time.time(), names)
    return names


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


TARGET_TAIL = re.compile(r"(?:\s+(?:app|application|apps|ah|a|ai|ya|va|da|di|please|pannu|panni|pannunga|panniru|now|"
                         r"website|site|web\s*site|page|web))+$")


def clean_target(name: str) -> str:
    q = re.sub(r"^(?:the|my|en|enoda|namma)\s+", "", name.strip().lower().strip(" .'-"))
    return TARGET_TAIL.sub("", q).strip(" .'-")


def match_app(q: str, apps) -> str | None:
    """The installed app a spoken name means: exact, prefix ("android" -> Android Studio), every word of it
    ("word" -> Microsoft Word, "photoshop" -> Adobe Photoshop 2025), else a close spelling (Whisper's
    "spotifai"); None below 0.78."""
    nq = _norm(q)
    if len(nq) < 2:
        return None
    qt = re.findall(r"[a-z0-9]+", q.lower())
    best, best_s = None, 0.0
    for app in apps:
        na = _norm(app)
        if not na:
            continue
        toks = re.findall(r"[a-z0-9]+", app.lower())
        if na == nq:
            sc = 1.0
        elif len(nq) >= 3 and na.startswith(nq):
            sc = 0.92 - 0.002 * (len(na) - len(nq))
        elif qt and all(any(t == x or (len(t) >= 4 and x.startswith(t)) for x in toks) for t in qt):
            sc = 0.88 - 0.002 * len(na)
        else:
            sc = difflib.SequenceMatcher(None, nq, na).ratio()
            for x in toks:
                if len(x) >= 4:
                    sc = max(sc, difflib.SequenceMatcher(None, nq, x).ratio() - 0.04)
        if sc > best_s:
            best, best_s = app, sc
    return best if best_s >= 0.78 else None


def ducky_url(query: str) -> str:
    """DuckDuckGo's first result for a name ("hotstar" -> the official site): for a site dd has no address for."""
    return "https://duckduckgo.com/?q=" + urllib.parse.quote_plus("!ducky " + query)


DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:com|in|org|net|io|ai|co|dev|app|me|so|gov|edu|tv)(?:/\S*)?$")


NOT_A_SITE = re.compile(r"^(?:the|your|my|his|her|their|our|a|an|this|that|it|them|him|me|us|up|down|fire|hell|door|gate|"
                        r"eyes?|everything|all|now|here|there|plan|out|in|on|off|over|away|yourself|myself)\b")


def resolve_open(name: str, web_ok: bool = True) -> dict:
    """What "open <name>" means: {"kind": "app"|"site"|"web"|"none", "target": ..., "label": ...}. "web" (the name's
    first search result) only for a name that looks like one ("hotstar", "zerodha website") — film lines like "Open
    the gate!" / "Open fire!" mean nothing here (kind "none": the model decides)."""
    q = clean_target(name)
    want_site = bool(re.search(r"\b(?:website|site|web\s*site|page|dot com)\b|\.\w{2,4}$", name.lower()))
    q = re.sub(r"\s+dot\s+(com|in|org|net|io|ai|co|dev|app)\b", r".\1", q)
    if DOMAIN_RE.match(q.replace(" ", "")):
        return {"kind": "site", "target": "https://" + q.replace(" ", ""), "label": q.replace(" ", "")}
    if q in SITES or q in MORE_SITES:
        return {"kind": "site", "target": SITES.get(q) or MORE_SITES[q], "label": q}
    if not want_site:
        apps = installed_apps()
        real = APPS.get(q)
        if real and (real in apps or not apps):
            return {"kind": "app", "target": real, "label": SPOKEN.get(q, q)}
        hit = match_app(q, apps)
        if hit:
            return {"kind": "app", "target": hit, "label": hit}
    if not q or not web_ok or (not want_site and (NOT_A_SITE.match(q) or len(q.split()) > 3)):
        return {"kind": "none", "target": "", "label": q}
    return {"kind": "web", "target": ducky_url(q), "label": q}


KIND_EXT = {"pdf": (".pdf",), "word": (".doc", ".docx", ".pages", ".rtf"), "doc": (".doc", ".docx", ".pages", ".rtf"),
            "docx": (".docx", ".doc"), "excel": (".xls", ".xlsx", ".csv", ".numbers"), "sheet": (".xls", ".xlsx", ".csv", ".numbers"),
            "ppt": (".ppt", ".pptx", ".key"), "presentation": (".ppt", ".pptx", ".key"),
            "photo": (".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp"), "image": (".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp"),
            "video": (".mp4", ".mov", ".mkv", ".avi", ".webm"), "song": (".mp3", ".m4a", ".wav", ".aac", ".flac")}
FILE_SKIP = ("/Library/", "/.", "/node_modules/", "/vendor/", "/__pycache__/", "/.git/", ".app/")


def find_files(query: str, kind: str = "", limit: int = 5) -> list[str]:
    """Files in the home folder whose name has every word of `query` (Spotlight), newest first."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in ("the", "my", "a", "file", "files", "en", "oru")]
    if not words or not shutil.which("mdfind"):
        return []
    r = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), "-name", max(words, key=len)],
                       capture_output=True, text=True, timeout=15)
    exts = KIND_EXT.get(kind, ())
    hits = []
    for path in r.stdout.splitlines():
        base = os.path.basename(path).lower()
        if not path or any(x in path for x in FILE_SKIP) or not all(w in base for w in words):
            continue
        if exts and not base.endswith(exts):
            continue
        if not exts and os.path.isdir(path):
            continue
        hits.append(path)
    hits.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)
    return hits[:limit]


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
    if action == "youtube-play":  # the top video itself (DuckDuckGo's first YouTube hit) — it starts playing
        return [["open", ducky_url(" ".join(args).strip() + " site:youtube.com")]]
    if action == "open-settings":
        return [["open", "x-apple.systempreferences:" + SETTINGS.get(a0.lower(), a0)]]
    if action == "brightness":  # the keyboard's brightness keys (16 steps from dark to full)
        n, code = {"up": (2, 144), "down": (2, 145), "max": (16, 144), "min": (14, 145)}.get(a0, (2, 144))
        return [osa(f'tell application "System Events"\n repeat {n} times\n  key code {code}\n  delay 0.03\n'
                    f' end repeat\nend tell')]
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


SECRET_KEY_RE = re.compile(r"(?:_API_KEY|_TOKEN|_SECRET)$")


def skill_script(skill: str, script: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not SECRET_KEY_RE.search(k)}  # (mac.py may run inside dd itself)
    return subprocess.run([sys.executable, str(SKILLS / skill / "scripts" / script), *args], capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL, env={**env, "DD_AGENT": "1"})


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
  const res = req.results, lines = [], boxes = argv.length > 1 && argv[1] === 'boxes';
  for (let i = 0; i < res.count; i++) {
    const o = res.objectAtIndex(i), c = o.topCandidatesWithMaximumCandidates(1);
    if (!c.count) continue;
    const s = ObjC.unwrap(c.objectAtIndex(0).string);
    if (!boxes) { lines.push(s); continue; }
    let b = '0\t0\t0\t0';
    try { const r = o.boundingBox; b = [r.origin.x, r.origin.y, r.size.width, r.size.height].map(v => Number(v).toFixed(3)).join('\t'); } catch (e) {}
    lines.push(b + '\t' + s);
  }
  return lines.join('\n');
}
"""


def ocr(image: str, boxes: bool = False) -> tuple[bool, str]:
    """Text on an image: Apple Vision through built-in JXA (no install), else tesseract if present.
    boxes=True: one "x y w h text" line per text line (Vision's 0-1 coordinates, y from the bottom)."""
    js = Path(tempfile.gettempdir()) / "dd_ocr.js"
    js.write_text(OCR_JXA, encoding="utf-8")
    r = subprocess.run(["osascript", "-l", "JavaScript", str(js), image] + (["boxes"] if boxes else []),
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out and not out.startswith("ERR:"):
        return True, out
    if boxes:
        return False, (out or r.stderr.strip() or "OCR failed")[:300]
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


def _say_open(r: dict) -> str:
    return f"{r['label']} open panniten." if r["kind"] != "web" else f"{r['label']} web la open panniten."


def open_any(name: str, web_ok: bool = True) -> dict:
    """open-app / open-any: the installed app the name means, else the site (known address, a domain, or the
    name's first web result). An alias whose app is not installed (Spotify) opens its website instead."""
    r = resolve_open(name, web_ok=web_ok)
    if r["kind"] == "none":
        return {"ok": False, "action": "open-any", "unresolved": True, "error": f"'{r['label']}' nu app / site illa"}
    cmd = ["open", "-a", installed_apps().get(r["target"], r["target"])] if r["kind"] == "app" else ["open", r["target"]]
    if DRY:
        return {"ok": True, "action": "open-any", "dry_run": True, "would_run": [cmd], "resolved": r, "say": _say_open(r)}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if p.returncode != 0 and r["kind"] == "app":
        if subprocess.run(["open", ducky_url(r["label"])], capture_output=True, text=True, timeout=30).returncode == 0:
            return {"ok": True, "action": "open-any", "say": f"{r['label']} app illa — web la open panniten."}
    if p.returncode != 0:
        return {"ok": False, "action": "open-any", "error": (p.stderr or p.stdout).strip()[:200]}
    return {"ok": True, "action": "open-any", "resolved": r, "say": _say_open(r)}


def close_any(name: str) -> dict:
    """quit-app / close-any: quits the app if it is running (never launches it just to quit it)."""
    q = clean_target(name)
    apps = installed_apps()
    real = APPS.get(q)
    hit = real if real and (real in apps or not apps) else match_app(q, apps)
    if not hit:
        return {"ok": False, "action": "close-any", "unresolved": True, "error": f"'{q}' nu app illa"}
    label = SPOKEN.get(q, q) if real else hit
    script = (f'if application "{hit}" is running then\n tell application "{hit}" to quit\n return "closed"\n'
              f'else\n return "not running"\nend if')
    if DRY:
        return {"ok": True, "action": "close-any", "dry_run": True, "would_run": [osa(script)], "say": f"{label} close panniten."}
    p = subprocess.run(osa(script), capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        return {"ok": False, "action": "close-any", "error": (p.stderr or p.stdout).strip()[:200]}
    if "not running" in p.stdout:
        return {"ok": True, "action": "close-any", "say": f"{label} already open la illa."}
    return {"ok": True, "action": "close-any", "say": f"{label} close panniten."}


def file_action(action: str, query: str, kind: str = "") -> dict:
    """open-file / find-file: the newest file in the home folder whose name has every word of the query."""
    if DRY:
        return {"ok": True, "action": action, "dry_run": True, "would_run": [["mdfind", "-onlyin", "~", "-name", query]],
                "say": f"'{query}' file thedren."}
    hits = find_files(query, kind)
    if not hits:
        return {"ok": True, "action": action, "say": f"'{query}' nu {kind or ''} file kidaikkala.".replace("  ", " ")}
    top, where = hits[0], os.path.dirname(hits[0]).replace(os.path.expanduser("~"), "~")
    if action == "find-file":
        return {"ok": True, "action": action, "files": hits, "say": f"{os.path.basename(top)} — {where} la irukku."}
    p = subprocess.run(["open", top], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        return {"ok": False, "action": action, "error": (p.stderr or p.stdout).strip()[:200]}
    return {"ok": True, "action": action, "files": hits, "say": f"{os.path.basename(top)} open panniten."}


WA_DIR = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
APPLE_EPOCH = 978307200  # Core Data dates count seconds from 2001-01-01


def _when(ts) -> str:
    try:
        t = time.localtime(float(ts) + APPLE_EPOCH)
    except (TypeError, ValueError, OverflowError):
        return ""
    now = time.localtime()
    if t[:3] == now[:3]:
        return time.strftime("%I:%M %p", t).lstrip("0")
    if time.mktime(now) - time.mktime(t) < 2 * 86400:
        return "nethu " + time.strftime("%I:%M %p", t).lstrip("0")
    return time.strftime("%d %b", t).lstrip("0")


def whatsapp_db(limit: int = 3) -> dict:
    """The latest messages from WhatsApp's own database (the Mac app keeps it as ChatStorage.sqlite in its group
    container) — read from a private copy, never opened in place. macOS keeps other apps' data private: without Full
    Disk Access for dd this is {"ok": False, "why": "denied"}."""
    src = Path(os.path.expanduser(WA_DIR)) / "ChatStorage.sqlite"
    tmp = Path(tempfile.mkdtemp(prefix="dd-wa-"))
    try:
        try:
            shutil.copyfile(src, tmp / "c.sqlite")
            for suf in ("-wal", "-shm"):
                if Path(str(src) + suf).exists():
                    shutil.copyfile(str(src) + suf, str(tmp / "c.sqlite") + suf)
        except FileNotFoundError:
            return {"ok": False, "why": "missing"}
        except OSError as e:
            return {"ok": False, "why": "denied", "error": str(e)[:160]}
        try:
            con = sqlite3.connect(str(tmp / "c.sqlite"))
            cols = {r[1] for r in con.execute("PRAGMA table_info(ZWAMESSAGE)")}
            scols = {r[1] for r in con.execute("PRAGMA table_info(ZWACHATSESSION)")}
            if not {"ZTEXT", "ZMESSAGEDATE", "ZCHATSESSION", "ZISFROMME"} <= cols or "ZPARTNERNAME" not in scols:
                return {"ok": False, "why": "schema"}
            push = "m.ZPUSHNAME" if "ZPUSHNAME" in cols else "NULL"
            jid = "s.ZCONTACTJID" if "ZCONTACTJID" in scols else "''"
            rows = con.execute(f"SELECT s.ZPARTNERNAME, m.ZTEXT, m.ZMESSAGEDATE, m.ZISFROMME, {push}, {jid} "
                               "FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
                               "WHERE m.ZMESSAGEDATE IS NOT NULL ORDER BY m.ZMESSAGEDATE DESC LIMIT 80").fetchall()
            unread = []
            if "ZUNREADCOUNT" in scols:
                order = "ZLASTMESSAGEDATE DESC" if "ZLASTMESSAGEDATE" in scols else "Z_PK DESC"
                unread = con.execute(f"SELECT ZPARTNERNAME, ZUNREADCOUNT FROM ZWACHATSESSION WHERE ZUNREADCOUNT > 0 "
                                     f"ORDER BY {order} LIMIT 6").fetchall()
            con.close()
        except sqlite3.Error as e:
            return {"ok": False, "why": "sqlite", "error": str(e)[:160]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    latest = []
    for name, text, when, mine, pushname, cjid in rows:
        if mine:
            continue  # (his own messages)
        who = (name or "").strip() or "Oruthar"
        if str(cjid or "").endswith("@g.us") and pushname:
            who = f"{str(pushname).strip()} ({who})"
        body = re.sub(r"\s+", " ", (text or "").strip())[:160] or "(photo / file)"
        latest.append({"from": who, "text": body, "when": _when(when)})
        if len(latest) >= limit:
            break
    return {"ok": True, "latest": latest, "unread": [{"chat": (n or "?").strip(), "count": int(c)} for n, c in unread]}


WA_UI = re.compile(r"(?i)^(?:chats?|search|search or start (?:a )?new chat|all|unread|favou?rites|groups|archived|whatsapp|"
                   r"(?:ask )?meta ai|communities|status|calls|settings|new chat|channels|lists?|contacts|drafts?)$")
TIME_LIKE = re.compile(r"(?i)^(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?|yesterday|today|nethu|monday|tuesday|wednesday|thursday|friday|"
                       r"saturday|sunday|\d{1,2}/\d{1,2}/\d{2,4}|\d+)$")


def whatsapp_screen() -> dict:
    """No database access: bring WhatsApp up and read its chat list (left column of its window) off the screen."""
    if DRY:
        return {"ok": True, "action": "whatsapp-read", "dry_run": True, "would_run": [["open", "-a", "WhatsApp"], ["screencapture", "-x"]]}
    if subprocess.run(["open", "-a", "WhatsApp"], capture_output=True, text=True, timeout=20).returncode != 0:
        return {"ok": False, "action": "whatsapp-read", "error": "WhatsApp app indha Mac la illa"}
    time.sleep(2.0)
    b = subprocess.run(osa('tell application "System Events" to tell process "WhatsApp" to get {position, size} of front window'),
                       capture_output=True, text=True, timeout=10)
    nums = [int(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", b.stdout)] if b.returncode == 0 else []
    shot = str(Path(tempfile.gettempdir()) / f"dd_wa_{int(time.time())}.png")
    region = ["-R", ",".join(map(str, nums[:4]))] if len(nums) >= 4 and nums[2] > 200 and nums[3] > 200 else []
    r = subprocess.run(["screencapture", "-x", *region, shot], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not Path(shot).exists():
        return {"ok": False, "action": "whatsapp-read", "error": "screenshot aagala (Screen Recording la dd ku ON pannu)"}
    ok, out = ocr(shot, boxes=True)
    try:
        Path(shot).unlink()
    except OSError:
        pass
    if not ok:
        return {"ok": False, "action": "whatsapp-read", "error": out}
    items = []
    for ln in out.splitlines():
        parts = ln.split("\t", 4)
        if len(parts) == 5:
            try:
                items.append((float(parts[0]), float(parts[1]), parts[4].strip()))
            except ValueError:
                pass
    boxed = any(x or y for x, y, _ in items)
    left = [(y, t) for x, y, t in items if (x < 0.45 if boxed else True)]
    left.sort(key=lambda v: -v[0])  # top of the window first
    lines = [t for _, t in left if t and not WA_UI.match(t) and not TIME_LIKE.match(t) and len(t) > 1][:8]
    if not lines:
        return {"ok": True, "action": "whatsapp-read", "say": "WhatsApp open panniten — screen la message padikka mudiyala."}
    return {"ok": True, "action": "whatsapp-read", "text": "\n".join(lines),
            "say": "WhatsApp la mela irukkuradhu: " + "; ".join(lines[:6])[:260] + "."}


def wa_say(db: dict) -> dict:
    latest, unread = db.get("latest") or [], db.get("unread") or []
    if not latest:
        return {"ok": True, "action": "whatsapp-read", "say": "WhatsApp la pudhu message onnum illa.", **db}
    m = latest[0]
    say = f"Kadasi message {m['from']} kitta irundhu" + (f", {m['when']}" if m["when"] else "") + f": {m['text']}."
    if len(latest) > 1:
        say += " Adhukku munnadi: " + "; ".join(f"{x['from']}: {x['text'][:60]}" for x in latest[1:3]) + "."
    if unread:
        say += " Unread: " + ", ".join(f"{u['chat']} {u['count']}" for u in unread[:4]) + "."
    return {"ok": True, "action": "whatsapp-read", "say": say, **db}


def whatsapp_read() -> dict:
    """'WhatsApp la last message enna': the app's database when dd may read it (exact, newest first), else the
    chat list off the screen. (The database read runs in its own process with a time limit.)"""
    db = {"ok": False, "why": "dry"}
    if not DRY:
        try:
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "whatsapp-db"], capture_output=True,
                               text=True, timeout=15, stdin=subprocess.DEVNULL)
            db = json.loads(p.stdout or "{}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            db = {"ok": False, "why": type(e).__name__}
    if db.get("ok"):
        return wa_say(db)
    res = whatsapp_screen()
    if res.get("ok") and db.get("why") == "denied":
        res["say"] = res.get("say", "") + " (Full Disk Access la dd ku ON pannina exact ah padippen.)"
    return res


def sysinfo(what: str) -> dict:
    if what == "disk":
        root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
        st = os.statvfs(root)
        free, total = st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9
        return {"ok": True, "action": "sysinfo", "say": f"Disk la {free:.0f} GB free irukku ({total:.0f} GB la)."}
    cmds = {"ram": [["memory_pressure", "-Q"], ["sysctl", "-n", "vm.swapusage"]], "ip": [["ipconfig", "getifaddr", "en0"]]}
    if what not in cmds:
        raise ValueError(f"sysinfo: disk|ram|ip, not '{what}'")
    if DRY:
        return {"ok": True, "action": "sysinfo", "dry_run": True, "would_run": cmds[what]}
    outs = [subprocess.run(c, capture_output=True, text=True, timeout=15).stdout.strip() for c in cmds[what]]
    if what == "ip":
        return {"ok": True, "action": "sysinfo", "say": f"IP address {outs[0]}." if outs[0] else "Wi-Fi IP illa (connect aagala)."}
    pct = re.search(r"(\d+)%", outs[0])
    used = re.search(r"used = ([\d.]+)([MG])", outs[1])
    say = f"RAM {pct.group(1)}% free." if pct else "RAM info kidaikkala."
    if used and float(used.group(1)) > 0:
        say += f" Swap {float(used.group(1)):.0f}{used.group(2)}B use aagudhu."
    return {"ok": True, "action": "sysinfo", "say": say}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _ev(n):
    if isinstance(n, ast.Expression):
        return _ev(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        a, b = _ev(n.left), _ev(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 64:
            raise ValueError("too big")
        return _OPS[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_ev(n.operand))
    raise ValueError("not arithmetic")


CALC_WORDS = [(r"\bmultiplied\s+by\b|\btimes\b|\binto\b|[×x](?=\s*\d)", "*"), (r"\bdivided\s+by\b|\bby\b(?=\s*\d)|÷", "/"),
              (r"\bplus\b|\band\b(?=\s*\d)", "+"), (r"\bminus\b", "-"), (r"\s*(?:percent|%)\s+of\b", " * 0.01 *"),
              (r"\bsquared\b", "** 2"), (r"\bto the power of\b|\bpower\b|\^", "**")]


def calc_expr(text: str) -> str | None:
    """'what is 25 times 4' / '1500 into 12 evlo' / '15 percent of 2400' -> a plain arithmetic expression, or None."""
    e = text.lower().strip().rstrip("?.! ")
    e = re.sub(r"^(?:(?:hey|ok|jarvis|dd)[,\s]+)*(?:what(?:'s| is)|calculate|calc|kanakku\s+(?:pannu|podu))\s+", "", e)
    e = re.sub(r"(?:\s*(?:evlo|evvalavu|enna|equals|equal to|aagum|varum|=|sollu))+$", "", e).strip()
    for pat, sym in CALC_WORDS:
        e = re.sub(pat, sym, e)
    e = e.replace(",", "").strip()
    if not re.fullmatch(r"[\d\s.+\-*/()%]+", e) or not re.search(r"\d\s*(?:\*\*|[-+*/%])\s*[\d(]", e):
        return None
    if re.fullmatch(r"\d{1,2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{2,4}", e):  # a date, not a sum
        return None
    return e


def calc_action(expr: str) -> dict:
    try:
        v = _ev(ast.parse(expr, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as e:
        return {"ok": False, "action": "calc", "error": f"kanakku puriyala ({type(e).__name__})"}
    out = str(int(v)) if float(v).is_integer() and abs(v) < 1e15 else f"{v:.4f}".rstrip("0").rstrip(".")
    return {"ok": True, "action": "calc", "say": f"Answer {out}.", "value": v}


def run(action: str, args: list[str]) -> dict:
    a0 = args[0] if args else ""
    if action in ("open-any", "open-app"):
        return open_any(" ".join(args))
    if action == "switch-app":  # "switch to X": an app only, never a web search
        return open_any(" ".join(args), web_ok=False)
    if action in ("close-any", "quit-app"):
        return close_any(" ".join(args))
    if action in ("open-file", "find-file"):
        return file_action(action, a0, args[1] if len(args) > 1 else "")
    if action == "whatsapp-read":
        return whatsapp_read()
    if action == "sysinfo":
        return sysinfo(a0)
    if action == "calc":
        return calc_action(" ".join(args))
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
GOPEN = r"(?:open|thira|thera|tira|tire|tera|thirakk|launch)\w*"  # (generic "open X": not run/start — "run the tests")
POLITE = r"(?:\s+(?:pannu|panni|pannunga|panniru|please|da|di|now|seekiram))*"
FILE_KINDS = r"(file|pdf|document|doc|docx|excel|sheet|ppt|presentation|photo|image|video)s?"
SEND_RE = re.compile(r"\b(?:send|anupp\w*|reply|type|forward)\b")
TARGET_STOP = {"and", "then", "apram", "aprom", "with", "it", "this", "that", "idha", "idhu", "adha", "adhu", "enna",
               "what", "how", "why", "new", "file", "folder", "door", "code la", "the door"}
PLAY_GENERIC = {"music", "song", "songs", "the music", "the song", "paattu", "pattu", "it", "something", "anything",
                "some music", "some songs", "a song", "next", "next song", "previous song", "adutha paattu"}
SONG_SKIP = {"adutha", "next", "previous", "prev", "munnadi", "oru", "indha", "intha", "the", "a", "some", "nalla", "play"}
ROUTES: list[tuple[str, str, object]] = [
    (rf"\b{DUR}\s+(?:ku\s+|for\s+)?(?:oru\s+)?timer\b|\btimer\s+(?:for\s+|of\s+)?{DUR}", "remind", "timer"),
    (rf"\bremind\s+me\s+in\s+{DUR}\s+(?:to\s+)?(.+)$", "remind", "remind_en"),
    (rf"\b{DUR}\s+(?:la|le|kalichu|kazhichu|aprom|apram)\s+(.+?)\s+(?:nu\s+|ah\s+|a\s+)?(?:remind|nyabagapaduthu|sollu)\w*", "remind", "remind_ta"),
    (r"\bwhats\s*app\b.{0,40}\b(?:message|messages|msg|msgs|chat|chats|text)\b"
     r"|\b(?:message|messages|msg|msgs|chat|chats)\b.{0,40}\bwhats\s*app\b", "whatsapp-read", "wa"),
    (rf"\b(?:{OPEN})\s+({FOLDER_WORDS})\s+folder\b|\b({FOLDER_WORDS})\s+folder\s+(?:ah\s+|a\s+)?(?:{OPEN})|\b({FOLDER_WORDS})\s+folder\b", "open-folder", "grp"),
    (r"\b(bluetooth|wi-?fi|sound|display|battery|notification|privacy|keyboard|network)s?\s+settings?\b"
     r"|\bsettings?\s+(?:la\s+|il\s+)?(bluetooth|wi-?fi|sound|display|battery|notification|privacy|keyboard|network)\b"
     r"|\b(bluetooth)\b", "open-settings", "grp"),
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
    (rf"\b(?:youtube|yt)\s+(?:la|le|il|in|on)\s+(.+?)\s+(?:podu|pottu|play|paadu|vai|vei)\w*{POLITE}\s*$"
     r"|\bplay\s+(.+?)\s+(?:on|in)\s+youtube\b", "youtube-play", "grp"),
    (rf"\b(?:youtube|yt)\s+(?:la|le|il|in|on)\s+(.+?)\s+(?:search|thedu|kaattu|open)\w*{POLITE}\s*$"
     r"|\bsearch\s+(.+?)\s+(?:on|in)\s+youtube\b|\byoutube\s+(?:la\s+|il\s+)?search\s+(.+)$", "youtube-search", "grp"),
    (r"\b(?:next|adutha)\s+(?:song|track|paattu|pattu)\b", "media", ["next"]),
    (r"\b(?:previous|prev|munnadi)\s+(?:song|track|paattu|pattu)\b", "media", ["previous"]),
    (r"^(.+?)\s+(?:song|songs|paattu|pattu|padal|paadal)\s+(?:podu|pottu|play|paadu|vai|vei)\w*$", "youtube-play", "song"),
    (r"^play\s+(.+?)$", "youtube-play", "play"),
    (r"\b(?:pause|stop)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:pause|stop|niruthu)\w*", "media", ["pause"]),
    (r"\b(?:play|podu)\s+(?:song|music|paattu|pattu)\b|\b(?:song|music|paattu|pattu)\s+(?:play|podu)\w*", "media", ["play"]),
    (r"\b(?:read|padi|padikka|paaru|paar|sollu)\b.{0,12}\bscreen\b|\bscreen\b.{0,16}\b(?:padi|padikka|read|enna irukku|enna iruku|la enna)\b"
     r"|\bwhat(?:'s| is) on (?:my |the )?screen\b|\bidha padi\b|\bidhai padi\b", "read-screen", []),
    (r"\bscreen\s*shot\b|\bscreenshot\b", "screenshot", []),
    (r"\block\s+(?:the\s+)?(?:screen|mac|laptop|system)\b|\b(?:screen|mac|laptop)\s+lock\b|^lock(?:\s+(?:pannu|panniru|it))?$",
     "lock", []),
    (r"\bbrightness\s+(?:ah\s+|a\s+)?(up|down|increase|decrease|jaasthi|jasthi|athigam|kammi|korai|kurai|full|max|maximum|low|min|minimum)\w*"
     r"|\b(?:screen\s+)?(?:brightness|velicham)\s+(?:ah\s+|a\s+)?(jaasthi|kammi|up|down)\w*", "brightness", "bright"),
    (r"\b(?:storage|disk\s*space|disk|hard\s*disk|ssd|free\s+space|space)\b.{0,20}\b(?:evlo|evvalavu|enna|how much|free|left|irukku|iruku|balance)\b"
     r"|\bhow much (?:storage|disk space|space)\b", "sysinfo", ["disk"]),
    (r"\bram\b.{0,20}\b(?:evlo|evvalavu|how much|free|usage|irukku|iruku)\b|\bmemory\s+(?:evlo|evvalavu|free|usage)\b"
     r"|\bhow much (?:ram|memory)\b|\b(?:free|ram)\s+memory\b", "sysinfo", ["ram"]),  # ("memory la enna irukku": dd's memory)
    (r"\bip\s*address\b|\bmy\s+ip\b", "sysinfo", ["ip"]),
    (r"\bclipboard\b.{0,20}\b(?:enna|what|read|padi|sollu|irukku|iruku)\b|\bwhat(?:'s| is) (?:in|on) (?:the |my )?clipboard\b", "clipboard", ["get"]),
    (r"\b(?:which|what|enna|evvalavu)\s+apps?\b.{0,20}\b(?:open|running|odudhu|odudu)\b|\brunning\s+apps\b"
     r"|\bopen\s+(?:la\s+)?(?:irukka|irukkura)\s+apps?\b", "apps", []),
    (r"\bbattery\b|\bcharge\s+(?:evlo|enna|how much)\b", "battery", []),
    (r"\bwi-?fi\s+(on|off)\b|\bwi-?fi\s+(?:ah\s+)?(?:anachu|anaichu|niruthu|stop)\w*", "wifi-power", "wifi"),
    (r"\bwi-?fi\b", "wifi", []),
    (r"\b(?:time|mani|manu|maani)\s+(?:enna|ennai|evlo|aachu|what)\b|\bwhat(?:'s| is) the time\b|\bwhat time is it\b"
     r"|\bippo\w*\s+(?:mani|manu)\b", "time", []),
    (r"\b(?:date|thethi|thedhi|tarikh)\s+(?:enna|what|sollu)\b|\bwhat(?:'s| is)\s+(?:the\s+|today'?s\s+)?date\b"
     r"|\b(?:innaiku|today|inniki)\s+(?:enna\s+)?(?:date|thethi|kizhamai|day)\b|^(?:date|today'?s date)$", "date", []),
    (r"\bdark\s*mode\s+(on|off)\b", "dark-mode", "grp"),
    (r"\bdark\s*mode\b", "dark-mode", ["toggle"]),
    (rf"^(?:find|search|locate|thedu)\s+(?:the\s+|my\s+|en\s+)?(.{{2,40}}?)\s+{FILE_KINDS}$"
     rf"|^(.{{2,40}}?)\s+{FILE_KINDS}\s+(?:enga|where)\b.*$"
     rf"|^(.{{2,40}}?)\s+{FILE_KINDS}\s+(?:ah\s+|a\s+)?(?:thedu|find|kandupidi)\w*{POLITE}$", "find-file", "file"),
    (rf"^(?:{GOPEN})\s+(?:the\s+|my\s+|en\s+)?(.{{2,40}}?)\s+{FILE_KINDS}{POLITE}$"
     rf"|^(.{{2,40}}?)\s+{FILE_KINDS}\s+(?:ah\s+|a\s+)?(?:{GOPEN}){POLITE}$", "open-file", "file"),
    (r"\b(?:google|search)\s+(?:pannu\s+|for\s+)?(.+)$", "search-web", "rest"),
    (rf"^(?:{GOPEN})\s+(.{{2,48}}?){POLITE}$|^(.{{2,48}}?)\s+(?:{GOPEN}){POLITE}$", "open-any", "any"),
    (r"^(?:switch|go)\s+to\s+(.{2,48}?)$|^(.{2,48}?)\s+(?:ku|kku)\s+(?:po|poo|switch)\w*$", "switch-app", "any"),
    (rf"^(?:{CLOSE})\s+(.{{2,40}}?){POLITE}$|^(.{{2,40}}?)\s+(?:{CLOSE}){POLITE}$", "close-any", "any"),
]
KEY_PHRASES = [  # spoken window / tab / edit commands -> keys (whole phrase only; an LLM-sized request never matches)
    (r"^(?:open\s+)?(?:a\s+)?(?:new|pudhu|puthu)\s+tab(?:\s+(?:open|thira))?" + POLITE + "$", "cmd+t"),
    (r"^(?:close|moodu)\s+(?:this\s+|the\s+)?tab" + POLITE + r"$|^(?:indha\s+)?tab\s+(?:ah\s+|a\s+)?(?:close|moodu)\w*" + POLITE + "$", "cmd+w"),
    (r"^(?:open\s+)?(?:a\s+)?new\s+window" + POLITE + "$", "cmd+n"),
    (r"^(?:close|moodu)\s+(?:this\s+|the\s+)?window" + POLITE + r"$|^(?:indha\s+)?window\s+(?:ah\s+|a\s+)?(?:close|moodu)\w*" + POLITE + "$", "cmd+w"),
    (r"^(?:minimi[sz]e|window\s+minimi[sz]e)(?:\s+(?:it|this|the window|window))?" + POLITE + "$", "cmd+m"),
    (r"^(?:full\s*screen|make it full\s*screen)" + POLITE + r"$|^full\s*screen\s+(?:ah\s+)?(?:podu|aakku|vai)" + POLITE + "$", "ctrl+cmd+f"),
    (r"^(?:refresh|reload)(?:\s+(?:the\s+)?(?:page|this))?" + POLITE + r"$|^page\s+(?:ah\s+)?(?:refresh|reload)\w*" + POLITE + "$", "cmd+r"),
    (r"^(?:go\s+)?back(?:\s+po)?" + POLITE + r"$|^pinnadi\s+po" + POLITE + "$", "cmd+["),
    (r"^save(?:\s+(?:it|this|the file|file))?" + POLITE + "$", "cmd+s"),
    (r"^copy(?:\s+(?:it|this))?" + POLITE + "$", "cmd+c"),
    (r"^paste(?:\s+(?:it|this|here))?" + POLITE + "$", "cmd+v"),
    (r"^undo(?:\s+(?:it|that|this))?" + POLITE + "$", "cmd+z"),
    (r"^redo" + POLITE + "$", "cmd+shift+z"),
    (r"^select\s+all" + POLITE + r"$|^ellam\s+select" + POLITE + "$", "cmd+a"),
    (r"^scroll\s+down" + POLITE + r"$|^(?:keezha|kizha|keela|kela)\s+(?:po|scroll)\w*" + POLITE + "$", "pagedown"),
    (r"^scroll\s+up" + POLITE + r"$|^(?:mela|mele)\s+(?:po|scroll)\w*" + POLITE + "$", "pageup"),
    (r"^(?:switch|next)\s+app" + POLITE + "$", "cmd+tab"),
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
    expr = calc_expr(text)
    if expr:
        return {"action": "calc", "args": [expr]}
    t = re.sub(r"[^\w\s\-.']", " ", text.lower()).strip()
    t = re.sub(r"\s+", " ", t).strip(" .'-")
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis|dd)\s+", "", t)  # (a wake word left on the command)
    t = re.sub(r"\s+dot\s+(com|in|org|net|io|ai|co|dev|app)\b", r".\1", t)
    t = _NUM_RE.sub(lambda m: str(_NUM_WORDS[m.group(1)]), t)
    t = _VOL_NUM_RE.sub(lambda m: m.group(1) + str(_NUM_WORDS.get(m.group(2), 100)), t)  # "volume thirty" -> 30
    for pat, key in KEY_PHRASES:
        if re.match(pat, t):
            return {"action": "press-key", "args": [key]}
    for pat, action, kind in ROUTES:
        m = re.search(pat, t)
        if not m:
            continue
        groups = [g for g in m.groups() if g] if m.groups() else []
        if kind == "wa":
            if SEND_RE.search(t):
                continue  # (sending is not a fast action)
            return {"action": action, "args": []}
        if kind == "timer":
            n, unit = groups[0], groups[1]
            u = "m" if unit.startswith(("m", "n")) and "mani" not in unit else "h"
            return {"action": "remind", "args": [f"{n}{u}", "Timer mudinjiduchu"]}
        if kind == "bright":
            w = groups[0] if groups else "up"
            w = ("up" if w.startswith(("up", "incr", "jaas", "jas", "ath")) else "down" if w.startswith(("down", "decr", "kam", "kor", "kur"))
                 else "max" if w.startswith(("full", "max")) else "min")
            return {"action": action, "args": [w]}
        if kind == "song":
            subj = groups[0].strip()
            if not subj or subj in SONG_SKIP or subj.split()[0] in SONG_SKIP:
                continue
            return {"action": action, "args": [f"{subj} song"]}
        if kind == "play":
            subj = groups[0].strip()
            if subj in PLAY_GENERIC or re.match(r"^(?:the\s+)?(?:music|song|songs|paattu|pattu)\b", subj) or "youtube" in subj \
                    or re.match(r"^(?:it|that|this|again|along|with|dead|fair|nice|safe|around|me|him|her|them|us|the game|games?)\b", subj):
                continue
            return {"action": action, "args": [subj]}
        if kind == "file":
            q, k = groups[0].strip(), groups[1]
            k = {"file": "", "document": "", "docx": "docx", "doc": "doc", "sheet": "excel", "presentation": "ppt",
                 "image": "photo"}.get(k, k)
            return {"action": action, "args": [q] + ([k] if k else [])}
        if kind == "any":
            q = clean_target(groups[0])
            words = q.split()
            if not q or len(words) > 5 or TARGET_STOP & set(words) or q in TARGET_STOP:
                continue
            raw = re.sub(r"(?:\s+(?:ah|a|ai|ya|va))+$", "", groups[0].strip())  # (keeps "website": the site is meant)
            return {"action": action, "args": [raw]}
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
           "youtube-play": "YouTube la '{0}' podren.", "open-settings": "{0} settings open panniten.",
           "brightness": "Brightness {0}.",
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
    if res.get("unresolved"):
        return {"handled": False}  # ("close X" for an X that is no app: the model decides)
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
        "youtube la ilayaraja song podu": ("youtube-play", ["ilayaraja song"]),
        "play arijit singh songs on youtube": ("youtube-play", ["arijit singh songs"]),
        "youtube la ilayaraja song search pannu": ("youtube-search", ["ilayaraja song"]),
        "ilayaraja paattu podu": ("youtube-play", ["ilayaraja song"]), "play anirudh songs": ("youtube-play", ["anirudh songs"]),
        "play music": ("media", ["play"]), "Play.": (None, None),
        # any app / website / file
        "open microsoft word": ("open-any", ["microsoft word"]), "word open pannu": ("open-any", ["word"]),
        "Open Netflix.": ("open-any", ["netflix"]), "amazon.in open pannu": ("open-any", ["amazon.in"]),
        "flipkart dot com open pannu": ("open-any", ["flipkart.com"]), "hotstar website open pannu": ("open-any", ["hotstar website"]),
        "switch to safari": ("switch-app", ["safari"]), "Play it again.": (None, None), "Play along.": (None, None), "Jarvis, open Telegram.": ("open-app", ["telegram"]),
        "close microsoft teams": ("close-any", ["microsoft teams"]), "teams ah close pannu": ("close-any", ["teams"]),
        "run the tests": (None, None), "start the server": (None, None), "open the door": (None, None),
        "open a new file in vs code and write hello": (None, None),
        "resume pdf open pannu": ("open-file", ["resume", "pdf"]), "open my resume file": ("open-file", ["resume"]),
        "find invoice pdf": ("find-file", ["invoice", "pdf"]), "aadhar file enga irukku": ("find-file", ["aadhar"]),
        # keys, settings, system
        "new tab": ("press-key", ["cmd+t"]), "close tab": ("press-key", ["cmd+w"]), "tab close pannu": ("press-key", ["cmd+w"]),
        "Scroll down.": ("press-key", ["pagedown"]), "refresh pannu": ("press-key", ["cmd+r"]), "go back": ("press-key", ["cmd+["]),
        "minimize": ("press-key", ["cmd+m"]), "full screen": ("press-key", ["ctrl+cmd+f"]), "save pannu": ("press-key", ["cmd+s"]),
        "copy": ("press-key", ["cmd+c"]), "paste": ("press-key", ["cmd+v"]), "undo": ("press-key", ["cmd+z"]),
        "select all": ("press-key", ["cmd+a"]),
        "brightness kammi pannu": ("brightness", ["down"]), "brightness full ah vai": ("brightness", ["max"]),
        "bluetooth on pannu": ("open-settings", ["bluetooth"]), "wifi settings open pannu": ("open-settings", ["wifi"]),
        "storage evlo irukku": ("sysinfo", ["disk"]), "ram evlo free": ("sysinfo", ["ram"]), "memory la enna irukku": (None, None), "my ip address enna": ("sysinfo", ["ip"]),
        "clipboard la enna irukku": ("clipboard", ["get"]), "which apps are open": ("apps", []),
        "what is 25 times 4": ("calc", ["25 * 4"]), "1500 into 12 evlo": ("calc", ["1500 * 12"]),
        "15 percent of 2400": ("calc", ["15 * 0.01 * 2400"]), "What's 2 + 2?": ("calc", ["2 + 2"]),
        "5 minutes timer vai": ("remind", ["5m", "Timer mudinjiduchu"]), "set a timer for 10 minutes": ("remind", ["10m", "Timer mudinjiduchu"]),
        # WhatsApp (Whisper's spellings from Dhina's Mac, 21 Oct)
        "whatsapp la last message enna": ("whatsapp-read", []), "Whatsapp enna? Kina kadasiya message vandharu.": ("whatsapp-read", []),
        "Innaiku last aon the message inna whatsapp le.": ("whatsapp-read", []), "whatsapp la amma ku message anuppu": (None, None),
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
    assert do("youtube la ilayaraja song search pannu")["would_run"] == [["open", "https://www.youtube.com/results?search_query=ilayaraja+song"]]
    assert do("youtube la ilayaraja song podu")["would_run"] == [["open", "https://duckduckgo.com/?q=%21ducky+ilayaraja+song+site%3Ayoutube.com"]]
    global _APP_CACHE
    _APP_CACHE = (time.time(), {"Microsoft Word": "/Applications/Microsoft Word.app", "Google Chrome": "/Applications/Google Chrome.app",
                                "Microsoft Teams": "/Applications/Microsoft Teams.app", "Android Studio": "/Applications/Android Studio.app",
                                "Adobe Photoshop 2025": "/Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app",
                                "Mail": "/System/Applications/Mail.app", "Safari": "/Applications/Safari.app",
                                "Visual Studio Code": "/Applications/Visual Studio Code.app"})
    assert resolve_open("word")["target"] == "Microsoft Word" and resolve_open("photoshop")["target"] == "Adobe Photoshop 2025"
    assert resolve_open("android")["target"] == "Android Studio" and resolve_open("email")["target"] == "Mail"
    assert resolve_open("visual studio code app")["target"] == "Visual Studio Code"
    assert resolve_open("netflix")["target"] == "https://www.netflix.com" and resolve_open("amazon.in")["target"] == "https://amazon.in"
    assert resolve_open("spotify")["kind"] == "web", resolve_open("spotify")  # (alias, not installed here: its website)
    assert resolve_open("hotstar website")["kind"] == "site" and resolve_open("some random shop")["kind"] == "web"
    assert do("open microsoft word")["would_run"] == [["open", "-a", "/Applications/Microsoft Word.app"]]
    assert do("word open pannu")["say"] == "Microsoft Word open panniten."
    assert do("chrome open pannu")["would_run"] == [["open", "-a", "/Applications/Google Chrome.app"]]
    assert do("Open Netflix.")["would_run"] == [["open", "https://www.netflix.com"]]
    assert do("close microsoft teams")["say"] == "Microsoft Teams close panniten." and "is running" in do("close microsoft teams")["would_run"][0][-1]
    assert do("stop the timer") == {"handled": False}, do("stop the timer")
    for line in ("Open the gate!", "Open fire!", "Open your eyes.", "Switch to plan B.", "Go to hell.", "Open up!", "Stop him!"):
        assert do(line) == {"handled": False}, (line, do(line))
    assert do("switch to safari")["would_run"] == [["open", "-a", "/Applications/Safari.app"]]
    assert do("open zerodha")["would_run"] == [["open", ducky_url("zerodha")]]
    assert do("what is 25 times 4")["say"] == "Answer 100." and do("100 divided by 8")["say"] == "Answer 12.5."
    assert calc_expr("21-10-2026") is None and calc_expr("volume 30") is None and calc_expr("2 ** 99999") == "2 ** 99999"
    assert do("2 ** 99999")["ok"] is False
    assert do("storage evlo irukku")["say"].startswith("Disk la ") and do("bluetooth on pannu")["would_run"][0][-1].endswith("BluetoothSettings")
    assert do("brightness kammi pannu")["would_run"][0][-1].count("key code 145") == 1
    _APP_CACHE = None
    # WhatsApp's database (the Mac app's schema, kept in WAL mode while WhatsApp runs): read from a copy
    global WA_DIR
    wa = Path(tempfile.mkdtemp(prefix="dd-wa-test-"))
    con = sqlite3.connect(wa / "ChatStorage.sqlite")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript("CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZPARTNERNAME TEXT, ZCONTACTJID TEXT, "
                      "ZUNREADCOUNT INTEGER, ZLASTMESSAGEDATE REAL); CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, "
                      "ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE REAL, ZTEXT TEXT, ZPUSHNAME TEXT);")
    now = time.time() - APPLE_EPOCH
    con.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?)",
                    [(1, "Amma", "919000000000@s.whatsapp.net", 2, now - 20), (2, "Office", "1203630@g.us", 5, now - 30)])
    con.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?)",
                    [(1, 1, 0, now - 120, "Saptiya?", None), (2, 1, 1, now - 90, "Aamaa", None),
                     (3, 2, 0, now - 30, "Meeting at 5", "Ravi"), (4, 1, 0, now - 20, None, None)])
    con.commit()  # (still open: the rows sit in the -wal file, as with WhatsApp running)
    assert (wa / "ChatStorage.sqlite-wal").stat().st_size > 0
    old_dir, WA_DIR = WA_DIR, str(wa)
    try:
        w = whatsapp_db()
    finally:
        WA_DIR = old_dir
        con.close()
    assert w["ok"] and [x["from"] for x in w["latest"]] == ["Amma", "Ravi (Office)", "Amma"], w
    assert w["latest"][0]["text"] == "(photo / file)" and w["latest"][2]["text"] == "Saptiya?", w
    assert w["unread"] == [{"chat": "Amma", "count": 2}, {"chat": "Office", "count": 5}], w
    say = wa_say(w)["say"]
    assert say.startswith("Kadasi message Amma kitta irundhu, ") and "Ravi (Office): Meeting at 5" in say and "Unread: Amma 2, Office 5." in say, say
    WA_DIR = str(wa / "nowhere")
    assert whatsapp_db() == {"ok": False, "why": "missing"}
    WA_DIR = old_dir
    shutil.rmtree(wa, ignore_errors=True)
    assert do("write a poem") == {"handled": False}
    rs = do("screen la enna irukku")
    assert rs["handled"] and rs["dry_run"] and rs["would_run"][0][0] == "screencapture", rs
    assert do("downloads folder open pannu")["would_run"] == [["open", os.path.expanduser("~/Downloads")]]
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
        elif cmd == "whatsapp-db":  # (whatsapp_read runs this in its own process, with a time limit)
            res = whatsapp_db()
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
