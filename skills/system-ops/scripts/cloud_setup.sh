#!/usr/bin/env bash
# cloud_setup.sh — run dd 24/7 on a free cloud server (Oracle Always Free, Ubuntu 22.04/24.04, ARM or x86).
# Run it ON the server as the default sudo user (ubuntu). Your Mac does nothing.
#   bash cloud_setup.sh install [--model qwen2.5:3b]   one-time: packages, Ollama + model, dd, Telegram token + pairing, service
#   bash cloud_setup.sh update                          git pull + deps + restart
#   bash cloud_setup.sh status                          services, loaded model, RAM, disk, last log lines
#   bash cloud_setup.sh token                           re-enter the Telegram bot token
#   bash cloud_setup.sh pair                            re-link your Telegram account
#   bash cloud_setup.sh --selftest
# Safety: dd runs as its own user "dd" (no sudo, NoNewPrivileges, read-only system dirs, no access to /home).
# Nothing listens on the internet: Telegram is polled outbound, Ollama stays on 127.0.0.1:11434.
# Never open port 11434 in the Oracle security list — Ollama has no password.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SELF="$REPO/skills/system-ops/scripts/cloud_setup.sh"
DD_USER="${DD_USER:-dd}"
DD_BASE="${DD_BASE:-/var/lib/dd}"     # home of the dd user
DD_HOME_SRV="$DD_BASE/.dd"            # dd data: memory, rules, reminders, .env
VENV="${DD_VENV:-/opt/dd-venv}"
UNIT_DIR="${DD_UNIT_DIR:-/etc/systemd/system}"
UNIT="$UNIT_DIR/dd-telegram.service"
DROPIN="$UNIT_DIR/ollama.service.d/dd.conf"
DRY="${DD_SETUP_DRY:-}"
TZ_NAME="${DD_TZ:-Asia/Kolkata}"
MODEL="qwen2.5:3b"                    # fits 2 OCPU / 12 GB; --model qwen2.5:7b is smarter but ~2-3x slower
NUM_CTX=16384
TEST_LOCAL=""                         # selftest: edit .env as the current user instead of sudo -u dd

run()  { if [ -n "$DRY" ]; then echo "DRY: $*"; else "$@"; fi; }
die()  { echo "ERROR: $*"; exit 1; }
step() { echo; echo "[$1/8] $2"; }

as_dd() {  # run a command as the dd user with its data dir
  if [ -n "$TEST_LOCAL" ]; then env DD_HOME="$DD_HOME_SRV" "$@"
  else run sudo -u "$DD_USER" -H env DD_HOME="$DD_HOME_SRV" "$@"; fi
}

guard() {
  if [ -n "${DD_AGENT:-}" ]; then
    echo "REFUSED: server setup is owner-only — run it yourself over SSH."
    exit 1
  fi
  if [ "${DD_FAKE_UNAME:-$(uname -s)}" = "Darwin" ]; then
    echo "Idhu cloud server (Linux) ku mattum. Mac la: bash skills/system-ops/scripts/background.sh install"
    exit 1
  fi
  [ -n "$DRY" ] && return 0
  command -v systemctl >/dev/null 2>&1 || die "systemd illa — Ubuntu 22.04/24.04 server venum"
  command -v apt-get >/dev/null 2>&1 || die "apt-get illa — Ubuntu image use pannu"
}

# .env edits: value comes on stdin (a token never shows up in `ps`), file stays chmod 600
PY_SET_ENV='
import os, re, sys
from pathlib import Path
p, k = Path(sys.argv[1]), sys.argv[2]
v = sys.stdin.read().strip()
lines = p.read_text().splitlines() if p.exists() else []
out, done = [], False
for line in lines:
    if re.match("^" + re.escape(k) + "=", line):
        out.append(k + "=" + v); done = True
    else:
        out.append(line)
if not done:
    out.append(k + "=" + v)
