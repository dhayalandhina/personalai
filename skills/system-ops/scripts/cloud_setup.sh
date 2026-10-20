#!/usr/bin/env bash
# cloud_setup.sh — run dd 24/7 on a free cloud server (Oracle Always Free, Ubuntu 22.04/24.04, ARM or x86).
# Run it ON the server (as the default sudo user, or as root from cloud-init). Your Mac does nothing.
#   bash cloud_setup.sh install [--model qwen2.5:3b]   one-time: packages, Ollama + model, dd, Telegram token + pairing, services
#   bash cloud_setup.sh update                          git pull + deps + restart
#   bash cloud_setup.sh status                          services, pairing, loaded model, RAM, disk, last log lines
#   bash cloud_setup.sh token                           re-enter the Telegram bot token
#   bash cloud_setup.sh pair                            re-link your Telegram account (prints a /pair CODE)
#   bash cloud_setup.sh cloud-init                      print a no-SSH first-boot script for Oracle's "Initialization script" box
#   bash cloud_setup.sh --selftest
# Non-interactive (cloud-init): DD_TELEGRAM_TOKEN=... DD_PAIR_SECRET=... bash cloud_setup.sh install
#   -> no prompts; the bot then waits for "/pair <secret>" from your Telegram (works even if sent earlier).
#
# Layout — what defines dd is root-owned and read-only to dd, so even a hijacked dd cannot change it:
#   /etc/dd/rules/            locked rules (root, 444)              DD_RULES_DIR
#   /etc/dd/dd.env            settings: model, Ollama URL, tz…      root 644, loaded by systemd
#   /etc/dd/dd-telegram.env   bot token + owner id + pair secret    dd-pair:root 600, loaded by systemd; dd cannot read it
#   /var/lib/dd/.dd/          dd's data: memory, reminders, plans, learning, inbox   (user dd)
# Services: dd-telegram (the bot, user dd) · dd-pair (pairing only, user dd-pair, may write just the secrets file)
#           dd-telegram.path (starts the bot when pairing completes) · dd-firewall (dd may not read cloud metadata)
# Nothing listens on the internet: Telegram is polled outbound, Ollama stays on 127.0.0.1:11434.
# Never open port 11434 in the Oracle security list — Ollama has no password.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SELF="$REPO/skills/system-ops/scripts/cloud_setup.sh"
DD_USER="${DD_USER:-dd}"
PAIR_USER="${DD_PAIR_USER:-dd-pair}"
DD_BASE="${DD_BASE:-/var/lib/dd}"     # home of the dd user
DD_HOME_SRV="$DD_BASE/.dd"            # dd data: memory, reminders, learning, plans, inbox
ETC_DD="${DD_ETC:-/etc/dd}"
RULES_DIR="$ETC_DD/rules"
SETTINGS="$ETC_DD/dd.env"
SECRETS="$ETC_DD/dd-telegram.env"
VENV="${DD_VENV:-/opt/dd-venv}"
UNIT_DIR="${DD_UNIT_DIR:-/etc/systemd/system}"
UNIT="$UNIT_DIR/dd-telegram.service"
DROPIN="$UNIT_DIR/ollama.service.d/dd.conf"
FW_SCRIPT="${DD_FW_SCRIPT:-/usr/local/sbin/dd-firewall}"
IMDS="169.254.169.254"                # cloud metadata service (holds the first-boot script) — dd must never read it
DRY="${DD_SETUP_DRY:-}"
TZ_NAME="${DD_TZ:-Asia/Kolkata}"
MODEL="qwen2.5:3b"                    # fits 2 OCPU / 12 GB; --model qwen2.5:7b is smarter but ~2-3x slower
NUM_CTX=16384
TEST_LOCAL=""                         # selftest: plain files as the current user instead of sudo

run()  { if [ -n "$DRY" ]; then echo "DRY: $*"; else "$@"; fi; }
die()  { echo "ERROR: $*"; exit 1; }
step() { echo; echo "[$1/8] $2"; }

