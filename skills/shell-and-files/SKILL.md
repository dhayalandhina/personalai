---
name: shell-and-files
description: Safe shell execution and file operations for dd — run commands with timeout/output caps and a destructive-command gate, read/write/edit files with exact-match replacement, inspect directories. Use for any "run this", "check the server/logs/disk", "read/create/edit this file", or when another skill needs a command executed.
triggers: [run, execute, command, terminal, bash, file, folder, directory, read, write, edit, create file, ls, cat, grep, logs]
metadata:
  version: "1.0"
  owner: dhina
---

# shell-and-files

## Principles
1. **Read before write.** Never edit a file you haven't read this session. Never assume config values — `cat` them.
2. **Smallest change.** Edit = exact-string replace (`edit.py`), not rewriting the whole file, unless creating a new file.
3. **Show only the changed part** in the reply (diff-style) unless the file is new or Dhina asks for the whole thing.
4. **Batch** related checks into one command (`cmd1 && cmd2; cmd3`) instead of several round trips.
5. Destructive commands (`rm -rf`, `git reset --hard`, `drop`, `truncate`, `> file`, `chmod -R`, `kill -9`, `dd of=`) are gated: `safe_exec.py` refuses them unless `--allow-destructive` is passed, and dd passes that only after the `security-privacy` checklist (explicit go-ahead for Track A always, for Track B when irreversible).

## Commands
```bash
python3 scripts/safe_exec.py "df -h && free -m && uptime"            # JSON {ok, code, stdout, stderr, secs}
python3 scripts/safe_exec.py "tail -n 100 /var/log/syslog" --timeout 30 --max-output 20000
python3 scripts/safe_exec.py "rm -rf ./build" --allow-destructive       # only after the checklist
python3 scripts/files.py read PATH [--start 1 --lines 200]              # numbered lines
python3 scripts/files.py write PATH --content "..."  (or --from-file F) # creates dirs, refuses to overwrite unless --force
python3 scripts/files.py edit PATH --old "exact text" --new "replacement"   # must match exactly once
python3 scripts/files.py tree DIR [--depth 2]
python3 scripts/files.py find DIR --name "*.py" [--grep "pattern"]
```

## Path rules
- Work inside `$DD_WORKDIR` (default `~/dd-work`) unless Dhina names another path. Never touch `~/.dd/rules`.
- Track A repo paths and Track B repo paths never appear in the same command.
- Paths in replies go in code blocks; say "un folder la" in prose.
- Temp files → `$DD_HOME/tmp`, not `/tmp` (survives, easy to clean).

## Error handling
Command fails → read the stderr, fix the cause, rerun once. Same failure twice → report the exact error line and what was tried; don't loop.
