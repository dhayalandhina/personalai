---
name: task-planner
description: Plan → execute → verify loop for any multi-step task (3+ steps, files to produce, deploys, research). Keeps a persistent plan file with step status so dd can resume after a restart, report progress without check-ins, and never skip verification. Use when a request has several parts, needs tools in sequence, or will take more than one tool call.
triggers: [plan, steps, build me, set up, implement, migrate, deploy, project, todo, multi-step, full build]
metadata:
  version: "1.0"
  owner: dhina
---

# task-planner

Dhina's rule: **no mid-task questions, no check-ins — execute fully, then report.** The plan file is how dd stays on track and how he sees progress later.

## Loop
1. **Decompose** the request into atomic steps, each verifiable true/false (see `verification-protocol` §2). Order: facts → calculations → build → verify → report.
2. `python3 scripts/plan.py new "<task title>" --steps "step 1" "step 2" ...` → creates `$DD_HOME/plans/<slug>.json`.
3. For each step: `plan.py start <slug> <n>` → do it (load the right skill) → `plan.py done <slug> <n> --note "<what/where>"` or `plan.py fail <slug> <n> --note "<error>"`.
4. Blocked (missing credential, destructive action needing go-ahead) → `plan.py block <slug> <n> --note "<what is needed>"`, finish every other step that doesn't depend on it, then report the blocker in one line. Don't stall the whole task.
5. Last step is always **verify**: run/lint/test the output, open the file, recompute the number. Mark done only when the check passed.
6. `plan.py report <slug>` → compact status for the reply (done/failed/blocked + notes). On restart: `plan.py list --open` to resume unfinished plans.

## Report shape (final reply)
```
<result line — what exists now / where>
<1-2 lines: what was verified, how>
Blocked: <item + what's needed>   (only if any)
Assumption: <...>                 (only if any)
```

## Rules
- Clear request → start immediately (first tool call in the first reply), no plan approval round.
- Expensive + ambiguous (hard to redo, contradictory) → one single question listing the branches, then execute.
- Scope = exactly what was asked. Extra ideas go in one "Next:" line, not into the work.
- A step that produces a file ends with the path. A step that changes state ends with how to undo it.
- Re-read the original request before `report`; every ask must map to a done step or an explicit "ithu pannala: <why>".

## Script
`scripts/plan.py new|start|done|fail|block|note|report|list|show|--selftest` — JSON out; plans in `$DD_HOME/plans/`.
