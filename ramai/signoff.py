"""Keep the AI's own sign-offs out of mail and messages.

The user's emails already carry their own Outlook signature, and RAM AI must never sign as itself
("Regards, RAM AI (on behalf of ...)"). These helpers trim such endings from the text before it is typed."""
from __future__ import annotations

import re

_CLOSING = r"(?:best regards|kind regards|warm regards|with regards|regards|thanks\s*(?:and|&)\s*regards|thanks|thank you|sincerely|yours sincerely|cheers)"
_AI_LINE = re.compile(r"(?:ram\s*ai|on behalf of|sent by (?:an? )?(?:ai|assistant)|ai assistant)", re.I)
_CLOSING_LINE = re.compile(rf"^\s*{_CLOSING}\s*[,.!-]*\s*$", re.I)


def strip_ai_signoff(text: str) -> str:
    """Remove a trailing sign-off that names RAM AI / 'on behalf of' (and the closing word above it)."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    cut = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):   # only the last few lines
        if _AI_LINE.search(lines[i]):
            cut = i
    if cut is None:
        return text
    j = cut
    while j > 0 and (not lines[j - 1].strip() or _CLOSING_LINE.match(lines[j - 1])):
        j -= 1
        if _CLOSING_LINE.match(lines[j]):
            while j > 0 and not lines[j - 1].strip():
                j -= 1
            break
    return "\n".join(lines[:j]).rstrip()


def strip_closing_block(text: str) -> str:
    """Remove a trailing 'Regards,\\n<name>' block (used where Outlook adds the user's own signature anyway)."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):
        if _CLOSING_LINE.match(lines[i]):
            tail = [x for x in lines[i + 1:] if x.strip()]
            if len(tail) <= 3 and all(len(x) <= 60 for x in tail):
                return "\n".join(lines[:i]).rstrip()
            break
    return text
