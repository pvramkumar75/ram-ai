"""WhatsApp: desktop app first, web (Playwright + Edge) only if the app is not installed.

Desktop driving reads the window with the built-in Windows OCR (local) because WhatsApp's WebView accessibility tree
is unreliable. Every step is verified on screen:
  search by name -> list ALL matching chats (duplicates kept) -> ask when ambiguous -> click the row ->
  confirm the chat HEADER shows that name -> click 'Type a message' -> type -> confirm the box holds exactly the text ->
  (approval) -> re-confirm header + text -> Enter -> confirm the box emptied and the new bubble shows the text.
Keystrokes are only sent while WhatsApp is the foreground window.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import time

from pydantic import Field, field_validator

from .. import uia
from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

WA_TITLE = re.compile(r"^(\(\d+\)\s*)?WhatsApp$", re.I)
TIME_TOKEN = re.compile(
    r"\s+(\d{1,2}:\d{2}(?:\s?[ap]m)?|Yesterday|Today|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Mon|Tue|Wed|Thu|Fri|Sat|Sun|\d{1,2}/\d{1,2}/\d{2,4})(\s|$)", re.I)
LEFT_RATIO = 0.285  # fallback only: chat list width as a fraction of the (maximised) window
_split_cache: dict = {}


def find_split(h: int) -> int | None:
    """Screen x of the line between WhatsApp's chat list and the open chat. The list has a fixed width (and the user can
    drag it), so it is measured on screen: the first column right of 250 px that is one solid colour top to bottom while
    the column just right of it differs. Cached per window position/size."""
    from .. import capture

    try:
        l, t, r, b = uia.rect(uia.root(h))
    except Exception:
        return None
    key = (h, l, t, r, b)
    if key in _split_cache:
        return _split_cache[key]
    found = None
    try:
        img, rc = capture.capture_window(h)
        img = img.convert("RGB")
        W, H = img.size
        px = img.load()
        ys = list(range(int(H * 0.25), int(H * 0.9), 6))

        def close(a, c, tol):
            return sum(abs(p - q) for p, q in zip(a, c)) <= tol

        for x in range(250, min(W - 3, int(W * 0.7))):
            col = [px[x, y] for y in ys]
            base = max(set(col), key=col.count)
            if sum(close(c, base, 12) for c in col) < 0.9 * len(col):
                continue
            if sum(not close(px[x + 2, y], c, 6) for y, c in zip(ys, col)) >= 0.6 * len(col):
                found = rc[0] + x + 1
                break
    except Exception:
        found = None
    if len(_split_cache) > 20:
        _split_cache.clear()
    _split_cache[key] = found
    return found


def find_wa_window() -> int | None:
    """The desktop app's top-level window. Title may carry an unread count, e.g. '(118) WhatsApp'. Never a browser tab."""
    best = None
    for h, t in uia.windows():
        if WA_TITLE.match(t.strip()) and not uia.is_browser_title(t):
            if uia.classname(h) == "WinUIDesktopWin32WindowClass":
                return h
            best = best or h
    return best


def parse_title(row_name: str) -> str:
    """Accessible row names look like 'Ravi 3:04 pm Shipment status…' -> 'Ravi' (used by tests / fallbacks)."""
    m = TIME_TOKEN.search(row_name)
    return (row_name[: m.start()] if m else row_name).strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def same_name(a: str, b: str) -> bool:
    x, y = _norm(a).replace(" ", ""), _norm(b).replace(" ", "")
    return bool(x) and (x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.85)


def match_rows(query: str, rows: list[dict]) -> list[dict]:
    toks = _norm(query).split()
    return [r for r in rows if r.get("title") and all(t in _norm(r["title"]) for t in toks)]


def pick(query: str, rows: list[dict], choice: int | None = None) -> tuple[dict | None, list[dict]]:
    """Choose a chat row by name. Duplicated names are NOT merged: two chats called 'Ravi' => ask the user."""
    m = match_rows(query, rows)
    if choice is not None:
        if not 1 <= choice <= len(m):
            raise PolicyError(f"choice {choice} is out of range; there are {len(m)} matching chats")
        return m[choice - 1], m
    exact = [r for r in m if _norm(r["title"]) == _norm(query)]
    if len(exact) == 1:
        return exact[0], m
    if len(m) == 1:
        return m[0], m
    return None, m


def choose(query: str, titles: list[str]) -> tuple[str | None, list[str]]:
    row, m = pick(query, [{"title": t} for t in titles])
    return (row["title"] if row else None), [r["title"] for r in m]


def describe(rows: list[dict]) -> list[dict]:
    return [{"choice": i + 1, "title": r["title"], "last_activity": r.get("time", ""), "preview": r.get("preview", "")[:70]}
            for i, r in enumerate(rows)]


def type_lines(text: str, ctx):
    """Type text; newlines become Shift+Enter (plain Enter would SEND in WhatsApp)."""
    import pyautogui

    from .computer import type_unicode

    for i, line in enumerate(text.split("\n")):
        if i:
            pyautogui.hotkey("shift", "enter")
        if line:
            type_unicode(line, ctx, 0.003)


