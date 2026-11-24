"""Screen capture, mouse/keyboard control, windows and UI Automation tools (Windows 11)."""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import time
from ctypes import wintypes
from typing import Literal

from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

try:  # DPI awareness so pixel coordinates match screenshots
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

APPS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe", "excel": "excel.exe",
    "word": "winword.exe", "powerpoint": "powerpnt.exe", "outlook": "outlook.exe", "edge": "msedge.exe",
    "chrome": "chrome.exe", "explorer": "explorer.exe",
}
BLOCKED_HOTKEYS = [{"win", "r"}, {"win", "x"}, {"ctrl", "shift", "esc"}, {"ctrl", "alt", "delete"}, {"win", "l"}]
user32 = ctypes.windll.user32


# ------------------------------------------------------------------ helpers
def foreground_title() -> str:
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def list_windows() -> list[str]:
    out: list[str] = []
    cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                out.append(buf.value)
        return True

    user32.EnumWindows(cb_t(cb), 0)
    return out


def is_excluded(app, title: str | None = None) -> str | None:
    t = (title if title is not None else foreground_title()).lower()
    for ex in app.settings.get("excluded_apps", []):
        if ex and ex.lower() in t:
            return ex
    return None


def grab(monitor: int = 1) -> tuple[bytes, dict]:
    import mss
    import mss.tools

    with mss.mss() as sct:
        if monitor < 0 or monitor >= len(sct.monitors):
            raise PolicyError(f"Monitor {monitor} not found (0=all, 1..{len(sct.monitors) - 1})")
        mon = sct.monitors[monitor]
        shot = sct.grab(mon)
        png = mss.tools.to_png(shot.rgb, shot.size)
        return png, {"monitor": monitor, "left": mon["left"], "top": mon["top"], "width": shot.width, "height": shot.height}


def _need_fresh(app, monitor: int):
    st = app.screen_state
    if not st or time.time() - st["time"] > 120:
        raise PolicyError("No recent screenshot. Call screenshot first, then act using that screenshot's coordinates.")
    if st["monitor"] != monitor:
        raise PolicyError("Coordinates belong to a different monitor than the last screenshot.")
    import mss

    with mss.mss() as sct:
        m = sct.monitors[monitor]
        if (m["width"], m["height"]) != (st["width"], st["height"]):
            raise PolicyError("Screen size/scaling changed since the last screenshot. Take a new screenshot.")
    return st


def _stop_check(ctx):
    if ctx.stopped():
        raise PolicyError("Stopped by user")
    tracker = getattr(ctx.app, "work", None)
    if tracker is not None:
        from .. import uia

        if uia.is_ramai_window(user32.GetForegroundWindow()):
            uia.ensure_work_foreground(tracker)


# ------------------------------------------------------------------ typing via SendInput (unicode safe)
ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HWINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HWINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_key(vk=0, scan=0, flags=0):
    i = _INPUT(type=1, ki=_KEYBDINPUT(vk, scan, flags, 0, 0))
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def type_unicode(text: str, ctx, delay: float = 0.012) -> None:
    for ch in text:
        _stop_check(ctx)
        if ch == "\n":
            _send_key(vk=0x0D)
            _send_key(vk=0x0D, flags=2)
        elif ch == "\t":
            _send_key(vk=0x09)
            _send_key(vk=0x09, flags=2)
        else:
            code = ord(ch)
            units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
            for u in units:
                _send_key(scan=u, flags=4)
                _send_key(scan=u, flags=4 | 2)
        time.sleep(delay)


# ------------------------------------------------------------------ tool arg models
class ScreenshotArgs(Args):
    monitor: int = Field(1, ge=0, le=8, description="0=all monitors, 1=primary, 2.. others")


class DescribeArgs(Args):
    question: str = Field("Describe the screen and list actionable controls.", max_length=500)
    monitor: int = Field(1, ge=0, le=8)


class MoveArgs(Args):
    x: int = Field(ge=0, le=20000)
    y: int = Field(ge=0, le=20000)
    monitor: int = Field(1, ge=1, le=8)


class ClickArgs(MoveArgs):
    button: Literal["left", "right"] = "left"
    double: bool = False


class TypeArgs(Args):
    text: str = Field(max_length=2000)


class HotkeyArgs(Args):
    keys: list[str] = Field(min_length=1, max_length=4, description="e.g. ['ctrl','s'] or ['enter']")


class ScrollArgs(Args):
    amount: int = Field(ge=-30, le=30, description="Mouse-wheel notches (1 notch = about 3 lines). Positive=up/right, negative=down/left")
    horizontal: bool = False


class WaitArgs(Args):
    seconds: float = Field(ge=0, le=120)


class OpenAppArgs(Args):
    app: Literal["notepad", "calculator", "paint", "excel", "word", "powerpoint", "outlook", "edge", "chrome", "explorer"]


