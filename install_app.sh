#!/usr/bin/env bash
# install_app.sh — build dd.app natively on this Mac, put it in ~/Applications, start it at login. One command, once.
#   bash install_app.sh install [--no-login]   compile the applet (osacompile, built into macOS), add launcher + icon +
#                                               permissions strings, install voice/menu-bar deps, sign, Login Item, open
#   bash install_app.sh uninstall               remove the Login Item and ~/Applications/dd.app (dd data stays)
#   bash install_app.sh open                    open the app
#   bash install_app.sh --selftest
# Why an applet: a plain shell-script .app is launched under Rosetta on Apple silicon (arm64 numpy/whisper then
# fail to load) and macOS never asks it for Microphone permission. The AppleScript applet is a real native app.
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/macapp"
APPS="${DD_APPS_DIR:-$HOME/Applications}"
APP="$APPS/dd.app"
DD_HOME="${DD_HOME:-$HOME/.dd}"
DRY="${DD_APP_DRY:-}"
FAKE_APPLET="${DD_FAKE_APPLET:-}"   # selftest on Linux: skeleton instead of osacompile

run() { if [ -n "$DRY" ]; then echo "DRY: $*"; else "$@"; fi; }
die() { echo "ERROR: $*"; exit 1; }

guard() {
  [ -n "${DD_AGENT:-}" ] && { echo "REFUSED: installing the app is owner-only — run it yourself in Terminal."; exit 1; }
  [ "${DD_FAKE_UNAME:-$(uname -s)}" = "Darwin" ] || [ -n "$DRY" ] || [ -n "$FAKE_APPLET" ] || die "idhu Mac ku mattum (dd.app)"
  for f in dd.applescript launcher.sh dd.icns dd-mic.swift; do [ -f "$SRC/$f" ] || die "macapp/$f missing (git pull pannu)"; done
}

