"""Prompt evaluation.

offline: deterministic quality checks of the posts the channel published (format, limits, language, style,
         sources, hashtags, repeats inside a post). No model requests: safe to run any time, also in CI.
live:    the current writer prompt rewrites the stories of the last published days, then a judge model scores
         the published text and the new one side by side (blind, positions swapped every other entry).
         It needs its own API key (another Google project), so the daily digests never share its quota.
"""
from __future__ import annotations

import collections
import datetime as dt
import html
import json
import re

from . import articles, dedupe, pipeline, render, verify
from .feeds import is_web_link
from .llm import EDITOR_SCHEMA, JUDGE_CRITERIA, JUDGE_SCHEMA

LIMITS = {"headline": 90, "summary": 280, "why": 160}
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
LINK = re.compile(r'<a href="([^"]+)">(.*?)</a>')
CHECKS = {
    "limits": "длина заголовка, текста и «почему важно» в пределах",
    "language": "текст на языке канала",
    "style": "без восклицательных знаков и эмодзи",
    "sources": "1–3 источника, только http(s)",
    "hashtags": "хэштеги только из словаря канала",
    "unique": "в посте нет двух новостей об одном событии",
}


def parse_entry(entry: dict) -> dict:
    """headline, summary, why and sources of a published entry (render.render_digest in reverse)."""
    lines = entry["html"].split("\n")
    why = next((line for line in lines if line.startswith("→")), "")
    links = next((line for line in lines if line.startswith("🔗")), "")
    body = [line for line in lines[1:] if not line.startswith(("→", "🔗"))]
    return {"headline": entry["headline"],
            "summary": render.plain_text("\n".join(body)),
            "why": render.plain_text(re.sub(r"^→\s*<i>.*?</i>\s*", "", why)),
            "sources": [{"link": html.unescape(link), "source": render.plain_text(name)} for link, name in LINK.findall(links)]}


def script_share(text: str, lang: str) -> float:
    """Share of the text's letters written in the channel's script."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 1.0
    if lang == "ru":
        hits = sum("Ѐ" <= ch <= "ӿ" for ch in letters)
    elif lang == "zh":
        hits = sum("一" <= ch <= "鿿" for ch in letters)
    else:
        hits = sum(ch.isascii() for ch in letters)
    return hits / len(letters)


LANGUAGE_MIN = {"ru": 0.5, "zh": 0.3, "en": 0.9}   # names of companies keep their Latin spelling


def check_entry(entry: dict, channel: dict) -> dict[str, str | None]:
    """Each offline check → None when it passes, or what is wrong."""
    result: dict[str, str | None] = {}
    over = [f"{field} {len(entry[field])}>{limit}" for field, limit in LIMITS.items() if len(entry[field]) > limit]
    result["limits"] = ", ".join(over) or None
    share = script_share(entry["headline"] + " " + entry["summary"], channel["lang"])
    result["language"] = None if share >= LANGUAGE_MIN.get(channel["lang"], 0.5) else f"доля письма канала {share:.0%}"
    text = " ".join(entry[k] for k in ("headline", "summary", "why"))
    result["style"] = "восклицательный знак или эмодзи" if "!" in text or "！" in text or EMOJI.search(text) else None
    links = [s["link"] for s in entry["sources"]]
    bad = not 1 <= len(links) <= verify.MAX_SOURCES or not all(is_web_link(link) for link in links)
    result["sources"] = f"источников {len(links)}" if bad else None
    return result


def offline(cfg: dict, state, days: int = 14, today: dt.date | None = None) -> dict:
    """Run the offline checks over every published post of the last `days` days."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=days - 1)
    passed, total, failures = collections.Counter(), collections.Counter(), []
    posts = 0
    for channel in cfg["channels"].values():
        allowed = {channel["rubric_tag"], *channel["hashtags"]}
        for draft in state.drafts(channel["key"]):
            if draft.get("status") != "published" or not since.isoformat() <= draft.get("date", "") <= today.isoformat():
                continue
            posts += 1
            _, entries, tail = render.split_digest(draft.get("html", ""))
            parsed = [parse_entry(e) for e in entries]
            for entry in parsed:
                for name, problem in check_entry(entry, channel).items():
                    total[name] += 1
                    passed[name] += problem is None
                    if problem:
                        failures.append(f"{draft['id']} · {name}: «{entry['headline'][:60]}» — {problem}")
            tags = tail.split()
            total["hashtags"] += 1
            unknown = [tag for tag in tags if tag not in allowed]
            passed["hashtags"] += not unknown
            if unknown:
                failures.append(f"{draft['id']} · hashtags: {' '.join(unknown)}")
            total["unique"] += 1
            words = [dedupe.tokens(e["headline"]) for e in parsed]
            twins = [(a, b) for a in range(len(words)) for b in range(a + 1, len(words)) if dedupe.similar(words[a], words[b])]
            passed["unique"] += not twins
            if twins:
                failures.append(f"{draft['id']} · unique: новости {twins[0][0] + 1} и {twins[0][1] + 1} похожи")
    return {"period": {"from": since.isoformat(), "to": today.isoformat()}, "posts": posts,
            "checks": {name: {"passed": passed[name], "total": total[name]} for name in CHECKS},
            "failures": failures}


