#!/usr/bin/env python3
"""mail.py — IMAP/SMTP helper for dd (stdlib only). Gmail app password.

  unread [--max N] | search "query" [--days N] | read UID [--max-chars N]
  draft --to A --subject S (--body FILE | --text T) [--reply-to-uid UID]
  send --draft FILE.eml --confirm "SEND CONFIRM"
  attach-save UID --out DIR
  --selftest
Env: MAIL_USER, MAIL_APP_PASSWORD, IMAP_HOST, SMTP_HOST, MAIL_FROM_NAME (loaded from $DD_HOME/.env too)
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import smtplib
import sys
import tempfile
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path

CONFIRM = "SEND CONFIRM"


def home() -> Path:
    h = Path(os.environ.get("DD_HOME", Path.home() / ".dd"))
    (h / "mail" / "drafts").mkdir(parents=True, exist_ok=True)
    return h


def load_env() -> None:
    p = home() / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def creds() -> tuple[str, str]:
    load_env()
    u, p = os.environ.get("MAIL_USER", ""), os.environ.get("MAIL_APP_PASSWORD", "")
    if not u or not p:
        raise RuntimeError("MAIL_USER / MAIL_APP_PASSWORD missing in $DD_HOME/.env")
    return u, p


def imap() -> imaplib.IMAP4_SSL:
    u, p = creds()
    m = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
    m.login(u, p)
    m.select("INBOX")
    return m


def hdr(msg, name: str) -> str:
    v = msg.get(name, "")
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def body_text(msg) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ct = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        if ct in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ct == "text/html":
                text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", text, flags=re.I)
                text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"&nbsp;", " ", text)
            parts.append(text)
            if ct == "text/plain":
                break
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "\n".join(parts))).strip()


def summarize(m: imaplib.IMAP4_SSL, uids: list[bytes], max_n: int) -> list[dict]:
    out = []
    for uid in reversed(uids[-max_n:]):
        _, data = m.uid("fetch", uid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(data[0][1])
        out.append({"uid": uid.decode(), "from": hdr(msg, "From"), "subject": hdr(msg, "Subject"),
                    "date": hdr(msg, "Date"), "snippet": body_text(msg)[:160]})
    return out


def unread(max_n: int = 20) -> list[dict]:
    m = imap()
    try:
        _, data = m.uid("search", None, "UNSEEN")
        return summarize(m, data[0].split(), max_n)
    finally:
        m.logout()


def search(q: str, days: int = 30, max_n: int = 20) -> list[dict]:
    m = imap()
    try:
        since = time.strftime("%d-%b-%Y", time.localtime(time.time() - days * 86400))
        _, data = m.uid("search", None, f'(SINCE {since} TEXT "{q}")')
        return summarize(m, data[0].split(), max_n)
    finally:
        m.logout()


def read(uid: str, max_chars: int = 6000) -> dict:
    m = imap()
    try:
        _, data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        msg = email.message_from_bytes(data[0][1])
        atts = [p.get_filename() for p in msg.walk() if p.get_content_disposition() == "attachment"]
        return {"uid": uid, "from": hdr(msg, "From"), "to": hdr(msg, "To"), "subject": hdr(msg, "Subject"),
                "date": hdr(msg, "Date"), "message_id": msg.get("Message-ID", ""), "attachments": atts,
                "body": body_text(msg)[:max_chars]}
    finally:
        m.logout()


def attach_save(uid: str, out_dir: str) -> dict:
    m = imap()
    try:
        _, data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        msg = email.message_from_bytes(data[0][1])
        saved = []
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for p in msg.walk():
            if p.get_content_disposition() == "attachment" and p.get_filename():
                fn = Path(out_dir) / re.sub(r"[^\w.\-]", "_", p.get_filename())
                fn.write_bytes(p.get_payload(decode=True) or b"")
                saved.append(str(fn))
        return {"ok": True, "saved": saved}
    finally:
        m.logout()


def draft(to: str, subject: str, body: str, reply_to_uid: str | None = None) -> dict:
    load_env()
    user = os.environ.get("MAIL_USER", "me@example.com")
    msg = EmailMessage()
    msg["From"] = formataddr((os.environ.get("MAIL_FROM_NAME", "Dhina"), user))
    msg["To"], msg["Subject"] = to, subject
    if reply_to_uid:
        try:
            orig = read(reply_to_uid, 10)
            if orig.get("message_id"):
                msg["In-Reply-To"] = msg["References"] = orig["message_id"]
        except Exception:
            pass
    msg.set_content(body)
    slug = re.sub(r"[^\w]+", "_", subject)[:40]  # outside the f-string: py3.10/3.11 forbid backslashes there
    path = home() / "mail" / "drafts" / f"{int(time.time())}_{slug}.eml"
    path.write_bytes(bytes(msg))
    return {"ok": True, "draft": str(path), "to": to, "subject": subject, "sent": False}


def send(draft_path: str, confirm: str | None) -> dict:
    if confirm != CONFIRM:
        return {"error": f"refused: needs --confirm \"{CONFIRM}\" after Dhina's explicit go-ahead for this draft"}
    u, p = creds()
    msg = email.message_from_bytes(Path(draft_path).read_bytes())
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(u, p)
        s.send_message(msg)
    sent = Path(draft_path).with_suffix(".sent.eml")
    Path(draft_path).rename(sent)
    return {"ok": True, "sent": True, "to": msg["To"], "subject": msg["Subject"], "file": str(sent)}


def selftest() -> int:
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-mail-")
    os.environ["MAIL_USER"] = "dd@example.com"
    d = draft("x@y.com", "Re: test", "hello\nbody")
    assert d["ok"] and Path(d["draft"]).exists() and d["sent"] is False
    assert "error" in send(d["draft"], None) and "error" in send(d["draft"], "yes")
    raw = (b"From: a@b.com\r\nSubject: Hi\r\nContent-Type: text/html\r\n\r\n<p>Hello <b>dd</b></p><script>x</script>")
    msg = email.message_from_bytes(raw)
    assert body_text(msg).startswith("Hello dd"), body_text(msg)
    print("mail selftest ok (draft, send gate, html strip; live IMAP needs credentials)")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    try:
        if cmd == "unread":
            res = unread(int(arg(argv, "--max", 20)))
        elif cmd == "search":
            res = search(argv[1], int(arg(argv, "--days", 30)), int(arg(argv, "--max", 20)))
        elif cmd == "read":
            res = read(argv[1], int(arg(argv, "--max-chars", 6000)))
        elif cmd == "draft":
            body = arg(argv, "--text") or Path(arg(argv, "--body")).read_text(encoding="utf-8")
            res = draft(arg(argv, "--to", ""), arg(argv, "--subject", ""), body, arg(argv, "--reply-to-uid"))
        elif cmd == "send":
            res = send(arg(argv, "--draft", ""), arg(argv, "--confirm"))
        elif cmd == "attach-save":
            res = attach_save(argv[1], arg(argv, "--out", "downloads"))
        else:
            print(__doc__)
            return 2
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
