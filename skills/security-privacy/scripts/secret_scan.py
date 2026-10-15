#!/usr/bin/env python3
"""secret_scan.py — find leaked secrets in text or files before dd stores or sends them.

  secret_scan.py --text "..."        scan a string
  secret_scan.py --file path [...]   scan files (text files only)
  secret_scan.py --redact --text T   print the text with secrets masked
  secret_scan.py --selftest
Exit 1 if anything was found (so callers can gate on it), 0 if clean.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}\b")),
    ("supabase_jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("password_assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|db_pass|secret|api[_\-]?key|token)\s*[=:]\s*['\"]?([^\s'\"]{6,})")),
    ("connection_string", re.compile(r"(?i)\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis)://[^\s:]+:[^\s@]+@")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("indian_aadhaar", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b")),
]

ALLOWLIST_HINTS = ("example", "placeholder", "your_", "<", "xxxx", "REDACTED")


def luhn_ok(num: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", num)]
    if len(digits) < 13:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask(s: str) -> str:
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "***"


def scan_text(text: str) -> list[dict]:
    findings = []
    for ln, line in enumerate(text.splitlines(), 1):
        for kind, pat in PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(1) if (m.groups() and kind == "password_assignment") else m.group(0)
                if kind == "card_number" and not luhn_ok(raw):
                    continue
                if kind == "indian_aadhaar" and (luhn_ok(raw) or len(re.sub(r"\D", "", raw)) != 12):
                    continue
                if any(h.lower() in raw.lower() for h in ALLOWLIST_HINTS):
                    continue
                findings.append({"type": kind, "line": ln, "sample": mask(raw)})
    return findings


def redact(text: str) -> str:
    out = text
    for kind, pat in PATTERNS:
        def _r(m: re.Match) -> str:
            raw = m.group(0)
            if kind == "card_number" and not luhn_ok(raw):
                return raw
            if any(h.lower() in raw.lower() for h in ALLOWLIST_HINTS):
                return raw
            if kind == "password_assignment" and m.groups():
                return raw.replace(m.group(1), "***REDACTED***")
            return "***REDACTED***"
        out = pat.sub(_r, out)
    return out


def selftest() -> int:
    sample = "db_pass=SuperSecret123\nnormal line\nAKIAABCDEFGHIJKLMNOP\ncard 4111 1111 1111 1111\nexample key=your_api_key_here"
    f = scan_text(sample)
    kinds = {x["type"] for x in f}
    assert "password_assignment" in kinds and "aws_access_key" in kinds and "card_number" in kinds, kinds
    assert not any(x["line"] == 5 for x in f), "placeholder must be ignored"
    r = redact(sample)
    assert "SuperSecret123" not in r and "AKIAABCDEFGHIJKLMNOP" not in r
    assert scan_text("plain text, no secrets here, version 2.5.1") == []
    print("secret_scan selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    do_redact = "--redact" in argv
    texts: list[tuple[str, str]] = []
    if "--text" in argv:
        texts.append(("<text>", argv[argv.index("--text") + 1]))
    if "--file" in argv:
        for p in argv[argv.index("--file") + 1:]:
            if p.startswith("--"):
                break
            try:
                texts.append((p, Path(p).read_text(encoding="utf-8", errors="replace")))
            except OSError as e:
                print(json.dumps({"error": f"{p}: {e}"}))
                return 2
    if not texts:
        print(__doc__)
        return 2
    if do_redact:
        for _, t in texts:
            print(redact(t))
        return 0
    all_f = []
    for src, t in texts:
        for x in scan_text(t):
            x["source"] = src
            all_f.append(x)
    print(json.dumps({"findings": all_f, "count": len(all_f)}, indent=1))
    return 1 if all_f else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