as_dd() {  # run a command as the dd user with its data dir (HOME=/var/lib/dd for the whisper model cache)
  if [ -n "$TEST_LOCAL" ]; then env DD_HOME="$DD_HOME_SRV" "$@"
  else run sudo -u "$DD_USER" -H env DD_HOME="$DD_HOME_SRV" DD_RULES_DIR="$RULES_DIR" "$@"; fi
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

# KEY=VALUE edits: the value arrives on stdin (never on a command line, so never in `ps`); mode is set by the caller
PY_SET_ENV='
import os, re, sys
from pathlib import Path
p, k, mode = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3], 8)
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
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("\n".join(out) + "\n")
os.chmod(p, mode)
'
kv_set() {  # kv_set FILE KEY VALUE MODE [OWNER]  — root-owned files; OWNER optional (dd-pair for the secrets file)
  local f="$1" k="$2" v="$3" mode="$4" owner="${5:-}"
  if [ -n "$DRY" ]; then echo "DRY: set $k in $f"; return 0; fi
  if [ -n "$TEST_LOCAL" ]; then printf '%s' "$v" | python3 -c "$PY_SET_ENV" "$f" "$k" "$mode"
  else
    printf '%s' "$v" | sudo python3 -c "$PY_SET_ENV" "$f" "$k" "$mode" || return 1
    [ -n "$owner" ] && sudo chown "$owner:root" "$f"
  fi
  return 0
}
kv_get() {  # kv_get FILE KEY
  [ -n "$DRY" ] && return 0
  if [ -n "$TEST_LOCAL" ]; then grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2-
  else sudo grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2-; fi
  return 0
}
setting_set() { kv_set "$SETTINGS" "$1" "$2" 644; }
secret_set()  { kv_set "$SECRETS" "$1" "$2" 600 "$PAIR_USER"; }
secret_get()  { kv_get "$SECRETS" "$1"; }