p.write_text("\n".join(out) + "\n")
os.chmod(p, 0o600)
'
env_set() {
  if [ -n "$DRY" ]; then echo "DRY: set $1 in $DD_HOME_SRV/.env"; return 0; fi
  if [ -n "$TEST_LOCAL" ]; then printf '%s' "$2" | python3 -c "$PY_SET_ENV" "$DD_HOME_SRV/.env" "$1"
  else printf '%s' "$2" | sudo -u "$DD_USER" -H "$VENV/bin/python" -c "$PY_SET_ENV" "$DD_HOME_SRV/.env" "$1"; fi
}
env_get() {
  [ -n "$DRY" ] && return 0
  local f="$DD_HOME_SRV/.env"
  if [ -n "$TEST_LOCAL" ]; then grep -m1 "^$1=" "$f" 2>/dev/null | cut -d= -f2-
  else sudo -u "$DD_USER" grep -m1 "^$1=" "$f" 2>/dev/null | cut -d= -f2-; fi
}

write_root_file() {  # write_root_file PATH CONTENT
  if [ -n "$DRY" ]; then echo "DRY: write $1"; return 0; fi
  sudo mkdir -p "$(dirname "$1")" && printf '%s\n' "$2" | sudo tee "$1" >/dev/null
}

ollama_dropin() {
  cat <<EOF
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_CONTEXT_LENGTH=$NUM_CTX"
EOF
}

unit_text() {
  cat <<EOF
[Unit]
Description=dd personal AI on Telegram
After=network-online.target ollama.service
Wants=network-online.target ollama.service

[Service]
Type=simple
User=$DD_USER
Group=$DD_USER
WorkingDirectory=$DD_BASE
Environment=DD_HOME=$DD_HOME_SRV
Environment=TZ=$TZ_NAME
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python $REPO/examples/dd_telegram.py
Restart=always
RestartSec=10
RestartPreventExitStatus=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
}

wait_ollama() {
  [ -n "$DRY" ] && return 0

  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && return 0
    sleep 2
  done
  die "Ollama start aagala — paaru: sudo journalctl -u ollama -n 30"
}

pip_deps() {
  run sudo "$VENV/bin/pip" install -q --root-user-action=ignore --upgrade pip
  run sudo "$VENV/bin/pip" install -q --root-user-action=ignore -r "$REPO/requirements.txt" faster-whisper \
    || die "python packages install aagala"
}

ask_token() {
  if [ -n "$DRY" ]; then echo "DRY: ask for the Telegram bot token"; return 0; fi
  [ -t 0 ] || die "token ku interactive terminal venum: bash $SELF token"
  echo "  Telegram la @BotFather open pannu -> /newbot -> peru kudu -> adhu kudukkura token ah copy pannu."
  local tok=""
  while :; do
    read -rsp "  Bot token paste pannu (screen la theriyaadhu): " tok || die "token kidaikkala — thirumba: bash $SELF token"
    echo
    [[ "$tok" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]] && break
    echo "  Format thappu (example: 123456789:AAF...). Thirumba paste pannu."
  done
  env_set TELEGRAM_BOT_TOKEN "$tok"
  echo "  token saved (chmod 600, dd user mattum padikka mudiyum)"
}

do_pair() {
  if [ -n "$DRY" ]; then echo "DRY: pair your Telegram account (/pair CODE)"; return 0; fi
  sudo systemctl stop dd-telegram 2>/dev/null || true   # only one Telegram poller at a time
  as_dd "$VENV/bin/python" "$REPO/examples/dd_telegram.py" --pair || die "pairing aagala — thirumba: bash $SELF pair"
}

start_service() {
  write_root_file "$UNIT" "$(unit_text)"
  run sudo systemctl daemon-reload
  run sudo systemctl enable --now dd-telegram
  run sudo systemctl restart dd-telegram
}

