"""Prepare a verified digest, send it for moderation, publish it after approval.

Digest flow: select stories → download articles → write from full text → code checks →
model fact-check → one repair attempt for failed entries → re-check → render.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import json
import re
import time
import zoneinfo

from . import articles, dedupe, feeds, render, verify
from .config import ROOT, redact
from .llm import EDITOR_SCHEMA, REWRITE_SCHEMA, SELECTION_SCHEMA, VERIFIER_SCHEMA, WEEKLY_SCHEMA, usage_summary
from .telegram import TelegramError

FINAL_STATUSES = ("published", "skipped", "expired")
MODES = ("auto", "manual")
BOT_COMMANDS = [
    {"command": "mode", "description": "Текущий режим публикации"},
    {"command": "auto", "description": "Посты выходят сами (можно отменить кнопкой)"},
    {"command": "manual", "description": "Посты выходят только после одобрения"},
]
FEED_CACHE: dict = {}   # one RSS collection per run, shared by the channels built at the same moment
ENTRY_FIELDS = ("index", "source_ids", "headline", "summary", "why", "source_quotes", "hashtags")


def publish_day(cfg: dict, channel: dict, now: dt.datetime) -> dt.date:
    """Date of the channel's publication nearest to `now`.

    Drafts for all channels are prepared together, when some channels still have the previous
    calendar date, so the draft belongs to the nearest publish time rather than to the local date.
    """
    today = now.astimezone(zoneinfo.ZoneInfo(channel["timezone"])).date()
    days = [today + dt.timedelta(days=shift) for shift in (-1, 0, 1)]
    return min(days, key=lambda day: abs(publish_window(cfg, channel, day)[0] - now))


def draft_id(channel: dict, day: dt.date, kind: str = "digest") -> str:
    """ru-20260928 for the digest, weekly-ru-20260928 for the week in review."""
    base = f"{channel['key']}-{day.strftime('%Y%m%d')}"
    return base if kind == "digest" else f"{kind}-{base}"


def weekly_due(cfg: dict, day: dt.date) -> bool:
    weekly = cfg.get("weekly") or {}
    return bool(weekly.get("enabled")) and day.weekday() == weekly.get("weekday", 0)


def draft_kinds(cfg: dict, day: dt.date) -> list[str]:
    """What a channel publishes on this day, in order: on Mondays the week in review comes right before the digest."""
    return (["weekly"] if weekly_due(cfg, day) else []) + ["digest"]


def result_key(key: str, kind: str) -> str:
    return key if kind == "digest" else f"{key}-{kind}"


def post_noun(draft: dict | None) -> str:
    return "итоги недели" if (draft or {}).get("kind") == "weekly" else "дайджест"


def publish_window(cfg: dict, channel: dict, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(scheduled publish time, last moment an approval is still accepted), timezone-aware."""
    hour, minute = map(int, channel["publish_time"].split(":"))
    start = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zoneinfo.ZoneInfo(channel["timezone"]))
    return start, start + dt.timedelta(hours=cfg["moderation"].get("approval_wait_hours", 2))


def approval_deadline(cfg: dict, channel: dict, day: dt.date, draft: dict | None = None) -> dt.datetime:
    """Approval window: two hours after the publish time, and never less than that after the draft arrived.

    GitHub can delay a scheduled run by hours, and a late draft must still leave time to approve it.
    """
    start, deadline = publish_window(cfg, channel, day)
    sent = (draft or {}).get("sent_at")
    return max(deadline, dt.datetime.fromisoformat(sent) + (deadline - start)) if sent else deadline


def admin_time(cfg: dict, moment: dt.datetime) -> str:
    return moment.astimezone(zoneinfo.ZoneInfo(cfg["moderation"].get("admin_timezone", "UTC"))).strftime("%H:%M")


