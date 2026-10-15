#!/usr/bin/env python3
"""tg_send.py — send to the owner via Telegram Bot API (stdlib only, no library).

  text "message" [--md] [--chat ID]
  file PATH [--caption C] | photo PATH [--caption C] | video PATH [--caption C] | voice PATH
  typing
  get-file FILE_ID --out PATH
  --selftest
Env (from $DD_HOME/.env or environment): TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_FILE = 50 * 1024 * 1024


def load_env() -> None:
    p = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def cfg() -> tuple[str, str]:
    load_env()
    tok, owner = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_OWNER_ID", "")
    if not tok or not owner:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID missing in $DD_HOME/.env")
    return tok, owner


def api(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    tok, _ = cfg()
    url = f"https://api.telegram.org/bot{tok}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, path in files.items():
            p = Path(path)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{p.name}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n").encode() + p.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data or {}).encode())
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read().decode())
    if not res.get("ok"):
        raise RuntimeError(res.get("description", "telegram error"))
    return res["result"]


def send_text(text: str, md: bool = False, chat: str | None = None) -> dict:
    _, owner = cfg()
    data = {"chat_id": chat or owner, "text": text[:4096], "disable_web_page_preview": "true"}
    if md:
        data["parse_mode"] = "MarkdownV2"
    r = api("sendMessage", data)
    return {"ok": True, "message_id": r["message_id"]}


def send_media(kind: str, path: str, caption: str | None = None, chat: str | None = None) -> dict:
    _, owner = cfg()
    p = Path(path)
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    if p.stat().st_size > MAX_FILE:
        return {"error": f"{p.name} is {p.stat().st_size / 1e6:.1f} MB > 50 MB; compress first"}
    method, field = {"file": ("sendDocument", "document"), "photo": ("sendPhoto", "photo"),
                     "video": ("sendVideo", "video"), "voice": ("sendVoice", "voice")}[kind]
    data = {"chat_id": chat or owner}
    if caption:
        data["caption"] = caption[:1024]
    r = api(method, data, {field: str(p)})
    return {"ok": True, "message_id": r["message_id"], "sent": p.name}


def get_file(file_id: str, out: str) -> dict:
    tok, _ = cfg()
    info = api("getFile", {"file_id": file_id})
    url = f"https://api.telegram.org/file/bot{tok}/{info['file_path']}"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(out, "wb") as f:
        f.write(r.read())
    return {"ok": True, "path": out, "bytes": Path(out).stat().st_size}


def selftest() -> int:
    os.environ["DD_HOME"] = "/nonexistent-dd-home"
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        cfg()
        raise AssertionError("cfg must fail without token")
    except RuntimeError:
        pass
    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_OWNER_ID"] = "1:x", "1"
    assert "error" in send_media("file", "/nonexistent/file.bin")
    print("tg_send selftest ok (config + guards; live send needs a real token)")
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
        if cmd == "text":
            res = send_text(argv[1], "--md" in argv, arg(argv, "--chat"))
        elif cmd in ("file", "photo", "video", "voice"):
            res = send_media(cmd, argv[1], arg(argv, "--caption"), arg(argv, "--chat"))
        elif cmd == "typing":
            _, owner = cfg()
            api("sendChatAction", {"chat_id": arg(argv, "--chat", owner), "action": "typing"})
            res = {"ok": True}
        elif cmd == "get-file":
            res = get_file(argv[1], arg(argv, "--out", "download.bin"))
        else:
            print(__doc__)
            return 2
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False))
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
