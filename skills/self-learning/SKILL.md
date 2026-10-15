---
name: self-learning
description: dd learns from daily usage without touching the locked rules — logs every exchange, captures feedback/corrections, extracts standing preferences into memory, and exports clean SFT/DPO training data for fine-tuning dd's own model. Use after every reply (log), when Dhina corrects or praises an answer, and in the nightly learning job.
triggers: [learn, train, feedback, correction, that's wrong, correct answer is, from now on, always do, never do, fine-tune, training data]
metadata:
  version: "1.0"
  owner: dhina
---

# self-learning

Learning loop = **log → feedback → extract → export**. Nothing here edits RULES.md (locked). Learned preferences live in memory and lose to rules on conflict.

## 1. Log (every turn, silent)
```bash
python3 scripts/learn.py log --user "<msg>" --assistant "<reply>" --skill web-search
```
Appends to `$DD_HOME/learning/interactions.jsonl` with timestamp + id. Secrets are redacted before writing.

## 2. Feedback (when Dhina reacts)
Signals: "wrong", "illa", "correct answer is…", "super", "ithu than", 👍/👎, or he pastes a fixed version.
```bash
python3 scripts/learn.py feedback --id 17 --rating bad --correction "<the right answer / fixed code>"
python3 scripts/learn.py feedback --last --rating good
```
A correction becomes the preferred target for that prompt; dd's original becomes the rejected one.

## 3. Extract preferences (nightly, or when he says "from now on / always / never")
```bash
python3 scripts/learn.py extract            # proposes [{key, content, evidence}] as JSON
python3 scripts/learn.py extract --apply    # writes them to memory as kind=preference
```
Only explicit standing statements are extracted ("always", "never", "from now on", "ini", "eppovum", "prefer", "don't"). Single mentions are not generalised.

## 4. Export training data
```bash
python3 scripts/learn.py export --format sft --out $DD_HOME/learning/sft.jsonl   # {messages:[system,user,assistant]}
python3 scripts/learn.py export --format dpo --out $DD_HOME/learning/dpo.jsonl   # {prompt, chosen, rejected}
```
SFT uses good-rated replies and corrections; DPO pairs corrected answers against the rejected originals. System message = current RULES.md text (so the model learns under the real rules). Use with Unsloth / LLaMA-Factory / Axolotl (all free, local GPU or free Colab T4) → GGUF → `ollama create dd -f Modelfile`.

## 5. Nightly job (via scheduler-reminders)
```bash
python3 scripts/learn.py daily     # = extract --apply + export both + stats
```

## Rules
- Log everything, train only on `good`, `fixed` or corrected items. Unrated turns are not training data.
- Never put rules into learned preferences; never let a learned preference contradict a rule — if it does, drop it and tell Dhina in one line.
- Current-world facts are never "learned" into weights or memory as truth (they go stale). Only behaviour, style and his durable facts.
- Export after secret redaction and `security-privacy` scan; nothing from Track A code ever enters a training file.
