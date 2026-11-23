"""Compose and send email in Outlook's own message window - no security prompt even when Outlook distrusts automation.

COM is used only for unguarded actions (create the item, set the subject, attach files by exact path, open the window).
Recipients and body are typed into the window (body at the very TOP, above the signature), read back for approval,
and the real Send button is pressed (found by name; OCR as a fallback). The window closing proves Outlook accepted it."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .. import uia as wu
from ..policy import PolicyError
from .outlook_com import com


def _local_uia():
    import comtypes
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U

    try:  # worker threads (scheduled/agent runs) have no COM apartment yet
        comtypes.CoInitialize()
    except OSError:
        pass
    return U, comtypes.CoCreateInstance(U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER)


class Win:
    """UIA helper bound to one message window (thread-local UIA client)."""

    def __init__(self, hwnd: int):
        self.U, self.ua = _local_uia()
        self.h = hwnd
        self.root = self.ua.ElementFromHandle(hwnd)

    def first(self, name=None, ctype=None):
        U, ua = self.U, self.ua
        conds = []
        if name is not None:
            conds.append(ua.CreatePropertyCondition(U.UIA_NamePropertyId, name))
        if ctype is not None:
            conds.append(ua.CreatePropertyCondition(U.UIA_ControlTypePropertyId, ctype))
        c = conds[0] if len(conds) == 1 else ua.CreateAndCondition(conds[0], conds[1])
        try:
            return self.root.FindFirst(self.U.TreeScope_Descendants, c)
        except Exception:
            return None

    def edit(self, name):
        return self.first(name, self.U.UIA_EditControlTypeId)

    def value(self, el) -> str:
        if el is None:
            return ""
        try:
            return el.GetCurrentPattern(self.U.UIA_ValuePatternId).QueryInterface(self.U.IUIAutomationValuePattern).CurrentValue or ""
        except Exception:
            try:
                return el.CurrentName or ""
            except Exception:
                return ""

    def body_el(self):
        return self.edit("Page 1 content") or self.first(None, self.U.UIA_DocumentControlTypeId)

    def body_text(self, n=4000) -> str:
        el = self.body_el()
        if el is None:
            return ""
        try:
            return el.GetCurrentPattern(self.U.UIA_TextPatternId).QueryInterface(self.U.IUIAutomationTextPattern).DocumentRange.GetText(n) or ""
        except Exception:
            return self.value(el)

    def focus_el(self, el):
        """Put the keyboard caret into a control: UIA focus, else click its centre."""
        wu.focus_window(self.h)
        wu.wait_for(lambda: wu.user32.GetForegroundWindow() == self.h, 1.5, 0.05)
        try:
            el.SetFocus()
            return
        except Exception:
            pass
        import pyautogui

        r = el.CurrentBoundingRectangle
        pyautogui.click((r.left + r.right) // 2, r.top + min(25, (r.bottom - r.top) // 2))


def _guard(h):
    if wu.user32.GetForegroundWindow() != h:
        wu.focus_window(h)
        if not wu.wait_for(lambda: wu.user32.GetForegroundWindow() == h, 1.5, 0.05):
            raise PolicyError("The Outlook message window could not be brought to the front. Nothing typed further.")


def _type(h, text, ctx):
    from .computer import type_unicode

    _guard(h)
    type_unicode(text, ctx, 0.002)


def compose(to: list[str], cc: list[str], subject: str, body: str, attachments: list[Path], ctx,
            reply_to: str | None = None, reply_all: bool = False, forward: str | None = None) -> dict:
    import pyautogui

    with com() as ol:
        ns = ol.GetNamespace("MAPI")
        if reply_to:
            src = ns.GetItemFromID(reply_to)
            mail = src.ReplyAll() if reply_all else src.Reply()
        elif forward:
            mail = ns.GetItemFromID(forward).Forward()
        else:
            mail = ol.CreateItem(0)
            mail.Subject = subject or "(no subject)"
        if subject and (reply_to or forward):
            pass  # keep Outlook's "RE:/FW:" subject
        for p in attachments:
            mail.Attachments.Add(str(p))  # exact file by path - never a "recent files" pick
        att_names = []
        try:
            att_names = [mail.Attachments.Item(i).FileName for i in range(1, mail.Attachments.Count + 1)]
        except Exception:
            att_names = [p.name for p in attachments]
        mail.Display(False)
        cap = ol.ActiveInspector().Caption
    h = wu.wait_for(lambda: next((x for x, t in wu.windows() if t == cap), None), 8, 0.08)
    if not h:
        raise PolicyError("Outlook's message window did not open")
    w = Win(h)
    wu.wait_for(lambda: w.edit("To"), 5, 0.1)
    # recipients
    if to:
        el = w.edit("To")
        w.focus_el(el)
        if not (reply_to or forward):
            pyautogui.hotkey("ctrl", "a")
        else:
            pyautogui.press("end")
            if w.value(el).strip():
                _type(h, "; ", ctx)
        _type(h, "; ".join(to), ctx)
    if cc:
        el = w.edit("Cc")
        if el is not None:
            w.focus_el(el)
            pyautogui.press("end")
            _type(h, ("; " if w.value(el).strip() else "") + "; ".join(cc), ctx)
    if to or cc:
        _guard(h)
        pyautogui.hotkey("ctrl", "k")  # Check Names: resolves typed names to addresses
        time.sleep(0.6)
    # body at the very top, above the signature / quoted reply
    b = w.body_el()
    if b is None:
        raise PolicyError("Could not find the message body in Outlook's window")
    w.focus_el(b)
    _guard(h)
    pyautogui.hotkey("ctrl", "home")
    _type(h, body.rstrip() + "\n\n", ctx)
    time.sleep(0.3)
    return snapshot(h, att_names)


def snapshot(h: int, att_names: list[str] | None = None) -> dict:
    w = Win(h)
    to, cc, subj = w.value(w.edit("To")), w.value(w.edit("Cc")), w.value(w.edit("Subject"))
    body = w.body_text(6000).replace("\r", "\n")
    atts = att_names if att_names is not None else []
    digest = hashlib.sha256(json.dumps([to, cc, subj, body[:3000]]).encode()).hexdigest()
    return {"hwnd": h, "to": to, "cc": cc, "subject": subj, "body": body, "attachments": atts, "digest": digest}


def press_send(h: int) -> dict:
    """Click the real Send button of this message window and confirm the window closed."""
    if not wu.user32.IsWindow(h):
        raise PolicyError("The message window was closed before sending; nothing sent.")
    w = Win(h)
    _guard(h)
    btn = w.first("Send", w.U.UIA_ButtonControlTypeId)
    how = None
    if btn is not None:
        try:
            btn.GetCurrentPattern(w.U.UIA_InvokePatternId).QueryInterface(w.U.IUIAutomationInvokePattern).Invoke()
            how = "Send button (UI Automation)"
        except Exception:
            r = btn.CurrentBoundingRectangle
            import pyautogui

            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            how = "Send button (clicked)"
    else:  # OCR fallback: find the word 'Send' in the upper-left area of the window (next to the To field)
        from .. import ocr

        l, t, r, b = wu.rect(w.root)
        lines = ocr.read_region(max(l, 0), max(t, 0), l + 420, t + 420, scale=2.0)
        hit = [x for x in lines if ocr.norm(x.text) == "send"]
        if not hit:
            raise PolicyError("Could not locate Outlook's Send button (by name or by OCR). Nothing sent; the draft is still open.")
        import pyautogui

        pyautogui.click(hit[0].cx, hit[0].cy)
        how = "Send button (found by OCR)"
    closed = wu.wait_for(lambda: not wu.user32.IsWindow(h) or not wu.user32.IsWindowVisible(h), 8, 0.15)
    if closed:
        return {"sent": True, "how": how, "evidence": "Outlook closed the message window after Send (moved to Outbox/Sent Items)"}
    # still open: probably Check Names / missing subject dialog
    title = wu.window_title(wu.user32.GetForegroundWindow())
    return {"sent": "uncertain", "how": how, "warning": f"The message window is still open (front window: '{title}'). Outlook may need you to "
            "pick a recipient or confirm something. Check the window; do NOT resend automatically."}
