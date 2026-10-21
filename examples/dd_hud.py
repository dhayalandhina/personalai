#!/usr/bin/env python3
"""dd_hud.py — dd's on-screen voice panel (Siri-style) for the Mac app.

A small floating, click-through pill at the bottom of the screen. It appears the moment "hey jarvis" is heard:
blue voice bars that move with your voice while it listens, a slow wave while it thinks, a quicker one while it
speaks — and it fades out when the conversation is over. That is all it shows by default (nothing covers the
screen); DD_HUD=full adds the words it understood and the reply. It never takes focus and never touches
the keyboard or mouse. Pure AppKit through PyObjC (which rumps already needs); if AppKit is missing or anything
fails, dd_app falls back to notifications + the menu-bar icon.

Headless-testable: HudModel decides *what* to show from the listener's state; HudPanel (AppKit) only draws it.
  python3 examples/dd_hud.py --selftest
"""
from __future__ import annotations

import math
import os
import sys
import time

W, H, R = 440, 84, 24          # panel size (points) and corner radius
BARS, BAR_W, BAR_GAP = 5, 5, 9  # the voice meter on the left
TEXT_X = 86


def level_to_energy(rms: float) -> float:
    """Mic RMS -> 0..1 for the bars (log scale: a whisper already moves them, shouting pins them)."""
    lo, hi = math.log(150.0), math.log(4000.0)
    if rms <= 150:
        return 0.0
    return max(0.0, min(1.0, (math.log(rms) - lo) / (hi - lo)))


class HudModel:
    """What the panel shows right now, from the listener's public state (no AppKit here)."""
    PROBLEM_S = 8.0   # a mic problem stays on screen this long

    def __init__(self, listener, clock=time.monotonic):
        self.l, self.clock = listener, clock
        self.problem, self.problem_at = "", 0.0

    def report(self, msg: str) -> None:
        self.problem, self.problem_at = msg, self.clock()

    def frame(self) -> dict | None:
        """None = hidden. Else {"mode": listen|think|speak|wait|error, "title", "sub", "energy": 0..1}."""
        l, now = self.l, self.clock()
        if self.problem and now - self.problem_at < self.PROBLEM_S:
            return {"mode": "error", "title": "Mic problem", "sub": self.problem[:120], "energy": 0.0}
        st = l.state
        if st in ("paused", "stopped", "starting", "error"):
            return None
        window = max(l.armed_until, l.followup_until)
        if st == "recording":
            left = max(0.0, window - now)
            return {"mode": "listen", "title": "Kekkuren… sollu", "sub": f"hey jarvis ✓  ·  {left:.0f} s",
                    "energy": level_to_energy(l.level)}
        if st == "thinking":
            title = f"“{l.last_command}”" if l.last_command else "…"
            return {"mode": "think", "title": title, "sub": "yosikkiren…", "energy": 0.35}
        if st == "speaking":
            return {"mode": "speak", "title": l.last_said or "…", "sub": getattr(l, "last_timing", "") or "dd", "energy": 0.6}
        if now < l.followup_until:  # idle, but you may continue without the wake word
            return {"mode": "wait", "title": l.last_said or "…",
                    "sub": f"innum edhavadhu? sollu  ·  {l.followup_until - now:.0f} s",
                    "energy": level_to_energy(l.level)}
        return None


def bar_heights(energy: float, mode: str, t: float) -> list[float]:
    """Five bar heights (points) for the meter: your voice drives them while listening; a slow wave while
    thinking; a quicker one while dd speaks; flat when idle/error."""
    base, span = 6.0, H * 0.5
    out = []
    for i in range(BARS):
        if mode == "listen" or mode == "wait":
            wob = 0.6 + 0.4 * math.sin(t * 9.0 + i * 1.7)
            h = base + span * energy * wob
        elif mode == "think":
            h = base + span * 0.25 * (0.5 + 0.5 * math.sin(t * 2.5 + i * 0.9))
        elif mode == "speak":
            h = base + span * 0.45 * (0.5 + 0.5 * math.sin(t * 7.0 + i * 1.1))
        else:
            h = base
        out.append(max(base, min(span + base, h)))
    return out


