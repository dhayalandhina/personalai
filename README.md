# dd-skills — skill library for dd (personal AI assistant)

26 drop-in skills in the open **Agent Skills** format (`skills/<name>/SKILL.md` + `scripts/` + `references/` + `assets/`), a progressive-disclosure loader, a validator that runs every script's `--selftest`, and an example zero-key agent loop on Ollama. Everything is free / open-source / self-hosted. No paid APIs anywhere.

```
dd-skills/
├── skill_loader.py        # index → load → run (3 levels), Ollama/OpenAI tool schemas, keyword routing
├── test_skills.py         # validates all 26 skills + runs 33 selftests (run after every change)
├── examples/dd_loop_ollama.py   # minimal agent loop: rules check → memory recall → tool calls → log
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
    ├── voice                faster-whisper / whisper.cpp STT, Piper TTS, opus voice notes
    ├── telegram-interface   chunking, MarkdownV2, send text/file/photo/video/voice, commands
    ├── scheduler-reminders  "remind me tomorrow 9am", cron jobs, due-queue for the loop
    ├── email-imap           IMAP/SMTP via app password; send only with explicit confirm
    ├── local-models         Ollama manage, hardware advisor, bench, dd Modelfile builder
    ├── n8n-automation       self-hosted n8n REST: workflows, executions, webhooks, scaffolds
    ├── system-ops           healthcheck (/status), backup/restore of ~/.dd
    ├── mac-control          JARVIS actions: apps, websites, volume, music, screenshot, lock + instant router
    └── skill-creator        dd scaffolds + validates its own new skills
```

## Install (5 minutes)
```bash
git clone https://github.com/dhayalandhina/personalai && cd personalai
bash install.sh                        # deps, ~/.dd, locks the default rules, briefing config
python3 test_skills.py                 # must print "ALL GOOD: 26 skills valid"
cp .env.example ~/.dd/.env && chmod 600 ~/.dd/.env   # fill TELEGRAM_BOT_TOKEN etc. (optional per skill)
ollama pull qwen2.5-coder:7b-instruct-q4_K_M        # or run: python3 skills/local-models/scripts/models.py recommend
python3 examples/dd_loop_ollama.py     # chat
```
Personal rules: edit a copy of `skills/rules-guard/assets/RULES.default.md`, then `python3 skills/rules-guard/scripts/rules_guard.py update --file my_rules.md` (owner terminal only). Rules, memory, learning data and `.env` live in `~/.dd/` — never in this repo.

## JARVIS mode (voice)
```bash
pip3 install faster-whisper
python3 examples/dd_jarvis.py               # push-to-talk: Enter start, Enter stop
python3 examples/dd_jarvis.py --hands-free  # always listening: "Jarvis, innaiku weather enna?"
python3 examples/dd_jarvis.py --text        # type, dd speaks back
```
Mic via ffmpeg, speech-to-text via faster-whisper (local), brain = same dd agent loop + skills, voice via macOS `say` (Piper/espeak elsewhere). First run asks macOS for microphone permission for your terminal app.

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
Validated on 15 Oct 2026: 25 skills, 33 scripts, all selftests passing (`test_skills.py`). Live ComfyUI / Ollama / Telegram / IMAP paths need those services on your machine; their scripts fail gracefully with the install line when absent.
