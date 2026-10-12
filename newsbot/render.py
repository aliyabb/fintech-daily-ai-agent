"""Telegram HTML rendering of the digest and the navigation post."""
from __future__ import annotations

import collections
import datetime as dt
import html
import re

from .feeds import is_web_link

TELEGRAM_LIMIT = 4000   # Telegram allows 4096 characters; emoji may count double, so keep a margin
RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_date(lang: str, day: dt.date) -> str:
    if lang == "ru":
        return f"{day.day} {RU_MONTHS[day.month - 1]}"
    if lang == "zh":
        return f"{day.month}月{day.day}日"
    return f"{EN_MONTHS[day.month - 1]} {day.day}"


def format_range(lang: str, first: dt.date, last: dt.date) -> str:
    """A week in the title: «21–27 сентября», "September 21–27", «9月21日–27日»."""
    if first.month == last.month:
        if lang == "ru":
            return f"{first.day}–{last.day} {RU_MONTHS[first.month - 1]}"
        if lang == "zh":
            return f"{first.month}月{first.day}日–{last.day}日"
        return f"{EN_MONTHS[first.month - 1]} {first.day}–{last.day}"
    separator = "–" if lang == "zh" else " – "
    return format_date(lang, first) + separator + format_date(lang, last)


def format_short(lang: str, day: dt.date) -> str:
    if lang == "ru":
        return f"{day.day:02d}.{day.month:02d}"
    if lang == "zh":
        return f"{day.month}月{day.day}日"
    return f"{EN_MONTHS[day.month - 1][:3]} {day.day}"


def visible_length(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def digest_hashtags(channel: dict, entries: list[dict], limit: int = 8) -> list[str]:
    counts = collections.Counter(tag for entry in entries for tag in entry.get("hashtags", []))
    order = {tag: i for i, tag in enumerate(channel["hashtags"])}
    ranked = sorted(counts, key=lambda t: (-counts[t], order.get(t, 999)))
    return [channel["rubric_tag"]] + ranked[:limit]


def render_digest(channel: dict, day: dt.date, entries: list[dict]) -> str:
    labels = channel["labels"]
    blocks = [f"<b>{esc(labels['title'])} · {esc(format_date(channel['lang'], day))}</b>"]
    for number, entry in enumerate(entries, 1):
        links = " · ".join(
            f"<a href=\"{html.escape(source['link'], quote=True)}\">{esc(source['source'])}</a>"
            if is_web_link(source["link"]) else esc(source["source"])
            for source in entry["sources"])
        blocks.append(
            f"<b>{number}. {esc(entry['headline'])}</b>\n"
            f"{esc(entry['summary'])}\n"
            f"→ <i>{esc(labels['why'])}:</i> {esc(entry['why'])}\n"
            f"🔗 {links}"
        )
    blocks.append(" ".join(digest_hashtags(channel, entries)))
    return "\n\n".join(blocks)


def fit_digest(channel: dict, day: dt.date, entries: list[dict], min_items: int) -> tuple[str, list[dict]]:
    """Drop the last entries until the message fits Telegram's limit."""
    entries = list(entries)
    while entries:
        text = render_digest(channel, day, entries)
        if visible_length(text) <= TELEGRAM_LIMIT or len(entries) <= min_items:
            return text, entries
        entries.pop()
    return "", []


WEEKLY_SECTIONS = ("top", "deals", "regulation", "trends")


def render_weekly(channel: dict, first: dt.date, last: dt.date, intro: str, items: list[dict]) -> str:
    """Week in review: sections of one-line items, each linking to the channel's own posts of the week."""
    labels = channel["weekly_labels"]
    blocks = [f"<b>{esc(labels['title'])} · {esc(format_range(channel['lang'], first, last))}</b>"]
    if intro:
        blocks.append(esc(intro))
    for section in WEEKLY_SECTIONS:
        lines = []
        for item in (i for i in items if i["section"] == section):
            posts = {s["post"]: s["date"] for s in item["stories"]}   # one link per post, even for two stories in it
            refs = ", ".join(f"<a href=\"{html.escape(post, quote=True)}\">"
                             f"{esc(format_short(channel['lang'], dt.date.fromisoformat(date)))}</a>"
                             for post, date in posts.items())
            lines.append(f"▪️ {esc(item['text'])} ({refs})")
        if lines:
            blocks.append("\n".join([f"<b>{esc(labels[section])}</b>", *lines]))
    blocks.append(f"{channel['weekly_tag']} {channel['rubric_tag']}")
    return "\n\n".join(blocks)


def fit_weekly(channel: dict, first: dt.date, last: dt.date, intro: str, items: list[dict]) -> tuple[str, list[dict]]:
    """Drop items from the end of the longest section until the post fits Telegram's limit."""
    items = list(items)
    text = render_weekly(channel, first, last, intro, items)
    while visible_length(text) > TELEGRAM_LIMIT and len(items) > 1:
        counts = collections.Counter(i["section"] for i in items)
        longest = max(counts, key=counts.get)
        items.pop(max(n for n, i in enumerate(items) if i["section"] == longest))
        text = render_weekly(channel, first, last, intro, items)
    return text, items


def plain_text(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or "")).strip()