class OpenUrlArgs(Args):
    url: str = Field(pattern=r"^https?://[^\s]+$", max_length=1000)
    in_browser: bool = Field(False, description="Force the browser even if the service has an installed desktop app (only if the user asked for the website)")


class NoArgs(Args):
    pass


class UiElementsArgs(Args):
    max_items: int = Field(60, ge=1, le=200)


class UiClickArgs(Args):
    name: str = Field(max_length=200, description="Exact accessible name of the control")
    control_type: str | None = Field(None, max_length=40, description="e.g. Button, MenuItem, Edit")


# ------------------------------------------------------------------ implementations
def t_screenshot(a: ScreenshotArgs, ctx):
    _stop_check(ctx)  # never screenshot RAM AI's own window instead of the work window
    png, info = grab(a.monitor)
    app = ctx.app
    app.screen_state = {**info, "time": time.time()}
    app.last_screenshot = png
    d = app.data_path / "screens"
    d.mkdir(exist_ok=True)
    f = d / f"shot_{int(time.time() * 1000)}.png"
    f.write_bytes(png)
    for old in sorted(d.glob("shot_*.png"))[:-15]:
        old.unlink(missing_ok=True)
    return {"saved": str(f), **info, "foreground_window": foreground_title(), "note": "Use describe_screen for a visual description."}


async def t_describe(a: DescribeArgs, ctx):
    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'; screenshot not shared")
    ok, why = ctx.app.vision.ready()
    if not ok:
        raise PolicyError(why)
    if not ctx.app.screenshot_sharing:
        raise PolicyError("Screenshot sharing is paused by the user")
    png, info = grab(a.monitor)
    ctx.app.screen_state = {**info, "time": time.time()}
    ctx.app.last_screenshot = png
    text = await ctx.app.vision.describe(png, a.question)
    return {"width": info["width"], "height": info["height"], "description": text}


def _abs(ctx, x, y, monitor):
    st = _need_fresh(ctx.app, monitor)
    if x >= st["width"] or y >= st["height"]:
        raise PolicyError(f"Point ({x},{y}) is outside the {st['width']}x{st['height']} screen")
    return st["left"] + x, st["top"] + y


def t_move(a: MoveArgs, ctx):
    import pyautogui

    _stop_check(ctx)
    X, Y = _abs(ctx, a.x, a.y, a.monitor)
    pyautogui.moveTo(X, Y, duration=0.25)
    return {"moved_to": [X, Y]}


def t_click(a: ClickArgs, ctx):
    import pyautogui

    _stop_check(ctx)
    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'; refusing to click")
    X, Y = _abs(ctx, a.x, a.y, a.monitor)
    pyautogui.moveTo(X, Y, duration=0.2)
    _stop_check(ctx)
    pyautogui.click(X, Y, clicks=2 if a.double else 1, button=a.button)
    return {"clicked": [X, Y], "button": a.button, "double": a.double, "foreground_window": foreground_title()}


CHAT_TITLES = ("whatsapp", "teams", "telegram", "slack", "signal", "messenger", "discord", "skype", "message (html)", "message (plain text)")


def in_chat_app() -> bool:
    t = foreground_title().lower()
    return any(k in t for k in CHAT_TITLES)


def _hotkey_risk(a) -> "Risk":
    from ..policy import Risk as _R

    ks = {k.lower() for k in a.keys}
    if ks in ({"alt", "s"}, {"ctrl", "enter"}):
        return _R.HIGH
    if ks in ({"enter"}, {"return"}) and in_chat_app():  # Enter sends in chat apps
        return _R.HIGH
    return _R.LOW


def t_type(a: TypeArgs, ctx):
    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'; refusing to type")
    if "\n" in a.text and in_chat_app():  # a newline would SEND in chat apps: use Shift+Enter line breaks
        from .whatsapp import type_lines

        type_lines(a.text, ctx)
    else:
        type_unicode(a.text, ctx)
    return {"typed_chars": len(a.text), "foreground_window": foreground_title()}


def _check_hotkey(a: HotkeyArgs, ctx):
    import pyautogui

    ks = [k.lower() for k in a.keys]
    for k in ks:
        if k not in pyautogui.KEYBOARD_KEYS:
            raise PolicyError(f"Unknown key '{k}'")
    s = set(ks)
    for b in BLOCKED_HOTKEYS:
        if b <= s:
            raise PolicyError("This shortcut is blocked by policy (security-sensitive Windows shortcut)")


def t_hotkey(a: HotkeyArgs, ctx):
    import pyautogui

    _stop_check(ctx)
    pyautogui.hotkey(*[k.lower() for k in a.keys])
    return {"pressed": a.keys, "foreground_window": foreground_title()}


def t_scroll(a: ScrollArgs, ctx):
    from .scroll import wheel

    _stop_check(ctx)
    wheel(a.amount, horizontal=a.horizontal, stop=ctx.stopped)   # real wheel notches (pyautogui sent 1/120 of a notch per unit)
    return {"scrolled_notches": a.amount, "horizontal": a.horizontal,
            "tip": "To look for something, use scroll_to_text instead of repeated scroll + screenshot."}


