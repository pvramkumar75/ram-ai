"""Teach mode: you do a task once with your own mouse and keyboard, RAM AI turns it into a skill it can repeat.

Recording: low-level hooks see only PHYSICAL input (RAM AI's own simulated input is ignored) and nothing aimed at the RAM AI
window. For every click it notes the app, the window and the NAME of the control under the pointer (UI Automation), plus a
small picture around the pointer that is read with Windows OCR when the control has no name (SAP, canvases, images).
Typing is joined per field; password fields are never recorded. The recording lives only in memory: you review the steps,
untick anything private, and only then is it sent to the AI to write the skill, which you check and save."""
from __future__ import annotations

import ctypes
import math
import queue
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

MAX_STEPS = 400
MAX_SECONDS = 20 * 60
DOUBLE_CLICK_S, DOUBLE_CLICK_PX, DRAG_PX = 0.45, 6, 14
CROP_W, CROP_H = 520, 90
SECRET_RE = re.compile(r"pass ?word|passcode|\bpin\b|\botp\b|one[- ]time|secret|cvv|security code", re.I)

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_MOUSEWHEEL = 0x201, 0x202, 0x204, 0x20A
LLMHF_INJECTED, LLKHF_INJECTED = 0x01, 0x10

SHIFT, CTRL, ALT, WIN = {0x10, 0xA0, 0xA1}, {0x11, 0xA2, 0xA3}, {0x12, 0xA4, 0xA5}, {0x5B, 0x5C}
NAMED = {0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc", 0x2E: "Delete", 0x2D: "Insert", 0x25: "Left", 0x26: "Up", 0x27: "Right",
         0x28: "Down", 0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown", 0x08: "Backspace", 0x20: "Space",
         **{0x70 + i: f"F{i + 1}" for i in range(12)}}


