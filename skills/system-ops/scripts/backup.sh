#!/usr/bin/env bash
# backup.sh — backup / restore $DD_HOME (memory, rules, reminders, learning, plans, .env).
#   backup.sh                      -> $DD_BACKUP_DIR/dd-YYYYmmdd-HHMM.tar.gz (keeps last 14)
#   backup.sh restore FILE.tar.gz  -> restores into $DD_HOME (existing moved to $DD_HOME.bak-<ts>)
#   backup.sh --selftest
set -euo pipefail
DD_HOME="${DD_HOME:-$HOME/.dd}"
DD_BACKUP_DIR="${DD_BACKUP_DIR:-$HOME/dd-backups}"
EXCLUDES=(--exclude=browser-profile --exclude=tmp --exclude=video --exclude='*.pyc' --exclude=__pycache__)

backup() {
  [ -d "$DD_HOME" ] || { echo "no DD_HOME at $DD_HOME"; return 1; }
  mkdir -p "$DD_BACKUP_DIR"
  local out="$DD_BACKUP_DIR/dd-$(date +%Y%m%d-%H%M).tar.gz"
  tar -czf "$out" "${EXCLUDES[@]}" -C "$(dirname "$DD_HOME")" "$(basename "$DD_HOME")"
  chmod 600 "$out"
  ls -1t "$DD_BACKUP_DIR"/dd-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
  echo "backup: $out ($(du -h "$out" | cut -f1)), kept $(ls -1 "$DD_BACKUP_DIR"/dd-*.tar.gz | wc -l | tr -d ' ')"
}

restore() {
  local f="$1"; [ -f "$f" ] || { echo "no such file: $f"; return 1; }
  tar -tzf "$f" >/dev/null || { echo "corrupt archive"; return 1; }
  [ -d "$DD_HOME" ] && mv "$DD_HOME" "$DD_HOME.bak-$(date +%s)"
  mkdir -p "$(dirname "$DD_HOME")"
  tar -xzf "$f" -C "$(dirname "$DD_HOME")"
  echo "restored $f -> $DD_HOME"
}

selftest() {
  local t; t=$(mktemp -d); export DD_HOME="$t/.dd" DD_BACKUP_DIR="$t/bk"
  mkdir -p "$DD_HOME/rules" "$DD_HOME/tmp"; echo "r" > "$DD_HOME/rules/RULES.md"; echo "junk" > "$DD_HOME/tmp/x"
  backup | grep -q "backup:" || { echo "backup failed"; return 1; }
  local f; f=$(ls "$DD_BACKUP_DIR"/dd-*.tar.gz | head -n1)
  tar -tzf "$f" | grep -q "rules/RULES.md" || { echo "rules missing in archive"; return 1; }
  tar -tzf "$f" | grep -q "tmp/x" && { echo "exclude failed"; return 1; }
  rm -rf "$DD_HOME"; restore "$f" >/dev/null; [ -f "$DD_HOME/rules/RULES.md" ] || { echo "restore failed"; return 1; }
  rm -rf "$t"; echo "backup selftest ok"
}

case "${1:-}" in
  "") backup ;;
  restore) restore "${2:-}" ;;
  --selftest) selftest ;;
  *) echo "usage: backup.sh | backup.sh restore FILE | --selftest"; exit 2 ;;
esac
