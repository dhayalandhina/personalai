---
name: mac-control
description: Control Dhina's Mac like JARVIS — open/quit apps, open websites, volume, music play/pause/next, notifications, time, battery, Wi-Fi, screenshot, lock screen, clipboard, dark mode, running apps. Plus a fast intent router so common spoken/typed commands ("chrome open pannu", "volume 30", "mani enna") run instantly without waiting for the LLM. Use for any request to do something on the computer itself.
triggers: [open, quit, close app, volume, sound, mute, play, pause, next song, music, spotify, screenshot, lock, battery, wifi, time, mani, clipboard, dark mode, notification, chrome, safari, vscode]
metadata:
  version: "1.2"
  owner: dhina
---

# mac-control

All actions use built-in macOS tools (`open`, `osascript`, `screencapture`, `pmset`, `pbcopy`) — no installs, zero cost.

## Commands
```bash
python3 scripts/mac.py open-app "Google Chrome"
python3 scripts/mac.py quit-app "Spotify"
python3 scripts/mac.py open-url "https://github.com"
python3 scripts/mac.py search-web "ollama tool calling"
python3 scripts/mac.py volume 30 | up | down | mute | unmute
python3 scripts/mac.py media play | pause | next | previous      # Spotify if running, else Music
python3 scripts/mac.py notify "Build mudinjiduchu"
python3 scripts/mac.py time | battery | wifi | apps
python3 scripts/mac.py screenshot [--out ~/Desktop/shot.png]
python3 scripts/mac.py lock
python3 scripts/mac.py dark-mode on | off | toggle
python3 scripts/mac.py clipboard get | set "text"
python3 scripts/mac.py open-folder downloads | desktop | documents | personalai
python3 scripts/mac.py weather | news                             # via daily-briefing
python3 scripts/mac.py remind 10m "call amma"                     # via scheduler-reminders
python3 scripts/mac.py route "chrome open pannu"                  # fast router: JSON {action,args} or {"action": null}
```

## Fast router
`route` maps common English + Thanglish phrases to an action. The dd loop calls it before the LLM; a hit runs immediately and replies in one line. A miss goes to the LLM as usual. Add phrases in `ROUTES` inside `scripts/mac.py`.

Examples it understands: "chrome open pannu", "downloads folder", "volume 30", "adutha paattu", "innaiku weather enna", "innaiku news", "20 nimisham la tea kudikka remind pannu", "remind me in 2 hours to call client", "mani enna".
Weather/news/reminder routes only fire on clear phrasing, so coding requests like "news feed component fix pannu" still go to the LLM.
Spelled-out numbers from voice transcripts work too ("remind me in ten minutes …", "twenty five minutes", "an hour").
On the cloud server (`DD_HOST=server`) this skill is hidden from the model and `do` only handles time / weather / news / remind — dd says it can't control the Mac from there.

Reminders fire from the dd loop and JARVIS mode (checked every 30 s): Mac notification + printed + spoken in JARVIS mode.

## Rules
- These actions are reversible and need no confirmation.
- Anything irreversible (deleting files, sending messages/email to other people, payments, force-push) still goes through `security-privacy` — dd asks "seri-nu sollunga" first and waits.
- `quit-app` asks the app to quit normally, so unsaved documents still get their save prompt.
- Off macOS, every command prints what it would run (`DD_MAC_DRY=1` forces that anywhere).
