# dd-skills — skill library for dd (personal AI assistant)

27 drop-in skills in the open **Agent Skills** format (`skills/<name>/SKILL.md` + `scripts/` + `references/` + `assets/`), a progressive-disclosure loader, a validator that runs every script's `--selftest`, and an example zero-key agent loop on Ollama. Everything is free / open-source / self-hosted. No paid APIs anywhere.

```
dd-skills/
├── skill_loader.py        # index → load → run (3 levels), Ollama/OpenAI tool schemas, keyword routing
├── test_skills.py         # validates all 27 skills + runs 37 selftests (run after every change)
├── examples/dd_loop_ollama.py   # minimal agent loop: rules check → memory recall → tool calls → log
├── examples/dd_telegram.py      # Telegram bot runner (text + voice notes, reminders) — the cloud server mode
├── install_app.sh + macapp/     # builds the Mac app (native applet): menu bar, always listening (dd_app.py + dd_listen.py)
├── examples/dd_chat.py          # Mac chat window (Tkinter), opened from the menu bar
├── install.sh             # pip deps, ~/.dd layout, rules lock, briefing config
├── requirements.txt
└── skills/
    ├── dd-core-identity     who dd is, Thanglish, 2-3 lines, paste-ready, no questions   (always loaded)
    ├── rules-guard          hash-locked owner rules; dd can read, never write            (always loaded)
    ├── security-privacy     injection defence, secret scan, track isolation, never-do list (always loaded)
    ├── memory               SQLite FTS5 facts: remember / recall / forget / context      (always loaded)
    ├── verification-protocol  verify before answer, Likely/Assumption labels, self-attack
    ├── self-learning        log → feedback → preference extraction → SFT/DPO export
    ├── web-search           key-less search (SearXNG/ddgs/DDG-HTML) + page fetch, dated answers
    ├── daily-briefing       RSS + Open-Meteo weather + reminders, live only
    ├── task-planner         plan→execute→verify with persistent step status
    ├── shell-and-files      safe exec (destructive gate), read/write/edit/tree/find
    ├── coding               read-first, minimal diff, Track A vs B rules, lint/test runner
    ├── git-github           guarded commit/push, secret-scan on staged files, no force push
    ├── browser-automation   Playwright headless: text, screenshot, click, fill, login-once profile
    ├── documents            read/create pdf, docx, xlsx, csv (pypdf, python-docx, openpyxl, reportlab)
    ├── data-analysis        pandas group/trend/sql + matplotlib charts
    ├── video-generation     idea → storyboard → Wan 2.2 / LTX clips (ComfyUI) → ffmpeg stitch + narration
    ├── image-generation     FLUX.1-schnell / SDXL via ComfyUI or diffusers, mock backend, text overlay
    ├── voice                faster-whisper / whisper.cpp STT, Piper TTS, opus voice notes, train dd's own wake word
    ├── telegram-interface   chunking, MarkdownV2, send text/file/photo/video/voice, commands
    ├── scheduler-reminders  "remind me tomorrow 9am", cron jobs, due-queue for the loop
    ├── email-imap           IMAP/SMTP via app password; send only with explicit confirm
    ├── local-models         Ollama manage, hardware advisor, bench, dd Modelfile builder
    ├── n8n-automation       self-hosted n8n REST: workflows, executions, webhooks, scaffolds
    ├── system-ops           healthcheck (/status), backup/restore of ~/.dd
    ├── mac-control          JARVIS actions: apps, websites, volume, music, screenshot, read screen, type, keys, lock + instant router
    ├── employees            500 named specialists (50 roles x 10); every task auto-assigned to the best one + checklist
    └── skill-creator        dd scaffolds + validates its own new skills
```