ENTRY_START = re.compile(r"<b>(\d+)\. (.*?)</b>")


def split_digest(text: str) -> tuple[str, list[dict], str]:
    """A rendered digest back into (title line, entries, hashtag line); each entry has headline, links, html."""
    parts = (text or "").split("\n\n")
    if len(parts) < 3:
        return text or "", [], ""
    entries: list[dict] = []
    for part in parts[1:-1]:
        match = ENTRY_START.match(part)
        if match or not entries:
            entries.append({"headline": html.unescape(match.group(2)).strip() if match else "", "html": part})
        else:   # a summary with an empty line inside
            entries[-1]["html"] += "\n\n" + part
    for entry in entries:
        entry["links"] = [html.unescape(link) for link in re.findall(r'href="([^"]+)"', entry["html"])]
    return parts[0], entries, parts[-1]


def same_spacing(text) -> str:
    return " ".join(str(text).split())


def remove_entries(text: str, headlines: list[str], hashtag_line: str | None = None) -> tuple[str, list[str]]:
    """Drop the entries with these headlines from a rendered digest and renumber the rest.

    Returns (new text, headlines actually removed). Nothing is removed when nothing matched,
    or when every entry matched: a post is never emptied.
    """
    wanted = {same_spacing(h) for h in headlines}
    head, entries, tail = split_digest(text)
    kept = [e for e in entries if same_spacing(e["headline"]) not in wanted]
    removed = [e["headline"] for e in entries if same_spacing(e["headline"]) in wanted]
    if not removed or not kept:
        return text, []
    blocks = [ENTRY_START.sub(lambda m, n=number: f"<b>{n}. {m.group(2)}</b>", e["html"], count=1)
              for number, e in enumerate(kept, 1)]
    return "\n\n".join([head, *blocks, hashtag_line if hashtag_line is not None else tail]), removed


def utf16_len(text: str) -> int:
    """Telegram measures entity offsets in UTF-16 code units (an emoji often counts as two)."""
    return len(text.encode("utf-16-le")) // 2


def drop_lines(text: str, entities: list[dict], phrases: list[str]) -> tuple[str, list[dict], list[str]]:
    """Remove the lines containing any of the phrases from a formatted Telegram text, shifting the entities."""
    wanted = [p.lower() for p in phrases if p]
    kept, removed, spans, position = [], [], [], 0
    for line in text.splitlines(keepends=True):
        size = utf16_len(line)
        if any(p in line.lower() for p in wanted):
            removed.append(line.strip())
            spans.append((position, position + size))
        else:
            kept.append(line)
        position += size

    def shift(point: int) -> int:
        """Where a position of the old text lands in the new one; inside a removed line: at its start."""
        moved = 0
        for start, end in spans:
            if point >= end:
                moved += end - start
            elif point > start:
                return start - moved
            else:
                break
        return point - moved

    new_text = "".join(kept).rstrip("\n")
    total, shifted = utf16_len(new_text), []
    for entity in entities or []:
        start, end = shift(entity["offset"]), min(shift(entity["offset"] + entity["length"]), total)
        if end > start:
            shifted.append({**entity, "offset": start, "length": end - start})
    return new_text, shifted, removed


NAVIGATION_TITLES = {
    "ru": ("🧭 Навигация по каналу", "Нажмите на хэштег, чтобы увидеть все новости по теме.", "Темы", "Регионы", "Рубрика"),
    "en": ("🧭 Channel navigation", "Tap a hashtag to see every story on that topic.", "Topics", "Regions", "Format"),
    "zh": ("🧭 频道导航", "点击话题标签，即可查看该主题的所有新闻。", "主题", "地区", "栏目"),
}
REGION_HINTS = {"США", "Европа", "Великобритания", "Азия", "Китай", "ЛатАм", "Африка",
                "US", "EU", "UK", "Asia", "China", "LATAM", "Africa",
                "美国", "欧洲", "英国", "亚洲", "中国", "新加坡", "香港"}


def render_navigation(channel: dict) -> str:
    title, hint, topics_label, regions_label, rubric_label = NAVIGATION_TITLES[channel["lang"]]
    topics, regions = [], []
    for tag, description in channel["hashtags"].items():
        line = f"{tag} — {esc(description)}"
        (regions if tag.lstrip("#") in REGION_HINTS else topics).append(line)
    return "\n".join([f"<b>{esc(title)}</b>", esc(hint), "", f"<b>{esc(topics_label)}</b>", *topics, "",
                      f"<b>{esc(regions_label)}</b>", *regions, "",
                      f"<b>{esc(rubric_label)}</b>", " ".join(filter(None, [channel["rubric_tag"], channel.get("weekly_tag")]))])
