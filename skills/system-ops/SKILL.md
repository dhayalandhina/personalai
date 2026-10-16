---
name: system-ops
description: Keep dd and its machine healthy — health check (disk, RAM, CPU, GPU, Ollama, ComfyUI, n8n, Telegram bot reachability), logs, service restart commands, backups of ~/.dd (memory, rules, reminders, learning data) and restore. Use for "/status", "is everything running", slow machine, disk full, after a crash, or the weekly backup job.
triggers: [status, health, is it running, disk full, restart, logs, backup, restore, uptime, crash, service down, cpu, memory usage]
metadata:
  version: "1.0"
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
```

## Service cheat-sheet (local, free)
| Service | Check | Start |
|---|---|---|
| Ollama | `curl -s localhost:11434/api/tags` | `ollama serve` (macOS app autostarts) |
| ComfyUI | `curl -s localhost:8188/system_stats` | `cd ComfyUI && source .venv/bin/activate && python main.py --listen 127.0.0.1` |
| n8n | `curl -s $N8N_URL/healthz` | `npx n8n` or `docker compose up -d n8n` |
| dd loop | `pgrep -f dd/main.py` | `python3 main.py` (launchd/systemd unit in `references/`) |

## Rules
- `/status` reply = `healthcheck.py --brief` output, nothing else, unless something is red — then one line per red item with the fix command.
- Disk < 10 % free → run `disk_cleanup.sh` (preview) and show Dhina the list. Never delete anything yourself: he runs `--clean` in his own terminal. The script only knows regenerable caches; passwords, Keychain, Chrome profile, Documents/Desktop/Downloads/Photos, Trash, `_Review_Before_Delete`, models and project folders are never on its list.
- Restart a service only when its check fails; say what was restarted.
- Backups run weekly (via `scheduler-reminders` `--action system-ops`) and before any `rules_guard.py update`. Backup contains memory + learning data → stays local, never uploaded to a public repo.
- Logs: `$DD_HOME/logs/dd.log` (rotate at 10 MB). Read the last 100 lines on a crash; grep the first `Traceback`.
