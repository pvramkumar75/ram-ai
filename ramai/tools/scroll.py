"""Human-like scrolling: real mouse-wheel notches with natural pacing, and 'scroll until you find it' for any app.

scroll_to_text works in WhatsApp, Outlook, Word, PDFs, browsers, Excel... anything that scrolls with the wheel:
move the pointer onto the pane, roll the wheel a few notches, let the content settle, read the pane (local Windows OCR),
repeat until the requested text is visible or the top/bottom is reached."""
from __future__ import annotations

import ctypes
import difflib
import random
import time
from typing import Literal

from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

WHEEL_DELTA = 120                      # one real mouse-wheel notch (about 3 lines in most apps)
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
PANES = {"left": (0.0, 0.42), "center": (0.22, 0.78), "right": (0.40, 1.0), "full": (0.0, 1.0)}


def wheel(notches: int, horizontal: bool = False, human: bool = True, stop=None) -> None:
    """Roll the wheel like a hand does: one notch at a time with small, uneven pauses. +up/right, -down/left."""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    step = 1 if notches > 0 else -1
    for i in range(abs(notches)):
        if stop and stop():
            raise PolicyError("Stopped by user")
        ctypes.windll.user32.mouse_event(flag, 0, 0, ctypes.c_int(step * WHEEL_DELTA), 0)
        if human:
            time.sleep(random.uniform(0.025, 0.06) if i % 4 != 3 else random.uniform(0.08, 0.14))


class _PT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _window_at(x: int, y: int) -> int:
    """Top-level window under a screen point (what a wheel notch at that point would scroll)."""
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.WindowFromPoint.argtypes = [_PT]
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    w = u.WindowFromPoint(_PT(x, y))
    return int(u.GetAncestor(w, 2) or 0) if w else 0   # GA_ROOT


def human_move(x: int, y: int) -> None:
    import pyautogui

    cx, cy = pyautogui.position()
    if abs(cx - x) + abs(cy - y) < 6:
        return
    pyautogui.moveTo(x + random.randint(-3, 3), y + random.randint(-3, 3), duration=random.uniform(0.18, 0.32),
                     tween=pyautogui.easeOutQuad)


# ------------------------------------------------------------------ matching
def _norm(s: str) -> str:
    from .. import ocr

    return ocr.norm(s)


def match_score(target: str, line: str) -> float:
    """1.0 = contains the text; otherwise tolerant of OCR slips (O/0 in words, l/1, missing spaces) and lines that wrap.
    Numbers must always match exactly: 'Section 12' never matches 'Section 95', '12 Nov' never matches '13 Nov'."""
    import re

    t, s = _norm(target), _norm(line)
    if not t or not s:
        return 0.0
    nums_t, nums_s = re.findall(r"\d+", t), set(re.findall(r"\d+", s))
    if any(n not in nums_s for n in nums_t):
        return 0.0
    if len(t) < 4:                                 # short words: whole-word only ('ok' must not match 'book')
        return 1.0 if re.search(rf"\b{re.escape(t)}\b", s) else 0.0
    if t in s or t.replace(" ", "") in s.replace(" ", ""):
        return 1.0
    words, toks = t.split(), set(s.split())
    if len(words) > 1 and all(w in toks for w in words):
        return 0.95
    n = len(t)                                     # best fuzzy window of the same length
    best = max((difflib.SequenceMatcher(None, t, s[i:i + n]).ratio() for i in range(0, max(1, len(s) - n + 1))), default=0)
    return best if best >= 0.86 else 0.0


def find_in(lines, target: str):
    """Best visible match. A line that holds the text itself wins; two lines are joined only for text that really
    wraps (starts at the end of one line and continues on the next) - the first of the two is reported."""
    best, best_score = None, 0.0
    for ln in lines:
        sc = match_score(target, ln.text)
        if sc > best_score:
            best, best_score = ln, sc
    if best_score >= 0.86:
        return best, best_score
    words = _norm(target).split()
    if len(words) >= 2:
        for i, ln in enumerate(lines[:-1]):
            nxt = lines[i + 1]
            if abs(nxt.y - ln.y) >= 3 * max(ln.h, 10):
                continue
            a, b = _norm(ln.text), _norm(nxt.text)
            if words[0] in a.split() and words[-1] in b.split():
                sc = match_score(target, ln.text + " " + nxt.text)
                if sc > best_score:
                    best, best_score = ln, sc
    return best, best_score


