"""Group duplicate stories and pick the candidates sent to the model."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse

STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "over", "after", "amid", "its", "has", "have", "will",
    "new", "says", "said", "report", "reports", "news", "update", "how", "why", "what", "this", "that",
}


def tokens(title: str) -> set[str]:
    words = re.findall(r"[\w$€£%.]+", title.lower())
    return {w.strip(".") for w in words if len(w.strip(".")) >= 3 and w not in STOPWORDS}


def similar(a: set[str], b: set[str], threshold: float = 0.5) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


def slug_title(url: str) -> str:
    """Words of a link's slug ("/news/bitget-hit-by-hack" → "bitget hit by hack"), or "" for numeric paths."""
    segments = [s for s in urllib.parse.urlsplit(url or "").path.split("/") if s]
    for segment in reversed(segments):
        words = re.sub(r"\.html?$", "", segment).replace("_", "-").split("-")
        if sum(bool(re.search(r"[a-z]", w, re.I)) for w in words) >= 4:
            return " ".join(words)
    return ""


def repeats_recent(members: list[dict], recent: list[set[str]], threshold: float) -> bool:
    return any(similar(tokens(m["title"]), words, threshold) for m in members for words in recent)


def cluster(items: list[dict]) -> list[list[dict]]:
    """Greedy clustering by URL and title similarity; each cluster is one story."""
    clusters: list[tuple[set[str], list[dict]]] = []
    seen_urls: dict[str, int] = {}
    for item in sorted(items, key=lambda i: i["published"], reverse=True):
        if item["url_key"] in seen_urls:
            clusters[seen_urls[item["url_key"]]][1].append(item)
            continue
        words = tokens(item["title"])
        for index, (cluster_words, members) in enumerate(clusters):
            if similar(words, cluster_words):
                members.append(item)
                seen_urls[item["url_key"]] = index
                break
        else:
            clusters.append((words, [item]))
            seen_urls[item["url_key"]] = len(clusters) - 1
    return [members for _, members in clusters]


def is_excluded(item: dict, cfg: dict) -> bool:
    link = item["link"].lower()
    title = item["title"].lower()
    return (any(pattern.lower() in link for pattern in cfg.get("exclude_url_patterns", []))
            or any(word.lower() in title for word in cfg.get("exclude_title_words", [])))


def select_candidates(items: list[dict], cfg: dict, posted_keys: set[str], now: dt.datetime,
                      channel: dict | None = None, recent_titles: list[str] | None = None) -> list[dict]:
    """recent_titles: titles of stories this channel published lately; a story told under the same title
    by another outlet is left out even though its link is new."""
    weights = dict(cfg.get("source_weights", {}))
    weights.update((channel or {}).get("source_weights", {}))  # e.g. more weight for Asia in the Chinese channel
    max_per_source = cfg.get("max_per_source", 6)
    fresh = [i for i in items if i["url_key"] not in posted_keys and not is_excluded(i, cfg)]
    recent = [words for words in map(tokens, recent_titles or []) if len(words) >= 3]
    threshold = cfg.get("repeat_title_similarity", 0.6)
    scored = []
    for members in cluster(fresh):
        if repeats_recent(members, recent, threshold):
            continue
        lead = max(members, key=lambda i: (weights.get(i["folder"], 0.5), i["published"]))
        age_hours = (now - dt.datetime.fromisoformat(lead["published"])).total_seconds() / 3600
        sources = sorted({m["source"] for m in members})
        score = weights.get(lead["folder"], 0.5) + 0.15 * (len(sources) - 1) - 0.01 * age_hours
        scored.append((score, lead, members, sources))
    scored.sort(key=lambda s: s[0], reverse=True)

    # Interleave sources: each further story from the same source ranks a bit lower.
    rank_in_source: dict[str, int] = {}
    diversified = []
    for score, lead, members, sources in scored:
        rank = rank_in_source.get(lead["source"], 0)
        rank_in_source[lead["source"]] = rank + 1
        diversified.append((score - 0.12 * rank, lead, members, sources))
    diversified.sort(key=lambda s: s[0], reverse=True)

    per_source: dict[str, int] = {}
    candidates = []
    for score, lead, members, sources in diversified:
        if per_source.get(lead["source"], 0) >= max_per_source:
            continue
        per_source[lead["source"]] = per_source.get(lead["source"], 0) + 1
        candidates.append({
            "id": f"c{len(candidates) + 1}",
            "title": lead["title"],
            "summary": lead["summary"],
            "source": lead["source"],
            "published": lead["published"],
            "link": lead["link"],
            "url_key": lead["url_key"],
            "also_reported_by": [s for s in sources if s != lead["source"]],
            "member_keys": sorted({m["url_key"] for m in members}),
        })
        if len(candidates) >= cfg.get("max_candidates", 80):
            break
    return candidates
