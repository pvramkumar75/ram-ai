"""Fast native Windows UI Automation helpers (comtypes). Targeted lookups take ~0.01-0.1 s even in huge
WebView2 trees where a full walk takes 30 s. Never walk whole trees; always search by property or scoped subtree."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Callable

import comtypes
import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as U  # noqa: E402

user32 = ctypes.windll.user32
try:  # physical-pixel coordinates everywhere (must match screenshots/OCR)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
_uia = None

CT = {"button": U.UIA_ButtonControlTypeId, "edit": U.UIA_EditControlTypeId, "dataitem": U.UIA_DataItemControlTypeId,
      "listitem": U.UIA_ListItemControlTypeId, "menuitem": U.UIA_MenuItemControlTypeId, "text": U.UIA_TextControlTypeId,
      "checkbox": U.UIA_CheckBoxControlTypeId, "combobox": U.UIA_ComboBoxControlTypeId, "tabitem": U.UIA_TabItemControlTypeId,
      "hyperlink": U.UIA_HyperlinkControlTypeId, "document": U.UIA_DocumentControlTypeId, "group": U.UIA_GroupControlTypeId}
CT_NAMES = {v: k for k, v in CT.items()}


def uia():
    global _uia
    if _uia is None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)  # MTA: usable from any worker thread
        except OSError:
            pass
        _uia = comtypes.CoCreateInstance(U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER)
    return _uia


def windows(visible_only: bool = True) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if (not visible_only) or user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                out.append((h, buf.value))
        return True

    user32.EnumWindows(cb_t(cb), 0)
    return out


BROWSER_SUFFIX = ("microsoft edge", "microsoft\u200b edge", "google chrome", "mozilla firefox", "brave", "opera")


def is_browser_title(t: str) -> bool:
    tl = t.lower().strip()
    return any(tl.endswith(s) for s in BROWSER_SUFFIX)


def classname(h: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def find_window(match: str, exact: bool = False, include_browsers: bool = False) -> int | None:
    m = match.lower().strip()
    for h, t in windows():
        if not include_browsers and is_browser_title(t):
            continue
        tl = t.lower().strip()
        if (tl == m) if exact else (m in tl):
            return h
    return None


def wake_web_content(el) -> None:
    """Chromium/WebView2 builds its accessibility tree lazily; touching it through the raw walker wakes it up."""
    try:
        u = uia()
        raw = u.RawViewWalker
        hosts = el.FindAll(U.TreeScope_Descendants, u.CreatePropertyCondition(U.UIA_ClassNamePropertyId, "RootView"))
        for i in range(hosts.Length):
            c = raw.GetFirstChildElement(hosts.GetElement(i))
            n = 0
            while c and n < 10:
                g = raw.GetFirstChildElement(c)
                if g:
                    raw.GetFirstChildElement(g)
                c = raw.GetNextSiblingElement(c)
                n += 1
        el.FindFirst(U.TreeScope_Descendants, u.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_DocumentControlTypeId))
    except Exception:
        pass


def focus_window(h: int) -> None:
    """Bring a window to the front WITHOUT tapping Alt (a lone Alt switches Office into ribbon-shortcut mode).
    Background processes are normally refused by Windows' foreground lock, so escalate until it really is in front."""
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)
    if user32.GetForegroundWindow() == h:
        return
    k32 = ctypes.windll.kernel32

    def ok():
        time.sleep(0.05)
        return user32.GetForegroundWindow() == h

    # 1) synthetic input event (no key, no movement) grants the right to set the foreground
    user32.mouse_event(0x0001, 0, 0, 0, 0)
    user32.SetForegroundWindow(h)
    if ok():
        return
    # 2) join the input queue of the current foreground thread, then activate
    fg = user32.GetForegroundWindow()
    cur = k32.GetCurrentThreadId()
    tgt = user32.GetWindowThreadProcessId(fg, None)
    own = user32.GetWindowThreadProcessId(h, None)
    try:
        for t in {tgt, own} - {cur, 0}:
            user32.AttachThreadInput(cur, t, True)
        user32.BringWindowToTop(h)
        user32.SetForegroundWindow(h)
        user32.SetActiveWindow(h)
    finally:
        for t in {tgt, own} - {cur, 0}:
            user32.AttachThreadInput(cur, t, False)
    if ok():
        return
    # 3) an inert key (F24: no application binds it; never Alt) counts as user input
    user32.keybd_event(0x87, 0, 0, 0)
    user32.keybd_event(0x87, 0, 2, 0)
    user32.SetForegroundWindow(h)
    if ok():
        return
    # 4) the shell's own task-switch call
    try:
        user32.SwitchToThisWindow(h, True)
    except Exception:
        pass
    if ok():
        return
    # 5) minimise/restore always activates the restored window
    was_max = bool(user32.IsZoomed(h))
    user32.ShowWindow(h, 6)   # SW_MINIMIZE
    user32.ShowWindow(h, 3 if was_max else 9)
    ok()


def root(h: int):
    return uia().ElementFromHandle(h)


def _cond(name: str | None = None, ctype: str | None = None):
    u = uia()
    cs = []
    if name is not None:
        cs.append(u.CreatePropertyCondition(U.UIA_NamePropertyId, name))
    if ctype:
        cs.append(u.CreatePropertyCondition(U.UIA_ControlTypePropertyId, CT[ctype.lower()]))
    if not cs:
        return u.CreateTrueCondition()
    return cs[0] if len(cs) == 1 else u.CreateAndCondition(cs[0], cs[1])


