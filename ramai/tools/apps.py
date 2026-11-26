"""Smart app launcher (any installed Start-menu app, web fallback) and fast UI Automation tools."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Literal

from pydantic import Field

from .. import uia
from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

WEB_FALLBACK = {
    "whatsapp": "https://web.whatsapp.com", "outlook": "https://outlook.office.com/mail", "mail": "https://outlook.office.com/mail",
    "gmail": "https://mail.google.com", "teams": "https://teams.microsoft.com", "calendar": "https://outlook.office.com/calendar",
    "excel": "https://www.office.com/launch/excel", "word": "https://www.office.com/launch/word",
    "powerpoint": "https://www.office.com/launch/powerpoint", "onedrive": "https://onedrive.live.com",
    "drive": "https://drive.google.com", "sheets": "https://docs.google.com/spreadsheets", "docs": "https://docs.google.com/document",
    "grok": "https://grok.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "copilot": "https://copilot.microsoft.com",
    "perplexity": "https://www.perplexity.ai", "gemini": "https://gemini.google.com", "deepseek": "https://chat.deepseek.com",
    "youtube": "https://www.youtube.com", "linkedin": "https://www.linkedin.com", "zoom": "https://app.zoom.us",
}
# website -> desktop app with the same service: "open grok.com" opens the installed Grok app instead
APP_SITES = {"grok.com": "Grok", "x.ai": "Grok", "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "claude.ai": "Claude",
             "copilot.microsoft.com": "Copilot", "web.whatsapp.com": "WhatsApp", "outlook.office.com": "Outlook",
             "outlook.office365.com": "Outlook", "outlook.live.com": "Outlook", "teams.microsoft.com": "Microsoft Teams",
             "teams.live.com": "Microsoft Teams", "www.perplexity.ai": "Perplexity", "perplexity.ai": "Perplexity",
             "chat.deepseek.com": "DeepSeek", "app.zoom.us": "Zoom", "web.telegram.org": "Telegram", "open.spotify.com": "Spotify"}


def app_for_url(url: str) -> str | None:
    """The desktop app for a service's HOME page (grok.com, chatgpt.com/...). Specific pages (a search, a document) stay in the browser."""
    from urllib.parse import urlparse

    u = urlparse(url)
    host, path = (u.hostname or "").lower(), (u.path or "/").rstrip("/")
    name = APP_SITES.get(host) or APP_SITES.get(host.removeprefix("www."))
    if name and path in ("", "/new", "/chat", "/c", "/i", "/mail", "/mail/inbox") and not u.query:
        return name
    return None
# window-title words for apps whose title differs from their Start-menu name
TITLE_HINT = {"whatsapp": "whatsapp", "outlook": "outlook", "excel": "excel", "word": "word", "teams": "teams",
              "notepad": "notepad", "calculator": "calculator"}


class StartApps:
    """Cached list of installed Start-menu apps (name, AppID). Loaded in the background at startup."""

    def __init__(self):
        self.apps: list[dict] = []
        self.loaded = threading.Event()

    def load(self):
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                                 capture_output=True, text=True, timeout=30, creationflags=0x08000000).stdout
            d = json.loads(out or "[]")
            self.apps = d if isinstance(d, list) else [d]
        except Exception:
            self.apps = []
        self.loaded.set()

    def start_background(self):
        threading.Thread(target=self.load, daemon=True).start()

    def find(self, query: str, prefer: str | None = None) -> list[dict]:
        self.loaded.wait(20)
        q = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
        ex = [a for a in self.apps if re.sub(r"[^a-z0-9 ]", "", a["Name"].lower()).strip() == q]
        sw = [a for a in self.apps if a["Name"].lower().startswith(q)]
        co = [a for a in self.apps if q in a["Name"].lower()]
        res = ex or sw or co
        if prefer and len(res) > 1:
            pref = [a for a in res if prefer in a["AppID"].lower()]
            res = pref or res
        return res

    def has(self, query: str) -> bool:
        return bool(self.find(query))


def prefer_for(app, name: str) -> str | None:
    if "outlook" in name.lower():
        return "outlookforwindows" if app.settings.get("outlook_variant") == "new" else "office.outlook"
    return None