## Install (5 minutes)
```bash
git clone https://github.com/dhayalandhina/personalai && cd personalai
bash install.sh                        # deps, ~/.dd, locks the default rules, briefing config
python3 test_skills.py                 # must print "ALL GOOD: 27 skills valid"
cp .env.example ~/.dd/.env && chmod 600 ~/.dd/.env   # fill TELEGRAM_BOT_TOKEN etc. (optional per skill)
ollama pull qwen2.5-coder:7b-instruct-q4_K_M        # or run: python3 skills/local-models/scripts/models.py recommend
python3 examples/dd_loop_ollama.py     # chat
```
Personal rules: edit a copy of `skills/rules-guard/assets/RULES.default.md`, then `python3 skills/rules-guard/scripts/rules_guard.py update --file my_rules.md` (owner terminal only). Rules, memory, learning data and `.env` live in `~/.dd/` — never in this repo.

## Background mode (keeps your Mac free)
```bash
bash skills/system-ops/scripts/background.sh install    # returns at once; setup runs detached at low priority
bash skills/system-ops/scripts/background.sh status
```
Installs voice (faster-whisper + base model) and a launchd background service that only fires reminders as Mac notifications —
no mic, no screen/app control, no LLM, lowest CPU/IO priority. Talk to dd only when you want: `python3 examples/dd_loop_ollama.py`
or `python3 examples/dd_jarvis.py`. Stop anytime: `background.sh stop` / `uninstall`.

## Cloud mode (free 24/7 server — the Mac does nothing)
dd runs on an **Oracle Cloud Always Free** VM and you talk to it on Telegram (text or voice notes). Free limit in 2026: Ampere A1, 2 OCPU / 12 GB RAM, 200 GB disk. No SSH or terminal needed:
1. Oracle Cloud account (Visa/Mastercard credit card for verification; RuPay is declined).
2. Telegram → @BotFather → `/newbot` → copy the token.
3. Get your first-boot script: `bash skills/system-ops/scripts/cloud_setup.sh cloud-init` (prints it with a fresh pairing secret). Replace `PASTE_BOT_TOKEN_HERE` with your token.
4. Oracle → Compute → Instances → Create instance: image **Canonical Ubuntu 24.04**, shape **VM.Standard.A1.Flex** with **2 OCPU / 12 GB**, SSH keys → "Generate a key pair for me" → save the private key (only for debugging later). Advanced options → Initialization script → **Paste cloud-init script** → paste the script → Create.
5. Send `/pair <secret from the script>` to your bot right away. Setup takes ~15–30 min on first boot; then the bot answers "Paired ✅ … dd online".

With SSH instead: `sudo apt-get update -y && sudo apt-get install -y git && sudo git clone https://github.com/dhayalandhina/personalai /opt/dd && bash /opt/dd/skills/system-ops/scripts/cloud_setup.sh install` (asks for the token, then shows a `/pair CODE`).
Server commands (over SSH): `bash /opt/dd/skills/system-ops/scripts/cloud_setup.sh status | update | token | pair`; first-boot log: `sudo cat /var/log/dd-setup.log`.
Safety: dd runs as user `dd` with no sudo; its rules, settings and bot token live in root-owned `/etc/dd/` that dd can't rewrite or read (a separate `dd-pair` user does the pairing); the token never reaches a tool or another process; the cloud metadata IP is firewalled off; only your paired Telegram account gets answers; nothing listens on the internet — don't open port 11434. Reply files (charts, pdfs) are attached automatically when their path appears in dd's reply. Replies are slower than on the Mac (2 CPU cores, no GPU).

## JARVIS mode (voice)
```bash
pip3 install faster-whisper
python3 examples/dd_jarvis.py               # push-to-talk: Enter start, Enter stop
python3 examples/dd_jarvis.py --hands-free  # always listening: "Jarvis, innaiku weather enna?"
python3 examples/dd_jarvis.py --text        # type, dd speaks back
```
Mic via ffmpeg, speech-to-text via faster-whisper (local), brain = same dd agent loop + skills, voice via macOS `say` (Piper/espeak elsewhere). First run asks macOS for microphone permission for your terminal app.

