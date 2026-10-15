---
name: skill-creator
description: dd extends itself — scaffold a new skill folder (SKILL.md + scripts with --selftest), validate the frontmatter and run the library test, improve an existing skill from repeated corrections, and keep the skill index tidy. Use when a task keeps recurring without a skill, Dhina says "make this a skill / ithai skill ah pannu", or a skill's instructions turned out wrong.
triggers: [new skill, make a skill, skill ah pannu, add skill, improve skill, skill not working, SKILL.md, scaffold]
metadata:
  version: "1.0"
  owner: dhina
---

# skill-creator

Skills live in `skills/<name>/` (Agent Skills format): `SKILL.md` with frontmatter `name` (== folder, lowercase-hyphen, ≤ 64 chars), `description` (≤ 1024 chars, says WHAT + WHEN), optional `triggers` list, then concise instructions. Helpers in `scripts/` must each support `--selftest`.

## Commands
```bash
python3 scripts/new_skill.py new <name> --desc "What it does. Use when ..." [--triggers "a,b,c"] [--script helper.py]
python3 scripts/new_skill.py validate [<name>]          # frontmatter + body + selftests (whole library if no name)
python3 scripts/new_skill.py index                      # regenerate the one-line index for the system prompt
```
Then `python3 ../../test_skills.py` from the library root before committing.

## When to create a skill
- Same multi-step procedure done 3+ times, or Dhina corrected the same thing twice (check `self-learning/scripts/learn.py stats` + corrections).
- A tool/API that needs setup knowledge (URLs, auth, gotchas) that would otherwise be re-discovered.
- Not for one-offs. Not for anything that is really a rule (rules go to Dhina → `rules_guard.py update`).

## Writing a good SKILL.md (checklist)
1. Description = trigger conditions in plain words; the index is the only thing the model sees before loading, so put the keywords there.
2. Body ≤ ~120 lines: purpose, commands (paste-ready), rules, pitfalls, output template. Move long material to `references/`.
3. Every command shown must exist and have been run once (`--selftest` passes). No invented flags.
4. Zero-cost tools only; say the install line.
5. Security: note what is data vs instruction, what needs go-ahead, what is Track-A sensitive.
6. Improvement = edit in place with a version bump in `metadata.version`; keep what works, never rewrite from scratch for style.

## Proposing instead of creating
If unsure a skill is worth it, reply with a 3-line proposal (name, description, when it fires) and create it only on "ok". Skills change dd's behaviour, so Dhina sees every new one in the next `/status` ("new skill: <name>").