# ------------------------------------------------------------------ clipboard read-back of the focused message box
def _clip_get() -> str | None:
    import win32clipboard as cb

    for _ in range(8):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.03)
    else:
        return None
    try:
        return cb.GetClipboardData(cb.CF_UNICODETEXT) if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT) else None
    except Exception:
        return None
    finally:
        cb.CloseClipboard()


def _clip_set(s: str | None) -> None:
    import win32clipboard as cb

    for _ in range(8):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.03)
    else:
        return
    try:
        cb.EmptyClipboard()
        if s is not None:
            cb.SetClipboardData(cb.CF_UNICODETEXT, s)
    finally:
        cb.CloseClipboard()


_BULLET = re.compile(r"(?m)^[ \t]*[-*\u2022\u2013\u2014][ \t]+")


def _clean(s: str | None) -> str:
    """Normalise for 'is this the same message': whitespace and bullet style ('- ', '* ', '• ') are not differences."""
    t = (s or "").replace("\r", "")
    t = _BULLET.sub("• ", t)
    return re.sub(r"\s+", " ", t).strip()


def first_difference(a: str, b: str) -> str:
    x, y = _clean(a), _clean(b)
    i = next((k for k in range(min(len(x), len(y))) if x[k] != y[k]), min(len(x), len(y)))
    return f"at character {i}: intended …{x[max(0, i - 25):i + 35]!r}… but box has …{y[max(0, i - 25):i + 35]!r}… (lengths {len(x)} vs {len(y)})"


def read_box() -> str:
    """Text in the focused message box ('' if empty) via select-all + copy. Restores the user's clipboard."""
    import uuid

    import pyautogui

    saved = _clip_get()
    sentinel = "RAMAI-" + uuid.uuid4().hex
    _clip_set(sentinel)
    got = None
    try:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.08)
        pyautogui.hotkey("ctrl", "c")
        # long texts take a moment to reach the clipboard: wait until it changes and is stable
        end, last = time.time() + 2.0, None
        while time.time() < end:
            time.sleep(0.12)
            cur = _clip_get()
            if cur not in (None, sentinel) and cur == last:
                break
            last = cur
        got = _clip_get()
        pyautogui.press("end")
        pyautogui.hotkey("ctrl", "end")
    finally:
        _clip_set(saved)
    return "" if got in (None, sentinel) else got


def paste_text(text: str) -> None:
    """Insert text with Ctrl+V (exact, fast; no list auto-formatting, newlines never send). Restores the user's clipboard."""
    import pyautogui

    saved = _clip_get()
    _clip_set(text.replace("\r\n", "\n").replace("\n", "\r\n"))
    try:
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.25 + min(1.5, len(text) / 4000))
    finally:
        _clip_set(saved)


DATE_CHIP = re.compile(r"^(today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}/\d{1,2}/\d{2,4}|"
                       r"\d{1,2} [a-z]{3,9},? \d{4})$", re.I)
TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}\s?(?:[ap]m)?\b.{0,4}$", re.I)
TIME_TAIL = re.compile(r"\s+(\d{1,2}:\d{2}\s?[ap]m)\b.{0,4}$", re.I)


