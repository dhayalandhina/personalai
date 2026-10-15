---
name: coding
description: How dd writes, fixes and reviews code — read-first, minimal-diff, match existing style, run lint/tests before reporting, full paste-ready files. Covers Track A (legacy PHP/CodeIgniter, production, conservative) vs Track B (Next.js, Flutter, n8n, Supabase, Python agents — modern best practice). Use for any code task, bug, review, refactor request or "why is this failing".
triggers: [code, bug, fix, error, function, class, api, php, codeigniter, next.js, react, flutter, dart, python, supabase, n8n, sql, refactor, review, lint, test]
metadata:
  version: "1.0"
  owner: dhina
---

# coding

## Before touching anything
1. Identify the track from the repo path / stack. **Track A** = legacy PHP/CodeIgniter HMS, live production for hundreds of hotels. **Track B** = Dhina's own modern projects. Never mix (see `security-privacy`).
2. `shell-and-files/scripts/files.py tree <repo> --depth 2`, then **read the actual files** involved (`files.py read`). Never guess file contents or framework version from memory.
3. Find how the codebase already does it (`files.py find --grep`) and copy that convention: naming, indentation, quotes, error handling, DB access style.
4. `scripts/checks.sh detect <repo>` → which linters/tests exist. Plan to run them at the end.

## Track A rules (production, conservative)
- Minimal diff. Touch only the function that is wrong. No framework upgrades, no new dependencies, no reformatting of untouched lines.
- CodeIgniter style: keep `$this->db` query patterns, same model/controller layout, same escaping (`$this->db->escape`, `xss_clean` where already used). Never introduce raw SQL where the codebase uses the query builder.
- Any change to billing, room allocation, tax, or reports → add a one-line comment `// dd: <reason> <date>` and describe the rollback (`git stash`/previous file copy) in the reply.
- **Never commit or push Track A without explicit go-ahead.** Hand back the diff and the exact `git` commands.
- Security findings (SQL injection, plaintext passwords, exposed keys, shell injection) → flag immediately at the top of the reply, even if unrelated to the task.

## Track B rules (modern, pragmatic)
- Best practice is fine: TypeScript strict, React server components where it fits, Supabase RLS on every table, env via `.env.local`, Flutter null-safety, Python 3.11+ type hints, `ruff`.
- Schema / infra breaking changes (drop column, change auth, rename table, new paid service) → do everything else, then state the breaking step as a blocker with the exact migration SQL/command for Dhina to run. Don't run it yourself.
- Zero-cost dependencies only. No paid SaaS unless he named it.

## Output format
- New file → full file. Fix → full function (or full file if < 150 lines), paste-ready, no `...` placeholders.
- Reply: line 1 = what changed + file path. Then `scripts/checks.sh run <repo>` result in one line ("lint ok, 12 tests pass"). Then diff of the changed part only.
- Unknown API / flag → say `Likely:` and point to the doc you'd verify with, or fetch the doc (`web-search/fetch.py`) and confirm first.

## Debugging loop
Reproduce (run it / read the stack trace) → locate (grep the exact error string) → hypothesis → smallest fix → rerun → if same error twice, stop and report what was tried. Never "fix" by swallowing exceptions.

## Script
`scripts/checks.sh detect|run <repo> [--selftest]` — detects package.json/pyproject/composer/pubspec, runs available lint + tests, prints a one-line summary and exit code.
