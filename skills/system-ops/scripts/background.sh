#!/usr/bin/env bash
# background.sh — run dd quietly in the background on macOS. Owner terminal only (dd itself is refused).
#   background.sh install [--with-7b]   one-time setup, runs detached at low priority; Mac notification when done
#   background.sh status                service state, free disk, last log lines
#   background.sh stop | start          pause / resume the background service
#   background.sh uninstall             remove the service (dd code and ~/.dd data stay)
#   background.sh --selftest
# The background service (examples/dd_daemon.py): reminders -> Mac notifications every 30 s.
# No microphone, no screen/app control, no LLM, nice 10 + launchd ProcessType=Background.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SELF="$REPO/skills/system-ops/scripts/background.sh"
DD_HOME="${DD_HOME:-$HOME/.dd}"
LABEL="com.dhina.dd"
PLIST="${DD_PLIST_DIR:-$HOME/Library/LaunchAgents}/$LABEL.plist"
LOG_DIR="$DD_HOME/logs"
DRY="${DD_BG_DRY:-}"
MIN_FREE_7B="${DD_MIN_FREE_7B:-12}"   # GB free needed before pulling the 7B model (4.7 GB model + room for macOS)

run() { if [ -n "$DRY" ]; then echo "DRY: $*"; else "$@"; fi; }
free_gb() { df -Pk "$HOME" | awk 'NR==2 {printf "%d", $4/1048576}'; }
notify() {
  [ -n "$DRY" ] && { echo "DRY notify: $1"; return 0; }
  command -v osascript >/dev/null 2>&1 && osascript -e "display notification \"$1\" with title \"dd\"" >/dev/null 2>&1
  return 0
}

guard() {
  if [ -n "${DD_AGENT:-}" ]; then
    echo "REFUSED: installing/removing the background service is owner-only — run it yourself in Terminal."
    exit 1
  fi
}

pip_install() {  # same pattern install.sh already uses on this Mac
  python3 -m pip install --quiet "$@" 2>/dev/null || python3 -m pip install --quiet --break-system-packages "$@"
}

set_env() {  # set KEY=VALUE in ~/.dd/.env (portable, no sed -i differences)
  mkdir -p "$DD_HOME"
  python3 - "$DD_HOME/.env" "$1" "$2" <<'EOF'
import re, sys
from pathlib import Path
p, k, v = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = p.read_text().splitlines() if p.exists() else []
out, done = [], False
for line in lines:
    if re.match(rf"^{re.escape(k)}=", line):
        out.append(f"{k}={v}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n")
EOF
}

write_plist() {
  local py; py="$(command -v python3)"
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  python3 - "$PLIST" "$py" "$REPO" "$DD_HOME" "$LOG_DIR" "$LABEL" <<'EOF'
import plistlib, sys
plist, py, repo, dd_home, logs, label = sys.argv[1:7]
data = {
    "Label": label,
    "ProgramArguments": [py, f"{repo}/examples/dd_daemon.py"],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},   # restart only after a crash
    "ThrottleInterval": 60,
    "ProcessType": "Background",              # macOS gives it the lowest CPU/IO priority
    "LowPriorityIO": True,
    "Nice": 10,
    "WorkingDirectory": repo,
    "StandardOutPath": f"{logs}/daemon.log",
    "StandardErrorPath": f"{logs}/daemon.log",
    "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                             "DD_HOME": dd_home, "PYTHONUNBUFFERED": "1"},
}
with open(plist, "wb") as f:
    plistlib.dump(data, f)
print(plist)
EOF
}

svc_load()   { run launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || run launchctl load -w "$PLIST"; }
svc_unload() { run launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || run launchctl unload -w "$PLIST" 2>/dev/null || true; }
svc_state()  { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "running" || echo "not running"; }