class HudPanel:
    """The AppKit panel. Construct only on macOS inside the app's main thread (rumps timer).
    style "bars" (default): just the small pill with the blue voice bars — nothing covers the screen.
    style "full": bars + what dd heard + its reply (DD_HUD=full)."""

    def __init__(self, style: str | None = None):
        import AppKit  # pyobjc-framework-Cocoa (installed with rumps)
        from Foundation import NSMakeRect
        self.AppKit, self.NSMakeRect = AppKit, NSMakeRect
        self.style = style or ("full" if os.environ.get("DD_HUD", "bars").lower() == "full" else "bars")
        bars_w = BARS * BAR_GAP - (BAR_GAP - BAR_W)
        if self.style == "bars":
            self.W, self.H, self.R = bars_w + 2 * 14, 36, 18
        else:
            self.W, self.H, self.R = W, H, R
        bar_x0 = 14 if self.style == "bars" else 26
        AppKit.NSApplication.sharedApplication()  # the window server connection must exist before any window
        rect = NSMakeRect(0, 0, self.W, self.H)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(rect, 1 << 7, 2, False)
        panel.setLevel_(25)                                # status-window level: above full-screen video too
        panel.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 8))  # all spaces · stationary · full-screen aux
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setAlphaValue_(0.0)
        try:
            panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))
        except Exception:
            pass
        fx = AppKit.NSVisualEffectView.alloc().initWithFrame_(rect)
        fx.setMaterial_(13)        # HUD window material: dark, translucent
        fx.setBlendingMode_(0)     # behind window
        fx.setState_(1)            # always active
        fx.setWantsLayer_(True)
        try:
            fx.layer().setCornerRadius_(self.R)
            fx.layer().setMasksToBounds_(True)
        except Exception:
            pass
        panel.setContentView_(fx)
        tint = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.42, 0.72, 1.0, 1.0)
        self.bars = []
        for i in range(BARS):
            b = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(bar_x0 + i * BAR_GAP, self.H / 2 - 3, BAR_W, 6))
            b.setBoxType_(4)       # custom
            b.setBorderWidth_(0)
            b.setCornerRadius_(BAR_W / 2)
            b.setFillColor_(tint)
            fx.addSubview_(b)
            self.bars.append(b)
        self.title = self.sub = None
        if self.style == "full":
            self.title = AppKit.NSTextField.wrappingLabelWithString_("")
            self.title.setFrame_(NSMakeRect(TEXT_X, 30, W - TEXT_X - 20, 46))
            self.title.setFont_(AppKit.NSFont.systemFontOfSize_weight_(15.0, 0.23))
            self.title.setTextColor_(AppKit.NSColor.whiteColor())
            self.title.setMaximumNumberOfLines_(2)
            try:
                self.title.cell().setTruncatesLastVisibleLine_(True)
            except Exception:
                pass
            self.title.setSelectable_(False)
            fx.addSubview_(self.title)
            self.sub = AppKit.NSTextField.labelWithString_("")
            self.sub.setFrame_(NSMakeRect(TEXT_X, 9, W - TEXT_X - 20, 18))
            self.sub.setFont_(AppKit.NSFont.systemFontOfSize_(11.5))
            self.sub.setTextColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7))
            self.sub.setLineBreakMode_(4)  # truncate tail
            fx.addSubview_(self.sub)
        self.panel, self.fx, self.tint = panel, fx, tint
        self.alpha, self.shown = 0.0, False
        self._title, self._sub, self._mode = None, None, None

    def place(self) -> None:
        scr = self.AppKit.NSScreen.mainScreen()
        if scr is None:
            return
        vf = scr.visibleFrame()
        x = vf.origin.x + (vf.size.width - self.W) / 2
        y = vf.origin.y + 28
        self.panel.setFrameOrigin_((x, y))

    def render(self, frame: dict | None, t: float) -> None:
        target = 1.0 if frame else 0.0
        if frame:
            if not self.shown:
                self.place()
                self.panel.orderFrontRegardless()
                self.shown = True
            if self.title is not None and frame["title"] != self._title:
                self._title = frame["title"]
                self.title.setStringValue_(frame["title"])
            if self.sub is not None and frame["sub"] != self._sub:
                self._sub = frame["sub"]
                self.sub.setStringValue_(frame["sub"])
            if frame["mode"] != self._mode:
                self._mode = frame["mode"]
                red = self.AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.45, 0.4, 1.0)
                for b in self.bars:
                    b.setFillColor_(red if frame["mode"] == "error" else self.tint)
            scale = (self.H * 0.6) / (H * 0.5) if self.style == "bars" else 1.0  # the small pill uses its height
            for b, h in zip(self.bars, bar_heights(frame["energy"], frame["mode"], t)):
                f = b.frame()
                hh = 4.0 + (h - 6.0) * scale if self.style == "bars" else h
                b.setFrame_(self.NSMakeRect(f.origin.x, self.H / 2 - hh / 2, BAR_W, hh))
        step = 0.14
        self.alpha = max(0.0, min(1.0, self.alpha + max(-step, min(step, target - self.alpha))))
        self.panel.setAlphaValue_(self.alpha)
        if self.alpha <= 0.0 and self.shown and not frame:
            self.panel.orderOut_(None)
            self.shown = False
            self._mode = None


