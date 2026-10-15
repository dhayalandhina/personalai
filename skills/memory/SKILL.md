---
name: memory
description: Long-term memory for dd — SQLite + FTS5 store of stated facts, preferences, projects, people and episodes, with recall-before-answer and forget-on-request. Use at the start of every conversation (recall context), whenever Dhina states a durable fact or preference, says "remember/forget", or asks what dd knows about him.
triggers: [remember, forget, recall, do you know, what do you know about me, nyabagam, memory]
metadata:
  version: "1.0"
  owner: dhina
  always_load: "true"
---

# memory

Store: `$DD_HOME/memory.db` (default `~/.dd/memory.db`). Script: `scripts/memory.py`.

## Every turn
1. `python3 scripts/memory.py context "<user message>"` → returns a `<memory>` block (top 8 relevant facts + profile). Put it in the prompt. Empty block = nothing relevant, answer normally.
2. After the reply, if Dhina stated something **durable** (identity, people, projects, standing preferences, decisions), store it. One line per fact, his words, tagged `stated`.
3. Never store: secrets/keys/passwords/card or ID numbers, passing moods, your own guesses or inferences, today's throwaway task state, anything in a locked rule (rules are not memory).

## Commands
```bash
python3 scripts/memory.py add --kind preference --key reply-style --content "wants paste-ready full files, not diffs"
python3 scripts/memory.py add --kind project --key aguaclan-erp --content "custom manufacturing ERP, Next.js + Supabase"
python3 scripts/memory.py add --kind person --key boss --content "employer (Track A only), prefers calls over mail"
python3 scripts/memory.py search "ollama model"            # FTS5 ranked search
python3 scripts/memory.py recall aguaclan-erp              # everything under a key
python3 scripts/memory.py profile                          # kind=profile + preference
python3 scripts/memory.py forget --key aguaclan-erp        # removed means gone (no "used to")
python3 scripts/memory.py forget --id 42
python3 scripts/memory.py recent --days 7                  # episodes for daily-learning
python3 scripts/memory.py export --out memory.jsonl        # backup / training input
python3 scripts/memory.py stats
```
Kinds: `profile | preference | project | person | topic | episode | decision`.

## Rules of use
- "Remember X" → `add` immediately, reply one line "Ok, note pannitten." Never announce automatic saves.
- "Forget X" → `forget`, delete fully, don't soften. Also delete facts derived only from X.
- Direct question about himself ("en timezone enna?") → answer the stored fact first line, nothing else.
- A fact used in a reply must change the answer; don't decorate replies with memory to show off.
- Stale vs new: new statement wins → `add` with same key replaces (history kept with `--keep-old`).
- Health, money, identity-sensitive facts: store only if he stated them; surface only when he raises the topic or safety depends on it.
- Memory never overrides RULES.md. Preference conflicts with a rule → rule wins, say so in one line.