# ------------------------------------------------------------------ building steps (pure: no Windows calls, unit-tested)
def _same_win(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and (a.get("hwnd"), a.get("title")) == (b.get("hwnd"), b.get("title"))


def is_secret_field(field: dict | None) -> bool:
    f = field or {}
    return bool(f.get("password")) or bool(SECRET_RE.search(f"{f.get('name', '')} {f.get('auto_id', '')}"))


class Builder:
    """Turns raw clicks/keys (already enriched with window + control info) into readable steps."""

    def __init__(self):
        self.steps: list[dict] = []

    def _last(self) -> dict | None:
        return self.steps[-1] if self.steps else None

    def _add(self, step: dict) -> dict:
        self.steps.append(step)
        return step

    def click(self, t: float, x: int, y: int, win: dict, target: dict | None = None, pos=None, button: str = "left", crop=None) -> dict:
        last = self._last()
        if (button == "left" and last and last["kind"] == "click" and _same_win(last["win"], win) and t - last["t"] <= DOUBLE_CLICK_S
                and math.dist(last["xy"], (x, y)) <= DOUBLE_CLICK_PX):
            last["kind"] = "double_click"
            return last
        return self._add({"kind": "right_click" if button == "right" else "click", "t": t, "xy": (x, y), "win": win,
                          "target": target or {}, "pos": pos, "crop": crop, "near": ""})

    def release(self, t: float, x: int, y: int, pos=None) -> None:
        last = self._last()
        if last and last["kind"] == "click" and math.dist(last["xy"], (x, y)) > DRAG_PX:
            last["kind"], last["to"], last["to_pos"] = "drag", (x, y), pos

    def char(self, t: float, ch: str, win: dict, field) -> None:
        """field: a callable returning the focused control's info (looked up only when a new typing step starts)."""
        last = self._last()
        if last and last["kind"] == "type" and _same_win(last["win"], win):
            if not last["masked"]:
                last["text"] += ch
            last["t_end"] = t
            return
        f = field() if callable(field) else (field or {})
        masked = is_secret_field(f)
        self._add({"kind": "type", "t": t, "t_end": t, "win": win, "target": f, "masked": masked, "text": "" if masked else ch})

    def backspace(self, t: float, win: dict) -> None:
        last = self._last()
        if last and last["kind"] == "type" and _same_win(last["win"], win) and (last["masked"] or last["text"]):
            if not last["masked"]:
                last["text"] = last["text"][:-1]
            last["t_end"] = t
            return
        self.key(t, "Backspace", win)

    def key(self, t: float, name: str, win: dict) -> None:
        last = self._last()
        if last and last["kind"] == "key" and last["key"] == name and _same_win(last["win"], win) and t - last["t_end"] <= 1.5:
            last["count"] += 1
            last["t_end"] = t
            return
        self._add({"kind": "key", "t": t, "t_end": t, "win": win, "key": name, "count": 1})

    def scroll(self, t: float, notches: int, win: dict, target: dict | None = None) -> None:
        last = self._last()
        if (last and last["kind"] == "scroll" and _same_win(last["win"], win) and t - last["t_end"] <= 2.0
                and (last["notches"] > 0) == (notches > 0)):
            last["notches"] += notches
            last["t_end"] = t
            return
        self._add({"kind": "scroll", "t": t, "t_end": t, "win": win, "notches": notches, "target": target or {}})


def _control(target: dict | None) -> str:
    tg = target or {}
    name, kind = (tg.get("name") or "").strip(), (tg.get("type") or "").strip()
    if name:
        return f'the {kind or "control"} "{name[:80]}"'
    if tg.get("inside"):
        return f'{("a " + kind) if kind else "a spot"} inside "{tg["inside"][:60]}"'
    return ""


def _where(pos) -> str:
    return f"{round(pos[0] * 100)}% across, {round(pos[1] * 100)}% down the window" if pos else ""


def describe(step: dict) -> str:
    """One plain-English line for a step (shown to the user and given to the AI)."""
    k = step["kind"]
    if k in ("click", "double_click", "right_click", "drag"):
        verb = {"click": "Click", "double_click": "Double-click", "right_click": "Right-click", "drag": "Drag"}[k]
        what = _control(step.get("target"))
        near = (step.get("near") or "").strip()
        if near and (not what or near.lower() not in what.lower()):
            what = f'{what} (text there: "{near[:60]}")' if what else f'the text "{near[:60]}"'
        where = _where(step.get("pos"))
        line = f"{verb} {what or 'at ' + where}" + (f" [{where}]" if what and where else "")
        if k == "drag":
            line += f" to {_where(step.get('to_pos')) or 'another point'}"
        return line
    if k == "type":
        into = _control(step.get("target"))
        if step.get("masked"):
            return f"Type the password/secret{(' into ' + into) if into else ''} (NOT recorded - ask the user to type it)"
        return f'Type "{step["text"]}"' + (f" into {into}" if into else "")
    if k == "key":
        return f"Press {step['key']}" + (f" x{step['count']}" if step.get("count", 1) > 1 else "")
    if k == "scroll":
        n = step["notches"]
        return f"Scroll {'down' if n < 0 else 'up'} {abs(n)} notch{'es' if abs(n) != 1 else ''}"
    return k


def win_label(win: dict | None) -> str:
    w = win or {}
    app, title = (w.get("app") or "").strip(), (w.get("title") or "").strip()
    if app and title and app.lower() not in title.lower():
        return f"{title} ({app})"
    return title or app or "unknown window"


def public_steps(steps: list[dict]) -> list[dict]:
    """What the UI shows: id, window, line, and whether it may contain something private."""
    return [{"id": i, "window": win_label(s.get("win")), "line": describe(s), "typed": s["kind"] == "type" and not s.get("masked"),
             "masked": bool(s.get("masked"))} for i, s in enumerate(steps)]


def to_text(steps: list[dict], keep: set[int] | None = None) -> str:
    """The recording as numbered lines grouped by window (what the AI sees)."""
    out, cur, n = [], None, 0
    for i, s in enumerate(steps):
        if keep is not None and i not in keep:
            continue
        w = win_label(s.get("win"))
        if w != cur:
            out.append(f"In window: {w}")
            cur = w
        n += 1
        out.append(f"  {n}. {describe(s)}")
    return "\n".join(out)


def review_text(review: list[dict], keep: set[int] | None = None) -> str:
    """Same as to_text, from the reviewed lines (what the user saw and kept)."""
    out, cur, n = [], None, 0
    for i, s in enumerate(review):
        if keep is not None and i not in keep:
            continue
        w = str(s.get("window") or "unknown window")
        if w != cur:
            out.append(f"In window: {w}")
            cur = w
        n += 1
        out.append(f"  {n}. {s.get('line', '')}")
    return "\n".join(out)


def clean_review(items) -> list[dict]:
    """Validate steps handed back (e.g. after a restart): plain text only, capped."""
    out = []
    for s in (items or [])[:MAX_STEPS]:
        if isinstance(s, dict) and isinstance(s.get("line"), str) and s["line"].strip():
            out.append({"id": len(out), "window": str(s.get("window") or "")[:200], "line": s["line"][:500],
                        "typed": bool(s.get("typed")), "masked": bool(s.get("masked"))})
    return out


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40].strip("_")
    return s if len(s) >= 3 else (s + "_skill")[:40]


