#!/usr/bin/env bash
# disk_cleanup.sh — free disk by removing ONLY caches that apps rebuild by themselves.
#   disk_cleanup.sh            preview: what would be removed + sizes (deletes nothing)
#   disk_cleanup.sh --clean    remove them — owner terminal only, asks you to type YES
#   disk_cleanup.sh --selftest
# Never touched (hard-coded allowlist below, nothing outside it is ever removed):
#   passwords / Keychain, Chrome profile (bookmarks, history, logins in Application Support),
#   Documents, Desktop, Downloads, Photos, Trash, _Review_Before_Delete, ~/.ssh, ~/.dd data,
#   Ollama models, Whisper/HuggingFace models, project folders.
set -u

H="${CLEAN_HOME:-$HOME}"

# label | path   (only regenerable caches; apps/tools download them again when needed)
targets() {
  cat <<EOF
Chrome browser cache (not bookmarks/passwords)|$H/Library/Caches/Google/Chrome
Android Studio cache|$H/Library/Caches/Google/AndroidStudio*
Antigravity app cache|$H/Library/Caches/com.google.antigravity
Antigravity cache|$H/Library/Caches/antigravity
node-gyp build cache|$H/Library/Caches/node-gyp
TypeScript cache|$H/Library/Caches/typescript
Homebrew download cache|$H/Library/Caches/Homebrew
pip download cache|$H/Library/Caches/pip
pip download cache (~/.cache)|$H/.cache/pip
npm download cache|$H/.npm/_cacache
Gradle build caches|$H/.gradle/caches
Xcode DerivedData|$H/Library/Developer/Xcode/DerivedData
iOS Simulator caches|$H/Library/Developer/CoreSimulator/Caches
dd temp files|$H/.dd/tmp
EOF
}

# shown for information only — never deleted by this script
info_only() {
  cat <<EOF
Trash (empty it yourself after checking)|$H/.Trash
_Review_Before_Delete (your folder — review it yourself)|$H/_Review_Before_Delete
Playwright browsers (needed by browser skill)|$H/Library/Caches/ms-playwright
EOF
}

kb() { du -sk "$@" 2>/dev/null | awk '{s+=$1} END {print s+0}'; }
human() { awk -v k="$1" 'BEGIN { if (k>=1048576) printf "%.1fG", k/1048576; else if (k>=1024) printf "%.0fM", k/1024; else printf "%dK", k }'; }

chrome_running() { pgrep -x "Google Chrome" >/dev/null 2>&1; }

preview() {
  local total=0 label path size matched
  echo "Will remove (caches only — apps rebuild these):"
  while IFS='|' read -r label path; do
    [ -z "$label" ] && continue
    matched=$(ls -d $path 2>/dev/null)
    [ -z "$matched" ] && continue
    size=$(kb $matched)
    [ "$size" -lt 1024 ] && continue
    total=$((total + size))
    note=""
    case "$label" in Chrome*) chrome_running && note="  (Chrome is open — will be skipped; quit Chrome first)";; esac
    printf "  %-48s %7s%s\n" "$label" "$(human "$size")" "$note"
  done <<EOF
$(targets)
EOF
  echo "  ------------------------------------------------------------"
  printf "  %-48s %7s\n" "TOTAL" "$(human "$total")"
  echo
  echo "Not touched (info only):"
  while IFS='|' read -r label path; do
    [ -z "$label" ] && continue
    [ -e "$path" ] || continue
    printf "  %-48s %7s\n" "$label" "$(human "$(kb "$path")")"
  done <<EOF
$(info_only)
EOF
  echo
  echo "Passwords, Chrome bookmarks/history/logins, Documents, Desktop, Downloads, Photos: never touched."
}

delete_targets() {
  local label path p freed=0 size
  while IFS='|' read -r label path; do
    [ -z "$label" ] && continue
    case "$label" in Chrome*) if chrome_running; then echo "  skip  $label (Chrome is open)"; continue; fi;; esac
    for p in $path; do
      [ -e "$p" ] || continue
      case "$p" in
        "$H"/Library/Caches/*|"$H"/.cache/pip|"$H"/.npm/_cacache|"$H"/.gradle/caches|"$H"/Library/Developer/*|"$H"/.dd/tmp) ;;
        *) echo "  REFUSED (outside allowlist): $p"; continue;;
      esac
      case "$p" in *"Application Support"*|*Keychains*|*Documents*|*Desktop*|*Downloads*|*Photos*|*.ssh*) echo "  REFUSED: $p"; continue;; esac
      size=$(kb "$p")
      rm -rf "$p" && freed=$((freed + size)) && echo "  removed $label ($(human "$size"))"
    done
  done <<EOF
$(targets)
EOF
  echo "Freed about $(human "$freed")."
}

clean() {
  if [ -n "${DD_AGENT:-}" ]; then
    echo "REFUSED: cleanup deletes files — run it yourself in Terminal (dd only shows the preview)."
    return 1
  fi
  if [ ! -t 0 ]; then
    echo "REFUSED: needs an interactive terminal."
    return 1
  fi
  preview
  printf "Type YES to remove the items listed above: "
  read -r ans
  [ "$ans" = "YES" ] || { echo "Cancelled — nothing removed."; return 0; }
  delete_targets
  df -h "$H" | tail -1
}

selftest() {
  local t; t=$(mktemp -d)
  mkdir -p "$t/Library/Caches/Google/Chrome" "$t/Library/Caches/pip" "$t/.npm/_cacache" \
           "$t/Library/Application Support/Google/Chrome/Default" "$t/Documents" "$t/_Review_Before_Delete"
  head -c 2000000 /dev/zero > "$t/Library/Caches/Google/Chrome/blob"
  head -c 2000000 /dev/zero > "$t/Library/Caches/pip/blob"
  head -c 2000000 /dev/zero > "$t/.npm/_cacache/blob"
  echo pw > "$t/Library/Application Support/Google/Chrome/Default/Login Data"
  echo doc > "$t/Documents/a.txt"; echo keep > "$t/_Review_Before_Delete/x"
  H="$t"; chrome_running() { return 1; }   # everything below works only inside the temp home
  local out; out=$(preview)
  echo "$out" | grep -q "pip download cache" || { echo "preview missing pip"; return 1; }
  echo "$out" | grep -q "Application Support" && { echo "preview lists protected path"; return 1; }
  echo "$out" | grep -q "npm download cache" || { echo "preview missing npm"; return 1; }
  DD_AGENT=1 clean >/dev/null && { echo "agent must be refused"; return 1; }
  delete_targets >/dev/null
  [ ! -e "$t/Library/Caches/pip" ] && [ ! -e "$t/.npm/_cacache" ] && [ ! -e "$t/Library/Caches/Google/Chrome" ] || { echo "caches not removed"; return 1; }
  [ -f "$t/Library/Application Support/Google/Chrome/Default/Login Data" ] || { echo "Chrome profile touched!"; return 1; }
  [ -f "$t/Documents/a.txt" ] && [ -f "$t/_Review_Before_Delete/x" ] || { echo "user files touched!"; return 1; }
  rm -rf "$t"; echo "disk_cleanup selftest ok (caches removed, passwords/profile/docs untouched, agent refused)"
}

case "${1:-}" in
  "") preview ;;
  --clean) clean ;;
  --selftest) selftest ;;
  *) echo "usage: disk_cleanup.sh [--clean | --selftest]"; exit 2 ;;
esac
