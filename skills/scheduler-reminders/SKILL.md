---
name: scheduler-reminders
description: Reminders and scheduled jobs for dd — "remind me in 2h / tomorrow 9am / every Monday", recurring tasks (morning briefing, nightly learning, backups) in a SQLite queue that dd's loop polls, with cron/launchd setup for the recurring system jobs. Use when Dhina asks to be reminded, to schedule or repeat something, or asks what's pending today.
triggers: [remind, reminder, schedule, every day, every week, tomorrow, at 9, alarm, cron, recurring, nyabagapaduthu, pending today]
metadata:
  version: "1.0"
  owner: dhina
---

# scheduler-reminders

Store: `$DD_HOME/reminders.db`. Times are IST (`Asia/Kolkata`) unless Dhina says otherwise. Delivery = `telegram-interface`.

## Commands
```bash
python3 scripts/reminders.py add "call AguaClan about invoice" --at "tomorrow 9am"
python3 scripts/reminders.py add "water plants" --in 2h
python3 scripts/reminders.py add "weekly review" --cron "0 18 * * 5"          # Fri 18:00
python3 scripts/reminders.py add "morning brief" --cron "30 7 * * *" --action "daily-briefing"   # runs a skill, not just a message
python3 scripts/reminders.py list [--all]
python3 scripts/reminders.py due                 # what the loop should fire now (JSON); marks one-offs done, reschedules cron ones
python3 scripts/reminders.py done ID | delete ID | snooze ID --in 30m
```
Accepted `--at`: `9am`, `18:30`, `tomorrow 9am`, `mon 10am`, `25 oct 6pm`, `2026-10-25 18:00`. `--in`: `45m`, `2h`, `3d`, `1w`.

## dd loop integration
Every 30 s: `reminders.py due` → for each item: if `action` is set, load that skill and run it (e.g. `daily-briefing`, `self-learning daily`, `system-ops backup`), else send the text via Telegram. Never fire the same id twice (the script handles it).

## System jobs to install once (cron, free)
```
30 7  * * *  cd ~/personalai && python3 skills/daily-briefing/scripts/briefing.py --json > ~/.dd/tmp/brief.json && python3 skills/telegram-interface/scripts/tg_send.py file ~/.dd/tmp/brief.json
0  23 * * *  cd ~/personalai && python3 skills/self-learning/scripts/learn.py daily
0  2  * * 0  cd ~/personalai && bash skills/system-ops/scripts/backup.sh
```
(or add them as `--cron` reminders with `--action` and let dd's loop run them — one mechanism, no OS cron needed.)

## Rules
- Confirm in one line with the resolved absolute time: "Ok — 16-Oct 09:00 IST ku remind panren."
- Ambiguous time (just "evening") → pick a sensible default (18:00) and state the assumption.
- Reminders are not memory: don't store them as facts. Completed items stay in the db for 30 days then are pruned (`--prune`).
