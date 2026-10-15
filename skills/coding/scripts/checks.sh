#!/usr/bin/env bash
# checks.sh — detect project type and run lint/tests. Usage:
#   checks.sh detect <repo>      prints detected stacks + available commands
#   checks.sh run <repo>         runs lint + tests that exist; one-line summary; exit 1 on failure
#   checks.sh --selftest
set -u

detect() {
  local d="$1"; local found=0
  [ -f "$d/package.json" ]      && { echo "node: package.json ($(grep -o '"scripts"' "$d/package.json" >/dev/null && echo 'has scripts' || echo 'no scripts'))"; found=1; }
  [ -f "$d/pyproject.toml" ] || [ -f "$d/requirements.txt" ] && { echo "python: $( [ -f "$d/pyproject.toml" ] && echo pyproject.toml || echo requirements.txt )"; found=1; }
  [ -f "$d/composer.json" ]     && { echo "php: composer.json"; found=1; }
  [ -f "$d/pubspec.yaml" ]      && { echo "flutter: pubspec.yaml"; found=1; }
  [ -d "$d/application" ] && [ -d "$d/system" ] && { echo "codeigniter3: application/ + system/ (Track A pattern — conservative)"; found=1; }
  [ -f "$d/go.mod" ]            && { echo "go: go.mod"; found=1; }
  [ "$found" = 0 ] && echo "unknown: no manifest found in $d"
  echo "tools: $(for t in npm pnpm ruff pytest php composer flutter dart go; do command -v $t >/dev/null 2>&1 && printf '%s ' $t; done)"
}

run_one() {  # label, cmd...
  local label="$1"; shift
  local out; out=$("$@" 2>&1); local rc=$?
  if [ $rc -eq 0 ]; then echo "  ok   $label"; else echo "  FAIL $label (exit $rc)"; echo "$out" | tail -n 15 | sed 's/^/       /'; fi
  return $rc
}

run() {
  local d="$1"; local fails=0; local ran=0
  cd "$d" || { echo "no such dir: $d"; return 2; }
  if [ -f package.json ]; then
    local pm=npm; command -v pnpm >/dev/null && [ -f pnpm-lock.yaml ] && pm=pnpm
    grep -q '"lint"' package.json && { ran=1; run_one "$pm run lint" $pm run lint --silent || fails=$((fails+1)); }
    grep -q '"typecheck"' package.json && { ran=1; run_one "$pm run typecheck" $pm run typecheck --silent || fails=$((fails+1)); }
    grep -q '"test"' package.json && ! grep -q '"test": "echo' package.json && { ran=1; run_one "$pm test" $pm test --silent -- --watch=false || fails=$((fails+1)); }
  fi
  if [ -f pyproject.toml ] || [ -f requirements.txt ]; then
    command -v ruff >/dev/null && { ran=1; run_one "ruff check" ruff check . || fails=$((fails+1)); }
    ls tests test 2>/dev/null | grep -q . && command -v pytest >/dev/null && { ran=1; run_one "pytest" pytest -q -x || fails=$((fails+1)); }
    [ "$ran" = 0 ] && { ran=1; run_one "python compileall" python3 -m compileall -q . || fails=$((fails+1)); }
  fi
  if [ -f composer.json ] || [ -d application ]; then
    command -v php >/dev/null && { ran=1; local bad=0
      while IFS= read -r f; do php -l "$f" >/dev/null 2>&1 || { echo "  FAIL php -l $f"; bad=1; }; done < <(find . -name '*.php' -not -path '*/vendor/*' -not -path '*/system/*' 2>/dev/null | head -n 400)
      [ $bad = 0 ] && echo "  ok   php -l (syntax)" || fails=$((fails+1)); }
    [ -f vendor/bin/phpunit ] && { run_one "phpunit" vendor/bin/phpunit || fails=$((fails+1)); }
  fi
  if [ -f pubspec.yaml ] && command -v flutter >/dev/null; then
    ran=1; run_one "flutter analyze" flutter analyze --no-pub || fails=$((fails+1))
    [ -d test ] && { run_one "flutter test" flutter test || fails=$((fails+1)); }
  fi
  [ "$ran" = 0 ] && echo "  (no runnable checks found)"
  [ $fails = 0 ] && echo "SUMMARY: all checks passed" || echo "SUMMARY: $fails check(s) failed"
  return $fails
}

selftest() {
  local t; t=$(mktemp -d)
  mkdir -p "$t/application" "$t/system"; printf '<?php echo 1;\n' > "$t/application/ok.php"; printf '<?php echo ;\n' > "$t/application/bad.php"
  detect "$t" | grep -q codeigniter3 || { echo "detect failed"; return 1; }
  if command -v php >/dev/null; then
    run "$t" >/dev/null && { echo "php syntax error not caught"; return 1; }
  fi
  local p; p=$(mktemp -d); printf 'x = 1\n' > "$p/a.py"; printf 'requests\n' > "$p/requirements.txt"
  run "$p" | grep -q "SUMMARY: all checks passed" || { echo "python run failed"; return 1; }
  rm -rf "$t" "$p"; echo "checks selftest ok"
}

case "${1:-}" in
  detect) detect "${2:-.}" ;;
  run) run "${2:-.}" ;;
  --selftest) selftest ;;
  *) echo "usage: checks.sh detect|run <repo> | --selftest"; exit 2 ;;
esac
