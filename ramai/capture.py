"""Capture one window's own pixels (even when other windows, e.g. RAM AI itself, cover it) - no mirror effect."""
from __future__ import annotations

import ctypes

from PIL import Image

PW_RENDERFULLCONTENT = 2


def capture_window(hwnd: int) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    import win32gui
    import win32ui

    if ctypes.windll.user32.IsIconic(hwnd):
        return None
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w < 20 or h < 20:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hwnd_dc)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc, w, h)
        mem.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    if not ok:
        return None
    # maximised windows extend a few invisible pixels past the screen edge: trim them
    if ctypes.windll.user32.IsZoomed(hwnd) and l < 0 and t < 0:
        img = img.crop((-l, -t, w + l, h + l))
        l, t, r, b = 0, 0, r + l, b + l
    return img, (l, t, r, b)
