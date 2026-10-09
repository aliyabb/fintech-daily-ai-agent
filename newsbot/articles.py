"""Fetch article pages to give the fact-checker more than the RSS summary."""
from __future__ import annotations

import concurrent.futures
import html
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

from .feeds import is_web_link

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


class _WebOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s), never to file://, ftp:// or data: addresses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_web_link(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_WebOnlyRedirect)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in ("p", "h1", "h2", "h3", "li", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def page_text(raw_html: str, limit: int) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:
        return ""
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if len(line) > 40)[:limit]


def fetch(url: str, limit: int = 6000, timeout: int = 15) -> dict:
    """Returns {"status": int|None, "text": str}. Blocked pages (401/403/429) are not treated as broken links."""
    if not is_web_link(url):
        return {"status": None, "text": ""}
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(1_500_000).decode(charset, "ignore")
            return {"status": response.status, "text": page_text(raw, limit)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "text": ""}
    except Exception:
        return {"status": None, "text": ""}


def fetch_many(urls: list[str], limit: int) -> dict[str, dict]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(urls, pool.map(lambda u: fetch(u, limit), urls)))