setup_job() {  # runs detached under nohup + nice; output -> ~/.dd/logs/setup.log
  local with7b="${1:-0}"
  echo "== dd background setup $(date) — free disk $(free_gb) GB =="
  echo "[1/4] voice: faster-whisper"
  if python3 -c 'import faster_whisper' 2>/dev/null; then echo "  already installed"
  elif pip_install faster-whisper; then echo "  ok"
  else echo "  FAILED (JARVIS text mode still works)"; fi

  echo "[2/4] whisper 'base' model (~150 MB, one-time download)"
  if python3 -c 'from faster_whisper import WhisperModel; WhisperModel("base", device="cpu", compute_type="int8")' 2>/dev/null
  then echo "  ok"; else echo "  FAILED (will retry on first JARVIS use)"; fi

  if [ "$with7b" = 1 ]; then
    echo "[3/4] brain: qwen2.5:7b"
    if [ "$(free_gb)" -lt "$MIN_FREE_7B" ]; then
      echo "  skipped: needs ${MIN_FREE_7B} GB free, have $(free_gb) GB — staying on qwen2.5:3b"
    elif ! command -v ollama >/dev/null 2>&1; then
      echo "  skipped: ollama not found"
    elif ollama pull qwen2.5:7b; then
      set_env DD_MODEL qwen2.5:7b; echo "  ok — DD_MODEL=qwen2.5:7b"
    else
      echo "  pull FAILED — staying on qwen2.5:3b"
    fi
  else
    echo "[3/4] brain: keeping qwen2.5:3b (light enough to run while you work on 8 GB RAM)"
  fi

  echo "[4/4] background service (reminders -> notifications)"
  write_plist >/dev/null && svc_unload && svc_load && echo "  ok — $(svc_state)"
  notify "dd setup mudinjiduchu. Status: bash skills/system-ops/scripts/background.sh status"
  echo "== done $(date) — free disk $(free_gb) GB =="
}

status() {
  echo "service:   $(svc_state)  ($PLIST)"
  echo "free disk: $(free_gb) GB"
  [ -f "$LOG_DIR/setup.log" ] && { echo "--- setup.log ---"; tail -n 10 "$LOG_DIR/setup.log"; }
  [ -f "$LOG_DIR/daemon.log" ] && { echo "--- daemon.log (last 5) ---"; tail -n 5 "$LOG_DIR/daemon.log"; }
  return 0
}

selftest() {
  local t out; t=$(mktemp -d)
  DD_HOME="$t/.dd"; LOG_DIR="$DD_HOME/logs"; PLIST="$t/LaunchAgents/$LABEL.plist"; DRY=1
  write_plist >/dev/null || { echo "plist write failed"; return 1; }
  python3 - "$PLIST" "$LABEL" <<'EOF' || { echo "plist invalid"; return 1; }
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
assert d["Label"] == sys.argv[2] and d["ProcessType"] == "Background" and d["Nice"] == 10, d
assert d["ProgramArguments"][1].endswith("examples/dd_daemon.py") and d["KeepAlive"] == {"SuccessfulExit": False}, d
EOF
  [ -f "$REPO/examples/dd_daemon.py" ] || { echo "daemon script missing"; return 1; }
  set_env DD_MODEL qwen2.5:7b; set_env DD_MODEL qwen2.5:3b; set_env OTHER 1
  [ "$(grep -c '^DD_MODEL=' "$DD_HOME/.env")" = 1 ] && grep -q '^DD_MODEL=qwen2.5:3b$' "$DD_HOME/.env" \
    && grep -q '^OTHER=1$' "$DD_HOME/.env" || { echo "set_env failed"; return 1; }
  out=$(DD_AGENT=1 bash "$SELF" install 2>&1); echo "$out" | grep -q REFUSED || { echo "agent not refused: $out"; return 1; }
  out=$(DD_AGENT=1 bash "$SELF" uninstall 2>&1); echo "$out" | grep -q REFUSED || { echo "agent uninstall not refused"; return 1; }
  out=$(svc_load); echo "$out" | grep -q "DRY: launchctl bootstrap gui/" || { echo "load cmd wrong: $out"; return 1; }
  rm -rf "$t"; echo "background selftest ok (plist valid, .env edit, agent refused, dry launchctl)"
}

case "${1:-}" in
  install)
    guard
    with7b=0; [ "${2:-}" = "--with-7b" ] && with7b=1
    mkdir -p "$LOG_DIR"
    nohup nice -n 15 bash "$SELF" __job "$with7b" > "$LOG_DIR/setup.log" 2>&1 &
    echo "dd setup background la odudhu (low priority) — un Mac ah normal ah use pannu."
    echo "Mudinja Mac notification varum. Progress paakka: bash skills/system-ops/scripts/background.sh status"
    ;;
  __job) guard; setup_job "${2:-0}" ;;
  status) status ;;
  stop) guard; svc_unload; echo "background service stopped (start: background.sh start)" ;;
  start) guard; [ -f "$PLIST" ] || write_plist >/dev/null; svc_load; echo "background service started" ;;
  uninstall) guard; svc_unload; rm -f "$PLIST"; echo "background service removed (dd code + ~/.dd data kept)" ;;
  --selftest) selftest ;;
  *) sed -n '2,10p' "$SELF"; exit 2 ;;
esac
