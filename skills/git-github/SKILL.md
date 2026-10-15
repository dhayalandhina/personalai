---
name: git-github
description: Git and GitHub workflow for dd — status/diff/commit/branch/PR via git and gh CLI, GitHub-only workspace policy, separate accounts per track, never push Track A without go-ahead, never force-push. Use for commit, push, branch, PR, clone, "what changed", repo setup, or any GitHub question.
triggers: [git, github, commit, push, pull, branch, pr, pull request, clone, repo, merge, rebase, gh]
metadata:
  version: "1.0"
  owner: dhina
---

# git-github

## Policy
- GitHub is the only workspace: every project lives in a repo; no loose folders as source of truth.
- Two accounts, never crossed: personal account (`dhayalandhina`, Track B) vs employer account (Track A). Check `git config user.email` and `git remote -v` **before** committing; wrong account → stop, fix config, then continue.
- **Track A: never commit or push without explicit go-ahead in the same message.** Hand back the diff + commands.
- Track B: commit freely on feature branches; push to `main` of a repo with a live deploy only after `coding/scripts/checks.sh run` passes.
- Never: `push --force` to main, `reset --hard` on shared branches, rewrite published history, commit `.env`/secrets (run `security-privacy/scripts/secret_scan.py --file` on staged files first).

## Commands
```bash
bash scripts/git_safe.sh status <repo>                 # branch, upstream, staged/unstaged counts, account, track guess
bash scripts/git_safe.sh diff <repo>                   # concise diff of unstaged + staged
bash scripts/git_safe.sh commit <repo> "msg"           # scans staged files for secrets, commits (no push)
bash scripts/git_safe.sh push <repo> [--confirm]       # refuses without --confirm; refuses force; refuses Track A entirely
bash scripts/git_safe.sh branch <repo> feat/name       # create + switch
bash scripts/git_safe.sh pr <repo> "title" "body"      # gh pr create (needs gh auth login once)
bash scripts/git_safe.sh log <repo> [N]
```

## Commit messages
`<type>: <what> (<why>)` — types: fix, feat, chore, docs, refactor, test. Imperative, ≤ 72 chars first line. Body = bullets of what/why when more than one file.

## New repo (Track B)
```bash
gh repo create <name> --private --source=. --remote=origin --push
```
Default **private**. Public only when Dhina says so (and never for anything containing his rules/memory/credentials).

## Conflicts
Never auto-resolve by taking one side blindly. Show both hunks, pick per hunk with the reason, rerun checks, then commit.

## Track detection heuristics (git_safe.sh)
Remote URL or path containing the employer account/HMS names → Track A. Personal account → Track B. Unknown → treat as Track A (conservative) and say so.