pick_python() {  # the python3 your terminal uses, preferring one that already has faster-whisper + numpy
  local c
  for c in "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import faster_whisper, numpy' >/dev/null 2>&1 && { echo "$c"; return; }
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

compile_applet() {  # compile_applet OUT.app — a native app from macapp/dd.applescript
  local out="$1"
  if [ -n "$FAKE_APPLET" ]; then  # Linux selftest: the skeleton osacompile would produce
    mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
    printf '#!/bin/sh\necho fake applet\n' > "$out/Contents/MacOS/applet"; chmod 755 "$out/Contents/MacOS/applet"
    : > "$out/Contents/Resources/applet.icns"
    python3 - "$out/Contents/Info.plist" <<'EOF'
import plistlib, sys
plistlib.dump({"CFBundleExecutable": "applet", "CFBundleIconFile": "applet", "CFBundleIdentifier": "com.apple.ScriptEditor.id.dd",
               "CFBundleName": "dd", "CFBundlePackageType": "APPL"}, open(sys.argv[1], "wb"))
EOF
    return 0
  fi
  run osacompile -o "$out" "$SRC/dd.applescript"
}

customize_bundle() {  # customize_bundle APP.app — launcher, icon, Info.plist keys (same steps on every platform)
  local app="$1"
  [ -n "$DRY" ] && { echo "DRY: customize $app (launcher.sh, dd.icns, Info.plist keys)"; return 0; }
  cp "$SRC/launcher.sh" "$app/Contents/Resources/launcher.sh" && chmod 755 "$app/Contents/Resources/launcher.sh"
  cp "$SRC/dd.icns" "$app/Contents/Resources/applet.icns"
  python3 - "$app/Contents/Info.plist" <<'EOF'
import plistlib, sys
p = sys.argv[1]
d = plistlib.load(open(p, "rb"))
d.update({
    "CFBundleIdentifier": "com.dhina.dd", "CFBundleName": "dd", "CFBundleDisplayName": "dd",
    "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1.0",
    "LSUIElement": True,                      # menu-bar app: no Dock icon (the 🤖 comes from dd_app.py)
    "NSHighResolutionCapable": True,
    "NSMicrophoneUsageDescription": "dd listens for your voice commands (\"dd, chrome open pannu\").",
    "NSAppleEventsUsageDescription": "dd opens apps and sets volume/music for you.",
})
plistlib.dump(d, open(p, "wb"))
EOF
}

install() {
  guard
  local py tmp; py="$(pick_python)"; tmp="$(mktemp -d)"
  echo "[1/6] build dd.app natively (osacompile) -> $APP"
  run pkill -f "examples/dd_app.py" 2>/dev/null || true
  run osascript -e 'tell application "dd" to quit' 2>/dev/null || true
  compile_applet "$tmp/dd.app" || die "osacompile failed"
  customize_bundle "$tmp/dd.app"
  run mkdir -p "$APPS" "$DD_HOME/logs"
  run rm -rf "$APP"
  run mv "$tmp/dd.app" "$APP"
  if [ -z "$DRY" ]; then
    printf '%s\n' "$REPO" > "$DD_HOME/repo_path"          # so the app finds this folder wherever it is
    ln -sfn "$py" "$DD_HOME/python"                        # and uses the same python3 as your terminal
  else
    echo "DRY: write $DD_HOME/repo_path ($REPO), link $DD_HOME/python -> $py"
  fi
  echo "[2/6] voice + menu-bar deps in $py (one-time)"
  if [ -n "$DRY" ]; then echo "DRY: pip install faster-whisper rumps"
  else
    "$py" -c 'import faster_whisper' >/dev/null 2>&1 || pip_install "$py" faster-whisper || echo "  faster-whisper install aagala (voice venumna: pip3 install faster-whisper)"
    "$py" -c 'import rumps' >/dev/null 2>&1 || pip_install "$py" rumps || echo "  rumps install aagala (menu bar venumna: pip3 install rumps)"
  fi
  command -v ffmpeg >/dev/null 2>&1 || echo "  ffmpeg illa: brew install ffmpeg   (mic ku venum)"
  echo "[2b] mic helper with echo cancellation (swiftc, one-time) — hears you while a video plays"
  if [ -n "$DRY" ]; then echo "DRY: swiftc -O -o $DD_HOME/bin/dd-mic $SRC/dd-mic.swift"
  elif command -v swiftc >/dev/null 2>&1; then
    mkdir -p "$DD_HOME/bin"
    swiftc -O -suppress-warnings -o "$DD_HOME/bin/dd-mic" "$SRC/dd-mic.swift" && echo "  dd-mic built" \
      || echo "  dd-mic build failed — app uses ffmpeg mic (no echo cancellation); see errors above"
  else
    echo "  swiftc illa — run: xcode-select --install   (then: bash install_app.sh install again)"
  fi
  echo "[3/6] sign (ad-hoc) so macOS remembers the mic/automation permissions"
  if command -v codesign >/dev/null 2>&1; then run codesign --force --deep --sign - "$APP" 2>/dev/null || true
  else echo "  codesign illa (skip)"; fi
  if [ "${1:-}" = "--no-login" ]; then echo "[4/6] login item: skipped"
  else echo "[4/6] start at login"; login_item add || echo "  login item add aagala — System Settings → General → Login Items la dd ah add pannu"; fi
  echo "[5/6] open"
  run open "$APP"
  echo "[6/6] first seconds of the log"
  if [ -z "$DRY" ]; then sleep 10; tail -n 8 "$DD_HOME/logs/app.log" 2>/dev/null | sed 's/^/  /'; fi
  echo
  echo "done. Menu bar la 🤖 varum. Mudhal thadava Microphone + Automation permission kekkum — Allow kudu."
  echo "Sollu: 'dd, chrome open pannu'  ·  'jarvis'  ·  menu la 'Mic test (3 s)'  ·  log: $DD_HOME/logs/app.log"
}

uninstall() {
  guard
  login_item remove || true
  run pkill -f "examples/dd_app.py" 2>/dev/null || true
  run osascript -e 'tell application "dd" to quit' 2>/dev/null || true
  run rm -rf "$APP"
  echo "removed $APP + login item (dd data in $DD_HOME kept)"
}

selftest() {
  local t out; t=$(mktemp -d)
  bash -n "$SRC/launcher.sh" || { echo "launcher syntax"; return 1; }
  grep -q "/Users/" "$SRC/launcher.sh" && { echo "launcher has a user path baked in"; return 1; }
  grep -q 'examples/dd_app.py' "$SRC/launcher.sh" || { echo "launcher does not start dd_app"; return 1; }
  grep -q 'proc_translated' "$SRC/launcher.sh" || { echo "launcher lacks the Rosetta guard"; return 1; }
  grep -q 'launcher.sh' "$SRC/dd.applescript" && grep -q 'with timeout' "$SRC/dd.applescript" || { echo "applescript wrong"; return 1; }
  grep -q 'setVoiceProcessingEnabled(true)' "$SRC/dd-mic.swift" && grep -q 'sampleRate: 16000' "$SRC/dd-mic.swift" || { echo "dd-mic.swift wrong"; return 1; }
  grep -q 'swiftc' "$SRC/launcher.sh" || { echo "launcher does not build dd-mic"; return 1; }
  [ "$(head -c 4 "$SRC/dd.icns")" = "icns" ] || { echo "icon missing/invalid"; return 1; }
  # build with the fake applet skeleton, then check the customized bundle
  FAKE_APPLET=1; export DD_FAKE_APPLET=1
  compile_applet "$t/dd.app" && customize_bundle "$t/dd.app" || { echo "build failed"; return 1; }
  [ -x "$t/dd.app/Contents/Resources/launcher.sh" ] || { echo "launcher not installed/executable"; return 1; }
  [ "$(head -c 4 "$t/dd.app/Contents/Resources/applet.icns")" = "icns" ] || { echo "icon not replaced"; return 1; }
  python3 - "$t/dd.app/Contents/Info.plist" <<'EOF' || { echo "plist customization wrong"; return 1; }
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
assert d["CFBundleExecutable"] == "applet" and d["CFBundleIconFile"] == "applet", d   # osacompile's own, kept
assert d["CFBundleIdentifier"] == "com.dhina.dd" and d["LSUIElement"] is True, d
assert "NSMicrophoneUsageDescription" in d and "NSAppleEventsUsageDescription" in d, d
EOF
  out=$(DD_AGENT=1 bash "$REPO/install_app.sh" install 2>&1); echo "$out" | grep -q REFUSED || { echo "agent not refused"; return 1; }
  out=$(env -u DD_AGENT -u DD_FAKE_APPLET DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" DD_HOME="$t/.dd" bash "$REPO/install_app.sh" install 2>&1)
  for want in "DRY: osacompile -o" "DRY: customize" "DRY: mv " "repo_path ($REPO)" "DRY: pip install faster-whisper rumps" \
              "DRY: swiftc -O -o $t/.dd/bin/dd-mic" \
              "make login item" "DRY: open $t/Applications/dd.app" "done."; do
    echo "$out" | grep -qF -- "$want" || { echo "dry install missing: $want"; echo "$out"; return 1; }
  done
  out=$(env -u DD_AGENT -u DD_FAKE_APPLET DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" bash "$REPO/install_app.sh" install --no-login 2>&1)
  echo "$out" | grep -q "login item: skipped" || { echo "--no-login ignored"; return 1; }
  out=$(env -u DD_AGENT -u DD_FAKE_APPLET DD_APP_DRY=1 DD_FAKE_UNAME=Darwin DD_APPS_DIR="$t/Applications" bash "$REPO/install_app.sh" uninstall 2>&1)
  echo "$out" | grep -q "delete login item" && echo "$out" | grep -q "DRY: rm -rf $t/Applications/dd.app" || { echo "dry uninstall wrong"; return 1; }
  rm -rf "$t"
  echo "install_app selftest ok (native applet build + customization, Rosetta guard, icon, plist keys, owner-only, dry install/uninstall)"
}

case "${1:-}" in
  install) shift; install "$@" ;;
  uninstall) uninstall ;;
  open) guard; open "$APP" ;;
  --selftest) selftest ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
