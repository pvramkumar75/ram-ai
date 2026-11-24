"""Side-by-side while working: RAM AI docks on the right edge (on top), the apps the agent works in stay on the left.

So the user always sees both the work (Excel, WhatsApp, Outlook, SAP...) and the agent's steps/approvals.
Only while a task runs and only when the RAM AI window is open; restored afterwards. Setting: dock_while_working."""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOACTIVATE, SWP_NOZORDER, SWP_SHOWWINDOW, SWP_NOMOVE, SWP_NOSIZE = 0x0010, 0x0004, 0x0040, 0x0002, 0x0001
SW_RESTORE, SW_MAXIMIZE = 9, 3
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x00C00000, 0x00000080, 0x00000008

u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.MonitorFromWindow.restype = wintypes.HANDLE
u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.GetWindowLongW.restype = ctypes.c_long


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _rect(h) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def work_area(h) -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u32.GetMonitorInfoW(u32.MonitorFromWindow(h, 2), ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def dock_width(screen_w: int) -> int:
    return max(440, min(640, round(screen_w * 0.30)))


def plan(win: tuple[int, int, int, int], area: tuple[int, int, int, int], dock_left: int, maximized: bool) -> tuple | None:
    """Where a work window should go so it does not sit under the docked RAM AI panel (None = leave it)."""
    l, t, r, b = win
    al, at, ar, ab = area
    if r <= dock_left + 8:
        return None
    left_w = dock_left - al
    if maximized or (r - l) > left_w * 0.85:          # big window: fill the left area
        return al, at, left_w, ab - at
    x = max(al, dock_left - (r - l))                  # small window/dialog: just slide it left
    return x, t, r - l, b - t


class Dock:
    def __init__(self, app=None):
        self.app = app
        self.users = 0
        self.ramai = None
        self.saved = None            # (rect, was_maximized) of the RAM AI window before docking
        self.area = None
        self.dock_left = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        try:
            return bool(self.app.settings.get("dock_while_working", True))
        except Exception:
            return True

    def _find_ramai(self):
        from . import uia

        for h, t in uia.windows():
            if uia.is_ramai_window(h) and uia.classname(h) != "TkTopLevel" and u32.IsWindowVisible(h) and not u32.IsIconic(h):
                return h
        return None

    # ------------------------------------------------------------ task start / end
    def start(self) -> None:
        with self._lock:
            self.users += 1
            if self.users > 1 or not self.enabled():
                return
            h = self._find_ramai()
            if not h:
                return                                   # no RAM AI window (terminal use): nothing to arrange
            self.ramai, self.saved = h, (_rect(h), bool(u32.IsZoomed(h)))
            self.area = work_area(h)
            al, at, ar, ab = self.area
            w = dock_width(ar - al)
            self.dock_left = ar - w
            if u32.IsZoomed(h):
                u32.ShowWindow(h, SW_RESTORE)
            u32.SetWindowPos(h, HWND_TOPMOST, self.dock_left, at, w, ab - at, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._stop.clear()
            self._thread = threading.Thread(target=self._keep_left, name="ramai-dock", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self.users = max(0, self.users - 1)
            if self.users or not self.ramai:
                return
            self._stop.set()
            h, (rect, was_max) = self.ramai, self.saved
            self.ramai = None
            try:
                if u32.IsWindow(h):
                    l, t, r, b = rect
                    u32.SetWindowPos(h, HWND_NOTOPMOST, l, t, r - l, b - t, SWP_NOACTIVATE)
                    if was_max:
                        u32.ShowWindow(h, SW_MAXIMIZE)
            except Exception:
                pass

    def status(self) -> dict:
        return {"docked": bool(self.ramai), "enabled": self.enabled()}

    # ------------------------------------------------------------ keep the work on the left
    def _is_work_window(self, h) -> bool:
        from . import uia

        if not h or h == self.ramai or not u32.IsWindowVisible(h) or u32.IsIconic(h) or uia.is_ramai_window(h):
            return False
        ex, st = u32.GetWindowLongW(h, GWL_EXSTYLE), u32.GetWindowLongW(h, GWL_STYLE)
        if ex & WS_EX_TOOLWINDOW or ex & WS_EX_TOPMOST or not st & WS_CAPTION:
            return False
        return uia.classname(h) not in ("Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow")

    def fit(self, h) -> bool:
        if not self.ramai or not self._is_work_window(h):
            return False
        p = plan(_rect(h), self.area, self.dock_left, bool(u32.IsZoomed(h)))
        if not p:
            return False
        if u32.IsZoomed(h):
            u32.ShowWindow(h, SW_RESTORE)
        x, y, w, hh = p
        u32.SetWindowPos(h, None, x, y, w, hh, SWP_NOZORDER | SWP_NOACTIVATE)
        return True

    def _keep_left(self):
        while not self._stop.wait(0.35):
            try:
                if self.ramai and not u32.IsWindow(self.ramai):
                    self.ramai = None
                    return
                self.fit(u32.GetForegroundWindow())
            except Exception:
                pass
