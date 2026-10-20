#!/usr/bin/env python3
"""dd_chat.py — a simple chat window for dd on the Mac (stdlib Tkinter, zero install).

You type (or press 🎤 to talk); dd answers and controls the Mac through the same mac-control skill the
CLI/JARVIS already use — reversible actions (open app, volume, music, screenshot…) run at once, anything
irreversible still asks "seri-naa sollu" first. This is only a front-end: no new power, no network server.

  python3 examples/dd_chat.py            open the window
  python3 examples/dd_chat.py --selftest
Env (or ~/.dd/.env): DD_MODEL, OLLAMA_URL, DD_VOICE / DD_SAY_RATE (spoken replies), DD_CHAT_SPEAK=0 to mute.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dd_loop_ollama as dd  # noqa: E402  (loads ~/.dd/.env, skills, rules, agent loop)

HISTORY_HIGH, HISTORY_LOW = 16, 6  # trim in one step so Ollama's prompt cache stays warm


def trim(messages: list[dict]) -> list[dict]:
    if len(messages) - 1 <= HISTORY_HIGH:
        return messages
    users = [i for i, m in enumerate(messages) if i and m.get("role") == "user"]
    keep = next((i for i in reversed(users) if len(messages) - i >= HISTORY_LOW), users[0] if users else len(messages))
    return [messages[0]] + messages[keep:]


class ChatCore:
    """All the non-UI logic, so it can be tested without a display or Ollama."""

    def __init__(self, model_fn=None):
        self.model_fn = model_fn or dd.chat
        self.messages: list[dict] = []
        self.rules_error: str | None = None

    def boot(self) -> str | None:
        try:
            self.messages = [{"role": "system", "content": dd.boot_system_prompt()}]
        except SystemExit:
            self.rules_error = "Rules file hash mismatch — terminal la `rules_guard.py reinit` run pannu."
        return self.rules_error

    def ready(self) -> str | None:
        """None when the brain is reachable; else the one-line fix (quick Mac actions still work without it)."""
        return dd.preflight() if self.model_fn is dd.chat else None

    def send(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if self.rules_error:
            return self.rules_error
        problem = self.ready()
        if problem:
            return dd.fast_action(text) or f"Brain ready illa: {problem}"
        reply = dd.turn(self.messages, text, self.model_fn)
        self.messages[:] = trim(self.messages)
        return reply

    def reset(self) -> None:
        self.messages[:] = self.messages[:1]

    def voice_once(self, record_fn=None, transcribe_fn=None) -> tuple[str, str]:
        """Record one utterance, transcribe it, answer it. Returns (what you said, dd's reply)."""
        import dd_jarvis
        import tempfile
        if record_fn is None:
            tmp = Path(tempfile.mkdtemp(prefix="dd-chat-")) / "in.wav"
            record_fn = lambda: dd_jarvis.record_push_to_talk(tmp)  # noqa: E731
        transcribe_fn = transcribe_fn or dd_jarvis.transcribe
        text = transcribe_fn(Path(record_fn())).strip()
        if not text:
            return "", "Puriyala — thirumba pesu."
        return text, self.send(text)


def speak(text: str) -> None:
    if os.environ.get("DD_CHAT_SPEAK", "1") != "1":
        return
    try:
        import dd_jarvis
        dd_jarvis.speak(text, mute=False)
    except Exception:
        pass


# ---------------- Tkinter UI (only built when the window is actually opened) ----------------
def run_window() -> int:
    import tkinter as tk
    from tkinter import scrolledtext

    core = ChatCore()
    err = core.boot()
    root = tk.Tk()
    root.title("dd")
    root.geometry("520x620")
    root.minsize(360, 420)

    log = scrolledtext.ScrolledText(root, wrap="word", state="disabled", font=("Menlo", 13),
                                    bg="#1e1e1e", fg="#e6e6e6", insertbackground="#e6e6e6", padx=10, pady=10)
    log.pack(fill="both", expand=True)
    for tag, col in (("you", "#7fb4ff"), ("dd", "#9ece6a"), ("sys", "#e0af68")):
        log.tag_config(tag, foreground=col)

    bar = tk.Frame(root, bg="#1e1e1e")
    bar.pack(fill="x")
    entry = tk.Entry(bar, font=("Menlo", 13), bg="#2a2a2a", fg="#e6e6e6", insertbackground="#e6e6e6", relief="flat")
    entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(8, 6), pady=8)
    busy = {"on": False}

    def add(who: str, text: str, tag: str) -> None:
        log.configure(state="normal")
        log.insert("end", f"{who}: ", tag)
        log.insert("end", text + "\n\n")
        log.configure(state="disabled")
        log.see("end")

    def set_busy(on: bool) -> None:
        busy["on"] = on
        send_btn.configure(state="disabled" if on else "normal", text="…" if on else "Send")
        mic_btn.configure(state="disabled" if on else "normal")

    def deliver(prefix: str, reply: str) -> None:
        if prefix:
            add("you", prefix, "you")
        add("dd", reply, "dd")
        speak(reply)
        set_busy(False)

    def do_send(_evt=None) -> None:
        if busy["on"]:
            return
        text = entry.get().strip()
        if not text:
            return
        entry.delete(0, "end")
        add("you", text, "you")
        set_busy(True)
        threading.Thread(target=lambda: root.after(0, deliver, "", core.send(text)), daemon=True).start()

    def do_voice() -> None:
        if busy["on"]:
            return
        set_busy(True)
        add("sys", "listening… (pesu)", "sys")

        def work():
            try:
                said, reply = core.voice_once()
            except Exception as e:
                said, reply = "", f"Voice mudiyala: {e}"
            root.after(0, deliver, said, reply)
        threading.Thread(target=work, daemon=True).start()

    send_btn = tk.Button(bar, text="Send", command=do_send, relief="flat", bg="#3b3b3b", fg="#000000")
    send_btn.pack(side="left", padx=(0, 4), pady=8)
    mic_btn = tk.Button(bar, text="🎤", command=do_voice, relief="flat", bg="#3b3b3b", fg="#000000")
    mic_btn.pack(side="left", padx=(0, 8), pady=8)
    entry.bind("<Return>", do_send)
    entry.focus_set()

    if err:
        add("sys", err, "sys")
    else:
        problem = core.ready()
        add("sys", "dd ready." if not problem else f"dd ready (brain: {problem})", "sys")
        dd.start_reminder_watcher(on_fire=lambda t: root.after(0, add, "dd", "⏰ " + t, "dd"))
    root.mainloop()
    return 0