def measure_shift(before, after) -> int | None:
    """How many pixels the content moved between two reads (median over lines seen in both, each text seen once)."""
    from collections import Counter

    cb, ca = Counter(_norm(x.text) for x in before), Counter(_norm(x.text) for x in after)
    yb = {_norm(x.text): x.y for x in before if cb[_norm(x.text)] == 1 and len(_norm(x.text)) > 6}
    d = sorted(yb[k] - x.y for x in after if ca[(k := _norm(x.text))] == 1 and k in yb)
    return d[len(d) // 2] if len(d) >= 2 else None


def _signature(lines) -> set:
    return {(_norm(x.text), x.y // 6) for x in lines if len(_norm(x.text)) > 2}


def _same_view(a: set, b: set) -> bool:
    if not a and not b:
        return True
    return len(a & b) / max(1, len(a | b)) >= 0.9


# ------------------------------------------------------------------ tools
class ScrollToTextArgs(Args):
    text: str = Field(min_length=2, max_length=200, description="What to find, as it appears on screen: a name, a phrase from a message, "
                                                                 "a heading, a date like '12 November', a subject line")
    direction: Literal["down", "up"] = Field("down", description="'up' for older WhatsApp/Teams messages or earlier in a document; "
                                                                 "'down' for later text, older mail in Outlook's list, further down a page")
    pane: Literal["auto", "left", "center", "right", "full"] = Field(
        "auto", description="Which part of the window to scroll and read. WhatsApp: left = chat list, right = open chat. "
                            "Outlook: center = message list, right = reading pane. Word/PDF/browser/Excel: auto")
    x: int | None = Field(None, description="Optional exact screen point to scroll at (overrides pane), e.g. from read_screen_text")
    y: int | None = None
    max_scrolls: int = Field(60, ge=1, le=250, description="Give up after this many scroll steps")
    click: bool = Field(False, description="Click the found text (e.g. open that chat/mail). Default: just bring it into view")
    speed: Literal["normal", "fast"] = "normal"


def _pane_rect(ctx, a: ScrollToTextArgs):
    from .. import uia
    from .apps import _work_rect

    h, l, t, r, b = _work_rect(ctx)
    if a.x is not None and a.y is not None:
        if not (l <= a.x <= r and t <= a.y <= b):
            raise PolicyError("That point is outside the window in front.")
        return h, (l, t, r, b), (a.x, a.y)
    w = r - l
    split = None
    if "whatsapp" in uia.window_title(h).lower() and a.pane in ("left", "right"):
        from .whatsapp import find_split

        split = find_split(h)
    if split:
        pl, pr = (l, split) if a.pane == "left" else (split, r)
    else:
        pl, pr = pane_bounds(uia.window_title(h), w, a.pane)
        pl, pr = l + pl, l + pr
    top = t + min(90, (b - t) // 6)               # skip title bar / toolbar rows when choosing the wheel point
    return h, (pl, t, pr, b), ((pl + pr) // 2, (top + b) // 2)


def pane_bounds(title: str, w: int, pane: str) -> tuple[int, int]:
    """Left/right pixel bounds (from the window's left edge) of a pane, knowing how common apps are laid out."""
    tl = (title or "").lower()
    if pane in ("auto", "full"):
        return 0, w
    if "whatsapp" in tl:                           # chat list has a fixed-ish width; the open chat is everything right of it
        split = int(max(360, min(0.28 * w, 560)))
        return (0, split) if pane == "left" else (split, w)
    if "outlook" in tl or "- mail" in tl:          # folders | message list | reading pane
        return {"left": (0, int(0.18 * w)), "center": (int(0.17 * w), int(0.50 * w)), "right": (int(0.48 * w), w)}[pane]
    lo, hi = PANES[pane]
    return int(w * lo), int(w * hi)


def t_scroll_to_text(a: ScrollToTextArgs, ctx):
    import pyautogui

    from .. import ocr, uia
    from .computer import _stop_check

    _stop_check(ctx)
    h, (l, t, r, b), (px, py) = _pane_rect(ctx, a)
    sign = -1 if a.direction == "down" else 1
    notches = 3                                    # first step is small; then sized to ~3/4 of the visible pane
    page = 0.85 if a.speed == "fast" else 0.7
    settle = 0.15 if a.speed == "fast" else 0.25

    def look():
        return sorted(ocr.read_region(l, t, r, b, scale=1.25), key=lambda x: (x.y // 10, x.x))

    lines = look()
    human_move(px, py)
    prev, prev_lines, still, steps = _signature(lines), lines, 0, 0
    hit, score = find_in(lines, a.text)
    while not (hit and score >= 0.86):
        if steps >= a.max_scrolls:
            break
        _stop_check(ctx)
        if steps and abs(pyautogui.position()[0] - px) + abs(pyautogui.position()[1] - py) > 60:
            raise PolicyError("You moved the mouse, so I stopped scrolling and left it to you. Ask again when you want me to continue.")
        if _window_at(px, py) != h:                # the wheel goes to whatever is under the pointer: make sure it is our app
            try:
                uia.focus_window(h)
                time.sleep(0.25)
            except Exception:
                pass
            if _window_at(px, py) != h:
                raise PolicyError(f"Another window is covering '{uia.window_title(h)}' - stopped so nothing else gets scrolled.")
        wheel(sign * notches, stop=ctx.stopped)
        steps += 1
        time.sleep(settle + random.uniform(0, 0.1))
        lines = look()
        sig = _signature(lines)
        moved = measure_shift(prev_lines, lines)
        if moved and abs(moved) > 8:               # learn how far one notch moves this app, then skim ~a page per step
            per = abs(moved) / notches
            notches = max(2, min(15, round(page * (b - t) / per)))
        prev_lines = lines
        if _same_view(sig, prev):
            still += 1
            if still == 1:                         # lazy-loading lists (WhatsApp history, Outlook 'more items'): give it a moment
                time.sleep(1.2)
                lines = prev_lines = look()
                sig = _signature(lines)
                if not _same_view(sig, prev):
                    still = 0
            if still >= 2:
                break
        else:
            still = 0
        prev = sig
        hit, score = find_in(lines, a.text)

    if not (hit and score >= 0.86):
        reason = f"reached the {'top' if a.direction == 'up' else 'end'}" if still >= 2 else f"stopped after {steps} scrolls"
        near = [x.text for x in lines if _norm(a.text)[:4] in _norm(x.text)][:8]
        return {"found": False, "scrolls": steps, "why": reason, "window": uia.window_title(h),
                "visible_now": [x.text for x in lines[:40]], "similar_text": near,
                "hint": "Try other words as they appear on screen, the other direction, another pane, or a larger max_scrolls."
                        + (" Outlook: the mail may be under the Other tab (Focused/Other), or use outlook_search." if "outlook" in uia.window_title(h).lower() else "")}

    # bring it away from the very edge so it is comfortably readable
    if hit.cy > b - (b - t) * 0.12 or hit.cy < t + (b - t) * 0.18:
        wheel(-1 if hit.cy > (t + b) // 2 else 1, stop=ctx.stopped)
        time.sleep(settle)
        lines = look()
        hit = find_in(lines, a.text)[0] or hit
    i = lines.index(hit) if hit in lines else 0
    out = {"found": True, "text": hit.text, "center": [hit.cx, hit.cy], "scrolls": steps, "match": round(score, 2),
           "window": uia.window_title(h), "around": [x.text for x in lines[max(0, i - 6): i + 7]]}
    if a.click:
        human_move(hit.cx, hit.cy)
        pyautogui.click(hit.cx, hit.cy)
        out["clicked"] = True
    return out


def _risk(a: ScrollToTextArgs) -> Risk:
    from .apps import _send_risk

    return _send_risk(a.text) if a.click else Risk.LOW


def register(reg: Registry) -> None:
    reg.register(Tool(
        "scroll_to_text",
        "Scroll like a person (real mouse-wheel, natural pace) in ANY app - WhatsApp chat or chat list, Outlook list or reading pane, "
        "Word, PDF, browser, Excel - until the given text is on screen, then return where it is and the lines around it "
        "(optionally click it). Stops at the top/bottom. Use this instead of repeated scroll + screenshot.",
        ScrollToTextArgs, t_scroll_to_text, _risk, 200, control=True, untrusted=True))