def fill(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def prompt(name: str, cfg: dict, channel: dict) -> str:
    template = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return fill(template, {
        "BRAND": channel["brand"], "LANGUAGE": channel["language"], "AUDIENCE": channel["audience"],
        "FOCUS": channel["focus"], "LOOKBACK": cfg["lookback_hours"], "MIN_ITEMS": cfg["min_items"],
        "MAX_ITEMS": cfg["max_items"], "SELECT_COUNT": cfg["max_items"] + cfg.get("reserve_items", 2),
        "STYLE_RULES": "\n".join(f"- {rule}" for rule in channel.get("style_rules", [])),
        "HASHTAGS": "\n".join(f"{tag} — {description}" for tag, description in channel["hashtags"].items()),
    })


def source_view(candidate: dict, pages: dict, limit: int) -> dict:
    return {"id": candidate["id"], "source": candidate["source"], "title": candidate["title"],
            "summary": candidate["summary"], "article_text": pages.get(candidate["link"], {}).get("text", "")[:limit]}


def story_sources(cfg: dict, story: dict, by_id: dict, pages: dict) -> list[dict]:
    views = []
    for position, candidate_id in enumerate(story["candidate_ids"]):
        limit = cfg["article_chars"] if position == 0 else cfg.get("secondary_article_chars", 2500)
        views.append(source_view(by_id[candidate_id], pages, limit))
    return views


def label(entry: dict) -> str:
    return (entry.get("headline") or "?")[:70]


# In the json_object fallback mode the model may return numbers and booleans as strings.
def as_index(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_ok(verdict: dict | None) -> bool:
    ok = (verdict or {}).get("ok")
    return ok is True or (isinstance(ok, str) and ok.strip().lower() == "true")


def llm_verify(llm, cfg: dict, channel: dict, entries: dict[int, dict], pages: dict,
               recent: list[dict] | None = None) -> dict[int, dict]:
    """Model fact-check of entries that already passed the code checks.

    The checker also sees the stories published lately: a second chance to catch a repeat the selector let through.
    """
    if not entries:
        return {}
    review = {"recently_published": [recent_view(r) for r in recent or []], "entries": [
        {"index": index, "headline": e["headline"], "summary": e["summary"], "why": e["why"],
         "sources": [source_view(s, pages, cfg["article_chars"] if n == 0 else cfg.get("secondary_article_chars", 2500))
                     for n, s in enumerate(e["sources"])]}
        for index, e in entries.items()]}
    verdicts = llm.json_completion(prompt("verifier", cfg, channel),
                                   "Entries to check:\n" + json.dumps(review, ensure_ascii=False),
                                   VERIFIER_SCHEMA, "verdicts").get("verdicts", [])
    by_index = {as_index(v.get("index")): v for v in verdicts if isinstance(v, dict)}
    return {index: by_index.get(index, {"ok": False, "issues": ["проверка не вернула вердикт"]}) for index in entries}


def recent_stories(cfg: dict, channel: dict, state, day: dt.date) -> list[dict]:
    """Stories this channel published (or is about to publish) in the days before `day`, newest first.

    Links alone do not catch a repeat: the next day other outlets write about the same event under new links.
    The selector sees these stories and leaves out candidates about the same events.
    """
    since = day - dt.timedelta(days=cfg.get("repeat_window_days", 10))
    stories = []
    for draft in reversed(state.drafts(channel["key"])):
        try:
            date = dt.date.fromisoformat(draft.get("date", ""))
        except ValueError:
            continue
        if not since <= date < day or draft.get("status") not in ("published", "ready"):
            continue
        # Drafts made before stories were stored: headlines and links are read back from the post.
        saved = draft.get("stories") or [{"headline": e["headline"], "titles": [], "links": e["links"]}
                                         for e in render.split_digest(draft.get("html", ""))[1]]
        for story in saved:
            if story.get("headline"):
                stories.append({"date": date.isoformat(), "headline": story["headline"],
                                "titles": list(story.get("titles") or []), "links": list(story.get("links") or [])})
    for number, story in enumerate(stories, 1):
        story["id"] = f"p{number}"
    return stories


def recent_view(story: dict) -> dict:
    """What the selector sees of a published story: its headline and the original titles (or links) of its sources."""
    return {"id": story["id"], "date": story["date"], "headline": story["headline"],
            "sources": story["titles"][:2] or story["links"][:2]}


def recent_titles(stories: list[dict]) -> list[str]:
    titles = [t for s in stories for t in s["titles"]]
    return titles + [dedupe.slug_title(link) for s in stories for link in s["links"] if dedupe.slug_title(link)]


def collect_feeds(cfg: dict, now: dt.datetime) -> tuple[int, list[dict], list[str]]:
    """Fetch the feeds once per run: channels built at the same moment share one collection."""
    key = (cfg["feeds_file"], cfg["lookback_hours"], now.isoformat())
    if key not in FEED_CACHE:
        FEED_CACHE.clear()
        feed_list = feeds.parse_opml(ROOT / cfg["feeds_file"])
        items, failed = feeds.collect(feed_list, cfg["lookback_hours"], now)
        FEED_CACHE[key] = (len(feed_list), items, failed)
    count, items, failed = FEED_CACHE[key]
    return count, copy.deepcopy(items), list(failed)


def code_problem(problem: str) -> str:
    """Which deterministic check stopped an entry (counted in draft["checks"])."""
    if problem.startswith("ссылается на источники"):
        return "code_sources"
    if problem.startswith("в источниках нет фрагментов"):
        return "code_quotes"
    return "code_other"


def close_draft(draft: dict, llm, calls_before: int, checks: collections.Counter) -> dict:
    """Attach what the guardrails did and what the model calls cost to a finished draft."""
    draft["checks"] = dict(checks)
    draft["usage"] = usage_summary(getattr(llm, "usage_log", [])[calls_before:])
    return draft


def stored_entry(entry: dict, by_id: dict) -> dict:
    """An entry as the draft keeps it: enough to rewrite and re-check it later without the RSS items."""
    return {"headline": entry["headline"], "summary": entry["summary"], "why": entry["why"],
            "hashtags": list(entry.get("hashtags") or []), "source_quotes": list(entry.get("source_quotes") or []),
            "sources": [{k: s.get(k, "") for k in ("id", "source", "link", "title", "summary")} for s in entry["sources"]],
            "keys": sorted({k for i in entry.get("assigned_ids", []) for k in by_id[i]["member_keys"]}
                           | {k for s in entry["sources"] for k in s.get("member_keys", [])})}


def report_text(lines: list[str], limit: int = 3000) -> str:
    """Report lines for a Telegram message: within its size limit and without secrets."""
    text = redact("\n".join(lines))
    return render.esc(text if len(text) <= limit else text[:limit] + "…")


def build_digest(cfg: dict, channel: dict, llm, state, now: dt.datetime, log=print) -> dict:
    """Returns a draft dict with status 'ready' (and html) or 'empty' (with the reasons in report)."""
    day = publish_day(cfg, channel, now)
    if hasattr(llm, "set_budget"):
        llm.set_budget(cfg["llm"].get("budget_seconds", 720))
    feed_count, items, failed_feeds = collect_feeds(cfg, now)
    recent = recent_stories(cfg, channel, state, day)
    candidates = dedupe.select_candidates(items, cfg, state.posted_keys(channel["key"]), now, channel,
                                          recent_titles(recent))
    log(f"[{channel['key']}] {len(items)} fresh items from {feed_count - len(failed_feeds)}/{feed_count} feeds → "
        f"{len(candidates)} candidates; {len(recent)} stories published lately")
    draft = {"id": draft_id(channel, day), "channel": channel["key"], "date": day.isoformat(),
             "created_at": now.isoformat(), "status": "empty", "report": [], "failed_feeds": failed_feeds}
    report = draft["report"]
    used_before = len(getattr(llm, "models_used", []))
    steps_before = len(getattr(llm, "step_log", []))
    calls_before = len(getattr(llm, "usage_log", []))
    checks = collections.Counter(candidates=len(candidates))
    if hasattr(llm, "notes"):
        llm.notes.clear()
    if len(candidates) < cfg["min_items"]:
        report.append("Недостаточно свежих новостей в фидах")
        return close_draft(draft, llm, calls_before, checks)
    by_id = {c["id"]: c for c in candidates}

    # 1. Select stories from titles and summaries, grouping candidates about the same event
    #    and leaving out events the channel has already covered.
    payload = {"recently_published": [recent_view(r) for r in recent],
               "candidates": [{k: c[k] for k in ("id", "title", "summary", "source", "published", "also_reported_by")}
                              for c in candidates]}
    selection = llm.json_completion(prompt("selector", cfg, channel),
                                    "Recently published stories and today's candidates:\n" + json.dumps(payload, ensure_ascii=False),
                                    SELECTION_SCHEMA, "selection")
    recent_by_id = {r["id"]: r for r in recent}
    stories, used = [], set()
    for chosen in selection.get("stories", []):
        repeat = recent_by_id.get(str(chosen.get("repeat_of") or "").strip())
        if repeat:
            first = next((by_id[str(i)]["title"] for i in chosen.get("candidate_ids") or [] if str(i) in by_id), "?")
            report.append(f"🔁 «{first[:70]}»: уже было {repeat['date']} («{repeat['headline'][:60]}») — пропущено")
            checks["repeat_selection"] += 1
            continue
        ids, outlets, assigned = [], set(), []
        for candidate_id in dict.fromkeys(str(i) for i in chosen.get("candidate_ids") or []):
            if candidate_id not in by_id or candidate_id in used:
                continue
            used.add(candidate_id)                  # a candidate belongs to one story only
            assigned.append(candidate_id)
            outlet = by_id[candidate_id]["source"]
            if outlet not in outlets and len(ids) < verify.MAX_SOURCES:   # one link per outlet in a post
                ids.append(candidate_id)
                outlets.add(outlet)
        if ids:
            stories.append({"index": len(stories), "candidate_ids": ids, "assigned_ids": assigned})
    stories = stories[: cfg["max_items"] + cfg.get("reserve_items", 2)]
    story_by_index = {s["index"]: s for s in stories}
    checks["selected"] = len(stories)
    if len(stories) < cfg["min_items"]:
        report.append(f"Модель выбрала только {len(stories)} новостей")
        return close_draft(draft, llm, calls_before, checks)

    # 2. Download the articles, so entries are written from the full text rather than the RSS teaser.
    links = sorted({by_id[i]["link"] for s in stories for i in s["candidate_ids"]})
    pages = articles.fetch_many(links, cfg["article_chars"])

    # 3. Write entries.
    writing = {"stories": [{"index": s["index"], "sources": story_sources(cfg, s, by_id, pages)} for s in stories]}
    written = llm.json_completion(prompt("editor", cfg, channel),
                                  "Stories:\n" + json.dumps(writing, ensure_ascii=False), EDITOR_SCHEMA, "digest")
    written_by_index = {}
    for entry in written.get("items", []):
        if isinstance(entry, dict) and as_index(entry.get("index")) in story_by_index:
            written_by_index.setdefault(as_index(entry.get("index")), entry)
    checks["written"] = len(written_by_index)

    # 4. Code checks, then the model's fact-check.
    passed: dict[int, dict] = {}
    failing: dict[int, tuple[dict, list[str]]] = {}
    for story in stories:
        entry = written_by_index.get(story["index"])
        if not entry:
            report.append(f"❌ «{by_id[story['candidate_ids'][0]]['title'][:70]}»: модель не написала текст")
            checks["not_written"] += 1
            continue
        checked, problem, repairable = verify.check_entry(entry, story, by_id, pages, channel["hashtags"])
        if checked:
            passed[story["index"]] = checked
            continue
        checks["code_flagged"] += 1
        checks[code_problem(problem)] += 1
        if repairable:
            failing[story["index"]] = (entry, [problem])
        else:
            report.append(f"❌ «{label(entry)}»: {problem}")
            checks["dropped"] += 1
    for index, verdict in llm_verify(llm, cfg, channel, passed, pages, recent).items():
        repeat = recent_by_id.get(str(verdict.get("repeat_of") or "").strip())
        if repeat:
            report.append(f"🔁 «{label(passed.pop(index))}»: уже было {repeat['date']} («{repeat['headline'][:60]}») — пропущено")
            checks["repeat_verifier"] += 1
            continue
        if is_ok(verdict):
            report.extend(f"⚠️ «{label(passed[index])}»: {issue}" for issue in verdict.get("issues", []))
        else:
            failing[index] = (passed.pop(index), verdict.get("issues") or ["не прошла проверку"])
            checks["verifier_flagged"] += 1

    # 5. One repair attempt instead of dropping the story.
    if failing:
        to_fix = {"entries": [
            {**{k: entry.get(k) for k in ENTRY_FIELDS}, "index": index, "issues": issues,
             "sources": story_sources(cfg, story_by_index[index], by_id, pages)}
            for index, (entry, issues) in failing.items()]}
        repaired = llm.json_completion(prompt("repair", cfg, channel),
                                       "Entries to fix:\n" + json.dumps(to_fix, ensure_ascii=False), EDITOR_SCHEMA, "repair")
        fixed: dict[int, dict] = {}
        for entry in repaired.get("items", []):
            index = as_index(entry.get("index")) if isinstance(entry, dict) else None
            if index in failing and index not in fixed:
                checked, problem, _ = verify.check_entry(entry, story_by_index[index], by_id, pages, channel["hashtags"])
                if checked:
                    fixed[index] = checked
                else:
                    failing[index][1].append(f"после исправления: {problem}")
        verdicts = llm_verify(llm, cfg, channel, fixed, pages, recent)
        for index, (original, issues) in failing.items():
            verdict = verdicts.get(index)
            repeat = recent_by_id.get(str((verdict or {}).get("repeat_of") or "").strip())
            if repeat:
                report.append(f"🔁 «{label(original)}»: уже было {repeat['date']} («{repeat['headline'][:60]}») — пропущено")
                checks["repeat_verifier"] += 1
            elif is_ok(verdict):
                passed[index] = fixed[index]
                report.append(f"🔧 «{label(fixed[index])}»: исправлено — {'; '.join(issues)}")
                checks["repaired"] += 1
            else:
                final_issues = (verdict or {}).get("issues") or issues
                report.append(f"❌ «{label(original)}»: {'; '.join(final_issues)}")
                checks["dropped"] += 1

    # 6. Assemble in the selector's order of importance.
    entries, primaries = [], set()
    for story in stories:
        entry = passed.get(story["index"])
        if not entry:
            continue
        primary = entry["sources"][0]["id"]
        if primary in primaries:
            report.append(f"❌ «{label(entry)}»: повтор источника")
            checks["dropped"] += 1
            continue
        primaries.add(primary)
        entry["assigned_ids"] = story["assigned_ids"]
        entries.append(entry)
    entries = entries[: cfg["max_items"]]

    used = list(dict.fromkeys(getattr(llm, "models_used", [])[used_before:]))
    draft["model"] = ", ".join(used) or llm.last_model
    step_names = {"selection": "отбор", "digest": "написание", "repair": "исправление", "verdicts": "проверка"}
    fallbacks = list(dict.fromkeys(
        f"{step_names.get(step, step)} — {model}" for step, model in getattr(llm, "step_log", [])[steps_before:]
        if hasattr(llm, "preferred") and model != llm.preferred(step)))
    notes = list(getattr(llm, "notes", []))
    if notes:
        log(redact(f"[{channel['key']}] model errors before a successful answer: " + " | ".join(notes[-8:])))
    if fallbacks:
        reasons = list(dict.fromkeys(re.sub(r"/json_\w+", "", note) for note in notes))
        reason = f" (причины: {'; '.join(reasons[:3])[:300]})" if reasons else ""
        report.append(redact(f"⚠️ Часть работы сделала запасная модель: {', '.join(fallbacks)}{reason}"))
    if len(entries) < cfg["min_items"]:
        report.append(f"После проверки осталось {len(entries)} новостей (минимум {cfg['min_items']})")
        return close_draft(draft, llm, calls_before, checks)
    text, entries = render.fit_digest(channel, day, entries, cfg["min_items"])
    checks["published"] = len(entries)
    draft.update(status="ready", html=text,
                 entries=[stored_entry(e, by_id) for e in entries],
                 # what the channel said, so the next days' selection can leave these events out
                 stories=[{"headline": e["headline"], "hashtags": list(e.get("hashtags") or []),
                           "titles": list(dict.fromkeys(by_id[i]["title"] for i in e.get("assigned_ids", [])))[:3],
                           "links": [s["link"] for s in e["sources"]]} for e in entries],
                 # every article assigned to a published story counts as posted, not only the linked ones
                 keys=sorted({k for e in entries for i in e.get("assigned_ids", []) for k in by_id[i]["member_keys"]}
                             | {k for e in entries for s in e["sources"] for k in s["member_keys"]}),
                 sources=[s["link"] for e in entries for s in e["sources"]])
    return close_draft(draft, llm, calls_before, checks)


def week_stories(channel: dict, state, day: dt.date) -> list[dict]:
    """Stories of the posts the channel published in the 7 days before `day`, oldest first, with links to the posts."""
    stories = []
    for draft in state.drafts(channel["key"]):
        date = dt.date.fromisoformat(draft["date"])
        if not day - dt.timedelta(days=7) <= date < day or draft.get("status") != "published" or not draft.get("message_id"):
            continue
        post = f"https://t.me/{channel['chat_id'].lstrip('@')}/{draft['message_id']}"
        for entry in render.split_digest(draft.get("html", ""))[1]:
            lines = [line for line in entry["html"].split("\n")[1:] if not line.startswith("🔗")]
            stories.append({"id": f"s{len(stories) + 1}", "date": date.isoformat(), "headline": entry["headline"],
                            "text": render.plain_text("\n".join(lines)), "post": post})
    return stories


def build_weekly(cfg: dict, channel: dict, llm, state, now: dt.datetime, log=print) -> dict:
    """The week in review, written from the channel's own posts of the past week; numbers are checked against them."""
    day = publish_day(cfg, channel, now)
    weekly = cfg.get("weekly") or {}
    draft = {"id": draft_id(channel, day, "weekly"), "kind": "weekly", "channel": channel["key"],
             "date": day.isoformat(), "created_at": now.isoformat(), "status": "empty", "report": []}
    report = draft["report"]
    stories = week_stories(channel, state, day)
    log(f"[{channel['key']}] week in review from {len(stories)} stories")
    if len(stories) < weekly.get("min_stories", 10):
        report.append(f"За неделю опубликовано только {len(stories)} новостей")
        return draft
    if hasattr(llm, "set_budget"):
        llm.set_budget(cfg["llm"].get("budget_seconds", 720))
    calls_before = len(getattr(llm, "usage_log", []))
    payload = {"stories": [{k: s[k] for k in ("id", "date", "headline", "text")} for s in stories]}
    written = llm.json_completion(prompt("weekly", cfg, channel),
                                  "This week's published stories:\n" + json.dumps(payload, ensure_ascii=False),
                                  WEEKLY_SCHEMA, "weekly")
    by_id = {s["id"]: s for s in stories}
    items, cited = [], set()
    for item in written.get("items") or []:
        if not isinstance(item, dict) or item.get("section") not in render.WEEKLY_SECTIONS:
            continue
        text = " ".join(str(item.get("text") or "").split())[:200]
        ids = [i for i in dict.fromkeys(map(str, item.get("story_ids") or [])) if i in by_id][:3]
        trend = item["section"] == "trends"
        if not text or not ids:
            report.append(f"❌ «{text[:70] or '?'}»: нет ссылки на новость недели")
            continue
        if not trend and len(ids) > 1:   # several events squeezed into a line lose their links and details
            report.append(f"❌ «{text[:70]}»: в одной строке несколько новостей")
            continue
        if trend and len(ids) < 2:
            report.append(f"❌ «{text[:70]}»: тренд опирается только на одну новость")
            continue
        if not trend and ids[0] in cited:
            report.append(f"❌ «{text[:70]}»: эта новость уже есть в итогах")
            continue
        missing = verify.unsupported_numbers(text, *[by_id[i]["headline"] + " " + by_id[i]["text"] for i in ids])
        if missing:
            report.append(f"❌ «{text[:70]}»: в новостях недели нет чисел {missing}")
            continue
        if not trend:
            cited.add(ids[0])
        items.append({"section": item["section"], "text": text, "stories": [by_id[i] for i in ids]})
    intro = " ".join(str(written.get("intro") or "").split())[:400]
    if intro and verify.unsupported_numbers(intro, *[s["headline"] + " " + s["text"] for s in stories]):
        report.append(f"⚠️ Вступление убрано: в новостях недели нет его чисел («{intro[:70]}»)")
        intro = ""
    draft["model"] = llm.last_model
    draft["usage"] = usage_summary(getattr(llm, "usage_log", [])[calls_before:])
    if len(items) < weekly.get("min_items", 4):
        report.append(f"После проверки осталось {len(items)} строк (минимум {weekly.get('min_items', 4)})")
        return draft
    text, items = render.fit_weekly(channel, day - dt.timedelta(days=7), day - dt.timedelta(days=1), intro, items)
    draft.update(status="ready", html=text)
    return draft


def default_mode(cfg: dict) -> str:
    moderation = cfg["moderation"]
    if moderation.get("mode") in MODES:
        return moderation["mode"]
    return "auto" if moderation.get("on_no_response") == "publish" else "manual"   # the older setting


def current_mode(cfg: dict, state) -> str:
    """auto: posts go out by themselves unless the admin presses «Не публиковать»; manual: only after approval.

    The mode chosen through the bot (/auto, /manual) overrides config.yaml.
    """
    saved = state.settings().get("mode") if state is not None else None
    return saved if saved in MODES else default_mode(cfg)


def auto_publish_time(cfg: dict, channel: dict, day: dt.date, draft: dict | None) -> dt.datetime:
    """Automatic mode: the post goes out at its publish time, but never sooner than the veto window after delivery."""
    start, _ = publish_window(cfg, channel, day)
    sent = (draft or {}).get("sent_at")
    veto = dt.timedelta(minutes=cfg["moderation"].get("auto_veto_minutes", 20))
    return max(start, dt.datetime.fromisoformat(sent) + veto) if sent else start


def moderation_buttons(draft_id_value: str, mode: str = "manual") -> list[list[dict]]:
    if mode == "auto":
        rows = [[{"text": "⛔ Не публиковать", "callback_data": f"skip:{draft_id_value}"}]]
    else:
        rows = [[{"text": "✅ Опубликовать", "callback_data": f"approve:{draft_id_value}"},
                 {"text": "⛔ Пропустить", "callback_data": f"skip:{draft_id_value}"}]]
    if not draft_id_value.startswith("weekly-"):   # the week in review is not rewritten on request
        rows.append([{"text": "✏️ Переписать", "callback_data": f"rewrite:{draft_id_value}"}])
    return rows


def mode_text(mode: str, already: bool = False, refreshed: int = 0) -> str:
    if mode == "auto":
        lines = ["🤖 <b>Режим: автоматическая публикация</b>",
                 "Черновики приходят вам утром, а посты выходят сами во время выхода канала. "
                 "Чтобы остановить конкретный пост, нажмите «⛔ Не публиковать» под черновиком."]
    else:
        lines = ["✋ <b>Режим: только с одобрения</b>",
                 "Пост выходит, только если нажать «✅ Опубликовать» под черновиком; без нажатия он не выйдет."]
    if already:
        lines.insert(0, "Этот режим уже включён.")
    if refreshed:
        lines.append(f"Кнопки под сегодняшними черновиками обновлены: {refreshed}.")
    lines += ["", "/auto — публиковать автоматически · /manual — только с одобрения · /mode — текущий режим",
              "Бот читает команды при ближайшем запуске, поэтому ответ может прийти с задержкой."]
    return "\n".join(lines)


def send_preview(tg, admin_chat_id: str, cfg: dict, channel: dict, draft: dict, test: bool = False,
                 mode: str = "manual") -> None:
    day = dt.date.fromisoformat(draft["date"])
    start, _ = publish_window(cfg, channel, day)
    deadline = approval_deadline(cfg, channel, day, draft)
    if test:
        # Test drafts carry no buttons: an approval must never apply to a draft the admin has not seen.
        if draft["status"] == "ready":
            tg.send_message(admin_chat_id, f"🧪 Тестовый черновик для <b>{render.esc(channel['chat_id'])}</b>: "
                            "в канал не публикуется, кнопок нет.", silent=True)
            tg.send_message(admin_chat_id, draft["html"])
        else:
            tg.send_message(admin_chat_id, f"🧪 Тестовый черновик для <b>{render.esc(channel['chat_id'])}</b> не собран.",
                            silent=True)
        if draft["report"]:
            tg.send_message(admin_chat_id, "<b>Отчёт проверки</b>\n" + report_text(draft["report"][:15]), silent=True)
        return
    what = "Итоги недели" if draft.get("kind") == "weekly" else "Черновик"
    if draft["status"] != "ready":
        reason = "сбой сервиса модели или сети" if draft["status"] == "error" else "мало подходящих новостей"
        unready = "итоги недели пока не собраны" if draft.get("kind") == "weekly" else "черновик пока не собран"
        tg.send_message(admin_chat_id,
                        f"⚠️ <b>{render.esc(channel['chat_id'])}</b>: {unready} за {draft['date']} ({reason}). "
                        f"Бот попробует снова при следующих проверках до {admin_time(cfg, deadline)}; "
                        "это уведомление приходит один раз.\n" + report_text(draft["report"][-8:]),
                        silent=True)
        return
    if mode == "auto":
        not_before = auto_publish_time(cfg, channel, day, draft)
        header = (f"🤖 {what} для <b>{render.esc(channel['chat_id'])}</b> выйдет автоматически не раньше "
                  f"{admin_time(cfg, not_before)} по вашему времени (выход канала — {admin_time(cfg, start)}). "
                  "Чтобы остановить публикацию, нажмите «Не публиковать» до этого времени.")
    else:
        rule = (f"Пост выйдет только после одобрения: нажмите «Опубликовать» до {admin_time(cfg, deadline)}. "
                "Бот увидит нажатие при ближайшей проверке (каждые 30 минут).")
        header = (f"📝 {what} для <b>{render.esc(channel['chat_id'])}</b> · выход в {admin_time(cfg, start)} "
                  f"по вашему времени. {render.esc(rule)}")
    if draft.get("kind") != "weekly":
        header += " Чтобы изменить текст, нажмите «✏️ Переписать»."
    tg.send_message(admin_chat_id, header, silent=True)
    message = tg.send_message(admin_chat_id, draft["html"], buttons=moderation_buttons(draft["id"], mode))
    draft["preview_message_id"] = message["message_id"]
    draft["preview_mode"] = mode
    if draft["report"]:
        tg.send_message(admin_chat_id, "<b>Отчёт проверки</b>\n" + report_text(draft["report"][:15]), silent=True)


def _quietly(action, *args) -> None:
    """Telegram rejects answers to button presses older than a few seconds; that must not stop publishing."""
    try:
        action(*args)
    except Exception:
        pass


def process_callbacks(tg, state, admin_chat_id: str, cfg: dict | None = None, now=None, log=print) -> dict:
    """Read button presses (and, when cfg is given, admin commands) from Telegram. Returns the decisions map."""
    data = state.telegram()
    data.setdefault("decisions", {})
    updates = tg.get_updates(data.get("offset"))
    for update in updates:
        data["offset"] = update["update_id"] + 1
        if update.get("message") and cfg is not None:
            handle_message(cfg, tg, state, admin_chat_id, update["message"], now, log, data=data)
            continue
        callback = update.get("callback_query")
        if not callback or ":" not in callback.get("data", ""):
            continue
        if str(callback["from"]["id"]) != str(admin_chat_id):
            _quietly(tg.answer_callback, callback["id"], "Нет доступа")
            continue
        action, draft_key = callback["data"].split(":", 1)
        if action not in ("approve", "skip", "rewrite"):
            continue
        draft = state.draft(draft_key)
        message_id = callback["message"]["message_id"]
        if not draft or draft.get("status") != "ready" or draft.get("preview_message_id") != message_id:
            # Only the exact draft message the admin saw counts: not an old test preview or a replaced draft.
            _quietly(tg.answer_callback, callback["id"], "Этот черновик неактуален")
            _quietly(tg.set_buttons, callback["message"]["chat"]["id"], message_id,
                     [[{"text": "Черновик неактуален", "callback_data": "noop:0"}]])
            continue
        if action == "rewrite":
            ask_for_rewrite(cfg, tg, admin_chat_id, draft, data)
            _quietly(tg.answer_callback, callback["id"], "Напишите, что изменить")
            continue
        data["decisions"][draft_key] = action
        _quietly(tg.answer_callback, callback["id"], "Принято")
        label_text = "✅ Одобрено — выйдет при ближайшей проверке" if action == "approve" else "⛔ Пропущено"
        _quietly(tg.set_buttons, callback["message"]["chat"]["id"], callback["message"]["message_id"],
                 [[{"text": label_text, "callback_data": "noop:0"}]])
    data["decisions"] = dict(list(data["decisions"].items())[-60:])
    state.save_telegram(data)
    return data["decisions"]


def handle_message(cfg, tg, state, admin_chat_id, message: dict, now=None, log=print, data: dict | None = None) -> None:
    """Admin messages in the private chat with the bot: the commands /mode, /auto, /manual, and replies to a draft
    with what to rewrite. Strangers are ignored."""
    text = (message.get("text") or "").strip()
    if str((message.get("from") or {}).get("id")) != str(admin_chat_id) or (message.get("chat") or {}).get("type") != "private":
        return
    if not text.startswith("/"):
        if text and message.get("reply_to_message"):
            own = data is None
            data = state.telegram() if own else data
            note_rewrite_request(cfg, tg, state, admin_chat_id, message, data, now)
            if own:
                state.save_telegram(data)
        return
    command = text.split()[0].split("@")[0].lower()
    now = now or dt.datetime.now(dt.timezone.utc)
    if command in ("/auto", "/manual"):
        mode, previous = command[1:], current_mode(cfg, state)
        state.save_settings({**state.settings(), "mode": mode, "changed_at": now.isoformat()})
        refreshed = refresh_buttons(cfg, tg, state, admin_chat_id, mode, now)
        log(f"publishing mode: {previous} → {mode}")
        _quietly(tg.send_message, admin_chat_id, mode_text(mode, already=previous == mode, refreshed=refreshed))
    elif command in ("/mode", "/status", "/start", "/help"):
        _quietly(tg.send_message, admin_chat_id, mode_text(current_mode(cfg, state)))


def refresh_buttons(cfg, tg, state, admin_chat_id, mode: str, now) -> int:
    """After a mode switch, give today's undecided drafts the buttons of the new mode."""
    decisions = state.telegram().get("decisions", {})
    count = 0
    for channel, kind in ((c, k) for c in cfg["channels"].values() for k in ("weekly", "digest")):
        draft = state.draft(draft_id(channel, publish_day(cfg, channel, now), kind))
        if (not draft or draft.get("status") != "ready" or not draft.get("preview_message_id")
                or draft["id"] in decisions or draft.get("preview_mode", "manual") == mode):
            continue
        try:
            tg.set_buttons(admin_chat_id, draft["preview_message_id"], moderation_buttons(draft["id"], mode))
        except Exception:
            continue
        draft["preview_mode"] = mode
        state.save_draft(draft)
        count += 1
    return count


REWRITE_HINT = ("✏️ <b>Что изменить в черновике {chat}?</b>\n"
                "Ответьте на это сообщение одним текстом, например: «убери новость 3», «сделай заголовки короче», "
                "«в новости 2 объясни, что это значит для банков». Бот перепишет черновик при ближайшем запуске "
                "(обычно в течение 30 минут), заново проверит факты и пришлёт новую версию. "
                "Источники и факты остаются прежними: бот не добавляет того, чего нет в статьях.")


def max_rewrites(cfg: dict | None) -> int:
    return int(((cfg or {}).get("moderation") or {}).get("max_rewrites", 2))


def ask_for_rewrite(cfg, tg, admin_chat_id, draft: dict, data: dict) -> None:
    """The «Переписать» button: ask what to change, as a message the editor answers with a reply."""
    channel = ((cfg or {}).get("channels") or {}).get(draft.get("channel"), {})
    chat = render.esc(channel.get("chat_id", draft.get("channel", "")))
    if draft.get("rewrites", 0) >= max_rewrites(cfg):
        _quietly(tg.send_message, admin_chat_id, f"✏️ Черновик {chat} уже переписан {draft['rewrites']} раза — "
                 "это предел для одного выпуска. Опубликуйте его или пропустите.", None, True)
        return
    try:
        message = tg.send_message(admin_chat_id, REWRITE_HINT.format(chat=chat), force_reply="Что изменить?")
    except Exception:
        return
    prompts = data.setdefault("rewrite_prompts", {})
    prompts[str(message["message_id"])] = draft["id"]
    data["rewrite_prompts"] = dict(list(prompts.items())[-30:])


def draft_for_reply(cfg, state, data: dict, message_id, now) -> dict | None:
    """The draft a reply refers to: the question asked by «Переписать», or the draft message itself."""
    key = (data.get("rewrite_prompts") or {}).get(str(message_id))
    if key:
        return state.draft(key)
    for channel in cfg["channels"].values():
        draft = state.draft(draft_id(channel, publish_day(cfg, channel, now)))
        if draft and draft.get("preview_message_id") == message_id:
            return draft
    return None


def note_rewrite_request(cfg, tg, state, admin_chat_id, message: dict, data: dict, now=None) -> None:
    """Remember what the editor asked to change; the rewrite itself needs the model and runs in apply_rewrites."""
    now = now or dt.datetime.now(dt.timezone.utc)
    draft = draft_for_reply(cfg, state, data, message["reply_to_message"].get("message_id"), now)
    if not draft:
        return   # a reply to some other message: not a request
    if draft.get("status") != "ready" or draft["id"] in data.get("decisions", {}) or draft.get("kind") == "weekly":
        _quietly(tg.send_message, admin_chat_id, "✏️ Этот черновик уже неактуален: переписать его нельзя.", None, True)
        return
    if draft.get("rewrites", 0) >= max_rewrites(cfg):
        _quietly(tg.send_message, admin_chat_id, "✏️ Предел переписываний для этого выпуска исчерпан.", None, True)
        return
    requests = data.setdefault("rewrites", {})
    previous = (requests.get(draft["id"]) or {}).get("text")
    text = message["text"].strip()[:1000]
    requests[draft["id"]] = {"text": f"{previous}\n{text}" if previous else text, "at": now.isoformat()}
    _quietly(tg.send_message, admin_chat_id, "✏️ Принято. Перепишу черновик при ближайшем запуске бота и пришлю "
             "новую версию. До этого он не выйдет.", None, True)


def merge_usage(first: dict | None, second: dict) -> dict:
    """Add the requests and tokens of a later step (a rewrite) to what the draft already used."""
    result = copy.deepcopy(first or {"requests": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                     "by_model": {}, "by_step": {}})
    for field in ("requests", "ok", "prompt_tokens", "completion_tokens"):
        result[field] = result.get(field, 0) + second.get(field, 0)
    for group in ("by_model", "by_step"):
        for name, numbers in second.get(group, {}).items():
            target = result.setdefault(group, {}).setdefault(name, {})
            for field, value in numbers.items():
                target[field] = target.get(field, 0) + value
    return result


def rewrite_draft(cfg, channel, llm, draft: dict, request: str, log=print) -> tuple[bool, list[str]]:
    """Rewrite a ready digest as the editor asked, with the same sources and the same checks as a new one.

    An entry that fails a check after the rewrite keeps its previous text. Returns (changed, report lines).
    """
    entries = draft.get("entries")
    if not entries:
        return False, ["Этот черновик собран старой версией бота: переписать его нельзя."]
    day = dt.date.fromisoformat(draft["date"])
    if hasattr(llm, "set_budget"):
        llm.set_budget(cfg["llm"].get("budget_seconds", 720))
    calls_before = len(getattr(llm, "usage_log", []))
    links = sorted({s["link"] for e in entries for s in e["sources"]})
    pages = articles.fetch_many(links, cfg["article_chars"])
    payload = {"request": request, "entries": [
        {"index": n, **{k: e[k] for k in ("headline", "summary", "why", "hashtags", "source_quotes")},
         "sources": [source_view(s, pages, cfg["article_chars"] if m == 0 else cfg.get("secondary_article_chars", 2500))
                     for m, s in enumerate(e["sources"])]}
        for n, e in enumerate(entries)]}
    answer = llm.json_completion(prompt("rewrite", cfg, channel),
                                 "The editor's request and the entries:\n" + json.dumps(payload, ensure_ascii=False),
                                 REWRITE_SCHEMA, "rewrite")
    by_index = {}
    for item in answer.get("items") or []:
        index = as_index(item.get("index")) if isinstance(item, dict) else None
        if index is not None and 0 <= index < len(entries):
            by_index.setdefault(index, item)
    report, changed, removed = [], {}, set()
    checks = collections.Counter()
    for index, entry in enumerate(entries):
        item = by_index.get(index)
        if not item:
            continue
        keep = item.get("keep")
        if keep is False or (isinstance(keep, str) and keep.strip().lower() == "false"):
            removed.add(index)
            continue
        if all(" ".join(str(item.get(k) or "").split()) == " ".join(str(entry[k]).split())
               for k in ("headline", "summary", "why")) and list(item.get("hashtags") or []) == entry["hashtags"]:
            continue
        candidates = {s["id"]: s for s in entry["sources"]}
        candidate = {**{k: item.get(k) for k in ("headline", "summary", "why", "hashtags", "source_quotes")},
                     "source_ids": list(candidates)}
        checked, problem, _ = verify.check_entry(candidate, {"candidate_ids": list(candidates)}, candidates, pages,
                                                 channel["hashtags"])
        if checked:
            changed[index] = checked
        else:
            checks["rewrite_rejected"] += 1
            report.append(f"↩️ «{label(entry)}»: правка не прошла проверку ({problem}) — оставлен прежний текст")
    for index, verdict in llm_verify(llm, cfg, channel, changed, pages).items():
        if not is_ok(verdict):
            checks["rewrite_rejected"] += 1
            report.append(f"↩️ «{label(entries[index])}»: правка не прошла проверку фактов "
                          f"({'; '.join(verdict.get('issues') or [])[:200]}) — оставлен прежний текст")
            changed.pop(index)
    usage = usage_summary(getattr(llm, "usage_log", [])[calls_before:])
    kept = [changed.get(n, e) for n, e in enumerate(entries) if n not in removed]
    if len(kept) < cfg["min_items"]:
        draft["usage"] = merge_usage(draft.get("usage"), usage)
        return False, report + [f"После правки осталось бы {len(kept)} новостей (минимум {cfg['min_items']}) — "
                                "черновик не изменён."]
    if not changed and not removed:
        draft["usage"] = merge_usage(draft.get("usage"), usage)
        return False, report + ["Модель не предложила изменений, которые прошли бы проверку: черновик не изменён."]
    stories = draft.get("stories") or []
    titles = {n: stories[n].get("titles", []) if n < len(stories) else [] for n in range(len(entries))}
    order = [n for n in range(len(entries)) if n not in removed]
    stored = []
    for n in order:
        entry = changed.get(n)
        stored.append(entries[n] if entry is None else {
            **{k: entry[k] for k in ("headline", "summary", "why")}, "hashtags": list(entry.get("hashtags") or []),
            "source_quotes": list(entry.get("source_quotes") or []), "sources": entries[n]["sources"],
            "keys": entries[n].get("keys", [])})
    text, fitted = render.fit_digest(channel, day, stored, cfg["min_items"])
    order = order[:len(fitted)]
    stored = stored[:len(fitted)]
    draft.update(html=text, entries=stored,
                 stories=[{"headline": e["headline"], "hashtags": e["hashtags"], "titles": titles[n],
                           "links": [s["link"] for s in e["sources"]]} for n, e in zip(order, stored)],
                 keys=sorted({k for e in stored for k in e.get("keys", [])}) or draft.get("keys", []),
                 sources=[s["link"] for e in stored for s in e["sources"]])
    draft["rewrites"] = draft.get("rewrites", 0) + 1
    draft.setdefault("rewrite_log", []).append({"request": request[:500], "changed": len(changed),
                                                "removed": len(removed), "rejected": checks["rewrite_rejected"]})
    stats = collections.Counter(draft.get("checks") or {})
    stats.update(rewrites=1, rewrite_changed=len(changed), rewrite_removed=len(removed), **checks)
    draft["checks"] = dict(stats)
    draft["usage"] = merge_usage(draft.get("usage"), usage)
    summary = f"✏️ Переписано по вашей просьбе: изменено {len(changed)}, убрано {len(removed)}"
    log(f"[{channel['key']}] {draft['id']} rewritten: {len(changed)} changed, {len(removed)} removed")
    return True, [summary] + report


def apply_rewrites(cfg, llm, tg, state, admin_chat_id, now, log=print) -> list[str]:
    """Rewrite every draft the editor asked to change, and send each new version for moderation."""
    data = state.telegram()
    requests = data.get("rewrites") or {}
    done = []
    for key in list(requests):
        request = requests.pop(key)
        data["rewrites"] = requests
        state.save_telegram(data)   # a request is tried once, even if the rewrite fails
        draft = state.draft(key)
        if not draft or draft.get("status") != "ready" or key in data.get("decisions", {}):
            continue
        channel = cfg["channels"][draft["channel"]]
        try:
            changed, lines = rewrite_draft(cfg, channel, llm, draft, request["text"], log)
        except Exception as e:   # quota or network: the draft stays as it was
            problem = redact(f"{type(e).__name__}: {e}")[:300]
            log(f"[{channel['key']}] rewrite of {key} failed: {problem}")
            changed, lines = False, [f"Не удалось переписать ({problem})."]
        if not changed:
            state.save_draft(draft)
            _quietly(tg.send_message, admin_chat_id, f"✏️ {render.esc(channel['chat_id'])}: "
                     + report_text(lines + ["В силе прежняя версия черновика."]), None, True)
            continue
        old = draft.pop("preview_message_id", None)
        if old:
            _quietly(tg.set_buttons, admin_chat_id, old, [[{"text": "Заменён новой версией ↓", "callback_data": "noop:0"}]])
        draft.pop("sent_at", None)   # automatic mode: the editor gets a full veto window for the new version
        draft["report"] = lines + list(draft.get("report") or [])
        state.save_draft(draft)
        notify_admin(cfg, channel, tg, state, admin_chat_id, draft, now)
        done.append(key)
    return done


def ensure_bot_commands(tg, state, admin_chat_id) -> None:
    """Show /mode, /auto and /manual in the command menu of the admin's chat (once)."""
    settings = state.settings()
    if settings.get("commands") == BOT_COMMANDS:
        return
    try:
        tg.set_commands(BOT_COMMANDS, admin_chat_id)
    except Exception:
        return
    state.save_settings({**settings, "commands": BOT_COMMANDS})


def inbox(cfg, tg, state, admin_chat_id, now, log=print) -> str:
    """Runs between publishing windows: pick up commands and button presses so a mode switch is answered soon."""
    ensure_bot_commands(tg, state, admin_chat_id)
    process_callbacks(tg, state, admin_chat_id, cfg, now, log)
    return current_mode(cfg, state)


def prepare(cfg, channel, llm, tg, state, admin_chat_id, now, log=print, notify=True, kind: str = "digest") -> dict:
    day = publish_day(cfg, channel, now)
    existing = state.draft(draft_id(channel, day, kind))
    if existing and existing.get("status") in ("ready",) + FINAL_STATUSES:
        log(f"[{channel['key']}] draft {existing['id']} already {existing['status']}")
        if notify and tg:
            notify_admin(cfg, channel, tg, state, admin_chat_id, existing, now)   # resend if it never reached the admin
        return existing
    try:
        build = build_weekly if kind == "weekly" else build_digest
        draft = build(cfg, channel, llm, state, now, log)
    except Exception as e:  # model quota (429), network: keep a record and retry later
        problem = redact(f"{type(e).__name__}: {e}")[:300]
        log(f"[{channel['key']}] build failed: {problem}")
        draft = {"id": draft_id(channel, day, kind), "channel": channel["key"], "date": day.isoformat(),
                 "created_at": now.isoformat(), "status": "error", "report": [problem]}
        if kind != "digest":
            draft["kind"] = kind
    if existing and existing.get("alerted"):
        draft["alerted"] = True
    draft["attempts"] = (existing or {}).get("attempts", 0) + 1
    state.save_draft(draft)
    state.prune_drafts(day)
    log(f"[{channel['key']}] draft {draft['id']}: {draft['status']}")
    if notify and tg:
        notify_admin(cfg, channel, tg, state, admin_chat_id, draft, now)
    return draft


def notify_admin(cfg, channel, tg, state, admin_chat_id, draft, now=None) -> None:
    """Send a ready draft with buttons once, or a one-time alert that it could not be built."""
    if draft["status"] == "ready":
        if draft.get("preview_message_id"):
            return
    elif draft["status"] in FINAL_STATUSES or draft.get("alerted"):
        return
    draft.setdefault("sent_at", (now or dt.datetime.now(dt.timezone.utc)).isoformat())
    try:
        send_preview(tg, admin_chat_id, cfg, channel, draft, mode=current_mode(cfg, state))
        if draft["status"] != "ready":
            draft["alerted"] = True
    finally:
        state.save_draft(draft)   # keeps preview_message_id even if a later message fails


def prepare_all(cfg, keys, llm, tg, state, admin_chat_id, now, log=print, retry_after=90, sleep=time.sleep,
                clock=time.monotonic, retry_window=20 * 60, retry: bool = True) -> dict:
    """Morning run: build every channel's draft, retry failures once, then send all drafts to the admin together."""
    drafts = {}
    started = clock()
    if tg:   # a /auto or /manual sent overnight applies to this morning's drafts
        try:
            ensure_bot_commands(tg, state, admin_chat_id)
            process_callbacks(tg, state, admin_chat_id, cfg, now, log)
        except Exception as e:
            log(f"reading Telegram updates failed: {redact(e)[:200]}")

    def build(key):
        try:
            drafts[key] = prepare(cfg, cfg["channels"][key], llm, tg, state, admin_chat_id, now, log, notify=False)
        except Exception as e:  # one channel failing must not block the others
            problem = redact(f"{type(e).__name__}: {e}")[:300]
            log(f"[{key}] prepare failed: {problem}")
            drafts[key] = {"status": "error", "report": [problem]}

    for key in keys:
        build(key)
    failed = [key for key in keys if drafts[key]["status"] in ("error", "empty")] if retry else []
    if failed and clock() - started > retry_window:
        log(f"no time left to retry {', '.join(failed)}: they will be rebuilt at their publish time")
        failed = []
    if failed:
        log(f"retrying {', '.join(failed)} in {retry_after}s")
        sleep(retry_after)
        for key in failed:
            build(key)
    for key in keys:
        if tg and "id" in drafts[key]:
            try:
                notify_admin(cfg, cfg["channels"][key], tg, state, admin_chat_id, drafts[key], now)
            except Exception as e:
                log(f"[{key}] sending the draft failed: {redact(e)[:300]}")
    return drafts


def undelivered(drafts: dict) -> list[str]:
    """Channels whose ready draft did not reach the admin."""
    return [key for key, draft in drafts.items() if draft.get("status") == "ready" and not draft.get("preview_message_id")]


def publish(cfg, channel, llm, tg, state, admin_chat_id, now, log=print, kind: str = "digest") -> str:
    """Returns published | skipped | waiting | expired | empty."""
    day = publish_day(cfg, channel, now)
    key = draft_id(channel, day, kind)
    draft = state.draft(key)
    if not draft or (draft.get("status") in ("empty", "error") and not draft.get("rebuilt")):
        # Build at most once per publish window: repeated rebuilds burn the model quota and make runs pile up.
        log(f"[{channel['key']}] no ready {kind} draft for {day}: building now")
        draft = prepare(cfg, channel, llm, tg, state, admin_chat_id, now, log, kind=kind)
        if draft.get("status") in ("empty", "error"):
            draft["rebuilt"] = True
            state.save_draft(draft)
    if draft.get("status") in FINAL_STATUSES:
        log(f"[{channel['key']}] {key} already {draft['status']}")
        return draft["status"]
    if draft.get("status") != "ready":
        return "empty"
    if not draft.get("preview_message_id"):
        log(f"[{channel['key']}] {key} never reached the admin: sending it now")
        notify_admin(cfg, channel, tg, state, admin_chat_id, draft, now)

    decision = process_callbacks(tg, state, admin_chat_id, cfg, now, log).get(key)
    if decision is None and key in (state.telegram().get("rewrites") or {}):
        apply_rewrites(cfg, llm, tg, state, admin_chat_id, now, log)
        return "waiting"   # the new version goes to the editor first
    mode = current_mode(cfg, state)
    deadline = approval_deadline(cfg, channel, day, draft)
    if decision is None and mode == "auto" and now < deadline:
        not_before = auto_publish_time(cfg, channel, day, draft)
        if now < not_before:
            log(f"[{channel['key']}] {key}: automatic publication after {not_before.isoformat()}")
            return "waiting"
        decision = "approve"   # automatic mode: nobody pressed «Не публиковать»
    if decision is None:
        if now < deadline:
            log(f"[{channel['key']}] {key} is waiting for approval until {deadline.isoformat()}")
            return "waiting"
        draft["status"] = "expired"
        state.save_draft(draft)
        weekly = draft.get("kind") == "weekly"
        what = f"{render.esc(channel['chat_id'])}: {post_noun(draft)} за {draft['date']}"
        if draft.get("preview_mode") == "auto":
            text = (f"⏰ {what} {'не вышли' if weekly else 'не вышел'} — окно публикации закрылось в "
                    f"{admin_time(cfg, deadline)}, а запуски бота не успели {'их' if weekly else 'его'} опубликовать.")
        else:
            text = (f"⏰ {what} {'не одобрены' if weekly else 'не одобрен'} до {admin_time(cfg, deadline)} "
                    f"и {'не вышли' if weekly else 'не вышел'}.")
        tg.send_message(admin_chat_id, text, silent=True)
        return "expired"
    if decision == "skip":
        draft["status"] = "skipped"
        state.save_draft(draft)
        skipped = "пропущены" if draft.get("kind") == "weekly" else "пропущен"
        tg.send_message(admin_chat_id, f"⛔ {render.esc(channel['chat_id'])}: {post_noun(draft)} за {draft['date']} {skipped}.",
                        silent=True)
        return "skipped"

    try:
        message = tg.send_message(channel["chat_id"], draft["html"])
    except TelegramError as e:
        if not e.unknown_outcome:
            raise   # not posted: the next run tries again
        # Telegram may have posted it without answering: never post twice, ask the admin to check the channel.
        log(f"[{channel['key']}] {key}: publication not confirmed ({e})")
        data = state.telegram()
        data["decisions"].pop(key, None)
        state.save_telegram(data)
        draft.pop("preview_message_id", None)
        draft["report"] = [f"⚠️ Telegram не подтвердил публикацию ({e}). Проверьте канал: если пост уже вышел, "
                           "нажмите «Пропустить», если нет — «Опубликовать»."] + list(draft.get("report") or [])
        draft.pop("sent_at", None)   # the admin gets a fresh window to decide
        notify_admin(cfg, channel, tg, state, admin_chat_id, draft, now)
        return "waiting"
    draft.update(status="published", published_at=now.isoformat(), message_id=message["message_id"])
    state.save_draft(draft)
    state.mark_posted(channel["key"], draft.get("keys", []), day)
    username = channel["chat_id"].lstrip("@")
    tg.send_message(admin_chat_id, f"✅ Опубликовано: https://t.me/{username}/{message['message_id']}", silent=True)
    log(f"[{channel['key']}] published message {message['message_id']}")
    return "published"


def edit_posts(cfg, edits: list[dict], state, tg=None, admin_chat_id=None, now=None, log=print) -> list[str]:
    """Remove entries (e.g. repeats of earlier posts) from published posts; returns report lines.

    Each edit: {channel, date, remove: [headlines as published], hashtags: "the new hashtag line" (optional)};
    or {channel, date, kind: weekly, delete: true} to delete a post (the bot can delete its posts for 48 hours);
    replace: [{from: "...", to: "..."}] swaps exact pieces of the post's HTML (e.g. a link in the week in review).
    Without `tg` nothing is sent: a preview. Entries are found by headline, so running the same edits again
    changes nothing.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, done = [], []
    for edit in edits:
        channel = cfg["channels"].get(str(edit.get("channel")))
        if not channel:
            lines.append(f"❌ неизвестный канал: {edit.get('channel')}")
            continue
        day = dt.date.fromisoformat(str(edit.get("date")))
        where = f"{channel['chat_id']} · {day.isoformat()}"
        kind = str(edit.get("kind") or "digest")
        draft = state.draft(draft_id(channel, day, kind))
        if edit.get("delete"):
            lines.append(delete_post(channel, draft, where, state, tg, now, log))
            if lines[-1].startswith("✅"):
                done.append(lines[-1])
            continue
        if not draft or draft.get("status") != "published" or not draft.get("message_id"):
            lines.append(f"❌ {where}: опубликованного поста нет в состоянии бота (черновики хранятся 14 дней)")
            continue
        wanted = [render.same_spacing(h) for h in edit.get("remove") or []]
        present = [render.same_spacing(e["headline"]) for e in render.split_digest(draft["html"])[1]]
        if present and all(h in wanted for h in present):
            lines.append(f"❌ {where}: нельзя убрать все новости поста")
            continue
        saved = draft.get("stories")
        remaining = [s for s in saved or [] if render.same_spacing(s["headline"]) not in wanted]
        hashtag_line = " ".join(render.digest_hashtags(channel, remaining)) if saved else None
        text, removed = render.remove_entries(draft["html"], wanted, hashtag_line)
        done_before = {render.same_spacing(h) for h in removed + draft.get("removed", [])}
        missing = [h for h in wanted if h not in done_before]
        lines += [f"⚠️ {where}: нет новости «{h}»" for h in missing]
        new_tags, old_tags = render.same_spacing(edit.get("hashtags") or ""), render.split_digest(text)[2]
        if new_tags:
            allowed = {channel["rubric_tag"], channel.get("weekly_tag"), *channel["hashtags"]}
            unknown = [t for t in new_tags.split() if t not in allowed]
            if unknown or not new_tags.startswith(channel["rubric_tag"]):
                lines.append(f"❌ {where}: хэштеги не из словаря канала или без {channel['rubric_tag']} в начале: {unknown}")
                continue
        retag = bool(new_tags) and new_tags != old_tags
        if retag:
            text = text.rsplit("\n\n", 1)[0] + "\n\n" + new_tags
        replaced = 0
        for pair in edit.get("replace") or []:
            old, new = str(pair.get("from") or ""), str(pair.get("to") or "")
            if old and old in text:
                text, replaced = text.replace(old, new), replaced + 1
            elif new and new in text:
                continue   # replaced on an earlier run
            else:
                lines.append(f"⚠️ {where}: нет фрагмента «{old[:60]}»")
        if not removed and not retag and not replaced:
            if not missing:
                lines.append(f"✔️ {where}: уже исправлено")
            continue
        link = f"https://t.me/{channel['chat_id'].lstrip('@')}/{draft['message_id']}"
        left = [e["headline"] for e in render.split_digest(text)[1]]
        changes = ([f"убрать {len(removed)} — " + "; ".join(f"«{h}»" for h in removed)] if removed else []) + \
                  ([f"хэштеги: {old_tags} → {new_tags}"] if retag else []) + \
                  ([f"заменено фрагментов: {replaced}"] if replaced else [])
        if tg is None:
            lines.append(f"📝 {where} ({link}): " + ". ".join(changes)
                         + (f". Останется {len(left)}: " + "; ".join(left) if kind == "digest" else ""))
            continue
        try:
            tg.edit_message(channel["chat_id"], draft["message_id"], text)
        except TelegramError as e:
            lines.append(f"❌ {where}: Telegram не принял правку ({e})")
            continue
        draft.update(html=text, edited_at=now.isoformat(), removed=draft.get("removed", []) + removed)
        if saved:
            draft["stories"] = remaining
        state.save_draft(draft)
        done.append(link)
        lines.append(f"✅ {where} ({link}): " + ". ".join(changes))
        log(f"[{channel['key']}] edited message {draft['message_id']}: removed {len(removed)}, retagged {retag}")
    if tg is not None and admin_chat_id and done:
        try:
            tg.send_message(admin_chat_id, f"✏️ Исправлено постов: {len(done)}\n" + "\n".join(done), silent=True)
        except TelegramError:
            pass   # the posts are edited; the report is on the run page too
    return lines


def delete_post(channel, draft, where, state, tg, now, log=print) -> str:
    if draft and draft.get("deleted_at"):
        return f"✔️ {where}: пост уже удалён"
    if not draft or draft.get("status") != "published" or not draft.get("message_id"):
        return f"❌ {where}: опубликованного поста нет в состоянии бота"
    link = f"https://t.me/{channel['chat_id'].lstrip('@')}/{draft['message_id']}"
    title = render.plain_text(draft.get("html", "").split("\n")[0])
    if tg is None:
        return f"📝 {where} ({link}): удалить пост «{title}»"
    try:
        tg.delete_message(channel["chat_id"], draft["message_id"])
    except TelegramError as e:
        return f"❌ {where} ({link}): Telegram не дал удалить пост ({e}); удалите его вручную"
    # "skipped" keeps the bot from ever publishing this draft again
    draft.update(status="skipped", deleted_at=now.isoformat())
    state.save_draft(draft)
    log(f"[{channel['key']}] deleted message {draft['message_id']}")
    return f"✅ {where} ({link}): пост «{title}» удалён"


def has_phrase(message: dict | None, phrases: list[str]) -> bool:
    text = ((message or {}).get("text") or (message or {}).get("caption") or "").lower()
    return any(p.lower() in text for p in phrases if p)


def find_post(tg, chat, admin_chat_id, phrases: list[str], pinned: dict | None, limit: int = 30) -> dict | None:
    """The channel post with one of the phrases: the pinned one, or one of the posts before it.

    The Bot API cannot read a channel's history, so each earlier post is forwarded silently to the admin's chat,
    read there and deleted at once.
    """
    if has_phrase(pinned, phrases):
        return pinned
    last = min((pinned or {}).get("message_id", limit + 1), limit + 1)
    for message_id in range(1, last):
        try:
            copy = tg.forward_message(admin_chat_id, chat, message_id)
        except TelegramError:
            continue   # service messages and deleted posts cannot be forwarded
        _quietly(tg.delete_message, admin_chat_id, copy["message_id"])
        if has_phrase(copy, phrases):
            return {**copy, "message_id": message_id}
    return None


def edit_pinned(cfg, removals: dict, tg, admin_chat_id=None, apply: bool = False, log=print) -> list[str]:
    """Remove lines (promises the channel does not keep) from each channel's intro post; a preview unless `apply`.

    removals: {channel key: [phrases]}; a line containing a phrase is removed, formatting is kept. The post is
    the pinned one, or, when another post (the navigation) was pinned later, the earlier post with the phrases.
    """
    lines = []
    for key, phrases in (removals or {}).items():
        channel = cfg["channels"].get(str(key))
        if not channel:
            lines.append(f"❌ неизвестный канал: {key}")
            continue
        chat = channel["chat_id"]
        phrases = [str(p) for p in phrases or []]
        try:
            pinned = tg.pinned_message(chat)
            message = find_post(tg, chat, admin_chat_id, phrases, pinned) if admin_chat_id else pinned
        except TelegramError as e:
            lines.append(f"❌ {chat}: не удалось прочитать посты канала ({e})")
            continue
        if not message:
            lines.append(f"✔️ {chat}: поста с такими строками нет — уже исправлено")
            continue
        caption = "text" not in message and "caption" in message
        text = message.get("caption" if caption else "text") or ""
        entities = message.get("caption_entities" if caption else "entities") or []
        link = f"https://t.me/{chat.lstrip('@')}/{message['message_id']}"
        new_text, new_entities, removed = render.drop_lines(text, entities, phrases)
        if not removed:
            lines.append(f"✔️ {chat} ({link}): таких строк нет — уже исправлено")
            continue
        if not apply:
            lines.append(f"📝 {chat} ({link}): убрать " + "; ".join(f"«{r}»" for r in removed)
                         + f". Станет:\n{new_text}")
            continue
        try:
            tg.edit_formatted(chat, message["message_id"], new_text, new_entities, caption=caption)
        except TelegramError as e:
            lines.append(f"❌ {chat} ({link}): Telegram не дал изменить пост ({e}). "
                         "Вероятно, пост написан не ботом — его можно исправить только вручную.")
            continue
        lines.append(f"✅ {chat} ({link}): убрано " + "; ".join(f"«{r}»" for r in removed))
        log(f"[{key}] pinned message {message['message_id']} edited")
    return lines


def in_prepare_window(cfg: dict, now: dt.datetime) -> bool:
    """The morning window in which all drafts are built and sent together."""
    hour, minute = map(int, cfg["moderation"]["prepare_time_utc"].split(":"))
    utc = now.astimezone(dt.timezone.utc)
    elapsed = (utc.hour * 60 + utc.minute - (hour * 60 + minute)) % 1440
    return elapsed < cfg["moderation"].get("prepare_window_minutes", 75)


def before_first_day(cfg: dict, channel: dict, now: dt.datetime) -> bool:
    first = cfg["moderation"].get("first_publish_date")
    return bool(first) and publish_day(cfg, channel, now) < dt.date.fromisoformat(str(first))


def tick(cfg, llm, tg, state, admin_chat_id, now, log=print, sleep=time.sleep, max_attempts: int = 3) -> dict:
    """One wake-up of the bot at any moment: do whatever is due now. Safe to run as often as wanted.

    - always: read the admin's commands and button presses;
    - in the morning window: build and send every draft that is not with the admin yet
      (on Mondays also the week in review);
    - from each channel's publish time: publish (or expire) its drafts, the week in review first;
    - a few minutes before a publish time, with the draft ready: wait for that minute and publish exactly on time.
    """
    results = {"mode": inbox(cfg, tg, state, admin_chat_id, now, log)}
    try:
        rewritten = apply_rewrites(cfg, llm, tg, state, admin_chat_id, now, log)
        if rewritten:
            results["rewritten"] = rewritten
    except Exception as e:   # publishing must not suffer from a failed rewrite
        log(f"rewrites failed: {redact(e)[:300]}")
    keys = [key for key, channel in cfg["channels"].items() if not before_first_day(cfg, channel, now)]

    def pending(kind: str) -> list[str]:
        waiting = []
        for key in keys:
            channel = cfg["channels"][key]
            day = publish_day(cfg, channel, now)
            draft = state.draft(draft_id(channel, day, kind))
            if kind in draft_kinds(cfg, day) and (
                    not draft or (draft.get("status") in ("empty", "error") and draft.get("attempts", 0) < max_attempts)
                    or (draft.get("status") == "ready" and not draft.get("preview_message_id"))):
                waiting.append(key)
        # the channel that publishes first comes first
        return sorted(waiting, key=lambda key: publish_window(cfg, cfg["channels"][key],
                                                              publish_day(cfg, cfg["channels"][key], now))[0])

    if in_prepare_window(cfg, now):
        digests, weeklies = pending("digest"), pending("weekly")
        if digests:
            drafts = prepare_all(cfg, digests, llm, tg, state, admin_chat_id, now, log, sleep=sleep, retry=False)
            results["prepared"] = {key: draft.get("status") for key, draft in drafts.items()}
            results["undelivered"] = undelivered(drafts)
        for key in weeklies:   # after the digests: the admin sees the day's drafts first
            try:
                draft = prepare(cfg, cfg["channels"][key], llm, tg, state, admin_chat_id, now, log, kind="weekly")
            except Exception as e:   # the digest must not suffer from the week in review
                log(f"[{key}] week in review failed: {redact(e)[:300]}")
                continue
            results.setdefault("prepared", {})[result_key(key, "weekly")] = draft.get("status")
    lead = dt.timedelta(minutes=cfg["moderation"].get("publish_lead_minutes", 3))
    for key in keys:
        channel = cfg["channels"][key]
        day = publish_day(cfg, channel, now)
        start, _ = publish_window(cfg, channel, day)
        moment = now
        for kind in draft_kinds(cfg, day):
            draft = state.draft(draft_id(channel, day, kind))
            if draft and draft.get("status") in FINAL_STATUSES:
                continue
            digest = state.draft(draft_id(channel, day))
            if kind == "weekly" and digest and digest.get("status") in FINAL_STATUSES:
                continue   # the week in review goes out right before the digest, never on its own after it
            if moment < start:
                # The wake-up a minute before the publish time: GitHub needs a few seconds to start the job,
                # so the bot starts early and waits, and the post goes out in the promised minute.
                if start - moment > lead or not draft or draft.get("status") != "ready":
                    continue
                log(f"[{key}] waiting {(start - moment).total_seconds():.0f}s for the publish time {start.isoformat()}")
                sleep((start - moment).total_seconds())
                moment = start
            if moment > approval_deadline(cfg, channel, day, draft):
                if not draft or draft.get("status") != "ready":
                    continue   # the window closed: nothing to build or publish any more
            results[result_key(key, kind)] = publish(cfg, channel, llm, tg, state, admin_chat_id, moment, log, kind=kind)
    return results