install_all() {
  guard
  local model="$MODEL"
  [ "${1:-}" = "--model" ] && [ -n "${2:-}" ] && model="$2"
  echo "== dd cloud setup — $(date) =="

  step 1 "system packages (git, python venv, ffmpeg, sqlite)"
  run sudo apt-get update -y -q
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -q git python3-venv python3-pip ffmpeg sqlite3 curl ca-certificates \
    || die "apt install aagala"

  step 2 "timezone $TZ_NAME (reminders, 'mani enna')"
  run sudo timedatectl set-timezone "$TZ_NAME"

  step 3 "Ollama — local brain, listens on 127.0.0.1 only"
  if [ -n "$DRY" ]; then echo "DRY: curl -fsSL https://ollama.com/install.sh | sh"
  elif ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh || die "Ollama install aagala"
  fi
  write_root_file "$DROPIN" "$(ollama_dropin)"
  run sudo systemctl daemon-reload
  run sudo systemctl restart ollama
  wait_ollama

  step 4 "model $model (one-time download)"
  run ollama pull "$model" || die "model download aagala — thirumba: ollama pull $model"

  step 5 "dd user + python packages (venv $VENV) — 5-10 nimisham aagalaam"
  if [ -n "$DRY" ] || ! id "$DD_USER" >/dev/null 2>&1; then
    run sudo useradd --system --create-home --home-dir "$DD_BASE" --shell /usr/sbin/nologin "$DD_USER"
  fi
  if [ -z "$DRY" ] && ! sudo -u "$DD_USER" test -r "$REPO/examples/dd_telegram.py"; then
    die "dd user $REPO ah padikka mudiyala. Repo ah /opt la clone pannu: sudo git clone https://github.com/dhayalandhina/personalai /opt/dd"
  fi
  if [ -n "$DRY" ] || [ ! -x "$VENV/bin/python" ]; then run sudo python3 -m venv "$VENV"; fi
  pip_deps

  step 6 "dd data in $DD_HOME_SRV (rules lock, briefing, settings, voice model)"
  as_dd mkdir -p "$DD_HOME_SRV"/{rules,learning,plans,tmp,video,voices,logs,inbox,mail/drafts}
  as_dd chmod 700 "$DD_HOME_SRV"
  as_dd "$VENV/bin/python" "$REPO/skills/rules-guard/scripts/rules_guard.py" init
  as_dd "$VENV/bin/python" "$REPO/skills/daily-briefing/scripts/briefing.py" --init-config >/dev/null
  if [ -n "$DRY" ] || ! sudo -u "$DD_USER" test -f "$DD_HOME_SRV/.env"; then
    as_dd cp "$REPO/.env.example" "$DD_HOME_SRV/.env"
    as_dd chmod 600 "$DD_HOME_SRV/.env"
  fi
  env_set DD_MODEL "$model"
  env_set DD_HOST server
  env_set DD_KEEP_ALIVE -1
  env_set DD_NUM_CTX "$NUM_CTX"
  env_set DD_TZ "$TZ_NAME"
  env_set DD_STT_MODEL base
  as_dd "$VENV/bin/python" -c 'from faster_whisper import WhisperModel; WhisperModel("base", device="cpu", compute_type="int8")' \
    || echo "  voice model download aagala — first voice note la thirumba try pannum"

  step 7 "Telegram (token + link your account)"
  [ -n "$(env_get TELEGRAM_BOT_TOKEN)" ] || ask_token
  [ -n "$(env_get TELEGRAM_OWNER_ID)" ] || do_pair

  step 8 "dd service (auto-start on boot, restarts itself)"
  start_service
  echo
  echo "== done — Telegram la un bot ku 'hi' anuppu. Status: bash $SELF status =="
}

status() {
  echo "ollama:      $(systemctl is-active ollama 2>/dev/null)"
  echo "dd-telegram: $(systemctl is-active dd-telegram 2>/dev/null)"
  if command -v ollama >/dev/null 2>&1; then echo "--- loaded model ---"; ollama ps 2>/dev/null; fi
  echo "--- memory ---"; free -h 2>/dev/null | head -2
  echo "--- disk ---"; df -h / | tail -1
  echo "--- dd log (last 12) ---"; sudo journalctl -u dd-telegram -n 12 --no-pager 2>/dev/null
  return 0
}

