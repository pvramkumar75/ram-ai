"""Local screen reading with the built-in Windows OCR engine (no cloud, ~0.1-0.3 s per window).
Gives RAM AI 'eyes' for apps whose accessibility tree is unreliable (e.g. WhatsApp's WebView)."""
from __future__ import annotations

import asyncio
import io
import re
import threading
from dataclasses import dataclass

from PIL import Image

_engine = None
_lock = threading.Lock()


@dataclass
class Line:
    text: str
    x: int  # left, screen coordinates
    y: int  # top
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def as_dict(self) -> dict:
        return {"text": self.text, "center": [self.cx, self.cy]}


def _get_engine():
    global _engine
    if _engine is None:
        import winrt.windows.media.ocr as ocr

        _engine = ocr.OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise RuntimeError("Windows OCR engine unavailable (install an OCR language pack in Windows settings)")
    return _engine


async def _recognize(img: Image.Image):
    import winrt.windows.graphics.imaging as gi
    import winrt.windows.storage.streams as ss

    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    stream = ss.InMemoryRandomAccessStream()
    writer = ss.DataWriter(stream)
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await gi.BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    return await _get_engine().recognize_async(bmp)


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # called from inside an event loop thread: run in a private loop on another thread
    out: dict = {}

    def t():
        out["r"] = asyncio.run(coro)

    th = threading.Thread(target=t)
    th.start()
    th.join()
    return out["r"]


def read_region(left: int, top: int, right: int, bottom: int, scale: float = 1.0) -> list[Line]:
    """OCR a screen rectangle (screen coordinates). Returns text lines with screen-coordinate boxes."""
    import mss

    with _lock:
        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": max(1, right - left), "height": max(1, bottom - top)})
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        res = _run(_recognize(img))
    lines: list[Line] = []
    for ln in res.lines:
        ws = list(ln.words)
        if not ws:
            continue
        xs = [w.bounding_rect.x for w in ws]
        ys = [w.bounding_rect.y for w in ws]
        x2 = [w.bounding_rect.x + w.bounding_rect.width for w in ws]
        y2 = [w.bounding_rect.y + w.bounding_rect.height for w in ws]
        lines.append(Line(ln.text, left + int(min(xs) / scale), top + int(min(ys) / scale),
                          int((max(x2) - min(xs)) / scale), int((max(y2) - min(ys)) / scale)))
    return lines


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.]", " ", s.lower())).strip()


def find_text(lines: list[Line], text: str) -> list[Line]:
    t = norm(text)
    return [l for l in lines if t and t in norm(l.text)]
