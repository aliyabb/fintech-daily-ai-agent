"""Deterministic checks that run alongside the model's own fact-check."""
from __future__ import annotations

import re
import unicodedata

BROKEN_STATUSES = {404, 410}
MAX_SOURCES = 3


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def missing_quotes(quotes: list[str], *sources: str) -> list[str]:
    """Quotes the writer claimed to copy from the sources but that are not there (hallucination guard)."""
    haystack = normalize(" ".join(sources))
    return [q for q in quotes if q.strip() and normalize(q) not in haystack]


def unsupported_numbers(text: str, *sources: str) -> list[str]:
    """Numbers in a text that none of the sources contains (the week in review adds no figures of its own)."""
    haystack = normalize(" ".join(sources))
    return [n for n in re.findall(r"\d+(?:[.,]\d+)*", normalize(text)) if n not in haystack]


def canonical_tag(tag: str, allowed: dict[str, str]) -> str | None:
    wanted = "#" + tag.strip().lstrip("#").lower()
    for option in allowed:
        if option.lower() == wanted:
            return option
    return None


def filter_hashtags(tags: list[str], allowed: dict[str, str], limit: int = 3) -> list[str]:
    result = []
    for tag in tags:
        option = canonical_tag(tag, allowed)
        if option and option not in result:
            result.append(option)
    return result[:limit]


def check_entry(entry: dict, story: dict, candidates: dict[str, dict], pages: dict[str, dict],
                allowed_tags: dict[str, str]) -> tuple[dict | None, str | None, bool]:
    """Check one written entry against its story's sources.

    Returns (checked entry or None, problem in Russian or None, whether a rewrite could fix it).
    A passing entry gets `sources`: the candidate dicts it cites, main source first.
    """
    ids = []
    for candidate_id in map(str, entry.get("source_ids") or []):
        if candidate_id in story["candidate_ids"] and candidate_id not in ids:
            ids.append(candidate_id)
    ids = ids[:MAX_SOURCES]
    if not ids:
        # a rewrite with this story's sources can fix a wrong reference
        return None, f"ссылается на источники не из этой новости: {str(entry.get('source_ids'))[:80]}", True

    sources = [candidates[i] for i in ids
               if pages.get(candidates[i]["link"], {}).get("status") not in BROKEN_STATUSES]
    if not sources:
        return None, "ссылки на источники не открываются", False

    texts = []
    for source in sources:
        texts += [source["title"], source["summary"], pages.get(source["link"], {}).get("text", "")]
    missing = missing_quotes(entry.get("source_quotes") or [], *texts)
    if missing:
        return None, f"в источниках нет фрагментов {missing}", True

    for field, limit in (("headline", 120), ("summary", 400), ("why", 220)):
        entry[field] = (entry.get(field) or "").strip()[:limit]
    if not entry["headline"] or not entry["summary"]:
        return None, "пустой заголовок или текст", True

    entry["hashtags"] = filter_hashtags(entry.get("hashtags") or [], allowed_tags)
    entry["source_ids"] = [s["id"] for s in sources]
    entry["sources"] = sources
    return entry, None, False