DRAFT_SYSTEM = """You write SKILLS for RAM AI, a Windows agent that repeats tasks the user has shown it.
You get a recording of the user doing a task once (clicks with the name of the control clicked, typing, keys, scrolling,
grouped by window). Turn it into a clear, reusable playbook in Markdown that the agent will follow later with its tools:
launch_app(name), focus_window(title), ui_click(name, control_type), ui_type(name, text), click_text(text), type_text(text),
hotkey(keys), scroll_to_text(text), scroll, wait(seconds), read_screen_text(), ui_elements(), screenshot(), ask_user(question).

Write exactly these sections:
## When to use
One or two lines.
## Inputs
Values that will change each time (numbers, dates, names, file names, search words...). Give each a {placeholder}, say what it
is and the example value seen in the recording. If nothing changes, write "None".
## Steps
Numbered, one action per line, in order. Say which app/window, then what to click or type BY ITS VISIBLE NAME OR TEXT
(prefer ui_click by name, else click_text by visible text; use the "% across/down the window" position only as a last resort).
Use {placeholders} for the inputs. Merge typing into one step. After steps that change the screen (opening a window, running
a report, searching), add what to check before going on (e.g. "wait until the list appears"). Leave out accidental or
repeated clicks and anything that was undone.
## Checks
How to confirm it worked, and what to do if the screen looks different (look again, then adapt; ask_user if unsure).

Rules: never write a password or secret (if one is needed, the step is "ask the user to type it"); do not invent steps that
were not shown, except obvious waits/checks; plain language; Markdown only, no code fences around the whole answer."""


def draft_prompt(title: str, description: str, steps_text: str) -> list[dict]:
    user = (f"Task name: {title}\nWhat it is for / what changes each time (from the user): {description or '(not given)'}\n\n"
            f"Recording:\n{steps_text}")
    return [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}]


