"""A clear view for the agent: whatever sits on top of the window it is working in gets minimised (not closed), and is
brought back when the task ends. Nothing is ever clicked or closed in the blocking window."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
GW_HWNDPREV, GW_OWNER = 3, 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT = 0x00000080, 0x00000020
SW_MAXIMIZE, SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 3, 4, 7
DWMWA_CLOAKED = 14
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow", "TkTopLevel",
                 "XamlExplorerHostIslandWindow", "NotifyIconOverflowWindow"}

u32.GetWindow.restype = wintypes.HWND
u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]


def rect(h) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _pid(h) -> int:
    p = wintypes.DWORD()
    u32.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(p))
    return p.value


def _class(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(wintypes.HWND(h), b, 256)
    return b.value


def _cloaked(h) -> bool:
    v = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(h), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        return False
    return bool(v.value)


def overlap(a, b) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def blockers(target: int) -> list[int]:
    """Visible windows lying ON TOP of `target` and covering a real part of it. Its own dialogs and pop-ups, the taskbar,
    tool windows and invisible system windows do not count."""
    tr, tpid, mine = rect(target), _pid(target), os.getpid()
    area = max(1, (tr[2] - tr[0]) * (tr[3] - tr[1]))
    out, h, seen = [], u32.GetWindow(target, GW_HWNDPREV), 0
    while h and seen < 2000:
        seen += 1
        try:
            if (u32.IsWindowVisible(h) and not u32.IsIconic(h) and not _cloaked(h) and _pid(h) not in (tpid, mine)
                    and u32.GetWindow(h, GW_OWNER) != target and _class(h) not in SHELL_CLASSES
                    and not u32.GetWindowLongW(h, GWL_EXSTYLE) & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT)):
                o = overlap(rect(h), tr)
                if o > 20000 and o > area * 0.03:
                    out.append(h)
        except Exception:
            pass
        h = u32.GetWindow(h, GW_HWNDPREV)
    return out


def clear_view(target: int, keep: set | None = None) -> list[int]:
    """Minimise everything covering `target` (except `keep`). Returns the windows that were minimised (to restore later)."""
    done = []
    for h in blockers(target):
        if keep and h in keep:
            continue
        u32.ShowWindow(h, SW_SHOWMINNOACTIVE)
        done.append(h)
    return done


def restore(windows) -> None:
    for h in list(windows):
        try:
            if u32.IsWindow(h) and u32.IsIconic(h):
                u32.ShowWindow(h, SW_SHOWNOACTIVATE)
        except Exception:
            pass


def wait_stable(h: int, timeout: float = 2.0, still: float = 0.45) -> bool:
    """Wait until the window has stopped moving / resizing (so what is read is still there when it is clicked)."""
    end = time.time() + timeout
    last, since = rect(h), time.time()
    while time.time() < end:
        time.sleep(0.08)
        now = rect(h)
        if now != last:
            last, since = now, time.time()
        elif time.time() - since >= still:
            return True
    return False
