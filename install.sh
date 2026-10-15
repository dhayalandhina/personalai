#!/usr/bin/env bash
# install.sh — set up dd-skills on this machine (macOS / Linux). Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
DD_HOME="${DD_HOME:-$HOME/.dd}"
PIP="python3 -m pip install --quiet"
command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 10), "python 3.10+ needed"'

echo "[1/5] python deps"
$PIP -r requirements.txt 2>/dev/null || $PIP --break-system-packages -r requirements.txt

echo "[2/5] data dir $DD_HOME"
mkdir -p "$DD_HOME"/{rules,learning,plans,tmp,video,voices,logs,mail/drafts}
chmod 700 "$DD_HOME"

echo "[3/5] lock default rules (skip if already locked)"
python3 skills/rules-guard/scripts/rules_guard.py init

echo "[4/5] briefing config + env template"
python3 skills/daily-briefing/scripts/briefing.py --init-config >/dev/null
[ -f "$DD_HOME/.env" ] || { cp .env.example "$DD_HOME/.env"; chmod 600 "$DD_HOME/.env"; echo "   created $DD_HOME/.env — fill in tokens you use"; }

echo "[5/5] optional tools"
command -v ffmpeg >/dev/null && echo "   ffmpeg ok" || echo "   ffmpeg missing: brew install ffmpeg | sudo apt install ffmpeg"
command -v ollama >/dev/null && echo "   ollama ok" || echo "   ollama missing: https://ollama.com/download"
python3 -c 'import playwright' 2>/dev/null && echo "   playwright ok (run: python3 -m playwright install chromium)" || echo "   browser skill: pip install playwright && python3 -m playwright install chromium"
echo
echo "done. next: python3 test_skills.py && python3 examples/dd_loop_ollama.py"