def offline_markdown(result: dict) -> list[str]:
    lines = [f"### Офлайн-оценка опубликованных постов · {result['period']['from']} — {result['period']['to']}", "",
             f"Постов: {result['posts']}. Запросов к модели: 0.", "", "| Проверка | Прошли |", "|---|---|"]
    for name, title in CHECKS.items():
        numbers = result["checks"][name]
        share = f"{numbers['passed'] / numbers['total']:.0%}" if numbers["total"] else "—"
        lines.append(f"| {title} | {numbers['passed']} из {numbers['total']} ({share}) |")
    if result["failures"]:
        lines += ["", "<details><summary>Что не прошло</summary>", "", *[f"- {f}" for f in result["failures"][:40]],
                  "", "</details>"]
    return lines


def golden_cases(channel: dict, state, days: int) -> list[dict]:
    """The last `days` published digests of the channel: the stories as they went out, with their sources."""
    cases = []
    for draft in reversed(state.drafts(channel["key"])):
        if draft.get("status") != "published" or draft.get("kind") == "weekly":
            continue
        published = [parse_entry(e) for e in render.split_digest(draft.get("html", ""))[1]]
        saved_entries, saved_stories = draft.get("entries") or [], draft.get("stories") or []
        stories = []
        for n, entry in enumerate(published):
            stored = saved_entries[n] if n < len(saved_entries) else {}
            titles = saved_stories[n].get("titles", []) if n < len(saved_stories) else []
            sources = stored.get("sources") or [
                {"link": s["link"], "source": s["source"], "title": titles[m] if m < len(titles) else "", "summary": ""}
                for m, s in enumerate(entry["sources"])]
            stories.append({"published": entry, "sources": [{**s, "id": f"d{len(cases)}s{n}x{m}"}
                                                            for m, s in enumerate(sources)]})
        if stories:
            cases.append({"draft": draft["id"], "date": draft["date"], "stories": stories})
        if len(cases) >= days:
            break
    return cases


