#!/usr/bin/env bash
# install_app.sh — put dd.app in ~/Applications, set it up, start it at login. One command, once; then no Terminal.
#   bash install_app.sh install [--no-login]   copy dd.app → ~/Applications, install voice + menu-bar deps now,
#                                               ad-hoc sign, add Login Item, open it
#   bash install_app.sh uninstall               remove the Login Item and ~/Applications/dd.app (dd data stays)
#   bash install_app.sh open                    open the app
#   bash install_app.sh --selftest
# You can also just double-click dd.app inside the personalai folder — it sets itself up on first run.
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/dd.app"
APPS="${DD_APPS_DIR:-$HOME/Applications}"
APP="$APPS/dd.app"
DD_HOME="${DD_HOME:-$HOME/.dd}"
DRY="${DD_APP_DRY:-}"

run() { if [ -n "$DRY" ]; then echo "DRY: $*"; else "$@"; fi; }
die() { echo "ERROR: $*"; exit 1; }

guard() {
  [ -n "${DD_AGENT:-}" ] && { echo "REFUSED: installing the app is owner-only — run it yourself in Terminal."; exit 1; }
  [ "${DD_FAKE_UNAME:-$(uname -s)}" = "Darwin" ] || [ -n "$DRY" ] || die "idhu Mac ku mattum (dd.app)"
  [ -f "$SRC/Contents/MacOS/dd" ] || die "dd.app missing in $REPO (git pull pannu)"
}

pick_python() {  # the python3 your terminal uses, preferring one that already has faster-whisper
  local c
  for c in "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import faster_whisper' >/dev/null 2>&1 && { echo "$c"; return; }
  done
  command -v python3 2>/dev/null || echo /usr/bin/python3
}

pip_install() {  # same fallbacks install.sh uses on this Mac
  local py="$1"; shift
  run "$py" -m pip install -q "$@" 2>/dev/null || run "$py" -m pip install -q --break-system-packages "$@" \
    || run "$py" -m pip install -q --user "$@"
}

login_item() {  # add|remove  (Login Items via System Events; macOS asks once to allow Terminal → System Events)
  if [ "$1" = add ]; then
    run osascript -e "tell application \"System Events\" to if not (exists login item \"dd\") then make login item at end with properties {path:\"$APP\", hidden:false, name:\"dd\"}"
  else
    run osascript -e 'tell application "System Events" to if (exists login item "dd") then delete login item "dd"'
  fi
}

install() {
  guard
  local py; py="$(pick_python)"
  echo "[1/5] copy dd.app -> $APP"
  run mkdir -p "$APPS" "$DD_HOME/logs"
  run rm -rf "$APP"
  run cp -R "$SRC" "$APP"
  run chmod 755 "$APP/Contents/MacOS/dd"
  if [ -z "$DRY" ]; then
    printf '%s\n' "$REPO" > "$DD_HOME/repo_path"          # so the app finds this folder wherever it is
    ln -sfn "$py" "$DD_HOME/python"                        # and uses the same python3 as your terminal
  else
    echo "DRY: write $DD_HOME/repo_path ($REPO), link $DD_HOME/python -> $py"
  fi
  echo "[2/5] voice + menu-bar deps in $py (one-time)"
  if [ -n "$DRY" ]; then echo "DRY: pip install faster-whisper rumps"
  else
    "$py" -c 'import faster_whisper' >/dev/null 2>&1 || pip_install "$py" faster-whisper || echo "  faster-whisper install aagala (voice venumna: pip3 install faster-whisper)"
    "$py" -c 'import rumps' >/dev/null 2>&1 || pip_install "$py" rumps || echo "  rumps install aagala (menu bar venumna: pip3 install rumps)"
  fi
  command -v ffmpeg >/dev/null 2>&1 || echo "  ffmpeg illa: brew install ffmpeg   (mic ku venum)"
  echo "[3/5] sign (ad-hoc) so macOS remembers the mic/automation permissions"
  if command -v codesign >/dev/null 2>&1; then run codesign --force --deep --sign - "$APP" 2>/dev/null || true
  else echo "  codesign illa (skip)"; fi
  if [ "${1:-}" = "--no-login" ]; then echo "[4/5] login item: skipped"
  else echo "[4/5] start at login"; login_item add || echo "  login item add aagala — System Settings → General → Login Items la dd ah add pannu"; fi
  echo "[5/5] open"
  run open "$APP"
  echo
  echo "done. Menu bar la 🤖 varum. Mudhal thadava Microphone + Automation permission kekkum — Allow kudu."
  echo "Sollu: 'dd, chrome open pannu'  ·  'jarvis'  ·  log: $DD_HOME/logs/app.log"
}

uninstall() {
  guard
  login_item remove || true
  run osascript -e 'tell application "dd" to quit' 2>/dev/null || true
  run rm -rf "$APP"
  echo "removed $APP + login item (dd data in $DD_HOME kept)"
}

selftest() {
  local t; t=$(mktemp -d)
  python3 - "$SRC/Contents/Info.plist" <<'EOF' || { echo "plist invalid"; return 1; }
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
assert d["CFBundleExecutable"] == "dd" and d["LSUIElement"] is True and d["CFBundleIconFile"] == "dd", d
assert "NSMicrophoneUsageDescription" in d and "NSAppleEventsUsageDescription" in d, d
EOF
  [ -x "$SRC/Contents/MacOS/dd" ] || { echo "launcher not executable"; return 1; }
  bash -n "$SRC/Contents/MacOS/dd" || { echo "launcher syntax"; return 1; }
  grep -q "/Users/" "$SRC/Contents/MacOS/dd" && { echo "launcher has a user path baked in"; return 1; }
  grep -q 'examples/dd_app.py' "$SRC/Contents/MacOS/dd" || { echo "launcher does not start dd_app"; return 1; }
  [ "$(head -c 4 "$SRC/Contents/Resources/dd.icns")" = "icns" ] || { echo "icon missing/invalid"; return 1; }
  out=$(DD_AGENT=1 bash "$REPO/install_app.sh" install 2>&1); echo "$out" | grep -q REFUSED || { echo "agent not refused"; return 1; }
  out=$(env -u DD_AGENT DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" DD_HOME="$t/.dd" bash "$REPO/install_app.sh" install 2>&1)
  for want in "DRY: cp -R $SRC $t/Applications/dd.app" "DRY: chmod 755" "repo_path ($REPO)" "DRY: pip install faster-whisper rumps" \
              "make login item" "DRY: open $t/Applications/dd.app" "done."; do
    echo "$out" | grep -qF -- "$want" || { echo "dry install missing: $want"; echo "$out"; return 1; }
  done
  out=$(env -u DD_AGENT DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" bash "$REPO/install_app.sh" install --no-login 2>&1)
  echo "$out" | grep -q "login item: skipped" || { echo "--no-login ignored"; return 1; }
  out=$(env -u DD_AGENT DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" bash "$REPO/install_app.sh" uninstall 2>&1)
  echo "$out" | grep -q "delete login item" && echo "$out" | grep -q "DRY: rm -rf $t/Applications/dd.app" || { echo "dry uninstall wrong"; return 1; }
  rm -rf "$t"
  echo "install_app selftest ok (bundle valid, generic launcher, icns, owner-only, dry install/uninstall, login item)"
}

case "${1:-}" in
  install) shift; install "$@" ;;
  uninstall) uninstall ;;
  open) guard; open "$APP" 2>/dev/null || open "$SRC" ;;
  --selftest) selftest ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