def launch(app, name: str, prefer_web: bool = False, wait: float = 12.0) -> dict:
    key = name.lower().strip()
    hint = next((v for k, v in TITLE_HINT.items() if k in key), key)
    if "whatsapp" in key and not prefer_web:
        from .whatsapp import find_wa_window

        h = find_wa_window()
    else:
        h = uia.find_window(hint)  # browser windows are excluded, so a web tab never counts as "the app"
    if h and not prefer_web:
        uia.focus_window(h)
        return {"status": "focused_existing", "window": dict(uia.windows()).get(h, "")}
    if not prefer_web:
        found = app.start_apps.find(name, prefer_for(app, name))
        if found:
            a = found[0]
            before = {x for x, _ in uia.windows()}
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{a['AppID']}"], creationflags=0x08000000)
            win = uia.wait_for(lambda: uia.find_window(hint) or next((x for x, t in uia.windows() if x not in before and t and not uia.is_browser_title(t)), None), wait, 0.15)
            if win:
                uia.focus_window(win)
                return {"status": "launched", "app": a["Name"], "window": dict(uia.windows()).get(win, "")}
            return {"status": "launched_no_window_yet", "app": a["Name"], "note": "App started but no window detected yet; try again"}
    url = next((u for k, u in WEB_FALLBACK.items() if k in key), None)
    if not url:
        close = [a["Name"] for a in app.start_apps.find(name.split()[0])][:8] if name.split() else []
        raise PolicyError(f"'{name}' is not installed and has no known web version. Similar installed apps: {close}")
    subprocess.Popen(["cmd", "/c", "start", "", "msedge", url], creationflags=0x08000000)
    return {"status": "opened_web_app", "url": url, "note": "Desktop app not installed (or web requested); opened the web version in Edge."}


# ------------------------------------------------------------------ tools
class LaunchArgs(Args):
    name: str = Field(max_length=60, description="App or service name, e.g. 'WhatsApp', 'Outlook', 'Excel', 'Grok', 'ChatGPT', 'Claude', 'SAP Logon'")
    prefer_web: bool = False


class FocusArgs(Args):
    title_contains: str = Field(max_length=100)


class UiElementsArgs(Args):
    max_items: int = Field(60, ge=1, le=200)
    types: list[Literal["button", "edit", "menuitem", "listitem", "dataitem", "checkbox", "combobox", "tabitem", "hyperlink"]] = Field(
        default_factory=lambda: ["button", "edit", "menuitem", "listitem", "checkbox", "combobox", "tabitem", "hyperlink"])


class UiClickArgs(Args):
    name: str = Field(max_length=200, description="Exact accessible name")
    control_type: Literal["button", "edit", "menuitem", "listitem", "dataitem", "checkbox", "combobox", "tabitem", "hyperlink", "text"] | None = None
    double: bool = False


class UiTypeArgs(Args):
    name: str = Field(max_length=200, description="Exact accessible name of the edit box")
    text: str = Field(max_length=5000)
    clear_first: bool = True
    at_start: bool = Field(False, description="Put the caret at the very start (Ctrl+Home) before typing, e.g. above an email signature")


def t_launch(a: LaunchArgs, ctx):
    return launch(ctx.app, a.name, a.prefer_web)


def t_focus(a: FocusArgs, ctx):
    h = uia.find_window(a.title_contains)
    if not h:
        raise PolicyError(f"No visible window containing '{a.title_contains}'")
    uia.focus_window(h)
    _unblock(h, ctx)
    return {"focused": dict(uia.windows()).get(h)}


def _unblock(h, ctx) -> None:
    """Anything still lying on top of the window the agent needs is minimised (and brought back when the task ends).
    The docked RAM AI panel stays: the dock moves the work window beside it instead."""
    try:
        from .. import view

        dock = getattr(ctx.app, "dock", None)
        if dock:
            dock.fit(h)
        moved = view.clear_view(h, keep={dock.ramai} if dock and dock.ramai else set())
        if dock:
            dock.minimized.extend(w for w in moved if w not in dock.minimized)
    except Exception:
        pass


def _fg_root(ctx):
    from .computer import is_excluded

    ex = is_excluded(ctx.app)
    if ex:
        raise PolicyError(f"Foreground window matches excluded app '{ex}'")
    tracker = getattr(ctx.app, "work", None)
    h = uia.user32.GetForegroundWindow()
    if uia.is_ramai_window(h) and tracker is not None:
        h = uia.ensure_work_foreground(tracker) or h
    return uia.root(h)


