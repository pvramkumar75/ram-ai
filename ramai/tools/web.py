"""Web access: search the web and read pages as plain text (read-only GET requests; content is untrusted data)."""
from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser

import httpx
from pydantic import Field

from ..policy import PolicyError, Risk
from .base import Args, Registry, Tool

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PRIVATE_HOST = re.compile(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?|0\.0\.0\.0)", re.I)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_to_text(src: str) -> tuple[str, str]:
    p = _Text()
    p.feed(src)
    t = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return p.title.strip(), t


def _check_url(url: str):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise PolicyError("Only http(s) URLs")
    if PRIVATE_HOST.match(u.hostname):
        raise PolicyError("Local/private network addresses are blocked for web tools")


class SearchArgs(Args):
    query: str = Field(min_length=2, max_length=300)
    max_results: int = Field(8, ge=1, le=20)


class FetchArgs(Args):
    url: str = Field(max_length=2000)
    max_chars: int = Field(12000, ge=500, le=40000)


def _enabled(ctx):
    if not ctx.app.settings.get("web_access", True):
        raise PolicyError("Web access is switched off in Settings")


def _bing_url(href: str) -> str:
    """Bing wraps results as /ck/a?...&u=a1<base64url>; unwrap to the real address."""
    import base64

    if "bing.com/ck/a" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
        u = (q.get("u") or [""])[0]
        if u.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
            except Exception:
                pass
    return html.unescape(href)


def _strip(x: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", x or "")).strip()


async def _bing(c, q, n):
    r = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "en", "count": max(10, n)})
    out = []
    for blk in re.findall(r'<li class="b_algo"(.*?)</li>', r.text, re.S):
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        sn = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        out.append({"title": _strip(a.group(2)), "url": _bing_url(a.group(1)), "snippet": _strip(sn.group(1))[:300] if sn else ""})
        if len(out) >= n:
            break
    return out


async def _ddg(c, q, n):
    r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        href = m.group(1)
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        out.append({"title": _strip(m.group(2)), "url": href, "snippet": ""})
        if len(out) >= n:
            break
    return out


async def t_search(a: SearchArgs, ctx):
    _enabled(ctx)
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, follow_redirects=True) as c:
        res = []
        for engine in (_bing, _ddg):
            try:
                res = await engine(c, a.query, a.max_results)
            except httpx.HTTPError:
                res = []
            if res:
                break
    if not res:
        raise PolicyError("Web search returned no results (search engines may be blocking automated searches right now)")
    return {"query": a.query, "results": res, "note": "Use web_fetch on a result URL to read the page."}


async def t_fetch(a: FetchArgs, ctx):
    _enabled(ctx)
    _check_url(a.url)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as c:
        r = await c.get(a.url)
    _check_url(str(r.url))
    ct = r.headers.get("content-type", "")
    if r.status_code >= 400:
        raise PolicyError(f"HTTP {r.status_code}")
    if "pdf" in ct:
        import io

        from pypdf import PdfReader

        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages[:40])
        title = a.url
    elif "html" in ct or "<html" in r.text[:500].lower():
        title, text = html_to_text(r.text)
    else:
        title, text = a.url, r.text
    return {"url": str(r.url), "title": title[:200], "chars": len(text), "text": text[: a.max_chars]}


def register(reg: Registry) -> None:
    reg.register(Tool("web_search", "Search the web (DuckDuckGo). Returns titles, URLs and snippets. Use for prices, news, company info, how-tos.", SearchArgs, t_search, Risk.READ, 30, untrusted=True))
    reg.register(Tool("web_fetch", "Read a web page or online PDF as plain text.", FetchArgs, t_fetch, Risk.READ, 45, untrusted=True))