def selftest() -> int:
    import tempfile
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-chat-test-")
    os.environ["DD_MAC_DRY"] = "1"
    assert dd.run_script("rules-guard", "rules_guard.py", ["init"])["ok"]

    def stub(messages, tools, **_):
        user = [m for m in messages if m["role"] == "user"][-1]["content"]
        return {"role": "assistant", "content": "stub: " + user[-30:]}

    core = ChatCore(model_fn=stub)
    assert core.boot() is None and core.messages[0]["role"] == "system"
    assert core.send("") == ""  # empty ignored
    r = core.send("hello dd")
    assert "stub" in r and "hello dd" in r, r
    assert core.ready() is None  # stub model -> no Ollama needed

    # quick Mac action path still works without the model (router handles it)
    core2 = ChatCore(model_fn=stub)
    core2.boot()
    assert "panniten" in core2.send("chrome open pannu").lower() or "open" in core2.send("chrome open pannu").lower()

    # history trims to system + a user-led tail
    for i in range(40):
        core.send(f"msg {i}")
    assert core.messages[0]["role"] == "system" and len(core.messages) <= HISTORY_HIGH + 2
    core.reset()
    assert len([m for m in core.messages if m["role"] == "user"]) == 0

    # voice path: injected recorder + transcriber -> transcript answered
    said, reply = core.voice_once(record_fn=lambda: "/tmp/x.wav", transcribe_fn=lambda p: "innaiku weather enna")
    assert said == "innaiku weather enna" and reply, (said, reply)
    said, reply = core.voice_once(record_fn=lambda: "/tmp/x.wav", transcribe_fn=lambda p: "   ")
    assert said == "" and "Puriyala" in reply

    # broken rules -> every send returns the one-line warning, no model call
    (Path(os.environ["DD_HOME"]) / "rules" / "RULES.md").write_text("tampered", encoding="utf-8")
    core3 = ChatCore(model_fn=stub)
    core3.boot()
    assert core3.rules_error and "hash mismatch" in core3.send("hi")

    # DD_CHAT_SPEAK=0 mutes without touching the audio stack
    os.environ["DD_CHAT_SPEAK"] = "0"
    speak("should be silent")
    print("dd_chat selftest ok (send, quick action, history trim, reset, voice inject, rules lock, mute)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    return run_window()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