class Hud:
    """Model + panel; call tick() from the app's main-thread timer (~20 Hz). Disables itself after errors."""

    def __init__(self, listener, panel=None, clock=time.monotonic):
        self.model = HudModel(listener, clock=clock)
        self.panel = panel if panel is not None else HudPanel()
        self.clock = clock
        self.errors = 0
        self.ok = True

    def report(self, msg: str) -> None:
        self.model.report(msg)

    def tick(self) -> dict | None:
        if not self.ok:
            return None
        try:
            frame = self.model.frame()
            self.panel.render(frame, self.clock())
            self.errors = 0
            return frame
        except Exception as e:
            self.errors += 1
            if self.errors >= 5:
                self.ok = False
                print(f"(hud disabled: {type(e).__name__}: {str(e)[:120]})", flush=True)
            return None


def selftest() -> int:
    class L:  # the slice of dd_listen.Listener the HUD reads
        state, level, armed_until, followup_until, last_command, last_said, woke_at = "starting", 0.0, 0.0, 0.0, "", "", 0.0

    now = {"t": 100.0}
    clock = lambda: now["t"]  # noqa: E731
    lis = L()
    m = HudModel(lis, clock=clock)
    assert m.frame() is None                       # starting: nothing on screen
    lis.state = "listening"
    assert m.frame() is None                       # idle: hidden (the menu-bar 🤖 is enough)
    lis.state, lis.armed_until, lis.level = "recording", 108.0, 900.0
    f = m.frame()
    assert f["mode"] == "listen" and "sollu" in f["title"] and "8 s" in f["sub"] and 0.3 < f["energy"] < 0.8, f
    lis.state, lis.last_command = "thinking", "chrome open pannu"
    f = m.frame()
    assert f["mode"] == "think" and "chrome open pannu" in f["title"], f
    lis.state, lis.last_said = "speaking", "Chrome open pannitten."
    assert m.frame()["mode"] == "speak" and m.frame()["title"] == "Chrome open pannitten."
    lis.state, lis.followup_until = "listening", 112.0
    f = m.frame()
    assert f["mode"] == "wait" and "12 s" in f["sub"] and f["title"] == "Chrome open pannitten.", f
    now["t"] = 113.0
    assert m.frame() is None                       # follow-up window over: fades out
    m.report("mic error: no device")
    assert m.frame()["mode"] == "error"
    now["t"] = 122.0
    assert m.frame() is None                       # problem shown for 8 s only
    lis.state = "paused"
    assert m.frame() is None
    # energy + bars
    assert level_to_energy(100) == 0.0 and level_to_energy(5000) == 1.0 and 0.4 < level_to_energy(900) < 0.7
    hs = bar_heights(1.0, "listen", 0.3)
    assert len(hs) == BARS and all(6.0 <= h <= H * 0.5 + 6.0 for h in hs) and max(hs) > 20, hs
    assert bar_heights(0.0, "listen", 0.0) == [6.0] * BARS and bar_heights(0.0, "error", 0.0) == [6.0] * BARS

    # the Hud wrapper with a fake panel: renders frames, survives a failing panel, disables after 5 errors
    frames = []

    class FakePanel:
        def render(self, frame, t):
            frames.append(frame)

    lis.state, lis.followup_until = "speaking", 0.0
    h = Hud(lis, panel=FakePanel(), clock=clock)
    assert h.tick()["mode"] == "speak" and frames[-1]["mode"] == "speak"

    class BadPanel:
        def render(self, frame, t):
            raise RuntimeError("no screen")

    bad = Hud(lis, panel=BadPanel(), clock=clock)
    for _ in range(5):
        assert bad.tick() is None
    assert bad.ok is False
    assert "AppKit" not in sys.modules, "AppKit must only be imported inside HudPanel"
    print("dd_hud selftest ok (frames per state, follow-up linger, problem banner, bars, fake panel, self-disable)")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else (print(__doc__) or 0))