def t_wait(a: WaitArgs, ctx):
    end = time.time() + a.seconds
    while time.time() < end:
        _stop_check(ctx)
        time.sleep(0.1)
    return {"waited": a.seconds}


def t_open_app(a: OpenAppArgs, ctx):
    os.startfile(APPS[a.app])  # fixed allow-list, no arguments
    time.sleep(1.5)
    return {"opened": a.app, "foreground_window": foreground_title()}


def t_open_url(a: OpenUrlArgs, ctx):
    from .apps import app_for_url, launch

    name = None if a.in_browser else app_for_url(a.url)
    if name and getattr(ctx.app, "start_apps", None) and ctx.app.start_apps.has(name):
        res = launch(ctx.app, name)   # the installed desktop app comes first, like 'open Grok'
        return {**res, "note": f"{name} is installed as a desktop app, so it was opened instead of {a.url}. "
                               "Pass in_browser=true only if the user wants the website."}
    subprocess.Popen(["cmd", "/c", "start", "", "msedge", a.url], creationflags=0x08000000)
    time.sleep(1.5)
    return {"opened": a.url}


def t_windows(a: NoArgs, ctx):
    from .. import uia

    h = ctx.app.work.current() if getattr(ctx.app, "work", None) else None
    return {"work_window": uia.window_title(h) if h else None,
            "windows": [t for t in list_windows() if t.strip() != "RAM AI"][:60],
            "note": "'RAM AI' (your own window) is hidden from this list and never a target."}


def t_screen_info(a: NoArgs, ctx):
    import mss

    with mss.mss() as sct:
        return {"monitors": [{"index": i, **{k: m[k] for k in ("left", "top", "width", "height")}}
                             for i, m in enumerate(sct.monitors)]}


def _uia_window():
    from pywinauto import Desktop

    h = user32.GetForegroundWindow()
    return Desktop(backend="uia").window(handle=h)


def t_ui_elements(a: UiElementsArgs, ctx):
    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'")
    w = _uia_window()
    out = []
    for e in w.descendants():
        try:
            info = e.element_info
            name = info.name or ""
            if not name:
                continue
            r = info.rectangle
            out.append({"name": name[:80], "type": info.control_type, "id": info.automation_id,
                        "center": [(r.left + r.right) // 2, (r.top + r.bottom) // 2]})
        except Exception:
            continue
        if len(out) >= a.max_items:
            break
    return {"window": foreground_title(), "elements": out}


def t_ui_click(a: UiClickArgs, ctx):
    _stop_check(ctx)
    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'")
    w = _uia_window()
    kw = {"title": a.name}
    if a.control_type:
        kw["control_type"] = a.control_type
    matches = w.descendants(**kw)
    if not matches:
        raise PolicyError("No control with that exact name")
    if len(matches) > 1:
        raise PolicyError(f"{len(matches)} controls match; pass control_type to disambiguate")
    el = matches[0]
    try:
        el.invoke()
    except Exception:
        el.click_input()
    return {"clicked": a.name, "foreground_window": foreground_title()}


def register(reg: Registry) -> None:
    R = Risk
    reg.register(Tool("screenshot", "Capture the screen. Required before coordinate-based actions.", ScreenshotArgs, t_screenshot, R.READ, 20))
    reg.register(Tool("describe_screen", "Ask the vision provider to describe the current screen (sends a screenshot to the configured vision provider).", DescribeArgs, t_describe, R.READ, 100, untrusted=True))
    reg.register(Tool("screen_info", "List monitors and their sizes.", NoArgs, t_screen_info, R.READ, 10))
    reg.register(Tool("list_windows", "List visible window titles and the foreground window.", NoArgs, t_windows, R.READ, 10, untrusted=True))
    reg.register(Tool("mouse_move", "Move the pointer to screenshot coordinates.", MoveArgs, t_move, R.LOW, 15, control=True))
    reg.register(Tool("click", "Click at screenshot coordinates (take a fresh screenshot first).", ClickArgs, t_click, R.LOW, 15, control=True))
    reg.register(Tool("type_text", "Type text into the focused control.", TypeArgs, t_type, R.LOW, 60, control=True))
    reg.register(Tool("hotkey", "Press a keyboard shortcut, e.g. ['ctrl','s'].", HotkeyArgs, t_hotkey,
                      _hotkey_risk, 10, control=True, check=_check_hotkey))
    reg.register(Tool("scroll", "Scroll the window under the pointer.", ScrollArgs, t_scroll, R.LOW, 10, control=True))
    reg.register(Tool("wait", "Wait for the UI to update (up to 120 s).", WaitArgs, t_wait, R.READ, 125))
    reg.register(Tool("open_url", "Open a specific web page (http/https) in Edge. For 'open <app/service>' use launch_app instead; a service home page (grok.com, chatgpt.com...) opens its installed desktop app.", OpenUrlArgs, t_open_url, R.LOW, 30))
