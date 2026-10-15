#!/usr/bin/env bash
# git_safe.sh — guarded git helpers for dd.
#   status|diff|log <repo> [N]
#   commit <repo> "message"          (secret-scans staged files; never pushes)
#   push <repo> [--confirm]          (refuses Track A, refuses without --confirm, never --force)
#   branch <repo> <name>
#   pr <repo> "title" "body"
#   --selftest
# Track A patterns (employer repos) come from $DD_TRACK_A_PATTERNS (comma-separated, case-insensitive);
# default covers common legacy-HMS markers. Personal account from $DD_PERSONAL_GH (default dhayalandhina).
set -u
PERSONAL="${DD_PERSONAL_GH:-dhayalandhina}"
TRACK_A="${DD_TRACK_A_PATTERNS:-codeigniter}"  # add your employer repo markers in ~/.dd/.env
SCAN="$(dirname "$0")/../../security-privacy/scripts/secret_scan.py"

track_of() {  # prints A or B
  local d="$1"; local remote; remote=$(git -C "$d" remote get-url origin 2>/dev/null | tr 'A-Z' 'a-z')
  local hay="$remote $(echo "$d" | tr 'A-Z' 'a-z')"
  IFS=',' read -ra pats <<< "$TRACK_A"
  for p in "${pats[@]}"; do [ -n "$p" ] && echo "$hay" | grep -q "$p" && { echo A; return; }; done
  [ -d "$d/application" ] && [ -d "$d/system" ] && { echo A; return; }
  echo "$remote" | grep -q "github.com[:/]$PERSONAL/" && { echo B; return; }
  echo A  # unknown -> conservative
}

status() {
  local d="$1"; git -C "$d" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repo: $d"; return 2; }
  echo "repo:     $d"
  echo "branch:   $(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)  upstream: $(git -C "$d" rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || echo none)"
  echo "remote:   $(git -C "$d" remote get-url origin 2>/dev/null || echo none)"
  echo "account:  $(git -C "$d" config user.name 2>/dev/null) <$(git -C "$d" config user.email 2>/dev/null)>"
  echo "track:    $(track_of "$d")"
  echo "staged:   $(git -C "$d" diff --cached --name-only | wc -l | tr -d ' ')   unstaged: $(git -C "$d" diff --name-only | wc -l | tr -d ' ')   untracked: $(git -C "$d" ls-files --others --exclude-standard | wc -l | tr -d ' ')"
  git -C "$d" status --short | head -n 40
}

commit() {
  local d="$1"; local msg="$2"
  [ -z "$msg" ] && { echo "need a commit message"; return 2; }
  local staged; staged=$(git -C "$d" diff --cached --name-only)
  [ -z "$staged" ] && { echo "nothing staged — run: git -C $d add <files>"; return 1; }
  echo "$staged" | grep -Eq '(^|/)\.env($|\.)|id_rsa|\.pem$' && { echo "REFUSED: secret-looking file staged:"; echo "$staged" | grep -E '(^|/)\.env($|\.)|id_rsa|\.pem$'; return 1; }
  if [ -f "$SCAN" ]; then
    local files=(); while IFS= read -r f; do [ -f "$d/$f" ] && files+=("$d/$f"); done <<< "$staged"
    if [ ${#files[@]} -gt 0 ] && ! python3 "$SCAN" --file "${files[@]}" >/dev/null; then
      echo "REFUSED: secrets found in staged files (run secret_scan.py --file for details)"; return 1; fi
  fi
  git -C "$d" commit -q -m "$msg" && echo "committed on $(git -C "$d" rev-parse --abbrev-ref HEAD): $(git -C "$d" log -1 --format='%h %s')"
}

push() {
  local d="$1"; shift; local confirm=0
  for a in "$@"; do [ "$a" = "--confirm" ] && confirm=1; echo "$a" | grep -q -- "--force\|-f$" && { echo "REFUSED: force push never"; return 1; }; done
  [ "$(track_of "$d")" = A ] && { echo "REFUSED: Track A push needs Dhina to run it himself: git -C $d push"; return 1; }
  [ $confirm = 1 ] || { echo "REFUSED: add --confirm after Dhina's go-ahead (branch $(git -C "$d" rev-parse --abbrev-ref HEAD))"; return 1; }
  git -C "$d" push -u origin "$(git -C "$d" rev-parse --abbrev-ref HEAD)"
}

selftest() {
  local t; t=$(mktemp -d); git -C "$t" init -q -b main; git -C "$t" config user.email dd@test; git -C "$t" config user.name dd
  git -C "$t" remote add origin "https://github.com/$PERSONAL/demo.git"
  [ "$(track_of "$t")" = B ] || { echo "track B detect failed"; return 1; }
  printf 'hello\n' > "$t/a.txt"; git -C "$t" add a.txt
  commit "$t" "chore: add a" | grep -q committed || { echo "commit failed"; return 1; }
  printf 'API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz123456\n' > "$t/.env"; git -C "$t" add -f .env
  commit "$t" "bad" | grep -q REFUSED || { echo "secret gate failed"; return 1; }
  git -C "$t" reset -q .env; rm -f "$t/.env"
  printf 'password=SuperSecret99\n' > "$t/cfg.txt"; git -C "$t" add cfg.txt
  if [ -f "$SCAN" ]; then commit "$t" "bad2" | grep -q REFUSED || { echo "content secret gate failed"; return 1; }; fi
  push "$t" | grep -q REFUSED || { echo "push gate failed"; return 1; }
  push "$t" --force --confirm | grep -q "force" || { echo "force gate failed"; return 1; }
  local a; a=$(mktemp -d); mkdir -p "$a/application" "$a/system"; git -C "$a" init -q
  [ "$(track_of "$a")" = A ] || { echo "track A detect failed"; return 1; }
  rm -rf "$t" "$a"; echo "git_safe selftest ok"
}

case "${1:-}" in
  status) status "${2:-.}" ;;
  diff) git -C "${2:-.}" --no-pager diff --stat; git -C "${2:-.}" --no-pager diff | head -n 300 ;;
  log) git -C "${2:-.}" --no-pager log --oneline -n "${3:-15}" ;;
  commit) commit "${2:-.}" "${3:-}" ;;
  push) shift; push "$@" ;;
  branch) git -C "${2:-.}" switch -c "$3" ;;
  pr) gh pr create --repo "$(git -C "${2:-.}" remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')" --title "$3" --body "$4" ;;
  --selftest) selftest ;;
  *) echo "usage: git_safe.sh status|diff|log|commit|push|branch|pr <repo> ... | --selftest"; exit 2 ;;
esac
