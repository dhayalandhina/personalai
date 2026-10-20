#!/bin/bash
# dd.app launcher — generic, no user paths. Started by the dd applet (macapp/dd.applescript).
# Finds the personalai folder and a python3 that has dd's voice deps, sets them up once if missing,
# then runs the menu-bar app (examples/dd_app.py). Log: ~/.dd/logs/app.log (+ personalai/logs/app.log)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${LANG:-en_US.UTF-8}" LC_ALL="${LC_ALL:-en_US.UTF-8}" DD_APP=1 PYTHONUNBUFFERED=1

# Apple silicon: if something launched us under Rosetta, re-exec natively — the arm64 numpy/whisper wheels
# cannot load into an x86_64 python.
if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ] && [ -z "${DD_ARCH_RETRY:-}" ]; then
  DD_ARCH_RETRY=1 exec arch -arm64 /bin/bash "$0" "$@"
fi

say_note() { osascript -e "display notification \"$1\" with title \"dd\"" >/dev/null 2>&1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cat "$HOME/.dd/repo_path" 2>/dev/null)"                                   # written by install_app.sh
[ -f "$REPO/examples/dd_app.py" ] || REPO="$(cd "$HERE/../../.." 2>/dev/null && pwd)"   # dd.app inside the folder
[ -f "$REPO/examples/dd_app.py" ] || REPO="$HOME/personalai"
mkdir -p "$HOME/.dd/logs"
if [ -d "$REPO" ] && mkdir -p "$REPO/logs" 2>/dev/null; then   # mirror the log next to the code too (easy to find/share)
  exec > >(tee -a "$HOME/.dd/logs/app.log" >> "$REPO/logs/app.log") 2>&1
else
  exec >>"$HOME/.dd/logs/app.log" 2>&1
fi
echo "== dd.app start $(date) repo=$REPO arch=$(uname -m) translated=$(sysctl -n sysctl.proc_translated 2>/dev/null)"
if [ ! -f "$REPO/examples/dd_app.py" ]; then
  say_note "personalai folder kidaikkala — ~/personalai la irukkanum"
  exit 1
fi

# python3 with the voice deps (install_app.sh records the one your terminal uses in ~/.dd/python)
PY=""
for c in "$HOME/.dd/python" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && "$c" -c 'import faster_whisper, numpy' >/dev/null 2>&1 && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  for c in "$HOME/.dd/python" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] && { PY="$c"; break; }
  done
fi
[ -n "$PY" ] || { say_note "python3 illa — Terminal la: brew install python"; exit 1; }
echo "python: $PY ($("$PY" -c 'import platform; print(platform.machine(), platform.python_version())' 2>/dev/null))"

need=""
"$PY" -c 'import faster_whisper' >/dev/null 2>&1 || need="$need faster-whisper"
"$PY" -c 'import rumps' >/dev/null 2>&1 || need="$need rumps"
if [ -n "$need" ]; then
  say_note "First run: voice + menu bar setup pannudhu (1-3 nimisham)…"
  # shellcheck disable=SC2086
  "$PY" -m pip install -q $need >/dev/null 2>&1 || "$PY" -m pip install -q --break-system-packages $need \
    || "$PY" -m pip install -q --user $need || say_note "setup partly failed — see ~/.dd/logs/app.log"
fi
command -v ffmpeg >/dev/null 2>&1 || say_note "ffmpeg illa — Terminal la: brew install ffmpeg"

cd "$REPO" || exit 1
exec "$PY" examples/dd_app.py