def find_first(el, name: str | None = None, ctype: str | None = None):
    try:
        return el.FindFirst(U.TreeScope_Descendants, _cond(name, ctype))
    except Exception:
        return None


def find_all(el, name: str | None = None, ctype: str | None = None) -> list:
    try:
        arr = el.FindAll(U.TreeScope_Descendants, _cond(name, ctype))
    except Exception:
        return []
    out = []
    for i in range(arr.Length):
        try:
            out.append(arr.GetElement(i))
        except Exception:
            pass
    return out


def wait_for(fn: Callable, timeout: float = 8.0, interval: float = 0.08):
    """Poll fn() until truthy (event-driven waiting instead of fixed sleeps)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(interval)
    return None


def parent(el, levels: int = 1):
    w = uia().ControlViewWalker
    for _ in range(levels):
        el = w.GetParentElement(el)
        if not el:
            return None
    return el


def name_of(el) -> str:
    try:
        return el.CurrentName or ""
    except Exception:
        return ""


def rect(el) -> tuple[int, int, int, int]:
    r = el.CurrentBoundingRectangle
    return r.left, r.top, r.right, r.bottom


def center(el) -> tuple[int, int]:
    l, t, r, b = rect(el)
    return (l + r) // 2, (t + b) // 2


def click_el(el, double: bool = False) -> None:
    import pyautogui

    x, y = center(el)
    pyautogui.click(x, y, clicks=2 if double else 1)


def invoke_or_click(el) -> str:
    try:
        pat = el.GetCurrentPattern(U.UIA_InvokePatternId)
        pat.QueryInterface(U.IUIAutomationInvokePattern).Invoke()
        return "invoked"
    except Exception:
        click_el(el)
        return "clicked"


def value_of(el) -> str | None:
    try:
        pat = el.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(U.IUIAutomationValuePattern)
        return pat.CurrentValue
    except Exception:
        return None


def focused():
    try:
        return uia().GetFocusedElement()
    except Exception:
        return None


# ------------------------------------------------------------------ RAM AI's own window vs the window the agent works in
def is_ramai_window(h: int) -> bool:
    """RAM AI's own app window (Edge --app window titled 'RAM AI') or its control banner. The agent must never act on these."""
    try:
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        t = buf.value.strip()
    except Exception:
        return False
    cls = classname(h)
    if t == "RAM AI" or t.startswith("RAM AI -") or cls == "TkTopLevel":
        return True
    # RAM AI's window while it is still loading shows its own address as the title
    return cls == "Chrome_WidgetWin_1" and ("127.0.0.1:8765" in t or "localhost:8765" in t)


_SHELL_CLASSES = {"Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                  "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland"}


class WorkWindowTracker:
    """Remembers the last real app window the user/agent had in front (never RAM AI itself, never the taskbar)."""

    def __init__(self):
        self.hwnd: int | None = None

    def _ok(self, h) -> bool:
        if not h or not user32.IsWindowVisible(h) or is_ramai_window(h):
            return False
        if classname(h) in _SHELL_CLASSES:
            return False
        return user32.GetWindowTextLengthW(h) > 0

    def poll(self):
        h = user32.GetForegroundWindow()
        if self._ok(h):
            self.hwnd = h

    def current(self) -> int | None:
        self.poll()
        # re-check every time: a window can be remembered while its title is still loading (e.g. RAM AI's own window
        # briefly shows its web address before its title becomes 'RAM AI')
        if self.hwnd and (not self._ok(self.hwnd) or user32.IsIconic(self.hwnd)):
            self.hwnd = None
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        h = top_app_window()  # nothing known yet (RAM AI has been in front): use the top-most real app window
        if h:
            self.hwnd = h
        return h

    def start(self):
        import threading

        def loop():
            while True:
                try:
                    self.poll()
                except Exception:
                    pass
                time.sleep(0.25)

        threading.Thread(target=loop, daemon=True).start()


def window_title(h: int) -> str:
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def ensure_work_foreground(tracker: "WorkWindowTracker") -> int | None:
    """If RAM AI's own window is in front, bring the agent's work window back before any keyboard/mouse action."""
    fg = user32.GetForegroundWindow()
    if fg and not is_ramai_window(fg):
        return fg
    h = tracker.current()
    if h:
        focus_window(h)
        wait_for(lambda: user32.GetForegroundWindow() == h, 1.5, 0.05)
    return h


def _cloaked(h: int) -> bool:
    try:
        v = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(h), 14, ctypes.byref(v), ctypes.sizeof(v))  # DWMWA_CLOAKED
        return v.value != 0
    except Exception:
        return False


def top_app_window() -> int | None:
    """Top-most real application window in z-order that is not RAM AI (EnumWindows walks top to bottom)."""
    found: list[int] = []
    cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if not user32.IsWindowVisible(h) or user32.IsIconic(h) or user32.GetWindowTextLengthW(h) == 0:
            return True
        if is_ramai_window(h) or classname(h) in _SHELL_CLASSES or _cloaked(h):
            return True
        ex = user32.GetWindowLongW(h, -20)  # GWL_EXSTYLE
        if ex & 0x80:  # WS_EX_TOOLWINDOW
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        if (r.right - r.left) < 200 or (r.bottom - r.top) < 150:
            return True
        found.append(h)
        return False

    user32.EnumWindows(cb_t(cb), 0)
    return found[0] if found else None


def ramai_window_rects() -> list[tuple[int, int, int, int]]:
    out = []
    for h, _t in windows():
        if is_ramai_window(h) and not user32.IsIconic(h):
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            out.append((r.left, r.top, r.right, r.bottom))
    return out
