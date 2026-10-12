"""Read the OPML feed list and collect fresh RSS/Atom items."""
from __future__ import annotations

import calendar
import concurrent.futures
import datetime as dt
import html
import re
import socket
import urllib.parse
import xml.etree.ElementTree as ET

import feedparser

USER_AGENT = "Mozilla/5.0 (compatible; FintechDailyBot/1.0; +https://t.me/fintech_daily)"
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|cmpid$)")


def is_web_link(url: str) -> bool:
    """Only http(s) links are fetched or published: a feed could supply file://, tg:// or javascript: links."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def parse_opml(path) -> list[dict]:
    feeds = []
    root = ET.parse(path).getroot()
    for folder in root.iter("outline"):
        if folder.get("type") == "rss":
            continue
        for node in folder.findall("outline"):
            if node.get("type") == "rss" and node.get("xmlUrl"):
                feeds.append({"folder": folder.get("text", ""), "name": node.get("text", ""),
                              "url": node.get("xmlUrl")})
    return feeds


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _without_tracking(parts) -> str:
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not TRACKING_PARAMS.match(k.lower())]
    return urllib.parse.urlencode(query)


def clean_link(url: str) -> str:
    """The link we publish: original spelling, minus tracking parameters and fragment."""
    parts = urllib.parse.urlsplit((url or "").strip())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, _without_tracking(parts), ""))


def canonical_url(url: str) -> str:
    """Key for deduplication: lowercase host, no trailing slash, no tracking."""
    parts = urllib.parse.urlsplit((url or "").strip())
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, _without_tracking(parts), ""))


def entry_time(entry) -> dt.datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return dt.datetime.fromtimestamp(calendar.timegm(parsed), tz=dt.timezone.utc)


def fetch_feed(feed: dict, since: dt.datetime) -> list[dict]:
    parsed = feedparser.parse(feed["url"], agent=USER_AGENT)
    items = []
    for entry in parsed.entries:
        published = entry_time(entry)
        link = clean_link(entry.get("link", ""))
        title = clean_text(entry.get("title", ""))
        if not (published and title and is_web_link(link)) or published < since:
            continue
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        items.append({
            "title": title,
            "summary": summary[:600],
            "link": link,
            "url_key": canonical_url(link),
            "published": published.isoformat(),
            "source": feed["name"],
            "folder": feed["folder"],
        })
    return items


def collect(feeds: list[dict], lookback_hours: int, now: dt.datetime, workers: int = 12) -> tuple[list[dict], list[str]]:
    """Fetch all feeds in parallel. Returns (items, names of feeds that failed)."""
    socket.setdefaulttimeout(25)
    since = now - dt.timedelta(hours=lookback_hours)
    items, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_feed, feed, since): feed for feed in feeds}
        for future in concurrent.futures.as_completed(futures):
            try:
                items.extend(future.result())
            except Exception:  # one broken feed must not stop the digest
                failed.append(futures[future]["name"])
    return items, sorted(failed)