def live(cfg: dict, channel: dict, llm, state, days: int = 3, log=print) -> dict:
    """Rewrite the last published days with the current writer prompt and judge both versions blind."""
    cases = golden_cases(channel, state, days)
    if not cases:
        return {"channel": channel["key"], "cases": 0, "error": "нет опубликованных выпусков"}
    links = sorted({s["link"] for case in cases for story in case["stories"] for s in story["sources"]})
    pages = articles.fetch_many(links, cfg["article_chars"])
    judged, code = [], collections.Counter()
    for case in cases:
        writing = {"stories": [{"index": n, "sources": [pipeline.source_view(s, pages, cfg["article_chars"] if m == 0 else
                                                                            cfg.get("secondary_article_chars", 2500))
                                                        for m, s in enumerate(story["sources"])]}
                               for n, story in enumerate(case["stories"])]}
        written = llm.json_completion(pipeline.prompt("editor", cfg, channel),
                                      "Stories:\n" + json.dumps(writing, ensure_ascii=False), EDITOR_SCHEMA, "digest")
        by_index = {pipeline.as_index(e.get("index")): e for e in written.get("items") or [] if isinstance(e, dict)}
        for n, story in enumerate(case["stories"]):
            entry = by_index.get(n)
            code["stories"] += 1
            if not entry:
                code["not_written"] += 1
                continue
            candidates = {s["id"]: s for s in story["sources"]}
            checked, problem, _ = verify.check_entry(dict(entry), {"candidate_ids": list(candidates)}, candidates, pages,
                                                     channel["hashtags"])
            code["passed" if checked else "failed"] += 1
            judged.append({"published": story["published"], "candidate": entry, "sources": story["sources"],
                           "code_problem": problem})
    entries = []
    for number, item in enumerate(judged):
        new = {k: item["candidate"].get(k, "") for k in ("headline", "summary", "why")}
        old = {k: item["published"][k] for k in ("headline", "summary", "why")}
        a, b = (old, new) if number % 2 == 0 else (new, old)   # the judge never knows which one is new
        entries.append({"index": number, "A": a, "B": b,
                        "sources": [pipeline.source_view(s, pages, cfg["article_chars"]) for s in item["sources"]]})
    scores = llm.json_completion(pipeline.prompt("judge", cfg, channel),
                                 "Entries to score:\n" + json.dumps({"entries": entries}, ensure_ascii=False),
                                 JUDGE_SCHEMA, "judge").get("scores", []) if entries else []
    totals = {"published": collections.defaultdict(list), "current": collections.defaultdict(list)}
    issues = []
    for score in scores:
        index = pipeline.as_index(score.get("index")) if isinstance(score, dict) else None
        if index is None or not 0 <= index < len(entries) or score.get("version") not in ("A", "B"):
            continue
        new_is_a = index % 2 == 1
        which = "current" if (score["version"] == "A") == new_is_a else "published"
        for name in JUDGE_CRITERIA:
            value = pipeline.as_index(score.get(name))
            if value is not None and 1 <= value <= 5:
                totals[which][name].append(value)
        issues += [f"{which} · «{entries[index][score['version']]['headline'][:50]}»: {issue}"
                   for issue in score.get("issues") or []]
    average = {which: {name: round(sum(v) / len(v), 2) if v else None for name, v in values.items()}
               for which, values in totals.items()}
    return {"channel": channel["key"], "cases": len(cases), "dates": [c["date"] for c in cases],
            "code": dict(code), "scores": average, "issues": issues[:30],
            "requests": len(cases) + (1 if entries else 0)}


def live_markdown(result: dict, previous: dict | None = None) -> list[str]:
    if result.get("error"):
        return [f"### Живая оценка промптов · {result['channel']}", "", result["error"]]
    code = result["code"]
    lines = [f"### Живая оценка промптов · {result['channel']} · выпуски {', '.join(result['dates'])}", "",
             f"Текущий промпт переписал {code.get('stories', 0)} новостей; проверку кода прошли "
             f"{code.get('passed', 0)}, не прошли {code.get('failed', 0)}, не написаны {code.get('not_written', 0)}. "
             f"Запросов к модели: {result['requests']} (отдельный ключ).", "",
             "| Критерий (1–5) | Опубликовано | Текущий промпт | Прошлый запуск |", "|---|---|---|---|"]
    names = {"accuracy": "точность по источникам", "tone": "тон канала", "why_value": "польза «почему важно»",
             "clarity": "ясность"}
    for name in JUDGE_CRITERIA:
        before = ((previous or {}).get("scores") or {}).get("current", {}).get(name)
        lines.append(f"| {names[name]} | {result['scores']['published'].get(name, '—')} | "
                     f"{result['scores']['current'].get(name, '—')} | {before if before is not None else '—'} |")
    if result["issues"]:
        lines += ["", "<details><summary>Замечания судьи</summary>", "", *[f"- {i}" for i in result["issues"]],
                  "", "</details>"]
    return lines
