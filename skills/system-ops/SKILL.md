---
name: system-ops
description: Keep dd and its machine healthy — health check (disk, RAM, CPU, GPU, Ollama, ComfyUI, n8n, Telegram bot reachability), logs, service restart commands, backups of ~/.dd (memory, rules, reminders, learning data) and restore. Use for "/status", "is everything running", slow machine, disk full, after a crash, or the weekly backup job.
triggers: [status, health, is it running, disk full, restart, logs, backup, restore, uptime, crash, service down, cpu, memory usage]
metadata:
  version: "1.1"
  owner: dhina
---

# system-ops

## Commands
```bash
python3 scripts/healthcheck.py                 # one JSON: host, disk, ram, cpu load, gpu, services {ollama, comfyui, n8n, telegram}, dd data sizes
python3 scripts/healthcheck.py --brief         # one-line summary for Telegram "/status"
bash scripts/backup.sh                         # tar.gz of $DD_HOME (minus browser-profile, tmp, video renders) → $DD_BACKUP_DIR (default ~/dd-backups), keeps last 14
bash scripts/backup.sh restore FILE.tar.gz     # restores into $DD_HOME (current one moved to $DD_HOME.bak)
bash scripts/disk_cleanup.sh                   # PREVIEW: regenerable caches + sizes (deletes nothing)
bash scripts/disk_cleanup.sh --clean           # owner terminal only, typed YES; dd itself is refused
bash scripts/background.sh install [--with-7b] # one-time setup, detached + low priority; Mac notification when done
bash scripts/background.sh status | stop | start | uninstall   # background service (owner terminal only)
bash scripts/cloud_setup.sh install [--model qwen2.5:3b]       # ON the free cloud server: Ollama + dd + Telegram bot, 24/7
bash scripts/cloud_setup.sh status | update | token | pair     # server: health / git pull + restart / new token / re-link Telegram
bash scripts/cloud_setup.sh cloud-init                          # no-SSH first-boot script for Oracle's "Initialization script" box
```

## Service cheat-sheet (local, free)
| Service | Check | Start |
|---|---|---|
| Ollama | `curl -s localhost:11434/api/tags` | `ollama serve` (macOS app autostarts) |
| ComfyUI | `curl -s localhost:8188/system_stats` | `cd ComfyUI && source .venv/bin/activate && python main.py --listen 127.0.0.1` |
| n8n | `curl -s $N8N_URL/healthz` | `npx n8n` or `docker compose up -d n8n` |
| dd loop | `pgrep -f dd/main.py` | `python3 main.py` (launchd/systemd unit in `references/`) |

## Background mode
`background.sh install` installs faster-whisper + the Whisper base model and a launchd agent (`com.dhina.dd`) that runs `examples/dd_daemon.py`: reminders → Mac notifications every 30 s. It never uses the mic, never controls the screen or apps, never loads the LLM, and runs at nice 10 with `ProcessType=Background`, so Dhina keeps full use of his Mac. `--with-7b` pulls qwen2.5:7b only when ≥ 12 GB is free. dd itself is refused from install/stop/uninstall.

## Cloud mode (free 24/7 server, Mac stays free)
`cloud_setup.sh install` runs on an Oracle Always Free VM (Ubuntu, ARM `VM.Standard.A1.Flex`, 2 OCPU / 12 GB — the 2026 free limit). It installs Ollama (127.0.0.1 only, model kept loaded), qwen2.5:3b, a venv in `/opt/dd-venv`, dd data in `/var/lib/dd/.dd`, asks for the Telegram bot token (hidden input), pairs Dhina's Telegram account with a one-time `/pair CODE`, and starts the `dd-telegram` systemd service (`examples/dd_telegram.py`). Hands-free variant: `cloud-init` prints a first-boot script (token placeholder + random pairing secret) that runs the same install with no prompts; the bot then accepts `/pair <secret>` from Dhina's Telegram and wipes the secret. The service runs as user `dd` with no sudo, `NoNewPrivileges`, read-only system dirs and no access to `/home`. Nothing listens on the internet — never open port 11434. On the server `DD_HOST=server`: mac-control is hidden from the model (only its time/weather/news/remind router stays), and reminders arrive as Telegram messages. dd itself is refused from every cloud_setup command.

## Rules
- `/status` reply = `healthcheck.py --brief` output, nothing else, unless something is red — then one line per red item with the fix command.
- Disk < 10 % free → run `disk_cleanup.sh` (preview) and show Dhina the list. Never delete anything yourself: he runs `--clean` in his own terminal. The script only knows regenerable caches; passwords, Keychain, Chrome profile, Documents/Desktop/Downloads/Photos, Trash, `_Review_Before_Delete`, models and project folders are never on its list.
- Restart a service only when its check fails; say what was restarted.
- Backups run weekly (via `scheduler-reminders` `--action system-ops`) and before any `rules_guard.py update`. Backup contains memory + learning data → stays local, never uploaded to a public repo.
- Logs: `$DD_HOME/logs/dd.log` (rotate at 10 MB). Read the last 100 lines on a crash; grep the first `Traceback`.