update() {
  guard
  run sudo git -C "$REPO" pull --ff-only || die "git pull aagala"
  pip_deps
  start_service
  echo "updated: $(sudo git -C "$REPO" log --oneline -1 2>/dev/null)"
}

selftest() {
  local t out want
  t=$(mktemp -d)
  out=$(unit_text)
  for want in "User=$DD_USER" "ExecStart=$VENV/bin/python $REPO/examples/dd_telegram.py" "Environment=DD_HOME=$DD_HOME_SRV" \
              "NoNewPrivileges=true" "ProtectSystem=full" "ProtectHome=true" "RestartPreventExitStatus=2"; do
    echo "$out" | grep -qF "$want" || { echo "unit missing: $want"; return 1; }
  done
  out=$(ollama_dropin)
  echo "$out" | grep -q 'OLLAMA_KEEP_ALIVE=-1' || { echo "dropin: keep-alive missing"; return 1; }
  echo "$out" | grep -q 'OLLAMA_HOST' && { echo "dropin must not expose Ollama"; return 1; }

  DD_HOME_SRV="$t/.dd"; TEST_LOCAL=1; mkdir -p "$DD_HOME_SRV"
  printf 'DD_MODEL=old\nOTHER=1\n' > "$DD_HOME_SRV/.env"
  env_set DD_MODEL qwen2.5:3b
  env_set TELEGRAM_BOT_TOKEN 'not-a-real-token'
  [ "$(grep -c '^DD_MODEL=' "$DD_HOME_SRV/.env")" = 1 ] && grep -q '^DD_MODEL=qwen2.5:3b$' "$DD_HOME_SRV/.env" \
    && grep -q '^OTHER=1$' "$DD_HOME_SRV/.env" || { echo ".env edit failed"; return 1; }
  [ "$(env_get TELEGRAM_BOT_TOKEN)" = 'not-a-real-token' ] || { echo "env_get failed"; return 1; }
  [ "$(stat -c %a "$DD_HOME_SRV/.env" 2>/dev/null || stat -f %Lp "$DD_HOME_SRV/.env")" = 600 ] || { echo ".env not 600"; return 1; }

  out=$(DD_AGENT=1 bash "$SELF" install 2>&1); echo "$out" | grep -q REFUSED || { echo "agent not refused: $out"; return 1; }
  out=$(env -u DD_AGENT DD_FAKE_UNAME=Darwin bash "$SELF" install 2>&1)
  echo "$out" | grep -q "background.sh" || { echo "Mac not refused: $out"; return 1; }
  out=$(env -u DD_AGENT DD_SETUP_DRY=1 DD_UNIT_DIR="$t/units" bash "$SELF" install --model qwen2.5:7b 2>&1)
  for want in "DRY: sudo apt-get update" "DRY: sudo timedatectl set-timezone Asia/Kolkata" \
              "DRY: curl -fsSL https://ollama.com/install.sh | sh" "DRY: write $t/units/ollama.service.d/dd.conf" \
              "DRY: ollama pull qwen2.5:7b" "DRY: sudo useradd --system" "DRY: set DD_HOST in" "DRY: set DD_MODEL in" \
              "DRY: pair your Telegram account" "DRY: sudo systemctl enable --now dd-telegram" "== done"; do
    echo "$out" | grep -qF "$want" || { echo "dry install missing: $want"; echo "$out" | tail -20; return 1; }
  done
  rm -rf "$t"
  echo "cloud_setup selftest ok (hardened unit, Ollama local-only, .env edit 600, owner-only, Mac refused, dry install)"
}

case "${1:-}" in
  install) shift; install_all "$@" ;;
  update) update ;;
  status) status ;;
  token) guard; ask_token; sudo systemctl restart dd-telegram 2>/dev/null || true ;;
  pair) guard; do_pair; start_service ;;
  --selftest) selftest ;;
  *) sed -n '2,12p' "$SELF"; exit 2 ;;
esac
