"""Turn the agent's markdown answers into natural terminal text (no **, #, | or ``` left on screen).

Bold becomes real bold, bullets become • with wrapped lines aligned under the text, tables become aligned
columns, links show as "text (url)". With colour off (NO_COLOR, or output piped to a file) only the markers go."""
from __future__ import annotations

import re
import shutil
import textwrap

B0, B1, K0, K1 = "\x01", "\x02", "\x03", "\x04"   # placeholders for bold on/off and code on/off while wrapping

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITAL = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")
_BULLET = re.compile(r"^(\s*)([-*+•])\s+(.*)$")
_NUM = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TROW = re.compile(r"^\s*\|.*\|\s*$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(s: str) -> str:
    s = _CODE.sub(lambda m: K0 + m.group(1) + K1, s)
    s = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", s)
    s = _BOLD.sub(lambda m: B0 + m.group(2) + B1, s)
    s = _ITAL.sub(lambda m: m.group(1), s)
    return s


def _plain_len(s: str) -> int:
    return len(re.sub(r"[\x01-\x04]", "", s))


def _wrap(text: str, width: int, first: str, rest: str) -> list[str]:
    w = textwrap.TextWrapper(width=width, initial_indent=first, subsequent_indent=rest,
                             break_on_hyphens=False, break_long_words=False)
    # placeholders count as width in textwrap; over-estimating slightly is harmless (lines are just a bit shorter)
    return w.wrap(text) or [first.rstrip()]


def _table(rows: list[str], width: int) -> list[str]:
    cells = [[_inline(c.strip()) for c in r.strip().strip("|").split("|")] for r in rows if not _TSEP.match(r)]
    if not cells:
        return []
    n = max(len(r) for r in cells)
    cells = [r + [""] * (n - len(r)) for r in cells]
    widths = [max(_plain_len(r[i]) for r in cells) for i in range(n)]
    out = []
    for k, r in enumerate(cells):
        line = "  " + "   ".join(c + " " * (widths[i] - _plain_len(c)) for i, c in enumerate(r)).rstrip()
        out.append(B0 + line + B1 if k == 0 and len(cells) > 1 else line)
        if k == 0 and len(cells) > 1:
            out.append("  " + "   ".join("─" * w for w in widths))
    return out


def render(md: str, width: int | None = None, color: bool = True) -> str:
    width = width or max(40, min(shutil.get_terminal_size((100, 20)).columns - 1, 110))
    out: list[str] = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):                       # code block: indent, no fences
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                out.append(K0 + "    " + lines[i] + K1)
                i += 1
            i += 1
            continue
        if _TROW.match(ln):                                     # table
            rows = []
            while i < len(lines) and (_TROW.match(lines[i]) or _TSEP.match(lines[i])):
                rows.append(lines[i])
                i += 1
            out += _table(rows, width)
            continue
        i += 1
        if not ln.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        if _RULE.match(ln):
            out.append("─" * min(width, 40))
            continue
        m = _HEAD.match(ln)
        if m:
            if out and out[-1] != "":
                out.append("")
            title = _inline(m.group(2)).replace(B0, "").replace(B1, "")
            out += [B0 + t + B1 for t in _wrap(title.upper() if len(m.group(1)) == 1 else title, width, "", "")]
            continue
        m = _BULLET.match(ln)
        if m:
            depth = len(m.group(1).replace("\t", "  ")) // 2
            pad = "  " + "   " * depth
            out += _wrap(_inline(m.group(3)), width, pad + ("•" if depth == 0 else "◦") + " ", pad + "  ")
            continue
        m = _NUM.match(ln)
        if m:
            depth = len(m.group(1).replace("\t", "  ")) // 3
            pad = "  " + "   " * depth
            num = m.group(2).rstrip(")").rstrip(".") + "."
            out += _wrap(_inline(m.group(3)), width, f"{pad}{num} ", pad + " " * (len(num) + 1))
            continue
        if ln.lstrip().startswith(">"):
            out += _wrap(_inline(ln.lstrip()[1:].strip()), width, "  │ ", "  │ ")
            continue
        out += _wrap(_inline(ln.strip()), width, "", "")
    while out and out[-1] == "":
        out.pop()
    text = "\n".join(out)
    on = {B0: "\033[1m", B1: "\033[22m", K0: "\033[36m", K1: "\033[39m"} if color else dict.fromkeys((B0, B1, K0, K1), "")
    for k, v in on.items():
        text = text.replace(k, v)
    return text
