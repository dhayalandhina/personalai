---
name: mac-control
description: Control Dhina's Mac like JARVIS — open/quit apps, open websites, volume, music play/pause/next, notifications, time, battery, Wi-Fi, screenshot, lock screen, clipboard, dark mode, running apps. Plus a fast intent router so common spoken/typed commands ("chrome open pannu", "volume 30", "mani enna") run instantly without waiting for the LLM. Use for any request to do something on the computer itself.
triggers: [open, quit, close app, volume, sound, mute, play, pause, next song, music, spotify, screenshot, lock, battery, wifi, time, mani, clipboard, dark mode, notification, chrome, safari, vscode]
metadata:
  version: "1.0"
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
python3 scripts/mac.py route "chrome open pannu"                  # fast router: JSON {action,args} or {"action": null}
```

## Fast router
`route` maps common English + Thanglish phrases to an action. The dd loop calls it before the LLM; a hit runs immediately and replies in one line. A miss goes to the LLM as usual. Add phrases in `ROUTES` inside `scripts/mac.py`.

## Rules
- These actions are reversible and need no confirmation.
- Anything irreversible (deleting files, sending messages/email to other people, payments, force-push) still goes through `security-privacy` — dd asks "seri-nu sollunga" first and waits.
- `quit-app` asks the app to quit normally, so unsaved documents still get their save prompt.
- Off macOS, every command prints what it would run (`DD_MAC_DRY=1` forces that anywhere).
