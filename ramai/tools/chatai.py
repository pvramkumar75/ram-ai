"""ask_chatgpt: one reliable call instead of clicking around the ChatGPT app.

Opens ChatGPT (desktop app, else chatgpt.com in Edge), starts a new chat, finds the REAL message box by its placeholder
text (OCR), pastes the prompt, presses Enter, waits until the answer stops growing, and returns only the answer text."""
from __future__ import annotations

import re
import time

from pydantic import Field

from .. import uia
from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

PLACEHOLDERS = ("what should we work on", "ask anything", "message chatgpt", "ask chatgpt", "how can i help", "what's on your mind",
                "where should we begin", "what's on the agenda")
NOT_COMPOSER = ("ask for approval",)  # a ChatGPT button that looks like a prompt - never click it
UI_NOISE = re.compile(r"^(copy|copy message|edit message|feedback|latest response|work with chatgpt|add files and more|full access|good response|bad response|share|edit|read aloud|more actions|sources|retry|switch model|"
                      r"chatgpt can make mistakes.*|chatgpt said:?|you said:?|thinking|thought for .*|searching.*|"
                      r"what should we work on\??|ask anything|message chatgpt|voice mode|attach|search|tools|￼)$", re.I)


def _doc_text(h: int) -> str:
    root = uia.root(h)
    best = ""
    for d in uia.find_all(root, None, "document")[:3]:
        try:
            tp = d.GetCurrentPattern(uia.U.UIA_TextPatternId).QueryInterface(uia.U.IUIAutomationTextPattern)
            t = tp.DocumentRange.GetText(200000) or ""
        except Exception:
            t = ""
        if len(t) > len(best):
            best = t
    return best.replace("\r", "\n")


def _find_window() -> int | None:
    for h, t in uia.windows():
        tl = t.lower()
        if not uia.is_ramai_window(h) and (tl == "chatgpt" or tl.startswith("chatgpt") or "chatgpt -" in tl):
            return h
    return None


FOOTER = re.compile(r"^(feedback|read aloud|more actions|copy|copy message|good response|bad response|latest response|share|retry|"
                    r"\d{1,2}:\d{2}\s?(am|pm)?|sources)$", re.I)


def extract_answer(doc: str, prompt: str) -> str:
    """Answer = text after the last 'ChatGPT said:' that follows our prompt, up to the first action label (Feedback/Read aloud/...)."""
    d = doc.replace("\r", "\n").replace("\ufffc", "")
    p = d.rfind(prompt.strip()[:60])
    if p >= 0:
        m = d.find("ChatGPT said:", p)
        if m >= 0:
            out = []
            for ln in d[m + len("ChatGPT said:"):].split("\n"):
                s2 = ln.strip()
                if FOOTER.match(s2):
                    if out:
                        break
                    continue
                out.append(ln.rstrip())
            ans = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
            if ans:
                return ans
    return _extract_fallback(doc, prompt)


def _extract_fallback(doc: str, prompt: str) -> str:
    head = re.sub(r"\s+", " ", prompt.strip())[:60]
    flat = doc
    i = flat.rfind(prompt.strip()[:60])
    if i < 0:  # whitespace may differ: search on a normalised copy and map back roughly
        norm = re.sub(r"\s+", " ", flat)
        j = norm.rfind(head)
        if j < 0:
            return ""
        flat, i = norm, j
        tail = flat[i + len(head):]
        start = tail.find(prompt.strip()[-30:]) if len(prompt) > 60 else 0
        body = tail[start + 30:] if start >= 0 and len(prompt) > 60 else tail
    else:
        body = flat[i + len(prompt.strip()):]
    lines = [ln.strip() for ln in body.split("\n")]
    while lines and (not lines[-1] or UI_NOISE.match(lines[-1])):
        lines.pop()
    while lines and (not lines[0] or UI_NOISE.match(lines[0])):
        lines.pop(0)
    out = "\n".join(ln for ln in lines if not UI_NOISE.match(ln) or len(ln) > 40)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


class AskArgs(Args):
    prompt: str = Field(min_length=3, max_length=8000)
    new_chat: bool = True
    timeout_seconds: int = Field(180, ge=20, le=600)


def t_ask(a: AskArgs, ctx):
    import pyautogui

    from .. import ocr
    from .apps import launch
    from .computer import _stop_check
    from .whatsapp import paste_text

    h = _find_window()
    if not h:
        launch(ctx.app, "ChatGPT")
        h = uia.wait_for(_find_window, 25, 0.3)
    if not h:
        launch(ctx.app, "ChatGPT", prefer_web=True)
        h = uia.wait_for(_find_window, 30, 0.5)
    if not h:
        raise PolicyError("Could not open ChatGPT (desktop app or chatgpt.com)")
    uia.user32.ShowWindow(h, 3)
    uia.focus_window(h)
    time.sleep(0.6)

    def ocr_lines():
        l, t, r, b = uia.rect(uia.root(h))
        return ocr.read_region(max(l, 0), max(t, 0), r, b, scale=1.5)

    if a.new_chat:
        _stop_check(ctx)
        nc = [x for x in ocr_lines() if "new chat" in ocr.norm(x.text)]
        if nc:
            pyautogui.click(nc[0].cx, nc[0].cy)
        else:
            pyautogui.hotkey("ctrl", "shift", "o")
        time.sleep(1.2)
    lines = ocr_lines()
    txt = " | ".join(ocr.norm(x.text) for x in lines)
    if any(k in txt for k in ("log in", "sign up", "verify you are human")) and not any(p in txt for p in PLACEHOLDERS):
        raise PolicyError("ChatGPT is asking you to log in / verify. Please do that yourself, then retry.")
    box = [x for x in lines if any(p in ocr.norm(x.text) for p in PLACEHOLDERS) and not any(n in ocr.norm(x.text) for n in NOT_COMPOSER)]
    _stop_check(ctx)
    if box:
        pyautogui.click(box[-1].cx, box[-1].cy)
    else:  # fallback: bottom-centre of the window is where the composer sits in an open chat
        l, t, r, b = uia.rect(uia.root(h))
        pyautogui.click((l + r) // 2, b - 110)
    time.sleep(0.3)
    before = _doc_text(h)
    uia.focus_window(h)
    paste_text(a.prompt)
    time.sleep(0.3)
    _stop_check(ctx)
    pyautogui.press("enter")
    # wait for the answer to appear and stop growing
    t0, last, stable, answer = time.time(), None, 0, ""
    while time.time() - t0 < a.timeout_seconds:
        _stop_check(ctx)
        time.sleep(2.0)
        doc = _doc_text(h)
        ans = extract_answer(doc, a.prompt)
        if ans and ans == last:
            stable += 1
            if stable >= 3:  # unchanged for ~6 s: finished
                answer = ans
                break
        else:
            stable = 0
        last = ans
    else:
        answer = last or ""
    if not answer:
        raise PolicyError("ChatGPT did not produce a readable answer in time (the prompt may not have been sent). "
                          "Check the ChatGPT window; use copy_window_text to read it.")
    return {"answer": answer, "chars": len(answer), "complete": stable >= 3,
            "note": "This is ChatGPT's answer only (UI text removed). Format it as needed before sending."}


def register(reg: Registry) -> None:
    reg.register(Tool("ask_chatgpt", "Ask ChatGPT (desktop app or chatgpt.com) a prompt and get back ONLY its answer, in one call. Use for articles, drafts, explanations.",
                      AskArgs, t_ask, Risk.LOW, 660, control=True, untrusted=True))