def t_ui_elements(a: UiElementsArgs, ctx):
    r = _fg_root(ctx)
    from .computer import foreground_title

    out, seen, t0 = [], set(), time.time()
    for ty in a.types:
        if time.time() - t0 > 6 or len(out) >= a.max_items:
            break
        for e in uia.find_all(r, None, ty):
            nm = uia.name_of(e)
            if not nm or (nm, ty) in seen:
                continue
            seen.add((nm, ty))
            try:
                out.append({"name": nm[:90], "type": ty, "center": list(uia.center(e))})
            except Exception:
                continue
            if len(out) >= a.max_items:
                break
    return {"window": foreground_title(), "elements": out, "note": "Click by exact name with ui_click (fast)."}


def t_ui_click(a: UiClickArgs, ctx):
    r = _fg_root(ctx)
    e = uia.find_first(r, a.name, a.control_type)
    if not e:
        raise PolicyError(f"No control named '{a.name}' in the foreground window (use ui_elements to see names)")
    if a.double:
        uia.click_el(e, True)
        return {"double_clicked": a.name}
    return {a.name: uia.invoke_or_click(e)}


def t_ui_type(a: UiTypeArgs, ctx):
    from .computer import type_unicode
    import pyautogui

    r = _fg_root(ctx)
    e = uia.find_first(r, a.name, "edit") or uia.find_first(r, a.name, "document")
    if not e:
        raise PolicyError(f"No edit box named '{a.name}'")
    try:
        e.SetFocus()
    except Exception:  # some document-like controls refuse UIA focus: click into them instead
        x, y = uia.center(e)
        pyautogui.click(x, y)
    time.sleep(0.05)
    if a.clear_first and not a.at_start:
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
    if a.at_start:
        pyautogui.hotkey("ctrl", "home")
    type_unicode(a.text, ctx, 0.004)
    return {"typed_into": a.name, "chars": len(a.text)}


def register(reg: Registry) -> None:
    R = Risk
    reg.register(Tool("launch_app", "Open (or focus) any app or online service BY NAME: the installed desktop app first (WhatsApp, Outlook, Excel, Grok, ChatGPT, Claude, Copilot, Teams, SAP Logon...), else its web version. ALWAYS use this when the user says 'open X'.", LaunchArgs, t_launch, R.LOW, 30))
    reg.register(Tool("focus_window", "Bring an open window to the front by part of its title.", FocusArgs, t_focus, R.LOW, 10))
    reg.register(Tool("ui_elements", "FAST list of named controls in the foreground window (name, type, centre).", UiElementsArgs, t_ui_elements, R.READ, 30, untrusted=True))
    reg.register(Tool("ui_click", "FAST click/invoke a control by exact name in the foreground window.", UiClickArgs, t_ui_click, lambda a: _send_risk(a.name), 20, control=True))
    reg.register(Tool("ui_type", "FAST focus an edit box by exact name and type text into it.", UiTypeArgs, t_ui_type, R.LOW, 30, control=True))


# ------------------------------------------------------------------ OCR: see and click any visible text in the work window
class ReadTextArgs(Args):
    contains: str | None = Field(None, max_length=100, description="Only return lines containing this text")


class ClickTextArgs(Args):
    text: str = Field(min_length=1, max_length=100, description="Visible text of the button/link/label to click, e.g. 'Send'")
    occurrence: int = Field(1, ge=1, le=20, description="Which match (top-to-bottom, left-to-right) if the text appears several times")
    double: bool = False
    exact: bool = Field(True, description="Whole-line match (safer) instead of 'contains'")


def _work_rect(ctx):
    tracker = getattr(ctx.app, "work", None)
    h = uia.user32.GetForegroundWindow()
    if uia.is_ramai_window(h) and tracker is not None:
        h = uia.ensure_work_foreground(tracker) or h
    l, t, r, b = uia.rect(uia.root(h))
    return h, max(l, 0), max(t, 0), r, b