def clean_draft(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", t, re.S)
    return (m.group(1) if m else t).strip()


# ------------------------------------------------------------------ Windows side: hooks + enrichment
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLL(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", ULONG_PTR)]


class KBDLL(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


def _api():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    u.GetKeyboardLayout.restype = ctypes.c_void_p
    u.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_char_p, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    k.GetModuleHandleW.restype = ctypes.c_void_p     # 64-bit handle: as a 32-bit int Windows rejects the hook (error 126)
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    return u, k


def exe_name(h) -> str:
    u, k = _api()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        return Path(buf.value).stem if k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)) else ""
    finally:
        k.CloseHandle(hp)


class Recorder:
    def __init__(self, app=None, include_injected: bool = False):
        self.app = app
        self.include_injected = include_injected   # tests only: let simulated input count as "the user"
        self.recording = False
        self.started = 0.0
        self.steps: list[dict] = []                # last finished recording (kept in memory until saved/discarded)
        self.review: list[dict] = []               # its readable lines, shown for review and used to write the skill
        self.stopped_reason = ""
        self._b: Builder | None = None
        self._q: queue.Queue = queue.Queue()
        self._hook_thread = None
        self._worker = None
        self._tid = 0
        self._procs = []
        self._lock = threading.Lock()
        self._win_cache: dict = {}
        self.errors: list[str] = []
        self._hook_ok = False

    # -------------------------------------------------------------- public
    def start(self) -> dict:
        with self._lock:
            if self.recording:
                return self.status()
            self._b, self.steps, self.review, self.stopped_reason = Builder(), [], [], ""
            self._q = queue.Queue()
            self._win_cache = {}
            self.errors = []
            self.started = time.monotonic()
            self.recording = True
            ready = threading.Event()
            self._hook_thread = threading.Thread(target=self._hooks, args=(ready,), name="ramai-teach-hooks", daemon=True)
            self._worker = threading.Thread(target=self._work, name="ramai-teach-worker", daemon=True)
            self._worker.start()
            self._hook_ok = False
            self._hook_thread.start()
            ready.wait(3)
        if not self._hook_ok:
            self.stop("could not watch the mouse/keyboard")
            raise RuntimeError("Teach mode could not start: Windows did not allow RAM AI to watch the mouse and keyboard.")
        self._dock(True)
        return self.status()

    def stop(self, reason: str = "") -> list[dict]:
        with self._lock:
            if not self.recording:
                return self.steps
            self.recording = False
            self.stopped_reason = reason
            if self._tid:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT -> unhook
            if self._hook_thread:
                self._hook_thread.join(3)
            self._q.put(None)
            if self._worker:
                self._worker.join(15)
            steps = self._b.steps if self._b else []
        self._dock(False)
        self._read_crops(steps)
        self.steps = steps
        self.review = public_steps(steps)
        return steps

    def discard(self) -> None:
        if self.recording:
            self.stop()
        self.steps, self.review = [], []

    def status(self) -> dict:
        b = self._b
        return {"recording": self.recording, "steps": len(b.steps) if (b and self.recording) else len(self.steps),
                "seconds": round(time.monotonic() - self.started) if self.recording else 0, "stopped_reason": self.stopped_reason,
                "last": describe(b.steps[-1]) if (b and self.recording and b.steps) else "",
                "pending": 0 if self.recording else len(self.review)}

    def _dock(self, on: bool) -> None:
        d = getattr(self.app, "dock", None)
        if d is None:
            return
        try:
            d.start() if on else d.stop()
        except Exception:
            pass

    # -------------------------------------------------------------- hook thread: must stay FAST (no UIA/OCR here)
    def _is_ramai(self, h) -> bool:
        try:
            from . import uia

            return bool(h) and uia.is_ramai_window(h)
        except Exception:
            return False

    def _hooks(self, ready: threading.Event):
        u, k = _api()
        q = self._q

        def mouse(n, w, l):
            if n == 0 and w in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_MOUSEWHEEL):
                m = ctypes.cast(l, ctypes.POINTER(MSLL)).contents
                if self.include_injected or not m.flags & LLMHF_INJECTED:
                    top = u.GetAncestor(u.WindowFromPoint(m.pt), 2)
                    if not self._is_ramai(top):
                        q.put(("m", w, m.pt.x, m.pt.y, ctypes.c_short(m.mouseData >> 16).value, top, time.monotonic()))
            return u.CallNextHookEx(None, n, w, l)

        def keyboard(n, w, l):
            if n == 0:
                kb = ctypes.cast(l, ctypes.POINTER(KBDLL)).contents
                fg = u.GetForegroundWindow()
                if (self.include_injected or not kb.flags & LLKHF_INJECTED) and not self._is_ramai(fg):
                    hkl = u.GetKeyboardLayout(u.GetWindowThreadProcessId(fg, None))
                    q.put(("k", w in (WM_KEYDOWN, WM_SYSKEYDOWN), kb.vk, kb.scan, fg, hkl, time.monotonic()))
            return u.CallNextHookEx(None, n, w, l)

        self._procs = [HOOKPROC(mouse), HOOKPROC(keyboard)]
        hm = u.SetWindowsHookExW(WH_MOUSE_LL, self._procs[0], k.GetModuleHandleW(None), 0)
        hk = u.SetWindowsHookExW(WH_KEYBOARD_LL, self._procs[1], k.GetModuleHandleW(None), 0)
        self._tid = k.GetCurrentThreadId()
        self._hook_ok = bool(hm and hk)
        ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h in (hm, hk):
            if h:
                u.UnhookWindowsHookEx(h)
        self._tid = 0

    # -------------------------------------------------------------- worker: enrich events and build steps
    def _win(self, h) -> dict:
        h = int(h or 0)
        if h in self._win_cache:
            info = self._win_cache[h]
        else:
            info = {"hwnd": h, "app": exe_name(h) if h else ""}
            self._win_cache[h] = info
        from . import uia

        return {**info, "title": uia.window_title(h) if h else ""}

    @staticmethod
    def _pos(h, x, y):
        u, _ = _api()
        r = wintypes.RECT()
        if not h or not u.GetWindowRect(h, ctypes.byref(r)) or r.right <= r.left or r.bottom <= r.top:
            return None
        return (round(min(1, max(0, (x - r.left) / (r.right - r.left))), 3), round(min(1, max(0, (y - r.top) / (r.bottom - r.top))), 3))

    @staticmethod
    def _element_info(el) -> dict:
        from . import uia
        from comtypes.gen import UIAutomationClient as U

        if not el:
            return {}
        info = {}
        try:
            info = {"name": (el.CurrentName or "").strip()[:120], "type": (el.CurrentLocalizedControlType or "").strip(),
                    "auto_id": (el.CurrentAutomationId or "")[:80]}
            info["password"] = bool(el.GetCurrentPropertyValue(U.UIA_IsPasswordPropertyId))
        except Exception:
            pass
        if not info.get("name"):
            p = el
            for _ in range(2):
                try:
                    p = uia.parent(p)
                    nm = uia.name_of(p).strip() if p else ""
                except Exception:
                    break
                if nm:
                    info["inside"] = nm[:80]
                    break
        return info

    def _target_at(self, x, y) -> dict:
        try:
            from . import uia
            from comtypes.gen import UIAutomationClient as U

            return self._element_info(uia.uia().ElementFromPoint(U.tagPOINT(x, y)))
        except Exception:
            return {}

    def _focused_field(self) -> dict:
        try:
            from . import uia

            return self._element_info(uia.focused())
        except Exception:
            return {}

    @staticmethod
    def _crop(x, y):
        try:
            import mss
            from PIL import Image

            left, top = max(0, x - CROP_W // 2), max(0, y - CROP_H // 2)
            with mss.mss() as sct:
                shot = sct.grab({"left": left, "top": top, "width": CROP_W, "height": CROP_H})
            return (left, top, Image.frombytes("RGB", shot.size, shot.rgb))
        except Exception:
            return None

    def _work(self):
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        u, _ = _api()
        held: set[int] = set()
        win_alone = False
        caps = bool(u.GetKeyState(0x14) & 1)
        while True:
            ev = self._q.get()
            if ev is None:
                return
            b = self._b
            try:
                if ev[0] == "m":
                    _, w, x, y, delta, top, t = ev
                    if w == WM_MOUSEWHEEL:
                        b.scroll(t - self.started, int(delta / 120) or (1 if delta > 0 else -1), self._win(top))
                    elif w == WM_LBUTTONUP:
                        b.release(t - self.started, x, y, self._pos(top, x, y))
                    else:
                        tg = self._target_at(x, y)
                        b.click(t - self.started, x, y, self._win(top), tg, self._pos(top, x, y),
                                "right" if w == WM_RBUTTONDOWN else "left", None if tg.get("name") else self._crop(x, y))
                    win_alone = False
                else:
                    _, down, vk, scan, fg, hkl, t = ev
                    t -= self.started
                    if vk in SHIFT | CTRL | ALT | WIN:
                        if down:
                            if vk in WIN and not held & WIN:
                                win_alone = True
                            held.add(vk)
                        else:
                            held.discard(vk)
                            if vk in WIN and win_alone:
                                b.key(t, "Win", self._win(fg))
                                win_alone = False
                        continue
                    if not down:
                        continue
                    win_alone = False
                    if vk == 0x14:
                        caps = not caps
                        continue
                    combo = [m for m, s in (("Ctrl", CTRL), ("Alt", ALT), ("Win", WIN)) if held & s]
                    if combo:
                        nm = NAMED.get(vk) or (chr(vk) if 0x30 <= vk <= 0x5A else f"key{vk}")
                        if held & SHIFT:
                            combo.append("Shift")
                        b.key(t, "+".join(combo + [nm]), self._win(fg))
                        continue
                    win = self._win(fg)
                    if vk == 0x08:
                        b.backspace(t, win)
                        continue
                    if vk in NAMED and vk != 0x20:
                        b.key(t, ("Shift+" if held & SHIFT else "") + NAMED[vk], win)
                        continue
                    ch = self._char(u, vk, scan, held, caps, hkl)
                    if ch:
                        b.char(t, ch, win, self._focused_field)
                if len(b.steps) >= MAX_STEPS or time.monotonic() - self.started > MAX_SECONDS:
                    threading.Thread(target=self.stop, args=("limit reached",), daemon=True).start()
            except Exception as e:  # keep recording; remember what went wrong for diagnosis
                if len(self.errors) < 20:
                    self.errors.append(f"{type(e).__name__}: {e}")

    @staticmethod
    def _char(u, vk, scan, held, caps, hkl) -> str:
        state = (ctypes.c_ubyte * 256)()
        if held & SHIFT:
            state[0x10] = 0x80
        if caps:
            state[0x14] = 0x01
        buf = ctypes.create_unicode_buffer(8)
        n = u.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 0x4, hkl)  # 0x4: keep dead-key state
        ch = buf.value[:n] if n > 0 else ""
        return ch if ch and ch.isprintable() else ""

    # -------------------------------------------------------------- after stopping: read the text near unnamed clicks
    @staticmethod
    def _read_crops(steps: list[dict]) -> None:
        for s in steps:
            c = s.pop("crop", None)
            if not c:
                continue
            try:
                from . import ocr

                left, top, img = c
                lines = ocr.read_image(img, left, top, scale=2.0)
                x, y = s["xy"]
                hit = [ln for ln in lines if ln.x - 4 <= x <= ln.x + ln.w + 4 and ln.y - 4 <= y <= ln.y + ln.h + 4]
                if not hit and lines:
                    best = min(lines, key=lambda ln: math.dist((ln.cx, ln.cy), (x, y)))
                    hit = [best] if math.dist((best.cx, best.cy), (x, y)) <= 60 else []
                s["near"] = hit[0].text.strip() if hit else ""
            except Exception:
                s["near"] = ""