write_root_file() {  # write_root_file PATH CONTENT [MODE]
  if [ -n "$DRY" ]; then echo "DRY: write $1"; return 0; fi
  sudo mkdir -p "$(dirname "$1")" && printf '%s\n' "$2" | sudo tee "$1" >/dev/null && sudo chmod "${3:-644}" "$1"
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

unit_text() {  # the bot
  cat <<EOF
[Unit]
Description=dd personal AI on Telegram
After=network-online.target ollama.service dd-firewall.service
Wants=network-online.target ollama.service dd-firewall.service

[Service]
Type=simple
User=$DD_USER
Group=$DD_USER
WorkingDirectory=$DD_BASE
EnvironmentFile=$SETTINGS
EnvironmentFile=$SECRETS
Environment=DD_HOME=$DD_HOME_SRV
Environment=DD_RULES_DIR=$RULES_DIR
Environment=DD_SECRETS_FILE=$SECRETS
Environment=TZ=$TZ_NAME
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python $REPO/examples/dd_telegram.py
Restart=always
RestartSec=10
SuccessExitStatus=2
RestartPreventExitStatus=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
}

path_unit_text() {  # pairing writes the owner id -> the file changes -> the bot starts
  cat <<EOF
[Unit]
Description=start dd when Telegram pairing completes

[Path]
PathChanged=$SECRETS
Unit=dd-telegram.service

[Install]
WantedBy=multi-user.target
EOF
}

pair_unit_text() {  # pairing: its own user, read-only system, may write exactly one file
  cat <<EOF
[Unit]
Description=dd Telegram pairing (waits for /pair <secret>, exits when done)
After=network-online.target dd-firewall.service
Wants=network-online.target dd-firewall.service

[Service]
Type=oneshot
User=$PAIR_USER
Group=$PAIR_USER
EnvironmentFile=$SECRETS
Environment=DD_SECRETS_FILE=$SECRETS
Environment=DD_HOME=/run/dd-pair-nohome
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python $REPO/examples/dd_telegram.py --claim
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$SECRETS
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
}

firewall_script_text() {
  cat <<EOF
#!/bin/sh
# dd-firewall — dd (and the pairing user) must never read the cloud metadata service: it serves the
# first-boot script, which carried the bot token. Idempotent; runs at boot after any persistent firewall.
for u in $DD_USER $PAIR_USER; do
  id "\$u" >/dev/null 2>&1 || continue
  iptables -w -C OUTPUT -d $IMDS -m owner --uid-owner "\$u" -j REJECT 2>/dev/null \\
    || iptables -w -I OUTPUT 1 -d $IMDS -m owner --uid-owner "\$u" -j REJECT
done
EOF
}

firewall_unit_text() {
  cat <<EOF
[Unit]
Description=dd firewall (block cloud metadata for the dd users)
After=network-pre.target netfilter-persistent.service
Before=dd-telegram.service dd-pair.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$FW_SCRIPT

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
  secret_set TELEGRAM_BOT_TOKEN "$tok" || die "token save aagala"
  echo "  token saved in $SECRETS (600, dd-pair user mattum padikka mudiyum — dd kooda padikka mudiyaadhu)"
}

do_pair() {  # interactive (SSH): prints /pair CODE; the owner id lands in the root-owned secrets file
  if [ -n "$DRY" ]; then echo "DRY: pair your Telegram account (/pair CODE)"; return 0; fi
  sudo systemctl stop dd-telegram dd-pair 2>/dev/null || true   # only one Telegram poller at a time
  sudo env DD_SECRETS_FILE="$SECRETS" DD_HOME=/run/dd-pair-nohome "$VENV/bin/python" "$REPO/examples/dd_telegram.py" --pair \
    || die "pairing aagala — thirumba: bash $SELF pair"
  sudo chown "$PAIR_USER:root" "$SECRETS"; sudo chmod 600 "$SECRETS"
}

ensure_users() {
  if [ -n "$DRY" ] || ! id "$DD_USER" >/dev/null 2>&1; then
    run sudo useradd --system --create-home --home-dir "$DD_BASE" --shell /usr/sbin/nologin "$DD_USER"
  fi
  if [ -n "$DRY" ] || ! id "$PAIR_USER" >/dev/null 2>&1; then
    run sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$PAIR_USER"
  fi
}

start_service() {
  write_root_file "$UNIT" "$(unit_text)"
  write_root_file "$UNIT_DIR/dd-telegram.path" "$(path_unit_text)"
  write_root_file "$UNIT_DIR/dd-pair.service" "$(pair_unit_text)"
  write_root_file "$UNIT_DIR/dd-firewall.service" "$(firewall_unit_text)"
  write_root_file "$FW_SCRIPT" "$(firewall_script_text)" 755
  run sudo systemctl daemon-reload
  run sudo systemctl enable --now dd-firewall dd-telegram.path dd-pair dd-telegram
  run sudo systemctl restart dd-telegram
}

install_all() {
  guard
  local model="$MODEL"
  [ "${1:-}" = "--model" ] && [ -n "${2:-}" ] && model="$2"
  echo "== dd cloud setup — $(date) =="

  step 1 "system packages (git, python venv, ffmpeg, sqlite, iptables)"
  # first boot: unattended-upgrades often holds the dpkg lock for a few minutes — wait instead of failing
  run sudo apt-get -o DPkg::Lock::Timeout=900 update -y -q
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 install -y -q \
    git python3-venv python3-pip ffmpeg sqlite3 curl ca-certificates iptables || die "apt install aagala"

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

  step 5 "users dd + dd-pair, python packages (venv $VENV) — 5-10 nimisham aagalaam"
  ensure_users
  if [ -z "$DRY" ] && ! sudo -u "$DD_USER" test -r "$REPO/examples/dd_telegram.py"; then
    die "dd user $REPO ah padikka mudiyala. Repo ah /opt la clone pannu: sudo git clone https://github.com/dhayalandhina/personalai /opt/dd"
  fi
  if [ -n "$DRY" ] || [ ! -x "$VENV/bin/python" ]; then run sudo python3 -m venv "$VENV"; fi
  pip_deps

  step 6 "rules + settings in $ETC_DD (root-owned), dd data in $DD_HOME_SRV, voice model"
  run sudo mkdir -p "$RULES_DIR"
  run sudo env DD_RULES_DIR="$RULES_DIR" DD_HOME="$DD_HOME_SRV" "$VENV/bin/python" "$REPO/skills/rules-guard/scripts/rules_guard.py" init
  run sudo chmod 755 "$ETC_DD" "$RULES_DIR"
  setting_set DD_HOST server
  setting_set DD_MODEL "$model"
  setting_set OLLAMA_URL http://127.0.0.1:11434
  setting_set DD_KEEP_ALIVE -1
  setting_set DD_NUM_CTX "$NUM_CTX"
  setting_set DD_TZ "$TZ_NAME"
  setting_set DD_STT_MODEL base
  as_dd mkdir -p "$DD_HOME_SRV"/{learning,plans,tmp,video,voices,logs,inbox,mail/drafts}
  as_dd chmod 700 "$DD_HOME_SRV"
  as_dd "$VENV/bin/python" "$REPO/skills/daily-briefing/scripts/briefing.py" --init-config >/dev/null
  as_dd "$VENV/bin/python" -c 'from faster_whisper import WhisperModel; WhisperModel("base", device="cpu", compute_type="int8")' \
    || echo "  voice model download aagala — first voice note la thirumba try pannum"

  step 7 "Telegram (token + link your account)"
  local waiting_pair=""
  [ -n "${DD_TELEGRAM_TOKEN:-}" ] && { secret_set TELEGRAM_BOT_TOKEN "$DD_TELEGRAM_TOKEN" || die "token save aagala"; }
  [ -n "$(secret_get TELEGRAM_BOT_TOKEN)" ] || [ -n "${DD_TELEGRAM_TOKEN:-}" ] || ask_token
  if [ -z "$(secret_get TELEGRAM_OWNER_ID)" ]; then
    if [ -n "${DD_PAIR_SECRET:-}" ]; then
      secret_set DD_PAIR_SECRET "$DD_PAIR_SECRET" || die "pair secret save aagala"
      waiting_pair=1
      echo "  pairing: the bot accepts /pair <your secret> from your Telegram (no terminal needed)"
    else
      do_pair
    fi
  fi

  step 8 "services (auto-start on boot, restart on crash, firewall for cloud metadata)"
  start_service
  echo
  if [ -n "$waiting_pair" ]; then
    echo "== done — Telegram la un bot ku /pair <secret> anuppu. Status: bash $SELF status =="
  else
    echo "== done — Telegram la un bot ku 'hi' anuppu. Status: bash $SELF status =="
  fi
}

status() {
  local owner; owner="$(secret_get TELEGRAM_OWNER_ID)"
  echo "ollama:      $(systemctl is-active ollama 2>/dev/null)"
  echo "dd-telegram: $(systemctl is-active dd-telegram 2>/dev/null)   dd-pair: $(systemctl is-active dd-pair 2>/dev/null)   firewall: $(systemctl is-active dd-firewall 2>/dev/null)"
  if [ -n "$owner" ]; then echo "paired:      yes (Telegram id $owner)"
  else echo "paired:      no — Telegram la bot ku /pair <secret> anuppu (illa: bash $SELF pair)"; fi
  if command -v ollama >/dev/null 2>&1; then echo "--- loaded model ---"; ollama ps 2>/dev/null; fi
  echo "--- memory ---"; free -h 2>/dev/null | head -2
  echo "--- disk ---"; df -h / | tail -1
  echo "--- dd log (last 12) ---"; sudo journalctl -u dd-telegram -u dd-pair -n 12 --no-pager 2>/dev/null
  return 0
}

update() {
  guard
  run sudo git -C "$REPO" pull --ff-only || die "git pull aagala"
  pip_deps
  start_service
  echo "updated: $(sudo git -C "$REPO" log --oneline -1 2>/dev/null)"
}

cloud_init_text() {  # cloud_init_text SECRET -> first-boot script (runs once as root; log /var/log/dd-setup.log)
  cat <<EOF
#!/bin/bash
TOKEN='PASTE_BOT_TOKEN_HERE'
PAIR_SECRET='$1'
export HOME=/root
exec >>/var/log/dd-setup.log 2>&1
chmod 600 /var/log/dd-setup.log
apt-get -o DPkg::Lock::Timeout=900 update -y
apt-get -o DPkg::Lock::Timeout=900 install -y git
[ -d /opt/dd/.git ] || git clone https://github.com/dhayalandhina/personalai /opt/dd
DD_TELEGRAM_TOKEN="\$TOKEN" DD_PAIR_SECRET="\$PAIR_SECRET" bash /opt/dd/skills/system-ops/scripts/cloud_setup.sh install
EOF
}

selftest() {
  local t out want
  t=$(mktemp -d)
  out=$(unit_text)
  for want in "User=$DD_USER" "ExecStart=$VENV/bin/python $REPO/examples/dd_telegram.py" "Environment=DD_HOME=$DD_HOME_SRV" \
              "EnvironmentFile=$SETTINGS" "EnvironmentFile=$SECRETS" "Environment=DD_RULES_DIR=$RULES_DIR" \
              "NoNewPrivileges=true" "ProtectSystem=full" "ProtectHome=true" "RestartPreventExitStatus=2" "SuccessExitStatus=2"; do
    echo "$out" | grep -qF "$want" || { echo "unit missing: $want"; return 1; }
  done
  out=$(pair_unit_text)
  for want in "User=$PAIR_USER" "dd_telegram.py --claim" "ProtectSystem=strict" "ReadWritePaths=$SECRETS" "Type=oneshot"; do
    echo "$out" | grep -qF -- "$want" || { echo "pair unit missing: $want"; return 1; }
  done
  echo "$out" | grep -q "DD_HOME=$DD_HOME_SRV" && { echo "pair unit must not point at dd's data"; return 1; }
  path_unit_text | grep -qF "PathChanged=$SECRETS" || { echo "path unit wrong"; return 1; }
  out=$(firewall_script_text); echo "$out" | bash -n || { echo "firewall script syntax"; return 1; }
  echo "$out" | grep -qF -- "--uid-owner \"\$u\" -j REJECT" && echo "$out" | grep -qF "$IMDS" || { echo "firewall rule wrong"; return 1; }
  out=$(ollama_dropin)
  echo "$out" | grep -q 'OLLAMA_KEEP_ALIVE=-1' || { echo "dropin: keep-alive missing"; return 1; }
  echo "$out" | grep -q 'OLLAMA_HOST' && { echo "dropin must not expose Ollama"; return 1; }

  ETC_DD="$t/etc"; SETTINGS="$ETC_DD/dd.env"; SECRETS="$ETC_DD/dd-telegram.env"; TEST_LOCAL=1
  setting_set DD_MODEL old; setting_set OTHER 1; setting_set DD_MODEL qwen2.5:3b
  secret_set TELEGRAM_BOT_TOKEN 'not-a-real-token'
  [ "$(grep -c '^DD_MODEL=' "$SETTINGS")" = 1 ] && grep -q '^DD_MODEL=qwen2.5:3b$' "$SETTINGS" \
    && grep -q '^OTHER=1$' "$SETTINGS" || { echo "settings edit failed"; return 1; }
  [ "$(secret_get TELEGRAM_BOT_TOKEN)" = 'not-a-real-token' ] || { echo "secret_get failed"; return 1; }
  [ "$(stat -c %a "$SECRETS" 2>/dev/null || stat -f %Lp "$SECRETS")" = 600 ] || { echo "secrets file not 600"; return 1; }
  [ "$(stat -c %a "$SETTINGS" 2>/dev/null || stat -f %Lp "$SETTINGS")" = 644 ] || { echo "settings file not 644"; return 1; }
  grep -q TELEGRAM "$SETTINGS" && { echo "token leaked into settings"; return 1; }

  out=$(DD_AGENT=1 bash "$SELF" install 2>&1); echo "$out" | grep -q REFUSED || { echo "agent not refused: $out"; return 1; }
  out=$(env -u DD_AGENT DD_FAKE_UNAME=Darwin bash "$SELF" install 2>&1)
  echo "$out" | grep -q "background.sh" || { echo "Mac not refused: $out"; return 1; }
  out=$(env -u DD_AGENT DD_SETUP_DRY=1 DD_UNIT_DIR="$t/units" bash "$SELF" install --model qwen2.5:7b 2>&1)
  for want in "DRY: sudo apt-get -o DPkg::Lock::Timeout=900 update" "DRY: sudo timedatectl set-timezone Asia/Kolkata" \
              "DRY: curl -fsSL https://ollama.com/install.sh | sh" "DRY: write $t/units/ollama.service.d/dd.conf" \
              "DRY: ollama pull qwen2.5:7b" "DRY: sudo useradd --system --create-home" "DRY: sudo useradd --system --no-create-home" \
              "rules_guard.py init" "DRY: set DD_HOST in /etc/dd/dd.env" "DRY: set DD_MODEL in /etc/dd/dd.env" \
              "DRY: pair your Telegram account" "DRY: write $t/units/dd-pair.service" "DRY: write $t/units/dd-telegram.path" \
              "DRY: write $t/units/dd-firewall.service" "DRY: sudo systemctl enable --now dd-firewall dd-telegram.path dd-pair dd-telegram" "== done"; do
    echo "$out" | grep -qF "$want" || { echo "dry install missing: $want"; echo "$out" | tail -20; return 1; }
  done

  # hands-free (cloud-init): token + pair secret from env -> no prompts, no terminal pairing
  out=$(env -u DD_AGENT DD_SETUP_DRY=1 DD_UNIT_DIR="$t/units" DD_TELEGRAM_TOKEN=x DD_PAIR_SECRET=y bash "$SELF" install 2>&1)
  for want in "DRY: set TELEGRAM_BOT_TOKEN in /etc/dd/dd-telegram.env" "DRY: set DD_PAIR_SECRET in /etc/dd/dd-telegram.env" "/pair <secret>"; do
    echo "$out" | grep -qF "$want" || { echo "hands-free install missing: $want"; return 1; }
  done
  echo "$out" | grep -qE "ask for the Telegram bot token|pair your Telegram account" && { echo "hands-free install prompted"; return 1; }
  out=$(bash "$SELF" cloud-init)
  echo "$out" | bash -n || { echo "cloud-init script has a syntax error"; return 1; }
  for want in "TOKEN='PASTE_BOT_TOKEN_HERE'" "git clone https://github.com/dhayalandhina/personalai /opt/dd" \
              'DD_TELEGRAM_TOKEN="$TOKEN" DD_PAIR_SECRET="$PAIR_SECRET" bash /opt/dd/skills/system-ops/scripts/cloud_setup.sh install'; do
    echo "$out" | grep -qF "$want" || { echo "cloud-init missing: $want"; return 1; }
  done
  [ "$(echo "$out" | sed -n "s/^PAIR_SECRET='\(.*\)'$/\1/p" | tr -d '\n' | wc -c)" -ge 16 ] || { echo "pair secret too short"; return 1; }
  [ "$(bash "$SELF" cloud-init | grep '^PAIR_SECRET=')" != "$(echo "$out" | grep '^PAIR_SECRET=')" ] || { echo "secret not random"; return 1; }
  rm -rf "$t"
  echo "cloud_setup selftest ok (hardened units, root-owned rules/settings/secrets, Ollama local-only, owner-only, Mac refused, dry + hands-free install, cloud-init)"
}

case "${1:-}" in
  install) shift; install_all "$@" ;;
  update) update ;;
  status) status ;;
  token) guard; ask_token; sudo systemctl restart dd-telegram dd-pair 2>/dev/null || true ;;
  pair) guard; do_pair; start_service ;;
  cloud-init) cloud_init_text "$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')" ;;
  --selftest) selftest ;;
  *) sed -n '2,12p' "$SELF"; exit 2 ;;
esac