def t_read_text(a: ReadTextArgs, ctx):
    from .. import ocr

    h, l, t, r, b = _work_rect(ctx)
    lines = sorted(ocr.read_region(l, t, r, b, scale=1.5), key=lambda x: (x.y // 12, x.x))
    if a.contains:
        lines = ocr.find_text(lines, a.contains)
    return {"window": uia.window_title(h), "lines": [x.as_dict() for x in lines[:300]],
            "note": "Text seen on screen (OCR) with centre coordinates. Use click_text to click a label."}


def t_click_text(a: ClickTextArgs, ctx):
    import pyautogui

    from .. import ocr
    from .computer import _stop_check

    _stop_check(ctx)
    h, l, t, r, b = _work_rect(ctx)
    lines = sorted(ocr.read_region(l, t, r, b, scale=2.0), key=lambda x: (x.y // 12, x.x))
    want = ocr.norm(a.text)
    hits = [x for x in lines if ocr.norm(x.text) == want] if a.exact else ocr.find_text(lines, a.text)
    if not hits and a.exact:  # also accept a line that starts with the label (e.g. "Send" + icon noise)
        hits = [x for x in lines if ocr.norm(x.text).startswith(want + " ") or ocr.norm(x.text).endswith(" " + want)]
    if len(hits) < a.occurrence:
        seen = [x.text for x in lines if want[:3] in ocr.norm(x.text)][:10]
        raise PolicyError(f"Text '{a.text}' not found on screen (found {len(hits)} matches). Similar text: {seen}")
    hit = hits[a.occurrence - 1]
    pyautogui.click(hit.cx, hit.cy, clicks=2 if a.double else 1)
    return {"clicked": hit.text, "at": [hit.cx, hit.cy], "matches": len(hits), "window": uia.window_title(h)}


SEND_WORDS = {"send", "send now", "send message", "post", "reply all", "submit", "pay", "pay now", "place order", "confirm payment"}


def _send_risk(label: str) -> Risk:
    """Clicking a Send/Submit/Pay button is an outward action: it needs the same approval as send_email."""
    import re as _re

    return Risk.HIGH if _re.sub(r"\s+", " ", (label or "").strip().lower()) in SEND_WORDS else Risk.LOW


def register_ocr(reg: Registry) -> None:
    reg.register(Tool("read_screen_text", "Read the visible text of the window in front (OCR, local) with positions. Use when ui_elements does not show what you need.", ReadTextArgs, t_read_text, Risk.READ, 30, untrusted=True))
    reg.register(Tool("click_text", "Click a button/link/label by its VISIBLE TEXT using OCR (e.g. 'Send', 'OK', 'Attach'). Works where UI Automation fails.", ClickTextArgs, t_click_text, lambda a: _send_risk(a.text), 30, control=True, reversible=False))


class CopyTextArgs(Args):
    title_contains: str | None = Field(None, max_length=100, description="Window to read (default: the window in front)")
    max_chars: int = Field(20000, ge=500, le=60000)


def t_copy_text(a: CopyTextArgs, ctx):
    """All text of a window: UI Automation document text first (no clipboard), else Ctrl+A/Ctrl+C (clipboard restored), else OCR."""
    import pyautogui

    from .whatsapp import _clip_get, _clip_set

    if a.title_contains:
        h = uia.find_window(a.title_contains)
        if not h:
            raise PolicyError(f"No window containing '{a.title_contains}'")
        uia.focus_window(h)
        time.sleep(0.3)
    else:
        h, *_ = _work_rect(ctx)
    title = uia.window_title(h)
    root = uia.root(h)
    text, how = "", ""
    docs = uia.find_all(root, None, "document")
    for d in docs[:3]:
        try:
            tp = d.GetCurrentPattern(uia.U.UIA_TextPatternId).QueryInterface(uia.U.IUIAutomationTextPattern)
            t = tp.DocumentRange.GetText(a.max_chars * 2) or ""
        except Exception:
            t = ""
        if len(t.strip()) > len(text.strip()):
            text, how = t, "UI Automation (document text)"
    if len(text.strip()) < 200:  # fall back to select-all + copy
        saved = _clip_get()
        sentinel = "RAMAI-COPY"
        _clip_set(sentinel)
        try:
            if docs:
                try:
                    docs[0].SetFocus()
                except Exception:
                    pass
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.3)
            got = _clip_get()
            pyautogui.press("esc")
        finally:
            _clip_set(saved)
        if got and got != sentinel and len(got.strip()) > len(text.strip()):
            text, how = got, "select-all + copy (your clipboard was restored)"
    if len(text.strip()) < 50:
        from .. import ocr

        l, t2, r, b = uia.rect(root)
        text = "\n".join(x.text for x in sorted(ocr.read_region(max(l, 0), max(t2, 0), r, b, scale=1.5), key=lambda x: (x.y // 12, x.x)))
        how = "OCR of the visible area"
    text = text.replace("\r", "\n")
    return {"window": title, "how": how, "chars": len(text), "text": text[-a.max_chars:] if len(text) > a.max_chars else text,
            "note": "If this is a chat (e.g. ChatGPT), the newest answer is at the END."}


def register_copy(reg: Registry) -> None:
    reg.register(Tool("copy_window_text", "Get ALL text of a window/app/web page (ChatGPT answers, web apps, documents) - better than screenshots.", CopyTextArgs, t_copy_text, Risk.READ, 40, untrusted=True))