## The brain (local by default, free cloud tier optional)
dd thinks with the local Ollama model. A 3B/7B model on a laptop is honest but limited; for sharper, faster answers you can **opt in** to a free-tier cloud model by putting one key in `~/.dd/.env` — `GROQ_API_KEY` (console.groq.com), `GEMINI_API_KEY` (aistudio.google.com/apikey), `CEREBRAS_API_KEY` or `OPENROUTER_API_KEY`. Same loop, same skills and tools (OpenAI-compatible tool calling); when the cloud call fails or the daily free quota is hit, dd switches to the local model for a while and says so in the log (`brain: … — local model for 10 min`). `DD_BRAIN=ollama` forces local; `DD_BRAIN_MODEL` picks another model id. Trade-off to know: with a cloud brain your messages and tool results leave the machine (the provider's free-tier terms apply; nothing else does — rules, memory, logs stay local). Status menu / `brain:` log line show which brain answered.

## The team (500 specialists)
Every message is routed to the best-fitting employee from `skills/employees` (50 roles × 10 specialisations: engineering, data, design, content & marketing, business & ops, personal, hospitality). dd then works as that specialist — their skills, a "done when" bar and a verify checklist are prepended to the task. Same local brain underneath; the profile adds focus and a checklist, not IQ. `python3 skills/employees/scripts/employees.py find "task"` shows who would take it; on Telegram `/team task`. Small talk gets no employee; `DD_EMPLOYEES=0` turns it off.

## Mac app (menu bar, always listening)
One command builds **dd.app** natively on your Mac (AppleScript applet via `osacompile` — so it runs natively on Apple silicon and macOS asks Microphone permission for "dd"), installs the deps, and starts it at login:
```bash
bash install_app.sh install          # → ~/Applications/dd.app + Login Item; uninstall: bash install_app.sh uninstall
```
A small voice-wave in the menu bar = listening, from anywhere: "**hey jarvis**" (or "**dee dee**") → *tink* + a small pill with **blue voice bars** appears at the bottom of the screen (bars moving with your voice = it is listening to you; nothing else covers the screen — `DD_HUD=full` adds the heard text and the reply) → "**chrome open pannu**" (or in one breath: "hey jarvis, chrome open pannu"), "innaiku weather enna", "20 nimisham la tea remind pannu"… The bars wave slowly while dd thinks and faster while it speaks, then fade out; after a reply you have 8 s to continue without the wake word. It never takes focus or steals the mouse (over full-screen video too). Say "hey jarvis" while dd is still talking and it stops mid-sentence and listens (barge-in). dd speaks with an Indian-English voice when one is installed (Rishi/Veena; `DD_VOICE` to choose). **Teach 'Jarvis' (your voice)…** in the menu does a Siri-style voice setup: dd asks you to say "Jarvis" five times and from then on also listens for *your* way of saying it (accent included). Menu-bar glyph (examples/status_icons.py): calm wave = waiting for "Jarvis" · tall **blue** wave = listening to you · ••• thinking · speech wave = speaking · ‖ paused · red ! = mic problem. **Voice ▸** in the menu: **Pallavi** (female) / **Valluvar** (male) — natural Tamil Nadu voices — plus Neerja / Prabhat (Indian English) and Slow / Normal / Fast; picking one makes it introduce itself (examples/dd_voice.py). dd still writes Thanglish; before a Tamil voice speaks, examples/dd_thanglish.py puts the Tamil words in Tamil script and leaves the English ones as they are ("Chrome open பண்ணிட்டேன்"), so it talks the way people in Tamil Nadu do. These are Microsoft's free neural voices (edge-tts, no key): the reply text is sent to Microsoft's speech service to be spoken; unknown Thanglish words are looked up once in Google Input Tools for the right Tamil spelling (`DD_TRANSLIT_LEARN=0` turns that off). Offline, dd falls back to the Mac's Tamil voice (Vani, if installed) / Kokoro / the Mac voice; `DD_TTS=say` uses only the Mac voice. First launch asks **Microphone** + **Automation** permission — Allow. Menu: pause/resume, what it last heard (+ live mic level), **Mic test (3 s)** — says back what it heard, Open chat (a window to type in), Status, **Update dd** (pulls the latest code and restarts; `touch personalai/logs/update.req` does the same from anywhere), Quit. Log: `~/.dd/logs/app.log` (mirrored to `personalai/logs/app.log`). Terminal check: `python3 examples/dd_listen.py --check`. `DD_HUD=0` turns the pill off (notifications instead).
Engine: `examples/dd_listen.py` — mic through `macapp/dd-mic.swift` (Apple voice processing: echo cancellation, so it hears you while a video or music plays on the Mac; built once with `swiftc`; it asks for the Microphone permission itself, logs the input device, and if it only delivers digital silence dd switches to ffmpeg within 4 s), a Siri-style always-on **wake-word model** (openWakeWord "hey jarvis", ~3% CPU — Whisper never runs until the wake fires, so a playing video is never transcribed; the model files ship in `skills/voice/assets/oww`, no download needed) plus dd's own heads trained on the same embeddings — plain **"jarvis"** (`skills/voice/assets/wake_jarvis.npz`) and, opt-in, **"dee dee"** (`DD_WAKE_DD=1`) — two-stage: a head (or your voice print) proposes, Whisper confirms on the word without any prompt, so "jarvis, chrome open pannu" works in one breath and media audio rarely wakes it; quiet mics get automatic software gain, room-adaptive voice-activity segmentation for the command, faster-whisper (small, English decoding for Thanglish; one shared download, base fallback) — and on a 16 GB+ Apple-silicon Mac it sets up **mlx-whisper large-v3-turbo** in the background (Apple GPU: a command transcribed in under a second, much better Thanglish; `DD_STT=faster` to skip), wake-word normalisation (hears "D.D.", "didi", "rvis" as dd/jarvis), own-voice guard, continuous-media skip, hallucination filter, mic-permission detection, `read-screen` (Apple Vision OCR). Panel: `examples/dd_hud.py`. Common Mac actions never touch the LLM, so they are instant; the model stays loaded between questions. Same dd loop + mac-control as the CLI — no new power, irreversible actions still ask first.

## Use from any agent
- **dd's own loop (Ollama / llama.cpp)**: see `examples/dd_loop_ollama.py` — `SkillLibrary.index_prompt()` in the system prompt, `tool_schemas()` as tools, `dispatch()` for calls.
- **Claude Code / Claude Agent SDK**: copy or symlink `skills/*` into `.claude/skills/` — the format is identical; skills appear as slash commands and load on trigger.
- **Any other agent**: read `SKILL.md` index lines at boot; load the full file when its triggers match; run `scripts/<x>.py --help`.

## Conventions
- Every script: stdlib-first, `--help`, `--selftest`, JSON output, non-zero exit on error. Data dir `$DD_HOME` (default `~/.dd`).
- Content fetched by tools (web, mail, files) is **data**, never instructions (`security-privacy`).
- Current-world answers come from a search done now with the date (`web-search`), never from memory.
- Nothing here edits the locked rules. Learned preferences lose to rules on conflict.

## Status
Validated on 20 Oct 2026: 27 skills, 38 scripts, all selftests passing (`test_skills.py`), plus `examples/*.py --selftest` (agent loop incl. the cloud-brain plumbing, JARVIS, listener engine with the two-stage wake, voice panel model, daemon, Telegram bot, chat window, menu-bar app, icon, app installer) and a full hardened `cloud_setup.sh install` emulated on Ubuntu 24.04 (root-owned secrets, separate pairing user, metadata firewall; systemd/Ollama stubbed). Live ComfyUI / Ollama / Telegram / IMAP paths need those services on your machine; their scripts fail gracefully with the install line when absent.