def bubble_side(shot, ln) -> str:
    """Who a message line belongs to, from the colour of the bubble behind it: green = the user's own (outgoing), white or
    dark grey = the other person's. OCR alone cannot tell, and mixing the two up gives a wrong summary."""
    if not shot:
        return ""
    img, rc = shot
    x0, y0 = ln.x - rc[0] - 7, ln.cy - rc[1]
    px = img.load()
    pts = []
    for dx in (-2, 0, 2):
        for dy in (-2, 0, 2):
            x, y = x0 + dx, y0 + dy
            if 0 <= x < img.width and 0 <= y < img.height:
                pts.append(px[x, y])
    if not pts:
        return ""
    r, g, b = (sorted(p[i] for p in pts)[len(pts) // 2] for i in range(3))
    return "you" if (g - r >= 12 and g - b >= 6) else "them"


def classify_messages(lines, shot, limit: int = 15) -> list[dict]:
    """OCR lines of the open conversation -> messages with who sent them and when (latest last)."""
    out: list[dict] = []
    if shot:
        shot = (shot[0].convert("RGB"), shot[1])  # once, not per line
    for ln in sorted(lines, key=lambda x: x.y):
        t = ln.text.strip()
        if not t:
            continue
        prev = out[-1] if out else None
        if TIME_ONLY.match(t):
            if prev and prev["from"] in ("you", "them", "unknown") and "time" not in prev:
                prev["time"] = t.split()[0] + (" " + t.split()[1] if len(t.split()) > 1 and t.split()[1].lower() in ("am", "pm") else "")
            continue
        if DATE_CHIP.match(t):
            out.append({"from": "system", "text": t})
            continue
        side = bubble_side(shot, ln) or "unknown"
        tm = TIME_TAIL.search(t)
        if tm:
            t = t[: tm.start()].strip()
        if prev and prev["from"] == side and "time" not in prev and ln.y - prev["_y"] <= 1.8 * max(ln.h, 18):
            prev["text"] += "\n" + t
            prev["_y"] = ln.y
        else:
            out.append({"from": side, "text": t, "_y": ln.y})
        if tm:
            out[-1]["time"] = tm.group(1)
    for m in out:
        m.pop("_y", None)
    return out[-limit:]


# ================================================================== desktop backend (OCR + keyboard)
class Desktop:
    def __init__(self, app):
        self.app = app
        self.h = None

    # ---------------- window
    def fg_guard(self):
        if not self.h or not uia.user32.IsWindow(self.h):
            raise PolicyError("WhatsApp window not found")
        if uia.user32.GetForegroundWindow() != self.h:
            uia.focus_window(self.h)
            if not uia.wait_for(lambda: uia.user32.GetForegroundWindow() == self.h, 1.5, 0.05):
                raise PolicyError("WhatsApp could not be brought to the front (another window has focus). Nothing typed. "
                                  "Please don't use the keyboard/mouse while RAM AI works in WhatsApp, then retry.")

    def geom(self):
        l, t, r, b = uia.rect(uia.root(self.h))
        L, T = max(l, 0), max(t, 0)
        split = find_split(self.h) or (l + int((r - l) * LEFT_RATIO))
        return L, T, r, b, split

    def ocr_left(self):
        from .. import ocr

        L, T, r, b, split = self.geom()
        return ocr.read_region(L, T, split, b, scale=1.5)

    def window_image(self):
        """WhatsApp's OWN pixels (PrintWindow), so another window on top (RAM AI itself, a chat app...) cannot hide or
        corrupt what is read. None when the window cannot be captured (minimised, or the GPU gives a black frame)."""
        from .. import capture

        try:
            got = capture.capture_window(self.h)
            if not got:
                return None
            img, rc = got
            if img.convert("L").getextrema()[1] < 8:
                return None
            return img, rc
        except Exception:
            return None

    def read_area(self, left: int, top: int, right: int, bottom: int, scale: float = 1.0, shot=None):
        """OCR a screen rectangle of the WhatsApp window: from the window's own pixels first, the live screen as fallback."""
        from .. import ocr

        shot = shot or self.window_image()
        if shot:
            img, rc = shot
            try:
                crop = img.crop((max(0, left - rc[0]), max(0, top - rc[1]), min(img.width, right - rc[0]), min(img.height, bottom - rc[1])))
                if crop.width > 5 and crop.height > 5:
                    lines = ocr.read_image(crop, left, top, scale)
                    if lines:
                        return lines
            except Exception:
                pass
        return ocr.read_region(left, top, right, bottom, scale=scale)

    def ocr_header(self):
        L, T, r, b, split = self.geom()
        for _ in range(3):  # retry: the window may still be repainting right after a focus switch
            lines = sorted(self.read_area(split + 5, T + 25, r - 200, T + 130, scale=2.0), key=lambda x: x.y)
            if lines:
                return lines[0].text
            time.sleep(0.15)
        return ""

    def uia_chat_name(self) -> str | None:
        """The open chat's name straight from Windows' accessibility tree - exact, needs no OCR: the message box is named
        'Type a message to <name>' and the header button 'Contact info <name>' (groups: 'Group info <name>')."""
        try:
            root = uia.root(self.h)
            for el in uia.find_all(root, ctype="edit"):
                m = re.match(r"^type a message to\s+(.+)$", uia.name_of(el).strip(), re.I)
                if m:
                    return m.group(1).strip()
            for el in uia.find_all(root, ctype="button"):
                m = re.match(r"^(?:contact|group|business) info\s+(.+)$", uia.name_of(el).strip(), re.I)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
        return None

    def header_ok(self, expect: str) -> str | None:
        """Is `expect` the chat that is open? True if Windows' own name for it, or an OCR of the header (from the window's
        pixels, then from the screen), matches. Returns the name that matched."""
        for get in (self.uia_chat_name, self.ocr_header):
            try:
                n = get()
            except Exception:
                n = None
            if n and same_name(n, expect):
                return n
        return None

    def header_seen(self) -> str:
        """What the header shows now, for error messages."""
        try:
            return self.uia_chat_name() or self.ocr_header() or "(nothing readable)"
        except Exception:
            return "(nothing readable)"

    def ocr_bottom(self):
        L, T, r, b, split = self.geom()
        return self.read_area(split + 5, b - 110, r, b, scale=2.0)

    def conversation_shot(self):
        """(lines, shot): the visible conversation as OCR lines, plus the window picture they were read from."""
        L, T, r, b, split = self.geom()
        shot = self.window_image()
        return sorted(self.read_area(split + 5, T + 120, r, b - 90, scale=1.5, shot=shot), key=lambda x: x.y), shot

    def ocr_conversation(self):
        return self.conversation_shot()[0]

    def ensure(self):
        import subprocess

        h = find_wa_window()
        if not h:
            apps = self.app.start_apps.find("WhatsApp")
            if not apps:
                raise PolicyError("The WhatsApp desktop app is not installed")
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{apps[0]['AppID']}"], creationflags=0x08000000)
            h = uia.wait_for(find_wa_window, 20, 0.2)
        if not h:
            raise PolicyError("Could not open the WhatsApp desktop app window")
        self.h = h
        uia.user32.ShowWindow(h, 3)  # maximise: stable layout
        self.fg_guard()
        end = time.time() + 30
        while time.time() < end:
            lines = self.ocr_left()
            txt = " | ".join(_norm(x.text) for x in lines)
            if any(_norm(x.text) == "chats" for x in lines):
                return h
            if any(k in txt for k in ("scan", "qr code", "link with phone", "log in")):
                raise PolicyError("WhatsApp desktop is showing its link-device/QR screen. Please link it from your phone, then retry.")
            time.sleep(0.4)
            self.fg_guard()
        raise PolicyError("WhatsApp did not finish loading (no chat list visible after 30 s). Check the WhatsApp window.")

    # ---------------- chat list
    def parse_rows(self, lines) -> list[dict]:
        heads = sorted([x for x in lines if _norm(x.text) == "chats"], key=lambda x: x.y)
        if not heads:
            return []
        hx = heads[0].x  # column of the 'Chats' title and section headings
        filt = [x for x in lines if _norm(x.text).startswith(("unread", "favourites", "all"))]
        start_y = (max(x.y for x in filt) + 25) if filt else heads[0].y + 120
        section_heads = sorted([x for x in lines if abs(x.x - hx) < 18 and x.y > start_y - 30], key=lambda x: x.y)
        title_col = [x for x in lines if hx + 45 < x.x < hx + 140 and x.y > start_y]
        right_col = [x for x in lines if x.x > hx + 220]
        rows = []
        for ln in sorted(title_col, key=lambda x: x.y):
            sec = [s for s in section_heads if s.y < ln.y]
            section = _norm(sec[-1].text) if sec else "chats"
            if section.startswith("messages"):
                continue  # message-content hits are not chats with that name
            tm = next((x for x in right_col if abs(x.cy - ln.cy) <= 10), None)
            prev = rows[-1] if rows else None
            below_prev = prev and 12 < ln.y - prev["y"] < 48
            if tm or (section.startswith("contacts") and not below_prev):
                # OCR splits highlighted text ("Ravi" + "Personal"): join pieces on the same row, left of the time
                limit = tm.x if tm else hx + 400
                parts = sorted([x for x in lines if abs(x.cy - ln.cy) <= 7 and ln.x < x.x < limit], key=lambda x: x.x)
                title = " ".join([ln.text.strip()] + [x.text.strip() for x in parts])
                rows.append({"title": title, "time": tm.text if tm else "", "preview": "", "cx": ln.cx, "cy": ln.cy, "y": ln.y,
                             "section": section})
            elif below_prev and not prev["preview"]:
                prev["preview"] = ln.text.strip()
        return rows

    def search(self, query: str, ctx) -> list[dict]:
        import pyautogui

        from .computer import type_unicode

        self.ensure()
        lines = self.ocr_left()
        heads = sorted([x for x in lines if _norm(x.text) == "chats"], key=lambda x: x.y)
        if not heads:
            raise PolicyError("Chat list not visible")
        rows = []
        for attempt in range(2):  # WhatsApp is sometimes slow to show results (busy, just woke up): type once more and wait longer
            self.fg_guard()
            pyautogui.click(heads[0].x + 140, heads[0].cy + 66)  # the search box under the 'Chats' title
            time.sleep(0.15)
            self.fg_guard()
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            type_unicode(query, ctx, 0.004)
            last, rows = None, []
            end = time.time() + (4 if attempt == 0 else 7)
            while time.time() < end:
                time.sleep(0.3)
                rows = self.parse_rows(self.ocr_left())
                key = [(r["title"], r["time"]) for r in rows]
                if key and key == last:
                    break
                last = key
            rows = match_rows(query, self.refine_titles(rows))
            if rows:
                break
        try:
            for r in rows:
                self.app.people.note(wa_title=r["title"], source="whatsapp")
        except Exception:
            pass
        return rows

    def refine_titles(self, rows: list[dict]) -> list[dict]:
        """WhatsApp paints the matching letters of search results in green and a whole-panel OCR often drops them
        ('Suresh Babu' -> 'u Ram'). Reading each title row on its own gets the full name."""
        from .. import ocr

        L, T, r, b, split = self.geom()
        for row in rows[:10]:
            try:
                lines = ocr.read_region(max(L, row["cx"] - 160), row["cy"] - 13, split, row["cy"] + 13, scale=2.0)
            except Exception:
                continue
            best = max(lines, key=lambda x: len(x.text), default=None)
            if best is None:
                continue
            text = TIME_TOKEN.sub(" ", " " + best.text + " ").strip() if TIME_TOKEN.search(" " + best.text) else best.text.strip()
            if len(_norm(text)) > len(_norm(row["title"])) and (_norm(row["title"])[-4:] in _norm(text) or not row["title"]):
                row["title"] = text
        return rows

    def clear_search(self):
        import pyautogui

        self.fg_guard()
        pyautogui.press("escape")

    def find(self, query: str, ctx) -> list[dict]:
        rows = self.search(query, ctx)
        self.clear_search()
        return rows

    def ranked_matches(self, query: str, ctx) -> tuple[list[dict], list[str]]:
        """Chats for a name or short form, best first. Tries: a short form you used before, the words as said, then names
        from the people directory ('KVR' -> 'K V Rao'). Equally good matches are ordered by how much you interact:
        recent chats first (WhatsApp lists them newest first), plus how often you messaged them through RAM AI."""
        from ..people import clean_query, match as pmatch, norm as pnorm, rank

        ppl = self.app.people
        q = clean_query(query)
        al = ppl.db.one("SELECT a.key, p.wa_title FROM aliases a JOIN people p ON p.key=a.key WHERE a.alias=? AND a.channel='whatsapp'", (q,))
        tries = ([al["wa_title"]] if al and al["wa_title"] else []) + [query]
        for c in ppl.candidates(query, "whatsapp") + ppl.candidates(query, "mail"):
            nm = c.get("wa_title") or c.get("name")
            if nm and c["score"] >= 0.8 and pnorm(nm) not in [pnorm(t) for t in tries]:
                tries.append(nm)
        rows, used = [], []
        for t in tries[:4]:
            used.append(t)
            rows = self.search(t, ctx)
            if rows:
                break
        cands = []
        for i, r in enumerate(rows):
            key = "wa:" + pnorm(r["title"])
            sc, why = pmatch(query, r["title"])
            if sc == 0 and used[-1] != query:
                sc, why = 0.8, f"found as '{used[-1]}' (people you know)"
            sc = max(sc, 0.6)
            if al and al["key"] == key:
                sc, why = 1.01, f"you used '{q}' for this chat before"
            known = ppl.db.one("SELECT messaged FROM people WHERE key=?", (key,))
            recent = max(0, 8 - i) if (r.get("section") or "chats").startswith("chats") else 0
            cands.append({**r, "key": key, "score": round(sc, 2), "why": why or "WhatsApp search",
                          "interactions": recent + 3 * (known["messaged"] if known else 0), "order": i})
        return rank(cands), used

    def reset_search(self):
        """Clear WhatsApp's search box (its x button) so the chat list is not left filtered after RAM AI used it."""
        import pyautogui

        from .. import ocr

        try:
            L, T, r, b, split = self.geom()
            xs = [x for x in ocr.read_region(L, T, split, T + 220, scale=2.0) if x.text.strip().lower() in ("x", "×")]
            if xs:
                self.fg_guard()
                pyautogui.click(xs[0].cx, xs[0].cy)
                time.sleep(0.2)
        except Exception:
            pass

    def open_chat(self, query: str, ctx, choice: int | None = None) -> str:
        import pyautogui

        from ..people import explain

        ranked, used = self.ranked_matches(query, ctx)
        self.pick_note = ""
        if not ranked:
            self.reset_search()
            raise PolicyError(f"No WhatsApp chat matches '{query}' (searched: {', '.join(used)}). Ask the user for the name as saved in WhatsApp.")
        if choice is not None:
            if not 1 <= choice <= len(ranked):
                raise PolicyError(f"choice {choice} is out of range; there are {len(ranked)} matching chats: {describe(ranked)}")
            row = ranked[choice - 1]
            self.pick_note = f"option {choice} chosen: {row['title']}"
        else:
            row = ranked[0]
            if len(ranked) > 1 or _norm(row["title"]) != _norm(query):
                self.pick_note = explain(query, ranked, lambda c: c["title"])
        hdr = None
        for attempt in range(2):  # a missed click or a slow repaint is retried once before giving up
            self.fg_guard()
            pyautogui.click(row["cx"], row["cy"])
            hdr = uia.wait_for(lambda: self.header_ok(row["title"]), 4, 0.25)
            if hdr:
                break
        if not hdr:
            raise PolicyError(f"Clicked '{row['title']}' twice but cannot confirm that chat is open (WhatsApp shows '{self.header_seen()}'). "
                              "Nothing typed. Do not work around this with manual clicks; report it to the user.")
        self.reset_search()
        return row["title"]

    def focus_composer(self):
        import pyautogui

        self.fg_guard()
        ph = [x for x in self.ocr_bottom() if "type a message" in _norm(x.text)]
        if not ph:
            raise PolicyError("The message box is not empty or not visible ('Type a message' not shown). Nothing typed; "
                              "ask the user to clear any draft in that chat.")
        pyautogui.click(ph[0].cx, ph[0].cy)
        time.sleep(0.15)
        self.fg_guard()

    def compose(self, text: str, ctx):
        import pyautogui

        self.focus_composer()
        self.fg_guard()
        paste_text(text)
        self.fg_guard()
        got = read_box()
        if _clean(got) != _clean(text):
            # leave nothing half-typed behind: clear the box, then report
            self.fg_guard()
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("delete")
            raise PolicyError("After pasting, the message box differs from the intended text " + first_difference(text, got) +
                              ". The box was cleared; nothing sent. This is NOT a length limit (WhatsApp allows ~65,000 characters).")

    def send(self, chat_title: str, text: str) -> dict:
        import pyautogui

        self.fg_guard()
        if not self.header_ok(chat_title):
            raise PolicyError(f"The open chat is now '{self.header_seen()}', not '{chat_title}'. Nothing sent.")
        if _clean(read_box()) != _clean(text):
            raise PolicyError("The message box no longer holds exactly the approved text. Nothing sent.")
        self.fg_guard()
        pyautogui.press("enter")
        snippet = _norm(text)[:25]

        def done():
            empty = any("type a message" in _norm(x.text) for x in self.ocr_bottom())
            tail = self.ocr_conversation()[-6:]
            bubble = any(snippet and snippet in _norm(x.text) for x in tail)
            return {"box_empty": empty, "bubble_seen": bubble} if empty else None

        st = uia.wait_for(done, 4, 0.3) or {"box_empty": False, "bubble_seen": False}
        return st

    def read_chat(self, query: str, n: int, ctx, choice: int | None = None) -> dict:
        title = self.open_chat(query, ctx, choice)
        lines, shot = self.conversation_shot()
        msgs = classify_messages(lines, shot, n)
        return {"chat": title, "messages": msgs,
                "note": "Latest message last. from='you' = sent BY THE USER (green bubble), from='them' = sent by the other person, "
                        "'system' = date/notice. Quote who said what exactly as labelled; if from is 'unknown', say you cannot tell. A message "
                        "that starts with 'You' + a line is a REPLY quoting an earlier message of the user's; the actual sender is the 'from' label."}


# ================================================================== web backend (only if the app is not installed)
SEARCH_BOX = 'div[contenteditable="true"][data-tab="3"], div[role="textbox"][aria-label*="Search" i]'
COMPOSER = 'footer div[contenteditable="true"][role="textbox"], footer div[contenteditable="true"][data-tab="10"]'
HEADER_TITLE = "header span[title], #main header span[dir='auto']"


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


class Web:
    def __init__(self, app):
        self.app = app
        self.pw = self.ctx = self.page = None

    async def ensure(self):
        if self.page and not self.page.is_closed():
            return self.page
        from playwright.async_api import async_playwright

        self.pw = await async_playwright().start()
        self.ctx = await self.pw.chromium.launch_persistent_context(str(self.app.data_path / "whatsapp_profile"), channel="msedge", headless=False)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self.page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded")
        return self.page

    async def require_login(self):
        p = await self.ensure()
        for _ in range(40):
            if await p.query_selector(SEARCH_BOX):
                return p
            if await p.query_selector("canvas[aria-label*='QR' i], div[data-ref]"):
                raise PolicyError("WhatsApp Web shows a QR code. Scan it yourself in the Edge window, then retry.")
            await asyncio.sleep(0.25)
        raise PolicyError("WhatsApp Web UI not recognised (changed or blocked). Complete the action manually.")

    async def titles(self, query: str):
        p = await self.require_login()
        box = await p.wait_for_selector(SEARCH_BOX, timeout=8000)
        await box.click()
        await p.keyboard.press("Control+A")
        await p.keyboard.type(query, delay=15)
        await asyncio.sleep(0.9)
        t = await p.eval_on_selector_all("#pane-side span[title]", "els => els.map(e => e.getAttribute('title'))")
        return p, [x for x in t if x]

    async def header(self):
        el = await self.page.query_selector(HEADER_TITLE)
        return (await el.get_attribute("title") or await el.inner_text()) if el else ""


# ================================================================== dispatcher
class WhatsApp:
    def __init__(self, app):
        self.app = app
        self.desktop = Desktop(app)
        self.web = Web(app)
        self.prepared: dict | None = None
        self.lock = asyncio.Lock()

    async def discard_draft(self) -> None:
        """The user declined a message that was already typed into the chat box: take it out again, so the unsent text is not
        left behind (it would block the next attempt and could be sent by accident). Best effort; only clears the box of the
        very chat it was typed in."""
        pr, self.prepared = self.prepared, None
        if not pr or pr.get("backend") != "desktop":
            return
        async with self.lock:
            def run():
                import pyautogui

                d = self.desktop
                d.h = find_wa_window() or d.h
                if not d.h:
                    return
                d.fg_guard()
                if not d.header_ok(pr["chat"]):
                    return
                for el in uia.find_all(uia.root(d.h), ctype="edit"):
                    if re.match(r"^type a message", uia.name_of(el).strip(), re.I):
                        uia.click_el(el)
                        time.sleep(0.15)
                        d.fg_guard()
                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.press("delete")
                        return
            try:
                await asyncio.to_thread(run)
            except Exception:
                pass

    def backend(self) -> str:
        pref = self.app.settings.get("whatsapp_backend", "auto")
        if pref in ("desktop", "web"):
            return pref
        return "desktop" if self.app.start_apps.has("whatsapp") else "web"

    async def status(self) -> str:
        if self.backend() == "desktop":
            def f():
                try:
                    self.desktop.ensure()
                    return "ready (desktop app)"
                except PolicyError as e:
                    return f"not_ready: {e}"
            return await asyncio.to_thread(f)
        try:
            await self.web.require_login()
            return "ready (web)"
        except PolicyError as e:
            return f"not_ready: {e}"


class NoArgs(Args):
    pass


class FindArgs(Args):
    query: str = Field(max_length=120, description="Name (or part of it) as the user said it")


class ReadArgs(Args):
    contact: str = Field(max_length=120)
    choice: int | None = Field(None, ge=1, le=50, description="Only when several chats share the name: the number the user picked")
    max_messages: int = Field(15, ge=1, le=50)


class MsgArgs(Args):
    contact: str = Field(max_length=120, description="Name as the user said it")
    text: str = Field(min_length=1, max_length=4000)
    choice: int | None = Field(None, ge=1, le=50, description="Only when several chats share the name: the number the user picked from the candidates")

    @field_validator("text")
    @classmethod
    def _no_ai_signoff(cls, v: str) -> str:
        from ..signoff import strip_ai_signoff

        return strip_ai_signoff(v)


async def t_open(a, ctx):
    return {"backend": ctx.app.whatsapp.backend(), "status": await ctx.app.whatsapp.status()}


async def t_find(a: FindArgs, ctx):
    wa = ctx.app.whatsapp
    async with wa.lock:
        if wa.backend() == "desktop":
            def run():
                ranked, used = wa.desktop.ranked_matches(a.query, ctx)
                wa.desktop.reset_search()
                return ranked, used
            ranked, used = await asyncio.to_thread(run)
            from ..people import explain
            return {"backend": "desktop", "candidates": describe(ranked), "picked": ranked[0]["title"] if ranked else None,
                    "why": explain(a.query, ranked, lambda c: c["title"]) if ranked else f"no chat found (searched {used})",
                    "next": "whatsapp_send_message picks the first candidate automatically (best match; if tied, the chat you "
                            "interact with most). Pass choice=<number> only if the user wants another one."}
        p, titles = await wa.web.titles(a.query)
        chosen, c2 = choose(a.query, titles)
        await p.keyboard.press("Escape")
        return {"backend": "web", "candidates": [{"choice": i + 1, "title": t} for i, t in enumerate(c2)], "unique_match": chosen}


async def t_read(a: ReadArgs, ctx):
    wa = ctx.app.whatsapp
    async with wa.lock:
        if wa.backend() == "desktop":
            return await asyncio.to_thread(wa.desktop.read_chat, a.contact, a.max_messages, ctx, a.choice)
        p, titles = await wa.web.titles(a.contact)
        chosen, c2 = choose(a.contact, titles)
        if a.choice:
            chosen = c2[a.choice - 1] if 0 < a.choice <= len(c2) else None
        if not chosen:
            raise PolicyError(f"Ambiguous or no match: {c2[:8]}")
        await p.click(f"#pane-side span[title={_q(chosen)}]")
        await asyncio.sleep(0.6)
        rows = await p.eval_on_selector_all(
            "div.message-in, div.message-out",
            "els => els.slice(-%d).map(e => ({dir: e.classList.contains('message-out') ? 'me' : 'them', text: (e.innerText||'').slice(0,500)}))" % a.max_messages)
        return {"chat": chosen, "messages": rows}


async def t_prepare(a: MsgArgs, ctx):
    wa = ctx.app.whatsapp
    async with wa.lock:
        wa.prepared = None
        if wa.backend() == "desktop":
            def run():
                title = wa.desktop.open_chat(a.contact, ctx, a.choice)
                wa.desktop.compose(a.text, ctx)
                return title
            title = await asyncio.to_thread(run)
            wa.prepared = {"contact": a.contact, "text": a.text, "choice": a.choice, "chat": title, "backend": "desktop",
                           "why": getattr(wa.desktop, "pick_note", "")}
        else:
            p, titles = await wa.web.titles(a.contact)
            chosen, c2 = choose(a.contact, titles)
            if a.choice:
                chosen = c2[a.choice - 1] if 0 < a.choice <= len(c2) else None
            if not chosen:
                raise PolicyError(f"AMBIGUOUS or no match for '{a.contact}'. Candidates: {c2[:8]}. Ask the user, then pass choice.")
            await p.click(f"#pane-side span[title={_q(chosen)}]")
            box = await p.wait_for_selector(COMPOSER, timeout=8000)
            await box.click()
            await p.keyboard.press("Control+A")
            await p.keyboard.press("Delete")
            await p.keyboard.insert_text(a.text)
            title = await wa.web.header()
            if not same_name(title, chosen):
                raise PolicyError(f"Opened chat '{title}', expected '{chosen}'. Nothing sent.")
            wa.prepared = {"contact": a.contact, "text": a.text, "choice": a.choice, "chat": title, "backend": "web"}
        return {"prepared_for": title, "text": a.text, "verified": "chat header matches and the message box holds exactly this text",
                "note": "NOT sent. Call whatsapp_send_prepared with the same contact, text and choice; the user will be asked to approve."}


def _same_prep(pr, a: MsgArgs) -> bool:
    return bool(pr) and pr["contact"] == a.contact and pr["text"] == a.text and pr.get("choice") == a.choice


async def send_preview(a: MsgArgs, ctx):
    pr = ctx.app.whatsapp.prepared
    if not _same_prep(pr, a):
        raise PolicyError("Prepare this exact message first with whatsapp_prepare_message")
    why = f"\nPicked: {pr['why']}" if pr.get("why") else ""
    return {"text": f"SEND WHATSAPP MESSAGE\nTo chat: {pr['chat']}  (you asked for: {a.contact}"
                    f"{', option ' + str(a.choice) if a.choice else ''}){why}\n--- message ---\n{a.text}",
            "digest": hashlib.sha256((a.contact + "\0" + a.text + "\0" + str(a.choice)).encode()).hexdigest()}


async def t_send(a: MsgArgs, ctx):
    wa = ctx.app.whatsapp
    key = "wa:" + hashlib.sha256((a.contact + "\0" + a.text + "\0" + ctx.task_id).encode()).hexdigest()
    if ctx.app.db.one("SELECT 1 FROM sent WHERE key=?", (key,)):
        raise PolicyError("Already sent in this task; not sending again.")
    async with wa.lock:
        pr = wa.prepared
        if not _same_prep(pr, a):
            raise PolicyError("Prepared message does not match the approved one. Nothing sent.")
        if pr["backend"] == "desktop":
            def run():
                # verification happens inside send() before Enter; mark the ledger right before pressing Enter
                ctx.app.db.exec("INSERT INTO sent(key,ts) VALUES(?,strftime('%s','now'))", (key,))
                try:
                    return wa.desktop.send(pr["chat"], a.text)
                except PolicyError:
                    ctx.app.db.exec("DELETE FROM sent WHERE key=?", (key,))  # refused before Enter: nothing was sent
                    raise
            st = await asyncio.to_thread(run)
            wa.prepared = None
            if st["box_empty"]:
                try:
                    from ..people import norm as pnorm
                    ctx.app.people.note(wa_title=pr["chat"], source="whatsapp", sent="whatsapp")
                    ctx.app.people.learn(a.contact, "whatsapp", "wa:" + pnorm(pr["chat"]))
                except Exception:
                    pass
            if st["box_empty"] and st["bubble_seen"]:
                return {"sent_verified": True, "chat": pr["chat"], "evidence": "message box emptied and the new bubble shows the text"}
            if st["box_empty"]:
                return {"sent": True, "chat": pr["chat"], "evidence": "message box emptied after Enter; bubble text not confirmed by OCR"}
            return {"sent": "uncertain", "warning": "Enter pressed but the box did not empty. Do NOT resend; ask the user to check the chat."}
        page = wa.web.page
        title = await wa.web.header()
        if not same_name(title, pr["chat"]):
            raise PolicyError(f"Open chat is '{title}', not '{pr['chat']}'. Nothing sent.")
        ctx.app.db.exec("INSERT INTO sent(key,ts) VALUES(?,strftime('%s','now'))", (key,))
        before = await page.eval_on_selector_all("div.message-out", "els => els.length")
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        after = await page.eval_on_selector_all("div.message-out", "els => els.slice(-1).map(e => (e.innerText||''))")
        count = await page.eval_on_selector_all("div.message-out", "els => els.length")
        wa.prepared = None
        if count > before and after and a.text.strip()[:40] in after[0]:
            return {"sent_verified": True, "chat": title}
        return {"sent": "uncertain", "warning": "Could not verify. Do NOT resend; ask the user to check the chat."}


async def quick_preview(a: MsgArgs, ctx):
    await t_prepare(a, ctx)  # opens the chat, verifies the header, types and verifies the text (nothing sent)
    return await send_preview(a, ctx)


def register(reg: Registry) -> None:
    R = Risk
    reg.register(Tool("whatsapp_send_message", "FASTEST way to send a WhatsApp message (also called a message/msg) in ONE call. contact may be a NAME or SHORT FORM (KVR, Suresh, Srini): the best-matching chat is picked (if several match equally, the one you interact with most) and shown on the approval card. Finds and verifies the chat, types the text, gets approval, sends. Use choice=<number> only if the user wants a different match.", MsgArgs, t_send, R.HIGH, 120, preview=quick_preview, reversible=False))
    reg.register(Tool("whatsapp_open", "Open WhatsApp (desktop app if installed, else web) and report readiness.", NoArgs, t_open, R.LOW, 60))
    reg.register(Tool("whatsapp_find_chat", "Search WhatsApp by name or short form; lists every matching chat best first (identical names listed separately) and which one would be picked.", FindArgs, t_find, R.READ, 60, untrusted=True))
    reg.register(Tool("whatsapp_read_chat", "Read the visible latest messages of a chat found by name.", ReadArgs, t_read, R.READ, 60, untrusted=True))
    reg.register(Tool("whatsapp_prepare_message", "Open the chat by name, verify it, and type the message WITHOUT sending. If it says AMBIGUOUS, ask the user and retry with choice.", MsgArgs, t_prepare, R.LOW, 90))
    reg.register(Tool("whatsapp_send_prepared", "Send the prepared message (same contact/text/choice). User sees recipient and exact text and must approve.", MsgArgs, t_send, R.HIGH, 60, preview=send_preview, reversible=False))
