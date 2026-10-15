#!/usr/bin/env python3
"""tg_format.py — Telegram message formatting helpers (stdlib only).

  chunk (--text T | --file F) [--limit 4000]     JSON list of parts; fenced code blocks are never split
  escape "text"                                   MarkdownV2-escape plain text
  render (--text T | --file F)                    simple Markdown (**bold**, `code`, ```fences```, [a](url), # heads) -> MarkdownV2
  --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LIMIT = 4000  # Telegram hard limit is 4096
SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape(text: str) -> str:
    return "".join("\\" + c if c in SPECIAL else c for c in text)


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    blocks = re.split(r"(```[\s\S]*?```)", text)
    parts, cur = [], ""

    def flush():
        nonlocal cur
        if cur.strip():
            parts.append(cur.rstrip())
        cur = ""

    for b in blocks:
        if not b:
            continue
        if b.startswith("```"):
            if len(cur) + len(b) > limit:
                flush()
            if len(b) > limit:  # giant code block: split inside, re-fence each piece
                lang = b.split("\n", 1)[0][3:]
                body = b.strip("`").split("\n", 1)[1] if "\n" in b else ""
                lines, piece = body.rstrip("`").splitlines(), ""
                overhead = 8 + len(lang)  # ```lang\n ... ```
                for ln in lines:
                    if len(piece) + len(ln) + 1 + overhead > limit:
                        parts.append(f"```{lang}\n{piece}```")
                        piece = ""
                    piece += ln + "\n"
                if piece:
                    parts.append(f"```{lang}\n{piece}```")
            else:
                cur += b
            continue
        for para in re.split(r"(\n\n)", b):
            if len(cur) + len(para) <= limit:
                cur += para
                continue
            flush()
            while len(para) > limit:
                cut = para.rfind("\n", 0, limit)
                cut = cut if cut > limit // 2 else limit
                parts.append(para[:cut].rstrip())
                para = para[cut:].lstrip("\n")
            cur = para
    flush()
    return parts


def render(md: str) -> str:
    """Minimal Markdown -> MarkdownV2."""
    out = []
    for seg in re.split(r"(```[\s\S]*?```|`[^`\n]+`)", md):
        if not seg:
            continue
        if seg.startswith("```"):
            lang, _, body = seg[3:-3].partition("\n")
            body = body.replace("\\", "\\\\").replace("`", "\\`")
            out.append(f"```{lang}\n{body}```")
        elif seg.startswith("`"):
            out.append("`" + seg[1:-1].replace("\\", "\\\\").replace("`", "\\`") + "`")
        else:
            tokens = re.split(r"(\*\*[^*\n]+\*\*|\[[^\]]+\]\([^)\s]+\))", seg)
            buf = ""
            for t in tokens:
                if not t:
                    continue
                if t.startswith("**"):
                    buf += "*" + escape(t[2:-2]) + "*"
                elif t.startswith("["):
                    m = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", t)
                    url = m.group(2).replace(")", "\\)")  # outside the f-string: py3.10/3.11 compat
                    buf += f"[{escape(m.group(1))}]({url})"
                else:
                    t = re.sub(r"^(#{1,3})\s+(.*)$", lambda m: f"**{m.group(2)}**", t, flags=re.M)
                    for sub in re.split(r"(\*\*[^*\n]+\*\*)", t):
                        buf += ("*" + escape(sub[2:-2]) + "*") if sub.startswith("**") else escape(sub)
            out.append(buf)
    return "".join(out)


def selftest() -> int:
    assert escape("a_b.c!") == "a\\_b\\.c\\!"
    code = "```python\n" + "x = 1\n" * 300 + "```"
    parts = chunk("intro\n\n" + code + "\n\noutro " + "word " * 900, 1200)
    assert all(len(p) <= 1200 for p in parts), [len(p) for p in parts]
    assert all(p.count("```") % 2 == 0 for p in parts), "unbalanced fences"
    assert parts[0].startswith("intro")
    r = render("# Title\nHello **world** `a_b` [doc](https://x.y/a_b) end.\n```sh\nls -la\n```")
    assert r.startswith("*Title*") and "*world*" in r and "`a_b`" in r and "[doc](https://x.y/a_b)" in r and "end\\." in r, r
    assert "```sh\nls -la\n```" in r
    assert chunk("short") == ["short"]
    print("tg_format selftest ok")
    return 0


def get_text(argv: list[str]) -> str:
    if "--text" in argv:
        return argv[argv.index("--text") + 1]
    if "--file" in argv:
        return Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
    return argv[1] if len(argv) > 1 else ""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    if cmd == "chunk":
        limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else LIMIT
        print(json.dumps(chunk(get_text(argv), limit), ensure_ascii=False, indent=1))
    elif cmd == "escape":
        print(escape(get_text(argv)))
    elif cmd == "render":
        print(render(get_text(argv)))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
