import collections
import copy
import datetime as dt
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import zoneinfo

import yaml

from newsbot import articles, config, dedupe, evaluate, feeds, metrics, pipeline, render, verify
from newsbot.config import ROOT, load_config
from newsbot.llm import LLM, FakeLLM, LLMError, usage_summary
from newsbot.state import State
from newsbot.telegram import Telegram, TelegramError

CFG = load_config()
RU = CFG["channels"]["ru"]
MANUAL = copy.deepcopy(CFG)
MANUAL["moderation"]["mode"] = "manual"   # approval-only flows
QUIET = lambda *_: None  # noqa: E731


def candidate(cid="c1", title="Chime to buy Stride Bank for $590 million", summary="Deal expected to close in H1 2027.",
              link="https://example.com/chime?utm_source=x", source="Example"):
    return {"id": cid, "title": title, "summary": summary, "link": link, "source": source,
            "url_key": feeds.canonical_url(link), "member_keys": [feeds.canonical_url(link)],
            "published": "2026-09-16T05:00:00+00:00", "also_reported_by": []}


class FeedsTest(unittest.TestCase):
    def test_opml_has_all_feeds(self):
        parsed = feeds.parse_opml(ROOT / CFG["feeds_file"])
        self.assertEqual(len(parsed), 54)
        names = {f["name"] for f in parsed}
        self.assertFalse(names & {"Not Boring", "Lenny's Newsletter", "r/ProductManagement"})
        self.assertTrue({"Fintech News Hong Kong", "未央网", "TechCabal", "LatamList", "Wamda"} <= names)
        self.assertTrue(all(f["folder"] in CFG["source_weights"] for f in parsed))

    def test_canonical_url_strips_tracking(self):
        self.assertEqual(feeds.canonical_url("https://Example.com/a/?utm_source=x&id=5#top"),
                         "https://example.com/a?id=5")

    def test_clean_link_keeps_path_and_drops_tracking(self):
        self.assertEqual(feeds.clean_link("https://www.Finextra.com/NewsArticle/123?utm_medium=rss&id=7#x"),
                         "https://www.Finextra.com/NewsArticle/123?id=7")

    def test_clean_text(self):
        self.assertEqual(feeds.clean_text("<p>Hello&nbsp;<b>world</b></p>"), "Hello world")


class DedupeTest(unittest.TestCase):
    def test_similar_titles_cluster(self):
        base = {"summary": "", "source": "A", "folder": "A-Daily", "published": "2026-09-16T05:00:00+00:00"}
        items = [dict(base, title="Chime to acquire Stride Bank for $590 million", link="https://a.com/1", url_key="a1"),
                 dict(base, title="Chime to acquire partner Stride Bank for $590 million", link="https://b.com/2",
                      url_key="b2", source="B"),
                 dict(base, title="Nasdaq invests $100 million in Kraken parent Payward", link="https://c.com/3", url_key="c3")]
        self.assertEqual(len(dedupe.cluster(items)), 2)

    def test_events_are_excluded_and_sources_interleaved(self):
        now = dt.datetime(2026, 9, 16, 6, tzinfo=dt.timezone.utc)
        base = {"summary": "", "folder": "A-Daily", "published": "2026-09-16T05:00:00+00:00"}
        items = [dict(base, title="How acquirers commercialise fraud intelligence", source="Finextra",
                      link="https://www.finextra.com/event-info/640/x", url_key="e1")]
        items += [dict(base, title=f"Finextra story number {n} about payments topic{n}", source="Finextra",
                       link=f"https://www.finextra.com/news/{n}", url_key=f"f{n}") for n in range(10)]
        items += [dict(base, title="Chime agrees to acquire Stride Bank", source="Banking Dive",
                       link="https://www.bankingdive.com/news/1", url_key="b1", folder="C-Media")]
        candidates = dedupe.select_candidates(items, CFG, set(), now)
        links = [c["link"] for c in candidates]
        self.assertNotIn("https://www.finextra.com/event-info/640/x", links)
        self.assertLessEqual(sum("finextra" in l for l in links), CFG["max_per_source"])
        self.assertIn("https://www.bankingdive.com/news/1", links[:3])

    def test_posted_items_are_excluded(self):
        now = dt.datetime(2026, 9, 16, 6, tzinfo=dt.timezone.utc)
        item = {"title": "Story", "summary": "", "link": "https://a.com/1", "url_key": "a1", "source": "A",
                "folder": "A-Daily", "published": "2026-09-16T05:00:00+00:00"}
        self.assertEqual(dedupe.select_candidates([item], CFG, {"a1"}, now), [])

    def test_slug_title(self):
        self.assertEqual(dedupe.slug_title("https://www.finextra.com/newsarticle/48481/bitget-hit-by-3875-million-hack"),
                         "bitget hit by 3875 million hack")
        self.assertEqual(dedupe.slug_title("https://www.weiyangx.com/480499.html"), "")

    def test_story_published_lately_under_the_same_title_is_left_out(self):
        now = dt.datetime(2026, 9, 26, 6, tzinfo=dt.timezone.utc)
        base = {"summary": "", "folder": "A-Daily", "published": "2026-09-26T05:00:00+00:00"}
        items = [dict(base, title="Bitget hit by $387.5 million hack", source="Finextra",
                      link="https://www.finextra.com/newsarticle/1/bitget", url_key="f1"),
                 dict(base, title="Bitget hit by $387.5 million hack, users compensated", source="PYMNTS",
                      link="https://www.pymnts.com/bitget", url_key="p1"),
                 dict(base, title="Visa agrees to buy BioCatch", source="Finextra",
                      link="https://www.finextra.com/newsarticle/2/visa", url_key="f2")]
        candidates = dedupe.select_candidates(items, CFG, set(), now, recent_titles=["bitget hit by 3875 million hack"])
        self.assertEqual([c["title"] for c in candidates], ["Visa agrees to buy BioCatch"])
        self.assertEqual(len(dedupe.select_candidates(items, CFG, set(), now, recent_titles=["Visa earnings beat"])), 2)

    def test_channel_can_boost_a_region(self):
        now = dt.datetime(2026, 9, 16, 6, tzinfo=dt.timezone.utc)
        base = {"summary": "", "published": "2026-09-16T05:00:00+00:00"}
        items = [dict(base, title="US bank launches new card product", source="US Media", folder="C-Media",
                      link="https://us.example/1", url_key="u1"),
                 dict(base, title="Singapore regulator licenses digital payment token firm", source="Asia Media",
                      folder="H-Asia", link="https://asia.example/1", url_key="a1")]
        cfg = copy.deepcopy(CFG)
        cfg["source_weights"].update({"C-Media": 0.9, "H-Asia": 0.8})
        self.assertEqual(dedupe.select_candidates(items, cfg, set(), now)[0]["source"], "US Media")
        zh = dict(cfg["channels"]["zh"], source_weights={"H-Asia": 1.2})
        self.assertEqual(dedupe.select_candidates(items, cfg, set(), now, zh)[0]["source"], "Asia Media")


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.cands = {"c1": candidate(), "c2": candidate("c2", title="Chime shares rise 7% after bank deal",
                                                         link="https://example.com/shares", source="Other"),
                      "c3": candidate("c3", link="https://example.com/gone")}
        self.story = {"index": 0, "candidate_ids": ["c1", "c2"]}

    def entry(self, **overrides):
        base = {"index": 0, "source_ids": ["c1"], "headline": "Chime покупает Stride", "summary": "Сделка $590M.",
                "why": "…", "source_quotes": ["$590 million"], "hashtags": ["#необанки", "#выдуманный"]}
        base.update(overrides)
        return base

    def test_missing_quotes(self):
        self.assertEqual(verify.missing_quotes(["$590 million"], "Chime to buy Stride for $590  million"), [])
        self.assertEqual(verify.missing_quotes(["$600 million"], "Chime to buy Stride for $590 million"), ["$600 million"])

    def test_hashtags_only_from_vocabulary(self):
        tags = verify.filter_hashtags(["платежи", "#США", "#выдуманный", "#Платежи"], RU["hashtags"])
        self.assertEqual(tags, ["#платежи", "#США"])

    def test_passing_entry_gets_sources_and_clean_tags(self):
        checked, problem, _ = verify.check_entry(self.entry(), self.story, self.cands, {}, RU["hashtags"])
        self.assertIsNone(problem)
        self.assertEqual([s["id"] for s in checked["sources"]], ["c1"])
        self.assertEqual(checked["hashtags"], ["#необанки"])

    def test_quotes_may_come_from_any_cited_source(self):
        merged = self.entry(source_ids=["c1", "c2"], source_quotes=["$590 million", "7%"])
        checked, problem, _ = verify.check_entry(merged, self.story, self.cands, {}, RU["hashtags"])
        self.assertIsNone(problem)
        self.assertEqual(len(checked["sources"]), 2)
        only_first = self.entry(source_ids=["c1"], source_quotes=["7%"])
        checked, problem, repairable = verify.check_entry(only_first, self.story, self.cands, {}, RU["hashtags"])
        self.assertIsNone(checked)
        self.assertTrue(repairable)

    def test_sources_outside_the_story_and_broken_links(self):
        _, problem, repairable = verify.check_entry(self.entry(source_ids=["c3"]), self.story, self.cands, {}, RU["hashtags"])
        self.assertIn("не из этой новости", problem)
        self.assertTrue(repairable)
        story = {"index": 1, "candidate_ids": ["c3"]}
        pages = {"https://example.com/gone": {"status": 404, "text": ""}}
        _, problem, repairable = verify.check_entry(self.entry(source_ids=["c3"], source_quotes=[]), story,
                                                    self.cands, pages, RU["hashtags"])
        self.assertIn("не открываются", problem)
        self.assertFalse(repairable)


class RenderTest(unittest.TestCase):
    def entry(self, n=1, sources=None):
        return {"headline": f"Chime <покупает> Stride {n}", "summary": "Сделка & условия.", "why": "Важно.",
                "hashtags": ["#необанки", "#США"],
                "sources": sources or [candidate(link=f"https://example.com/{n}?a=1&b=2")]}

    def test_escaping_and_structure(self):
        text = render.render_digest(RU, dt.date(2026, 9, 17), [self.entry()])
        self.assertIn("<b>☀️ Финтех Daily · 17 сентября</b>", text)
        self.assertIn("&lt;покупает&gt;", text)
        self.assertIn('href="https://example.com/1?a=1&amp;b=2"', text)
        self.assertTrue(text.rstrip().endswith("#главное #необанки #США"))

    def test_several_sources_in_one_entry(self):
        sources = [candidate(link="https://pymnts.com/a", source="PYMNTS"),
                   candidate("c2", link="https://crowdfundinsider.com/b", source="Crowdfund Insider")]
        text = render.render_digest(RU, dt.date(2026, 9, 17), [self.entry(sources=sources)])
        self.assertIn('🔗 <a href="https://pymnts.com/a">PYMNTS</a> · <a href="https://crowdfundinsider.com/b">Crowdfund Insider</a>', text)

    def test_dates_per_language(self):
        day = dt.date(2026, 9, 17)
        self.assertEqual(render.format_date("en", day), "September 17")
        self.assertEqual(render.format_date("zh", day), "9月17日")

    def test_fit_digest_respects_limit(self):
        long_entry = self.entry()
        long_entry["summary"] = "x" * 1500
        entries = [dict(long_entry) for _ in range(7)]
        text, kept = render.fit_digest(RU, dt.date(2026, 9, 17), entries, 3)
        self.assertLessEqual(len(kept), 7)
        self.assertTrue(render.visible_length(text) <= render.TELEGRAM_LIMIT or len(kept) == 3)

    def test_split_digest_reads_back_entries(self):
        text = render.render_digest(RU, dt.date(2026, 9, 17), [self.entry(1), self.entry(2)])
        head, entries, tail = render.split_digest(text)
        self.assertEqual(head, "<b>☀️ Финтех Daily · 17 сентября</b>")
        self.assertEqual([e["headline"] for e in entries], ["Chime <покупает> Stride 1", "Chime <покупает> Stride 2"])
        self.assertEqual(entries[1]["links"], ["https://example.com/2?a=1&b=2"])
        self.assertEqual(tail, "#главное #необанки #США")

    def test_remove_entries_renumbers_and_never_empties(self):
        text = render.render_digest(RU, dt.date(2026, 9, 17), [self.entry(n) for n in (1, 2, 3)])
        edited, removed = render.remove_entries(text, ["Chime <покупает> Stride 2"])
        self.assertEqual(removed, ["Chime <покупает> Stride 2"])
        self.assertIn("<b>1. Chime &lt;покупает&gt; Stride 1</b>", edited)
        self.assertIn("<b>2. Chime &lt;покупает&gt; Stride 3</b>", edited)
        self.assertNotIn("Stride 2", edited)
        self.assertTrue(edited.endswith("#главное #необанки #США"))
        self.assertEqual(render.remove_entries(edited, ["Chime <покупает> Stride 2"]), (edited, []))
        everything = [f"Chime <покупает> Stride {n}" for n in (1, 3)]
        self.assertEqual(render.remove_entries(edited, everything), (edited, []))

    def test_navigation_lists_every_tag(self):
        for ch in CFG["channels"].values():
            nav = render.render_navigation(ch)
            for tag in ch["hashtags"]:
                self.assertIn(tag, nav)
            self.assertLessEqual(render.visible_length(nav), render.TELEGRAM_LIMIT)


class ScriptedLLM:
    """Replays the real cases from the first preview: a merged CLARITY story, a Tarabut 'why' error,
    a wrong number that can be repaired, a broken link and an off-topic airline story."""

    last_model = "scripted"

    def __init__(self):
        self.calls = []

    def json_completion(self, system, user, schema, schema_name):
        payload = json.loads(user[user.index("{"):])
        self.calls.append(schema_name)
        if schema_name == "selection":
            ids = {c["title"].split()[0]: c["id"] for c in payload["candidates"]}
            self.selected_ids = ids
            return {"stories": [
                {"candidate_ids": [ids["Senate"], ids["Lawmakers"], ids["Coinbase"]], "reason": "crypto regulation"},
                {"candidate_ids": [ids["Tarabut"]], "reason": "open banking round"},
                {"candidate_ids": [ids["Grab"]], "reason": "BNPL M&A"},
                {"candidate_ids": [ids["Tabby"]], "reason": "funding"},
                {"candidate_ids": [ids["Lost"]], "reason": "broken link"},
            ]}
        if schema_name == "digest":
            by_title = {s["sources"][0]["title"].split()[0]: s for s in payload["stories"]}
            self.stories = by_title
            item = lambda word, **kw: {"index": by_title[word]["index"],  # noqa: E731
                                       "source_ids": [s["id"] for s in by_title[word]["sources"]][:1],
                                       "headline": f"{word} headline", "summary": f"{word} summary.", "why": "Важно.",
                                       "source_quotes": [], "hashtags": ["#сделки"], **kw}
            items = [
                item("Senate", source_ids=[s["id"] for s in by_title["Senate"]["sources"]], source_quotes=["10%"]),
                item("Tarabut", why="Tarabut поддерживает регулятор SAMA.", source_quotes=["$50 million"]),
                item("Grab", source_quotes=["$1.49 billion"]),
            ]
            items += [item("Tabby", source_quotes=["$9 billion"])] if "Tabby" in by_title else []
            return {"items": items + [item("Lost", source_quotes=[])]}
        if schema_name == "verdicts":
            if self.calls.count("verdicts") == 1:
                return {"verdicts": [
                    {"index": e["index"], "ok": not e["why"].startswith("Tarabut"),
                     "issues": ["Источник говорит, что нужно одобрение SAMA, а не поддержка"] if e["why"].startswith("Tarabut") else []}
                    for e in payload["entries"]]}
            return {"verdicts": [{"index": e["index"], "ok": True, "issues": []} for e in payload["entries"]]}
        if schema_name == "repair":
            fixed = []
            for entry in payload["entries"]:
                entry = {k: entry[k] for k in pipeline.ENTRY_FIELDS}
                if entry["headline"].startswith("Tarabut"):
                    entry["why"] = "Раунд ожидает одобрения регулятора."
                if entry["headline"].startswith("Tabby"):
                    entry["source_quotes"] = ["$233 million"]
                fixed.append(entry)
            return {"items": fixed}
        raise AssertionError(schema_name)


class StringlyLLM(ScriptedLLM):
    """The same answers with indexes and booleans as strings, as the json_object fallback mode may return."""

    def json_completion(self, system, user, schema, schema_name):
        result = super().json_completion(system, user, schema, schema_name)
        for item in result.get("items", []) + result.get("verdicts", []):
            item["index"] = str(item["index"])
            if "ok" in item:
                item["ok"] = str(item["ok"]).lower()
        return result


class FallbackLLM(ScriptedLLM):
    """Answers as if the preferred models were unavailable and the last fallback did the work."""

    def __init__(self):
        super().__init__()
        self.models_used, self.step_log = [], []
        self.notes = ["stale note from another channel"]

    def preferred(self, step):
        return CFG["llm"]["steps"][step][0]

    def json_completion(self, system, user, schema, schema_name):
        self.notes.append(f"{self.preferred(schema_name)}/json_schema: HTTP 429 quota exceeded")
        fallback = CFG["llm"]["steps"][schema_name][-1]
        self.models_used.append(fallback)
        self.step_log.append((schema_name, fallback))
        return super().json_completion(system, user, schema, schema_name)


class RejectAllLLM(ScriptedLLM):
    """Verifier rejects everything with the string "false": nothing may be published."""

    def json_completion(self, system, user, schema, schema_name):
        result = super().json_completion(system, user, schema, schema_name)
        for verdict in result.get("verdicts", []):
            verdict["ok"] = "false"
        return result


class RepeatLLM(ScriptedLLM):
    """The selector recognises Tabby as a story the channel published the day before."""

    def json_completion(self, system, user, schema, schema_name):
        result = super().json_completion(system, user, schema, schema_name)
        if schema_name == "selection":
            self.recent = json.loads(user[user.index("{"):])["recently_published"]
            for story in result["stories"]:
                story["repeat_of"] = "p1" if story["candidate_ids"] == [self.selected_ids["Tabby"]] else ""
        return result


class VerifierRepeatLLM(ScriptedLLM):
    """The selector misses the repeat (as Flash Lite did on 2026-10-02); the fact-checker catches it."""

    def json_completion(self, system, user, schema, schema_name):
        result = super().json_completion(system, user, schema, schema_name)
        if schema_name == "verdicts":
            payload = json.loads(user[user.index("{"):])
            self.recent_seen = payload["recently_published"]
            for verdict, entry in zip(result["verdicts"], payload["entries"]):
                verdict["repeat_of"] = "p1" if entry["headline"].startswith("Tabby") else ""
        return result


class BuildDigestTest(unittest.TestCase):
    def test_full_flow_merges_repairs_and_drops(self):
        self.check_full_flow(ScriptedLLM())

    def test_string_indexes_and_booleans_from_fallback_mode(self):
        self.check_full_flow(StringlyLLM())

    def test_string_false_verdicts_block_publication(self):
        draft = self.build(RejectAllLLM())
        self.assertEqual(draft["status"], "empty")
        self.assertNotIn("html", draft)

    def test_fallback_model_is_reported(self):
        draft = self.build(FallbackLLM())
        lite = CFG["llm"]["steps"]["digest"][-1]
        self.assertEqual(draft["model"], lite)
        report = "\n".join(draft["report"])
        self.assertIn(f"⚠️ Часть работы сделала запасная модель: отбор — {lite}, написание — {lite}", report)
        self.assertIn(f"причины: {CFG['llm']['steps']['selection'][0]}: HTTP 429 quota exceeded; "
                      f"{CFG['llm']['steps']['digest'][0]}: HTTP 429 quota exceeded", report)
        self.assertNotIn("stale note", report)

    def check_full_flow(self, llm):
        draft = self.build(llm)
        self.assertEqual(llm.calls, ["selection", "digest", "verdicts", "repair", "verdicts"])
        self.assertEqual(draft["status"], "ready")
        html = draft["html"]
        self.assertIn('<a href="https://pymnts.com/clarity">PYMNTS</a> · <a href="https://crowdfundinsider.com/coinbase">Crowdfund Insider</a>', html)
        self.assertIn("Раунд ожидает одобрения регулятора.", html)
        self.assertNotIn("поддерживает регулятор", html)
        self.assertIn("Tabby headline", html)
        self.assertNotIn("Lost headline", html)
        self.assertNotIn("ryanair", html.lower())
        self.assertEqual(html.count("<b>") - 1, 4)
        report = "\n".join(draft["report"])
        self.assertEqual(report.count("🔧"), 2)
        self.assertIn("❌ «Lost headline»: ссылки на источники не открываются", report)
        self.assertIn("https://crowdfundinsider.com/coinbase", draft["keys"])
        self.assertNotIn("pymnts.com/lawmakers", html)              # one link per outlet in a post
        self.assertIn("https://pymnts.com/lawmakers", draft["keys"])  # but the article still counts as posted

    def test_repeat_of_a_published_story_is_dropped(self):
        def publish_yesterday(state):   # a post from before stories were stored: read back from its html
            entry = {"headline": "Tabby привлекла $233 млн", "summary": "…", "why": "…", "hashtags": [],
                     "sources": [candidate(link="https://other.example/tabby-raises-233-million-series-f")]}
            state.save_draft({"id": "ru-20260915", "channel": "ru", "date": "2026-09-15", "status": "published",
                              "message_id": 5, "html": render.render_digest(RU, dt.date(2026, 9, 15), [entry])})

        llm = RepeatLLM()
        draft = self.build(llm, publish_yesterday)
        self.assertEqual(llm.recent, [{"id": "p1", "date": "2026-09-15", "headline": "Tabby привлекла $233 млн",
                                       "sources": ["https://other.example/tabby-raises-233-million-series-f"]}])
        self.assertEqual(draft["status"], "ready")
        self.assertNotIn("Tabby headline", draft["html"])
        self.assertIn("🔁 «Tabby raises $233 million at $6.5 billion valuation»: уже было 2026-09-15", "\n".join(draft["report"]))
        self.assertEqual([s["headline"] for s in draft["stories"]],
                         ["Senate headline", "Tarabut headline", "Grab headline"])
        self.assertEqual(draft["stories"][0]["titles"][:2], ["Senate votes against CLARITY Act market structure bill",
                                                             "Lawmakers weigh next steps for crypto market rules"])

    def test_fact_check_drops_a_repeat_the_selector_missed(self):
        def publish_yesterday(state):
            entry = {"headline": "Tabby привлекла $233 млн", "summary": "…", "why": "…", "hashtags": [],
                     "sources": [candidate(link="https://other.example/tabby-raises-233-million-series-f")]}
            state.save_draft({"id": "ru-20260915", "channel": "ru", "date": "2026-09-15", "status": "published",
                              "message_id": 5, "html": render.render_digest(RU, dt.date(2026, 9, 15), [entry])})

        llm = VerifierRepeatLLM()
        draft = self.build(llm, publish_yesterday)
        self.assertEqual(llm.recent_seen[0]["headline"], "Tabby привлекла $233 млн")
        self.assertNotIn("Tabby headline", draft["html"])
        self.assertIn("🔁 «Tabby headline»: уже было 2026-09-15", "\n".join(draft["report"]))

    def build(self, llm, setup=None):
        pipeline.FEED_CACHE.clear()
        now = dt.datetime(2026, 9, 16, 4, 55, tzinfo=dt.timezone.utc)
        published = (now - dt.timedelta(hours=2)).isoformat()

        def item(title, source, link, summary=""):
            return {"title": title, "summary": summary, "link": link, "url_key": feeds.canonical_url(link),
                    "published": published, "source": source, "folder": "C-Media"}

        items = [
            item("Senate votes against CLARITY Act market structure bill", "PYMNTS", "https://pymnts.com/clarity"),
            item("Coinbase shares fall over 10% after Senate vote", "Crowdfund Insider", "https://crowdfundinsider.com/coinbase"),
            item("Lawmakers weigh next steps for crypto market rules", "PYMNTS", "https://pymnts.com/lawmakers"),
            item("Tarabut raises $50 million pending SAMA approval", "Wamda", "https://wamda.com/tarabut"),
            item("Grab to buy 60% of Atome for $1.49 billion", "Fintech News Singapore", "https://fintechnews.sg/grab"),
            item("Tabby raises $233 million at $6.5 billion valuation", "Wamda", "https://wamda.com/tabby"),
            item("Lost story with a dead link", "Example", "https://example.com/gone"),
            item("Ryanair AI resolves 80% of customer chats", "PYMNTS", "https://pymnts.com/ryanair"),
        ]
        pages = {i["link"]: {"status": 200, "text": ""} for i in items}
        pages["https://example.com/gone"] = {"status": 404, "text": ""}

        originals = (feeds.parse_opml, feeds.collect, articles.fetch_many)
        feeds.parse_opml = lambda path: [{"folder": "C-Media", "name": "x", "url": "https://x"}]
        feeds.collect = lambda feed_list, hours, now: (items, [])
        articles.fetch_many = lambda links, limit: {link: pages[link] for link in links}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state = State(pathlib.Path(tmp))
                if setup:
                    setup(state)
                return pipeline.build_digest(CFG, RU, llm, state, now, log=QUIET)
        finally:
            feeds.parse_opml, feeds.collect, articles.fetch_many = originals


class ConfigTest(unittest.TestCase):
    def test_workflow_wakes_the_bot_with_tick(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/newsbot.yml").read_text(encoding="utf-8"))
        triggers = workflow.get("on") or workflow.get(True)  # PyYAML reads the key `on` as True
        self.assertNotIn("schedule", triggers)   # the public copy never runs on its own
        self.assertIn("tick", triggers["workflow_dispatch"]["inputs"]["command"]["options"])
        run_bot = next(step for step in workflow["jobs"]["run"]["steps"] if step.get("name") == "Run bot")
        self.assertIn("python -m newsbot tick", run_bot["run"])
    def test_publishing_mode_is_configured(self):
        self.assertIn(CFG["moderation"]["mode"], pipeline.MODES)
        self.assertGreater(CFG["moderation"]["auto_veto_minutes"], 0)

    def test_prompts_have_no_unfilled_placeholders(self):
        for name in ("selector", "editor", "repair", "verifier", "weekly"):
            for ch in CFG["channels"].values():
                self.assertNotIn("{{", pipeline.prompt(name, CFG, ch), f"{name}/{ch['key']}")

    def test_hashtags_are_valid_telegram_tags(self):
        for ch in CFG["channels"].values():
            self.assertIn(ch["rubric_tag"], ["#главное", "#brief", "#要闻"])
            for tag in ch["hashtags"]:
                self.assertRegex(tag, r"^#\w+$")

    def test_wake_schedule_covers_drafts_and_every_publish_window(self):
        moderation, utc = CFG["moderation"], dt.timezone.utc
        wakes = sorted(dt.datetime(2026, 9, 15, tzinfo=utc) + dt.timedelta(days=d, hours=h, minutes=m)
                       for d in (0, 1) for h in moderation["wake_hours_utc"] for m in moderation["wake_minutes"])
        day = dt.date(2026, 9, 16)
        morning = [w for w in wakes if pipeline.in_prepare_window(CFG, w) and
                   dt.datetime(2026, 9, 15, 22, tzinfo=utc) < w < dt.datetime(2026, 9, 16, 2, tzinfo=utc)]
        self.assertGreaterEqual(len(morning), 2, "at least two chances to build the drafts")
        for ch in CFG["channels"].values():
            start, deadline = pipeline.publish_window(CFG, ch, day)
            self.assertFalse(pipeline.in_prepare_window(CFG, start), f"{ch['key']}: drafts still being built at publish time")
            for moment in morning:   # every morning wake-up builds the drafts for this day
                self.assertEqual(pipeline.publish_day(CFG, ch, moment), day, ch["key"])
            window = [w for w in wakes if start <= w <= deadline]
            self.assertGreaterEqual(len(window), 3, f"{ch['key']}: too few wake-ups while the post may go out")
            self.assertLessEqual(window[0] - start, dt.timedelta(minutes=15), f"{ch['key']}: first wake-up is late")
            self.assertTrue(any(deadline < w <= deadline + dt.timedelta(minutes=90) for w in wakes),
                            f"{ch['key']}: nothing wakes up to close the window")

    def test_a_wake_up_comes_just_before_every_publish_time(self):
        moderation = CFG["moderation"]
        lead = dt.timedelta(minutes=moderation["publish_lead_minutes"])
        for day in (dt.date(2026, 1, 15), dt.date(2026, 7, 15)):   # winter and summer time alike
            wakes = [dt.datetime.combine(day, dt.time.fromisoformat(t), dt.timezone.utc) for t in moderation["publish_wake_utc"]]
            for ch in CFG["channels"].values():
                start, _ = pipeline.publish_window(CFG, ch, day)
                self.assertTrue(any(dt.timedelta(seconds=30) <= start - w <= lead for w in wakes),
                                f"{ch['key']}: no wake-up shortly before {start.isoformat()}")

class FakeTelegram:
    def __init__(self, updates=None, callbacks_expire=False):
        self.sent, self.updates, self.buttons = [], list(updates or []), []
        self.callbacks_expire = callbacks_expire

    def send_message(self, chat_id, text, buttons=None, silent=False, force_reply=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "force_reply": force_reply})
        return {"message_id": len(self.sent)}

    def get_updates(self, offset=None):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def answer_callback(self, callback_id, text):
        if self.callbacks_expire:
            raise RuntimeError("query is too old and response timeout expired")

    def set_buttons(self, chat_id, message_id, buttons):
        self.buttons.append(buttons)

    def channel_posts(self):
        return [m for m in self.sent if m["chat_id"] == RU["chat_id"]]


class EditingTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.edits = []

    def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


class RepeatsTest(unittest.TestCase):
    def post(self, state, day, headlines, status="published", with_stories=False, tags=None):
        entries = [{"headline": h, "summary": "…", "why": "…", "hashtags": (tags or {}).get(h, []),
                    "sources": [candidate(link=f"https://example.com/{n}-story-about-{h.split()[0].lower()}-news")]}
                   for n, h in enumerate(headlines)]
        draft = {"id": f"ru-{day:%Y%m%d}", "channel": "ru", "date": day.isoformat(), "status": status,
                 "message_id": 40 + day.day, "html": render.render_digest(RU, day, entries)}
        if with_stories:
            draft["stories"] = [{"headline": e["headline"], "hashtags": e["hashtags"], "titles": [f"{e['headline']} (en)"],
                                 "links": [e["sources"][0]["link"]]} for e in entries]
        state.save_draft(draft)
        return draft

    def test_recent_stories_cover_the_window_before_the_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            self.post(state, dt.date(2026, 9, 10), ["Too old"])
            self.post(state, dt.date(2026, 9, 20), ["Skipped post"], status="skipped")
            self.post(state, dt.date(2026, 9, 24), ["Bitget hacked"])
            self.post(state, dt.date(2026, 9, 25), ["Fed stablecoin rules"], status="ready", with_stories=True)
            self.post(state, dt.date(2026, 9, 26), ["Today"])
            recent = pipeline.recent_stories(CFG, RU, state, dt.date(2026, 9, 26))
        self.assertEqual([(r["id"], r["date"], r["headline"]) for r in recent],
                         [("p1", "2026-09-25", "Fed stablecoin rules"), ("p2", "2026-09-24", "Bitget hacked")])
        self.assertEqual(pipeline.recent_view(recent[0])["sources"], ["Fed stablecoin rules (en)"])
        self.assertEqual(pipeline.recent_view(recent[1])["sources"], ["https://example.com/0-story-about-bitget-news"])
        self.assertIn("0 story about bitget news", pipeline.recent_titles(recent))

    def test_edit_preview_apply_and_repeat(self):
        edits = [{"channel": "ru", "date": "2026-09-26", "remove": ["Bitget hacked again"]},
                 {"channel": "ru", "date": "2026-09-27", "remove": ["No such story"]}]
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            tags = {"Bitget hacked again": ["#крипто"], "Visa buys BioCatch": ["#сделки"]}
            original = self.post(state, dt.date(2026, 9, 26), ["Bitget hacked again", "Visa buys BioCatch", "SEC news"],
                                 with_stories=True, tags=tags)
            self.post(state, dt.date(2026, 9, 27), ["Monzo sale talks"])

            preview = pipeline.edit_posts(CFG, edits, state)
            self.assertIn("📝 @fintech_daily_ru · 2026-09-26 (https://t.me/fintech_daily_ru/66): убрать 1", preview[0])
            self.assertIn("⚠️ @fintech_daily_ru · 2026-09-27: нет новости «No such story»", preview)
            self.assertEqual(state.draft("ru-20260926")["html"], original["html"])

            tg = EditingTelegram()
            applied = pipeline.edit_posts(CFG, edits, state, tg, "42", log=QUIET)
            self.assertTrue(applied[0].startswith("✅"))
            chat_id, message_id, text = tg.edits[0]
            self.assertEqual((chat_id, message_id), ("@fintech_daily_ru", 66))
            self.assertIn("<b>1. Visa buys BioCatch</b>", text)
            self.assertIn("<b>2. SEC news</b>", text)
            self.assertTrue(text.endswith("#главное #сделки"))    # the removed story's tag is gone
            saved = state.draft("ru-20260926")
            self.assertEqual((saved["html"], saved["removed"]), (text, ["Bitget hacked again"]))
            self.assertEqual([s["headline"] for s in saved["stories"]], ["Visa buys BioCatch", "SEC news"])
            self.assertIn("✏️ Исправлено постов: 1", tg.sent[-1]["text"])

            again = pipeline.edit_posts(CFG, edits[:1], state, tg, "42")
            self.assertEqual(again, ["✔️ @fintech_daily_ru · 2026-09-26: уже исправлено"])
            self.assertEqual(len(tg.edits), 1)

            retag = [{"channel": "ru", "date": "2026-09-26", "remove": ["Bitget hacked again"], "hashtags": "#главное #сделки #США"}]
            self.assertIn("хэштеги: #главное #сделки → #главное #сделки #США", pipeline.edit_posts(CFG, retag, state)[0])
            pipeline.edit_posts(CFG, retag, state, tg, "42", log=QUIET)
            self.assertTrue(tg.edits[-1][2].endswith("\n\n#главное #сделки #США"))
            self.assertIn("<b>2. SEC news</b>", tg.edits[-1][2])
            self.assertEqual(pipeline.edit_posts(CFG, retag, state), ["✔️ @fintech_daily_ru · 2026-09-26: уже исправлено"])
            wrong = [{"channel": "ru", "date": "2026-09-26", "hashtags": "#главное #выдуманный"}]
            self.assertIn("хэштеги не из словаря", pipeline.edit_posts(CFG, wrong, state)[0])

            everything = [{"channel": "ru", "date": "2026-09-27", "remove": ["Monzo sale talks"]}]
            self.assertEqual(pipeline.edit_posts(CFG, everything, state, tg, "42"),
                             ["❌ @fintech_daily_ru · 2026-09-27: нельзя убрать все новости поста"])

    def test_delete_a_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.save_draft({"id": "weekly-en-20260928", "kind": "weekly", "channel": "en", "date": "2026-09-28",
                              "status": "published", "message_id": 18, "html": "<b>🗓 Week in review · September 21–27</b>\n\n…"})
            edits = [{"channel": "en", "date": "2026-09-28", "kind": "weekly", "delete": True}]
            self.assertIn("удалить пост «🗓 Week in review · September 21–27»", pipeline.edit_posts(CFG, edits, state)[0])
            tg = EditingTelegram()
            tg.deleted = []
            tg.delete_message = lambda chat_id, message_id: tg.deleted.append((chat_id, message_id))
            self.assertTrue(pipeline.edit_posts(CFG, edits, state, tg, "42", log=QUIET)[0].startswith("✅"))
            self.assertEqual(tg.deleted, [("@fintech_daily", 18)])
            self.assertEqual(state.draft("weekly-en-20260928")["status"], "skipped")
            self.assertEqual(pipeline.edit_posts(CFG, edits, state, tg, "42"), ["✔️ @fintech_daily · 2026-09-28: пост уже удалён"])

    def test_replace_a_link_in_the_week_in_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            html = '<b>🗓 Итоги недели</b>\n\n<b>Регулирование</b>\n▪️ Иск ICBA (<a href="https://t.me/fintech_daily_ru/25">04.10</a>)\n\n#итоги_недели #главное'
            state.save_draft({"id": "weekly-ru-20261005", "kind": "weekly", "channel": "ru", "date": "2026-10-05",
                              "status": "published", "message_id": 26, "html": html})
            edits = [{"channel": "ru", "date": "2026-10-05", "kind": "weekly",
                      "replace": [{"from": 'ru/25">04.10', "to": 'ru/24">03.10'}]}]
            self.assertEqual(pipeline.edit_posts(CFG, edits, state)[0],
                             "📝 @fintech_daily_ru · 2026-10-05 (https://t.me/fintech_daily_ru/26): заменено фрагментов: 1")
            tg = EditingTelegram()
            pipeline.edit_posts(CFG, edits, state, tg, "42", log=QUIET)
            self.assertIn('ru/24">03.10', tg.edits[0][2])
            self.assertEqual(pipeline.edit_posts(CFG, edits, state), ["✔️ @fintech_daily_ru · 2026-10-05: уже исправлено"])

    def test_edits_file_matches_published_headlines(self):
        edits = yaml.safe_load((ROOT / "edits.yaml").read_text(encoding="utf-8"))
        for edit in edits:
            self.assertIn(edit["channel"], CFG["channels"])
            dt.date.fromisoformat(edit["date"])
            if edit.get("delete") or edit.get("replace"):
                continue
            self.assertTrue(edit["remove"] and all(isinstance(h, str) and h for h in edit["remove"]))
            channel = CFG["channels"][edit["channel"]]
            for tag in edit.get("hashtags", channel["rubric_tag"]).split():
                self.assertIn(tag, {channel["rubric_tag"], *channel["hashtags"]})


class WeeklyLLM:
    last_model = "scripted"

    def json_completion(self, system, user, schema, schema_name):
        assert schema_name == "weekly", schema_name
        self.stories = json.loads(user[user.index("{"):])["stories"]
        ids = {s["headline"].split()[0]: s["id"] for s in self.stories}
        return {"intro": "Неделя стейблкоинов: 99 событий.", "items": [
            {"section": "top", "story_ids": [ids["Visa"]], "text": "Visa запускает расчёты в стейблкоинах"},
            {"section": "deals", "story_ids": [ids["Nubank"]], "text": "Nubank обсуждает покупку Monzo за $13 млрд"},
            {"section": "deals", "story_ids": [ids["Paymob"], ids["Nubank"]], "text": "Paymob и Nubank: сделки недели"},
            {"section": "deals", "story_ids": [ids["Nubank"]], "text": "Снова Nubank и Monzo"},
            {"section": "deals", "story_ids": [ids["Paymob"]], "text": "Paymob привлекла $50 млн"},
            {"section": "deals", "story_ids": ["s999"], "text": "Выдуманная сделка"},
            {"section": "regulation", "story_ids": [ids["ФРС"]], "text": "ФРС предложила правила для эмитентов стейблкоинов"},
            {"section": "regulation", "story_ids": [ids["SEC:"]], "text": "Хестер Пирс уходит из SEC"},
            {"section": "trends", "story_ids": [ids["ФРС"], ids["Visa"]], "text": "Стейблкоины входят в карточные сети"},
            {"section": "trends", "story_ids": [ids["ФРС"]], "text": "Тренд из одной новости"},
            {"section": "other", "story_ids": [ids["Visa"]], "text": "Раздел не из списка"},
        ]}


class WeeklyTest(unittest.TestCase):
    def publish_week(self, state):
        posts = {dt.date(2026, 9, 21): ["Paymob привлекла $35 млн", "SEC: Хестер Пирс уходит"],
                 dt.date(2026, 9, 25): ["ФРС предложила правила для стейблкоинов", "Visa запускает расчёты в стейблкоинах"],
                 dt.date(2026, 9, 27): ["Nubank обсуждает покупку Monzo за $13 млрд"] + [f"Мелкая новость {n}" for n in range(6)],
                 dt.date(2026, 9, 28): ["Сегодняшняя новость не входит в неделю"]}
        for day, headlines in posts.items():
            entries = [{"headline": h, "summary": f"Подробности: {h}.", "why": "Важно.", "hashtags": [],
                        "sources": [candidate(link=f"https://example.com/{day.day}/{n}")]} for n, h in enumerate(headlines)]
            state.save_draft({"id": f"ru-{day:%Y%m%d}", "channel": "ru", "date": day.isoformat(), "status": "published",
                              "message_id": day.day, "html": render.render_digest(RU, day, entries)})

    def test_week_in_review_is_written_from_the_weeks_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            self.publish_week(state)
            llm = WeeklyLLM()
            now = dt.datetime(2026, 9, 27, 23, 13, tzinfo=dt.timezone.utc)
            draft = pipeline.build_weekly(CFG, RU, llm, state, now, log=QUIET)
        self.assertEqual(len(llm.stories), 11)
        self.assertEqual(llm.stories[0], {"id": "s1", "date": "2026-09-21", "headline": "Paymob привлекла $35 млн",
                                          "text": "Подробности: Paymob привлекла $35 млн.\n→ Почему важно: Важно."})
        self.assertEqual((draft["id"], draft["kind"], draft["status"]), ("weekly-ru-20260928", "weekly", "ready"))
        html = draft["html"]
        self.assertTrue(html.startswith("<b>🗓 Итоги недели · 21–27 сентября</b>\n\n<b>Главное</b>\n"
                                        "▪️ Visa запускает расчёты в стейблкоинах"))
        self.assertIn("<b>Сделки и раунды</b>", html)
        self.assertIn('▪️ Nubank обсуждает покупку Monzo за $13 млрд (<a href="https://t.me/fintech_daily_ru/27">27.09</a>)', html)
        # two stories of the same post: one link
        self.assertIn('Стейблкоины входят в карточные сети (<a href="https://t.me/fintech_daily_ru/25">25.09</a>)', html)
        self.assertIn("<b>Регулирование</b>", html)
        self.assertTrue(html.endswith("#итоги_недели #главное"))
        for dropped in ("Снова Nubank", "$50 млн", "Выдуманная", "Раздел не из списка", "99 событий",
                        "сделки недели", "Тренд из одной"):
            self.assertNotIn(dropped, html)
        report = "\n".join(draft["report"])
        self.assertIn("❌ «Paymob привлекла $50 млн»: в новостях недели нет чисел ['50']", report)
        self.assertIn("❌ «Снова Nubank и Monzo»: эта новость уже есть в итогах", report)
        self.assertIn("⚠️ Вступление убрано", report)
        self.assertIn("❌ «Paymob и Nubank: сделки недели»: в одной строке несколько новостей", report)
        self.assertIn("❌ «Тренд из одной новости»: тренд опирается только на одну новость", report)

    def test_too_quiet_a_week_gives_no_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = pipeline.build_weekly(CFG, RU, WeeklyLLM(), State(pathlib.Path(tmp)),
                                          dt.datetime(2026, 9, 27, 23, 13, tzinfo=dt.timezone.utc), log=QUIET)
        self.assertEqual(draft["status"], "empty")
        self.assertIn("За неделю опубликовано только 0 новостей", draft["report"])

    def test_week_ranges(self):
        self.assertEqual(render.format_range("ru", dt.date(2026, 9, 28), dt.date(2026, 10, 4)), "28 сентября – 4 октября")
        self.assertEqual(render.format_range("en", dt.date(2026, 9, 21), dt.date(2026, 9, 27)), "September 21–27")
        self.assertEqual(render.format_range("zh", dt.date(2026, 9, 28), dt.date(2026, 10, 4)), "9月28日–10月4日")

    def test_unsupported_numbers(self):
        self.assertEqual(verify.unsupported_numbers("Сделка на $387,5 млн и 12%", "взлом на $387,5 млн", "рост 12%"), [])
        self.assertEqual(verify.unsupported_numbers("Сделка на $400 млн", "взлом на $387,5 млн"), ["400"])


class PinnedTelegram:
    def __init__(self, message, refuse=False, history=None):
        self.message, self.refuse, self.edits = message, refuse, []
        self.history, self.admin_chat, self.deleted = history or {}, [], []

    def pinned_message(self, chat_id):
        return self.message

    def forward_message(self, chat_id, from_chat_id, message_id):
        if message_id not in self.history:
            raise TelegramError("forwardMessage: Bad Request: message can't be forwarded")
        self.admin_chat.append(message_id)
        return {**self.history[message_id], "message_id": 100 + len(self.admin_chat)}

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def edit_formatted(self, chat_id, message_id, text, entities, caption=False):
        if self.refuse:
            raise TelegramError("editMessageText: Bad Request: message can't be edited")
        self.edits.append((chat_id, message_id, text, entities, caption))


class PinnedTest(unittest.TestCase):
    TEXT = ("Финтех Daily — главное за 3 минуты\n\nЧто здесь будет:\n▪️ Утренний дайджест\n"
            "▪️ Опросы читателей по выходным\n\nДля кого: продакт-менеджеры")

    def entity(self, fragment, kind="bold"):
        before = self.TEXT[:self.TEXT.index(fragment)]
        return {"type": kind, "offset": render.utf16_len(before), "length": render.utf16_len(fragment)}

    def test_drop_lines_keeps_the_formatting_of_the_rest(self):
        entities = [self.entity("Финтех Daily"), self.entity("Опросы"), self.entity("Для кого:")]
        text, shifted, removed = render.drop_lines(self.TEXT, entities, ["опросы читателей"])
        self.assertEqual(removed, ["▪️ Опросы читателей по выходным"])
        self.assertNotIn("Опросы", text)
        self.assertIn("▪️ Утренний дайджест\n\nДля кого:", text)
        self.assertEqual(len(shifted), 2)                  # the entity inside the removed line is gone
        bold = shifted[1]
        utf16 = text.encode("utf-16-le")
        self.assertEqual(utf16[2 * bold["offset"]:2 * (bold["offset"] + bold["length"])].decode("utf-16-le"), "Для кого:")

    def test_preview_apply_and_refusal(self):
        message = {"message_id": 3, "text": self.TEXT, "entities": [self.entity("Для кого:")]}
        tg = PinnedTelegram(message)
        preview = pipeline.edit_pinned(CFG, {"ru": ["Опросы читателей"]}, tg)
        self.assertTrue(preview[0].startswith("📝 @fintech_daily_ru (https://t.me/fintech_daily_ru/3): убрать «▪️ Опросы"))
        self.assertEqual(tg.edits, [])
        applied = pipeline.edit_pinned(CFG, {"ru": ["Опросы читателей"]}, tg, apply=True, log=QUIET)
        self.assertTrue(applied[0].startswith("✅"))
        chat_id, message_id, text, entities, caption = tg.edits[0]
        self.assertEqual((chat_id, message_id, caption), ("@fintech_daily_ru", 3, False))
        self.assertNotIn("Опросы", text)
        other = pipeline.edit_pinned(CFG, {"ru": ["Нет такой строки"]}, tg)
        self.assertTrue(other[0].startswith("✔️"))
        refused = pipeline.edit_pinned(CFG, {"ru": ["Опросы"]}, PinnedTelegram(message, refuse=True), apply=True, log=QUIET)
        self.assertIn("исправить только вручную", refused[0])

    def test_intro_post_is_found_before_a_later_pinned_navigation(self):
        navigation = {"message_id": 7, "text": "🧭 Навигация по каналу"}
        intro = {"text": self.TEXT, "entities": [self.entity("Для кого:")]}
        tg = PinnedTelegram(navigation, history={3: intro, 5: {"text": "Другой пост"}})
        applied = pipeline.edit_pinned(CFG, {"ru": ["Опросы читателей"]}, tg, "42", apply=True, log=QUIET)
        self.assertTrue(applied[0].startswith("✅ @fintech_daily_ru (https://t.me/fintech_daily_ru/3)"))
        self.assertEqual(tg.admin_chat, [3])                 # stops at the post it looks for
        self.assertEqual(tg.deleted, [("42", 101)])          # the forwarded copy is deleted at once
        self.assertEqual(tg.edits[0][1], 3)
        nothing = PinnedTelegram(navigation, history={5: {"text": "Другой пост"}})
        self.assertTrue(pipeline.edit_pinned(CFG, {"ru": ["Опросы"]}, nothing, "42")[0].startswith("✔️"))
        self.assertEqual(nothing.admin_chat, [5])

    def test_pinned_file_names_known_channels(self):
        removals = yaml.safe_load((ROOT / "pinned.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(removals), set(CFG["channels"]))


class BuildFailureTest(unittest.TestCase):
    def test_model_failure_alerts_once_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            tg = FakeTelegram()
            now = dt.datetime(2026, 9, 16, 4, 55, tzinfo=dt.timezone.utc)
            original = pipeline.build_digest

            def failing(*args, **kwargs):
                raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")

            pipeline.build_digest = failing
            try:
                first = pipeline.prepare(CFG, RU, None, tg, state, "42", now, log=QUIET)
                second = pipeline.publish(CFG, RU, None, tg, state, "42", now + dt.timedelta(minutes=37), log=QUIET)
            finally:
                pipeline.build_digest = original
            self.assertEqual(first["status"], "error")
            self.assertEqual(second, "empty")
            self.assertEqual(len(tg.sent), 1)
            self.assertIn("пока не собран", tg.sent[0]["text"])
            self.assertEqual(tg.channel_posts(), [])


class CommonPrepareTest(unittest.TestCase):
    def test_english_draft_prepared_at_night_is_published_the_next_morning(self):
        en = CFG["channels"]["en"]

        def fake_build(cfg, channel, llm, state, now, log=print):
            day = pipeline.publish_day(cfg, channel, now)
            return {"id": pipeline.draft_id(channel, day), "channel": channel["key"], "date": day.isoformat(),
                    "created_at": now.isoformat(), "status": "ready", "html": "<b>en</b>", "keys": [], "report": []}

        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            tg = FakeTelegram()
            original = pipeline.build_digest
            pipeline.build_digest = fake_build
            try:
                draft = pipeline.prepare(CFG, en, None, tg, state, "42",
                                         dt.datetime(2026, 9, 15, 23, 30, tzinfo=dt.timezone.utc), log=QUIET)
                self.assertEqual(draft["id"], "en-20260916")
                tg.updates.append({"update_id": 1, "callback_query": {
                    "id": "cb", "from": {"id": 42}, "data": "approve:en-20260916",
                    "message": {"chat": {"id": 42}, "message_id": draft["preview_message_id"]}}})
                early = pipeline.publish(CFG, en, None, tg, state, "42",
                                         dt.datetime(2026, 9, 16, 7, 32, tzinfo=dt.timezone.utc), log=QUIET)
            finally:
                pipeline.build_digest = original
            self.assertEqual(early, "published")
            self.assertEqual([m["chat_id"] for m in tg.sent].count(en["chat_id"]), 1)
            self.assertIn("15:30", tg.sent[0]["text"])   # publish time shown in the admin's timezone


class PrepareAllTest(unittest.TestCase):
    def test_drafts_are_built_first_retried_once_and_sent_together(self):
        now = dt.datetime(2026, 9, 15, 23, 30, tzinfo=dt.timezone.utc)
        tg = FakeTelegram()
        calls, sleeps, sent_during_build = [], [], []

        def fake_build(cfg, channel, llm, state, now, log=print):
            calls.append(channel["key"])
            sent_during_build.append(len(tg.sent))
            if channel["key"] == "ru" and calls.count("ru") == 1:
                raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")
            day = pipeline.publish_day(cfg, channel, now)
            status = "empty" if channel["key"] == "en" else "ready"
            return {"id": pipeline.draft_id(channel, day), "channel": channel["key"], "date": day.isoformat(),
                    "created_at": now.isoformat(), "status": status, "html": f"<b>{channel['key']}</b>",
                    "keys": [], "report": [] if status == "ready" else ["мало новостей"]}

        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            original = pipeline.build_digest
            pipeline.build_digest = fake_build
            try:
                drafts = pipeline.prepare_all(CFG, ["zh", "ru", "en"], None, tg, state, "42", now,
                                              log=QUIET, sleep=sleeps.append)
            finally:
                pipeline.build_digest = original
            self.assertEqual(calls, ["zh", "ru", "en", "ru", "en"])
            self.assertEqual(sent_during_build, [0, 0, 0, 0, 0])
            self.assertEqual(len(sleeps), 1)
            self.assertEqual({k: d["status"] for k, d in drafts.items()}, {"zh": "ready", "ru": "ready", "en": "empty"})
            self.assertEqual([m["text"] for m in tg.sent if m["buttons"]], ["<b>zh</b>", "<b>ru</b>"])
            self.assertEqual(sum("пока не собран" in m["text"] for m in tg.sent), 1)
            self.assertEqual(state.draft("ru-20260916")["preview_message_id"], 4)
            self.assertTrue(state.draft("en-20260916")["alerted"])


class PublishFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))
        moscow = zoneinfo.ZoneInfo("Europe/Moscow")
        self.at_publish = dt.datetime(2026, 9, 16, 8, 32, tzinfo=moscow)
        self.after_deadline = dt.datetime(2026, 9, 16, 10, 32, tzinfo=moscow)
        self.state.save_draft({"id": "ru-20260916", "channel": "ru", "date": "2026-09-16", "status": "ready",
                               "html": "<b>test</b>", "keys": ["https://example.com/a"], "report": [],
                               "preview_message_id": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, action, user_id=42, update_id=10, message_id=1):
        return {"update_id": update_id, "callback_query": {"id": "cb", "from": {"id": user_id},
                "data": f"{action}:ru-20260916", "message": {"chat": {"id": 42}, "message_id": message_id}}}

    def test_button_on_another_message_does_not_publish(self):
        tg = FakeTelegram([self.callback("approve", message_id=7)])
        self.assertEqual(self.run_publish(tg, self.at_publish), "waiting")
        self.assertEqual(tg.channel_posts(), [])
        self.assertIn("неактуален", tg.buttons[-1][0][0]["text"])

    def test_test_preview_has_no_buttons(self):
        tg = FakeTelegram()
        draft = self.state.draft("ru-20260916")
        pipeline.send_preview(tg, "42", CFG, RU, draft, test=True)
        self.assertTrue(tg.sent)
        self.assertTrue(all(m["buttons"] is None for m in tg.sent))
        self.assertIn("не публикуется", tg.sent[0]["text"])

    def run_publish(self, tg, now, cfg=MANUAL):
        return pipeline.publish(cfg, RU, None, tg, self.state, "42", now, log=QUIET)

    def test_waits_without_approval_then_expires(self):
        tg = FakeTelegram()
        self.assertEqual(self.run_publish(tg, self.at_publish), "waiting")
        self.assertEqual(tg.channel_posts(), [])
        self.assertEqual(self.run_publish(tg, self.after_deadline), "expired")
        self.assertEqual(tg.channel_posts(), [])
        self.assertIn("не одобрен", tg.sent[-1]["text"])

    def test_late_approval_within_window_publishes_once(self):
        tg = FakeTelegram()
        self.assertEqual(self.run_publish(tg, self.at_publish), "waiting")
        tg.updates.append(self.callback("approve"))
        self.assertEqual(self.run_publish(tg, self.at_publish + dt.timedelta(minutes=30)), "published")
        self.assertEqual(self.run_publish(tg, self.at_publish + dt.timedelta(minutes=60)), "published")
        self.assertEqual(len(tg.channel_posts()), 1)
        self.assertIn("https://example.com/a", self.state.posted_keys("ru"))

    def test_old_button_press_does_not_crash(self):
        tg = FakeTelegram([self.callback("approve")], callbacks_expire=True)
        self.assertEqual(self.run_publish(tg, self.at_publish), "published")

    def test_skip_button_prevents_publishing(self):
        tg = FakeTelegram([self.callback("skip")])
        self.assertEqual(self.run_publish(tg, self.at_publish), "skipped")
        self.assertEqual(tg.channel_posts(), [])

    def test_buttons_from_strangers_are_ignored(self):
        tg = FakeTelegram([self.callback("approve", user_id=999)])
        self.assertEqual(self.run_publish(tg, self.at_publish), "waiting")

    def test_auto_publish_mode_still_available(self):
        cfg = copy.deepcopy(CFG)
        cfg["moderation"]["mode"] = "auto"
        tg = FakeTelegram()
        self.assertEqual(self.run_publish(tg, self.at_publish, cfg), "published")


class SecurityTest(unittest.TestCase):
    def test_non_web_links_are_dropped_not_fetched_and_not_linked(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("TELEGRAM_BOT_TOKEN=a local file that the bot must never read\n")
        try:
            local = f"file://localhost{f.name}"
            links = [local, "javascript:alert(1)", "tg://resolve?domain=scam", "https://example.com/news"]
            xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
                   + "".join(f"<item><title>Story {n}</title><link>{link}</link>"
                             "<pubDate>Tue, 15 Sep 2026 22:00:00 GMT</pubDate></item>" for n, link in enumerate(links))
                   + "</channel></rss>")
            since = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
            items = feeds.fetch_feed({"url": xml, "name": "x", "folder": "C-Media"}, since)
            self.assertEqual([i["link"] for i in items], ["https://example.com/news"])
            self.assertEqual(articles.fetch(local), {"status": None, "text": ""})
            self.assertEqual(articles.fetch("FILE" + local[4:]), {"status": None, "text": ""})
        finally:
            os.unlink(f.name)
        entry = {"headline": "h", "summary": "s", "why": "w", "hashtags": [],
                 "sources": [candidate(link="tg://resolve?domain=scam", source="Scam")]}
        text = render.render_digest(RU, dt.date(2026, 9, 17), [entry])
        self.assertNotIn("tg://", text)
        self.assertIn("🔗 Scam", text)

    def test_secrets_are_validated_and_redacted(self):
        good = {"TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35, "ADMIN_CHAT_ID": "899601986",
                "GEMINI_API_KEY": "AIza" + "B" * 35}
        with unittest.mock.patch.dict(os.environ, good):
            self.assertEqual(config.secret("ADMIN_CHAT_ID"), "899601986")
            self.assertEqual(config.secret("TELEGRAM_BOT_TOKEN"), good["TELEGRAM_BOT_TOKEN"])
            for name, bad in [("ADMIN_CHAT_ID", '"899601986"'), ("GEMINI_API_KEY", "AIzaFIRSTPART\nSECONDPART"),
                              ("TELEGRAM_BOT_TOKEN", "123456789:AAA BBB"), ("TELEGRAM_BOT_TOKEN", "not-a-token")]:
                with unittest.mock.patch.dict(os.environ, {name: bad}):
                    with self.assertRaises(SystemExit) as raised:
                        config.secret(name)
                    self.assertNotIn(bad.strip('"').split()[0], str(raised.exception))
            leaked = (f"error at https://api.telegram.org/bot{good['TELEGRAM_BOT_TOKEN']}/getMe "
                      f"with Bearer {good['GEMINI_API_KEY']}")
            cleaned = config.redact(leaked)
            self.assertNotIn(good["TELEGRAM_BOT_TOKEN"], cleaned)
            self.assertNotIn(good["GEMINI_API_KEY"], cleaned)

    def test_malformed_secrets_never_reach_error_text(self):
        with self.assertRaises(TelegramError) as raised:
            Telegram("123456:FAKE TOKEN with space", retries=1).call("getMe")
        self.assertNotIn("FAKE TOKEN", str(raised.exception))
        key = "AIzaFAKEKEY0000first\nsecondhalfFAKE"
        llm = LLM("https://127.0.0.1:9/", key, ["m"], retries=1)
        with unittest.mock.patch("newsbot.llm.time.sleep"), unittest.mock.patch.dict(os.environ, {"GEMINI_API_KEY": key}):
            with self.assertRaises(LLMError) as raised:
                llm.json_completion("s", "u", {"type": "object"}, "x")
        self.assertNotIn("FAKEKEY0000first", str(raised.exception))
        self.assertNotIn("secondhalfFAKE", str(raised.exception))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))
        self.now = dt.datetime(2026, 9, 16, 5, 32, tzinfo=dt.timezone.utc)
        self.state.save_draft({"id": "ru-20260916", "channel": "ru", "date": "2026-09-16", "status": "ready",
                               "html": "<b>digest</b>", "keys": [], "report": ["x" * 400] * 15})

    def tearDown(self):
        self.tmp.cleanup()

    def test_undelivered_draft_is_resent_and_report_is_capped(self):
        tg = FakeTelegram()
        self.assertEqual(pipeline.publish(MANUAL, RU, None, tg, self.state, "42", self.now, log=QUIET), "waiting")
        self.assertEqual([m["text"] for m in tg.sent if m["buttons"]], ["<b>digest</b>"])
        self.assertTrue(all(render.visible_length(m["text"]) <= 4096 for m in tg.sent))
        self.assertEqual(self.state.draft("ru-20260916")["preview_message_id"], 2)

    def test_a_late_draft_still_gets_its_full_approval_window(self):
        day = dt.date(2026, 9, 16)
        start, deadline = pipeline.publish_window(CFG, RU, day)
        wait = dt.timedelta(hours=CFG["moderation"]["approval_wait_hours"])
        self.assertEqual(pipeline.approval_deadline(CFG, RU, day, {}), deadline)
        late = start + dt.timedelta(hours=1, minutes=30)   # GitHub can delay the draft run by hours
        extended = pipeline.approval_deadline(CFG, RU, day, {"sent_at": late.isoformat()})
        self.assertEqual(extended, late + wait)
        draft = self.state.draft("ru-20260916")
        draft.update(preview_message_id=1, sent_at=late.isoformat())
        self.state.save_draft(draft)
        tg = FakeTelegram()
        self.assertEqual(pipeline.publish(MANUAL, RU, None, tg, self.state, "42",
                                          deadline + dt.timedelta(minutes=5), log=QUIET), "waiting")
        self.assertEqual(pipeline.publish(MANUAL, RU, None, tg, self.state, "42",
                                          extended + dt.timedelta(minutes=5), log=QUIET), "expired")

    def test_draft_id_is_saved_even_if_the_report_message_fails(self):
        class FailingReport(FakeTelegram):
            def send_message(self, chat_id, text, buttons=None, silent=False):
                if text.startswith("<b>Отчёт проверки"):
                    raise TelegramError("sendMessage: Too Many Requests", retry_after=100)
                return super().send_message(chat_id, text, buttons, silent)

        with self.assertRaises(TelegramError):
            pipeline.notify_admin(CFG, RU, FailingReport(), self.state, "42", self.state.draft("ru-20260916"))
        self.assertEqual(self.state.draft("ru-20260916")["preview_message_id"], 2)

    def test_unconfirmed_channel_post_asks_the_admin_instead_of_posting_twice(self):
        draft = self.state.draft("ru-20260916")
        draft.update(preview_message_id=1, report=[])
        self.state.save_draft(draft)

        class LostAnswer(FakeTelegram):
            channel_attempts = 0

            def send_message(self, chat_id, text, buttons=None, silent=False):
                if chat_id == RU["chat_id"]:
                    self.channel_attempts += 1
                    raise TelegramError("sendMessage: no answer (TimeoutError)", unknown_outcome=True)
                return super().send_message(chat_id, text, buttons, silent)

        approve = {"update_id": 10, "callback_query": {"id": "cb", "from": {"id": 42}, "data": "approve:ru-20260916",
                                                       "message": {"chat": {"id": 42}, "message_id": 1}}}
        tg = LostAnswer([approve])
        self.assertEqual(pipeline.publish(MANUAL, RU, None, tg, self.state, "42", self.now, log=QUIET), "waiting")
        saved = self.state.draft("ru-20260916")
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(self.state.telegram()["decisions"], {})
        self.assertNotEqual(saved["preview_message_id"], 1)
        self.assertIn("не подтвердил публикацию", "\n".join(saved["report"]))
        later = self.now + dt.timedelta(minutes=30)
        self.assertEqual(pipeline.publish(MANUAL, RU, None, tg, self.state, "42", later, log=QUIET), "waiting")
        self.assertEqual(tg.channel_attempts, 1)


class RetryTest(unittest.TestCase):
    @staticmethod
    def answer(content='{"ok": true}'):
        return {"choices": [{"message": {"content": content}}]}

    def test_hanging_model_switches_to_the_fallback_at_once(self):
        llm = LLM("https://example.invalid/", "key", ["slow", "fast"], retries=3)
        calls = []

        def post(path, body, timeout):
            calls.append(body["model"])
            if body["model"] == "slow":
                raise TimeoutError("timed out")
            return self.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post), unittest.mock.patch("newsbot.llm.time.sleep"):
            self.assertEqual(llm.json_completion("s", "u", {"type": "object"}, "x"), {"ok": True})
        self.assertEqual(calls, ["slow", "fast"])

    def test_dropped_connection_is_retried(self):
        llm = LLM("https://example.invalid/", "key", ["m"], retries=3)
        outcomes = [ConnectionResetError("reset by peer"), self.answer('{"ok": 1}')]

        def post(path, body, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with unittest.mock.patch.object(llm, "_post", side_effect=post), unittest.mock.patch("newsbot.llm.time.sleep"):
            self.assertEqual(llm.json_completion("s", "u", {"type": "object"}, "x"), {"ok": 1})

    def test_time_budget_stops_a_slow_digest(self):
        llm = LLM("https://example.invalid/", "key", ["a", "b"], retries=3)
        llm.set_budget(0.001)
        time.sleep(0.01)
        with unittest.mock.patch.object(llm, "_post", side_effect=AssertionError("must not be called")):
            with self.assertRaises(LLMError):
                llm.json_completion("s", "u", {"type": "object"}, "x")

    def test_telegram_waits_on_429_but_never_repeats_an_unconfirmed_send(self):
        waits = []
        tg = Telegram("123456:" + "A" * 35, sleep=waits.append)
        outcomes = [TelegramError("sendMessage: Too Many Requests: retry after 3", retry_after=3), {"message_id": 7}]

        def once(method, params):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with unittest.mock.patch.object(tg, "_call_once", side_effect=once):
            self.assertEqual(tg.send_message("@channel", "hi"), {"message_id": 7})
        self.assertEqual(waits, [4])
        attempts = []

        def lost(method, params):
            attempts.append(method)
            raise TelegramError("sendMessage: no answer (TimeoutError)", unknown_outcome=True)

        with unittest.mock.patch.object(tg, "_call_once", side_effect=lost):
            with self.assertRaises(TelegramError):
                tg.send_message("@channel", "hi")
        self.assertEqual(attempts, ["sendMessage"])

    def test_overloaded_models_are_tried_again_in_a_new_round(self):
        """Google answers 503 "high demand" for every model: go round again instead of giving up."""
        seen, sleeps = [], []
        llm = LLM("https://example.invalid/", "key", ["a", "b"], rounds=3, round_delay=15, sleep=sleeps.append)

        def post(path, body, timeout):
            seen.append(body["model"])
            if len(seen) <= 2:   # the first round finds every model busy
                raise http_error(503, "This model is currently experiencing high demand.")
            return self.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            self.assertEqual(llm.json_completion("s", "u", {"type": "object"}, "x"), {"ok": True})
        self.assertEqual(seen, ["a", "b", "a"])   # busy models are skipped at once, not retried in place
        self.assertEqual(sleeps, [15])            # one pause between the rounds

    def test_patient_step_waits_for_the_main_models_before_the_last_resort(self):
        seen, sleeps = [], []
        llm = LLM("https://example.invalid/", "key", ["a", "b", "lite"], rounds=3, round_delay=20, sleep=sleeps.append,
                  last_resort=["lite"], patient_steps=["selection"])

        def post(path, body, timeout):
            seen.append(body["model"])
            if body["model"] != "lite":
                raise http_error(503, "This model is currently experiencing high demand.")
            return self.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            self.assertEqual(llm.json_completion("s", "u", {"type": "object"}, "selection"), {"ok": True})
        self.assertEqual(seen, ["a", "b", "a", "b", "a", "b", "lite"])   # lite only in the last round
        self.assertEqual(sleeps, [20, 40])
        seen.clear()
        with unittest.mock.patch.object(llm, "_post", side_effect=post):   # other steps take lite at once
            llm.json_completion("s", "u", {"type": "object"}, "digest")
        self.assertEqual(seen, ["a", "b", "lite"])

    def test_patient_step_goes_to_the_last_resort_when_waiting_is_pointless(self):
        seen, sleeps = [], []
        llm = LLM("https://example.invalid/", "key", ["a", "lite"], rounds=4, round_delay=20, sleep=sleeps.append,
                  last_resort=["lite"], patient_steps=["selection"])

        def post(path, body, timeout):
            seen.append(body["model"])
            if body["model"] == "a":
                raise http_error(404, "model not found for this key")
            return self.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            self.assertEqual(llm.json_completion("s", "u", {"type": "object"}, "selection"), {"ok": True})
        self.assertEqual((seen, sleeps), (["a", "a", "lite"], []))   # a permanent error: no waiting

    def test_permanent_errors_do_not_start_a_new_round(self):
        seen, sleeps = [], []
        llm = LLM("https://example.invalid/", "key", ["a", "b"], rounds=3, round_delay=15, sleep=sleeps.append)

        def post(path, body, timeout):
            seen.append(body["model"])
            raise http_error(404, "model not found for this key")

        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            with self.assertRaises(LLMError):
                llm.json_completion("s", "u", {"type": "object"}, "x")
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(sleeps, [])

    def test_publish_rebuilds_a_missing_draft_only_once(self):
        builds = []

        def empty_build(cfg, channel, llm, state, now, log=print):
            builds.append(now)
            day = pipeline.publish_day(cfg, channel, now)
            return {"id": pipeline.draft_id(channel, day), "channel": channel["key"], "date": day.isoformat(),
                    "created_at": now.isoformat(), "status": "empty", "report": ["мало новостей"]}

        with tempfile.TemporaryDirectory() as tmp:
            state, tg = State(pathlib.Path(tmp)), FakeTelegram()
            start = dt.datetime(2026, 9, 16, 5, 32, tzinfo=dt.timezone.utc)
            with unittest.mock.patch.object(pipeline, "build_digest", side_effect=empty_build):
                for minutes in (0, 30, 60):
                    moment = start + dt.timedelta(minutes=minutes)
                    self.assertEqual(pipeline.publish(CFG, RU, None, tg, state, "42", moment, log=QUIET), "empty")
        self.assertEqual(len(builds), 1)
        self.assertEqual(sum("пока не собран" in m["text"] for m in tg.sent), 1)

    def test_prepare_all_skips_the_retry_pass_when_time_is_up(self):
        def failing(cfg, channel, llm, state, now, log=print):
            raise RuntimeError("HTTP 503")

        ticks = iter([0] + [25 * 60] * 10)
        with tempfile.TemporaryDirectory() as tmp:
            state, tg, sleeps = State(pathlib.Path(tmp)), FakeTelegram(), []
            with unittest.mock.patch.object(pipeline, "build_digest", side_effect=failing):
                drafts = pipeline.prepare_all(CFG, ["zh"], None, tg, state, "42",
                                              dt.datetime(2026, 9, 15, 23, 30, tzinfo=dt.timezone.utc),
                                              log=QUIET, sleep=sleeps.append, clock=lambda: next(ticks))
        self.assertEqual(sleeps, [])
        self.assertEqual(drafts["zh"]["status"], "error")
        self.assertEqual(sum("пока не собран" in m["text"] for m in tg.sent), 1)


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))
        self.start, self.deadline = pipeline.publish_window(CFG, RU, dt.date(2026, 9, 16))
        self.state.save_draft({"id": "ru-20260916", "channel": "ru", "date": "2026-09-16", "status": "ready",
                               "html": "<b>digest</b>", "keys": [], "report": [], "preview_message_id": 1,
                               "preview_mode": "auto", "sent_at": (self.start - dt.timedelta(hours=6)).isoformat()})

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def auto():
        cfg = copy.deepcopy(CFG)
        cfg["moderation"]["mode"] = "auto"
        return cfg

    @staticmethod
    def command(text, user_id=42, update_id=30):
        return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": user_id},
                                                    "chat": {"id": user_id, "type": "private"}, "text": text}}

    def publish(self, tg, now):
        return pipeline.publish(self.auto(), RU, None, tg, self.state, "42", now, log=QUIET)

    def test_auto_mode_publishes_without_approval(self):
        tg = FakeTelegram()
        self.assertEqual(self.publish(tg, self.start + dt.timedelta(minutes=8)), "published")
        self.assertEqual(tg.channel_posts()[0]["text"], "<b>digest</b>")

    def test_auto_mode_leaves_time_to_press_do_not_publish(self):
        draft = self.state.draft("ru-20260916")
        draft["sent_at"] = self.start.isoformat()   # the draft arrived late, right at publish time
        self.state.save_draft(draft)
        tg = FakeTelegram()
        self.assertEqual(self.publish(tg, self.start + dt.timedelta(minutes=10)), "waiting")
        self.assertEqual(self.publish(tg, self.start + dt.timedelta(minutes=25)), "published")

    def test_do_not_publish_button_stops_auto_mode(self):
        veto = {"update_id": 10, "callback_query": {"id": "cb", "from": {"id": 42}, "data": "skip:ru-20260916",
                                                    "message": {"chat": {"id": 42}, "message_id": 1}}}
        tg = FakeTelegram([veto])
        self.assertEqual(self.publish(tg, self.start + dt.timedelta(minutes=8)), "skipped")
        self.assertEqual(tg.channel_posts(), [])

    def test_auto_mode_never_publishes_a_stale_draft(self):
        tg = FakeTelegram()
        self.assertEqual(self.publish(tg, self.deadline + dt.timedelta(hours=3)), "expired")
        self.assertEqual(tg.channel_posts(), [])
        self.assertIn("окно публикации", tg.sent[-1]["text"])

    def test_admin_switches_mode_from_the_chat(self):
        now = self.start + dt.timedelta(minutes=1)
        tg = FakeTelegram([self.command("/manual")])
        pipeline.process_callbacks(tg, self.state, "42", self.auto(), now, log=QUIET)
        self.assertEqual(pipeline.current_mode(self.auto(), self.state), "manual")
        self.assertIn("только с одобрения", tg.sent[-1]["text"])
        self.assertEqual(tg.buttons[-1], pipeline.moderation_buttons("ru-20260916", "manual"))
        self.assertEqual(self.publish(tg, now + dt.timedelta(minutes=30)), "waiting")   # manual: no post without approval
        tg.updates.append(self.command("/auto", update_id=31))
        pipeline.process_callbacks(tg, self.state, "42", self.auto(), now, log=QUIET)
        self.assertEqual(pipeline.current_mode(self.auto(), self.state), "auto")
        self.assertIn("автоматическая публикация", tg.sent[-1]["text"])
        self.assertEqual(tg.buttons[-1], pipeline.moderation_buttons("ru-20260916", "auto"))

    def test_strangers_cannot_switch_the_mode(self):
        tg = FakeTelegram([self.command("/manual", user_id=999)])
        pipeline.process_callbacks(tg, self.state, "42", self.auto(), self.start, log=QUIET)
        self.assertEqual(pipeline.current_mode(self.auto(), self.state), "auto")
        self.assertEqual(tg.sent, [])

    def test_mode_command_only_reports(self):
        tg = FakeTelegram([self.command("/mode")])
        pipeline.process_callbacks(tg, self.state, "42", self.auto(), self.start, log=QUIET)
        self.assertIn("автоматическая публикация", tg.sent[-1]["text"])
        self.assertIsNone(self.state.settings().get("mode"))

    def test_draft_buttons_follow_the_mode(self):
        draft = dict(self.state.draft("ru-20260916"), preview_message_id=None)
        for mode, texts in (("auto", ["⛔ Не публиковать"]), ("manual", ["✅ Опубликовать", "⛔ Пропустить"])):
            tg = FakeTelegram()
            pipeline.send_preview(tg, "42", CFG, RU, dict(draft), mode=mode)
            buttons = [m["buttons"] for m in tg.sent if m["buttons"]][0]
            self.assertEqual([button["text"] for button in buttons[0]], texts)


class TickTest(unittest.TestCase):
    """One wake-up does whatever is due; repeated wake-ups do nothing twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(pathlib.Path(self.tmp.name))
        self.cfg = copy.deepcopy(CFG)
        self.cfg["moderation"].update(mode="auto", first_publish_date="2026-09-16")
        self.builds, self.failing = [], set()

        def build(cfg, channel, llm, state, now, log=print):
            self.builds.append(channel["key"])
            if channel["key"] in self.failing:
                raise RuntimeError("HTTP 503 high demand")
            day = pipeline.publish_day(cfg, channel, now)
            return {"id": pipeline.draft_id(channel, day), "channel": channel["key"], "date": day.isoformat(),
                    "created_at": now.isoformat(), "status": "ready", "html": f"<b>{channel['key']}</b>",
                    "keys": [], "report": []}

        patcher = unittest.mock.patch.object(pipeline, "build_digest", side_effect=build)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tg = FakeTelegram()

    def tick(self, day, hour, minute):
        when = dt.datetime(2026, 9, day, hour, minute, tzinfo=dt.timezone.utc)
        return pipeline.tick(self.cfg, None, self.tg, self.state, "42", when, log=QUIET, sleep=lambda s: None)

    def channel_posts(self):
        return [m["chat_id"] for m in self.tg.sent if str(m["chat_id"]).startswith("@")]

    def test_morning_wake_up_sends_every_draft_once(self):
        self.assertEqual(self.tick(15, 23, 13)["prepared"], {"zh": "ready", "ru": "ready", "en": "ready"})
        self.assertEqual([m["text"] for m in self.tg.sent if m["buttons"]], ["<b>zh</b>", "<b>ru</b>", "<b>en</b>"])
        self.assertNotIn("prepared", self.tick(15, 23, 43))   # a second wake-up in the window changes nothing
        self.assertEqual(self.builds, ["zh", "ru", "en"])

    def test_each_channel_is_published_in_its_own_window(self):
        self.tick(15, 23, 13)
        results = self.tick(16, 0, 43)
        self.assertEqual(results.get("zh"), "published")
        self.assertNotIn("ru", results)
        self.assertEqual(self.tick(16, 5, 43).get("ru"), "published")
        self.assertEqual(self.tick(16, 7, 43).get("en"), "published")
        self.assertEqual(self.channel_posts(), ["@fintech_daily_zh", "@fintech_daily_ru", "@fintech_daily"])
        later = self.tick(16, 8, 13)
        self.assertFalse({"zh", "ru", "en"} & set(later))   # published drafts are left alone

    def test_on_monday_the_week_in_review_goes_out_right_before_the_digest(self):
        def weekly(cfg, channel, llm, state, now, log=print):
            day = pipeline.publish_day(cfg, channel, now)
            return {"id": pipeline.draft_id(channel, day, "weekly"), "kind": "weekly", "channel": channel["key"],
                    "date": day.isoformat(), "created_at": now.isoformat(), "status": "ready",
                    "html": f"<b>weekly {channel['key']}</b>", "report": []}

        with unittest.mock.patch.object(pipeline, "build_weekly", side_effect=weekly):
            prepared = self.tick(20, 23, 13)["prepared"]   # Sunday evening UTC: Monday's drafts
            self.assertEqual(prepared, {"zh": "ready", "ru": "ready", "en": "ready",
                                        "zh-weekly": "ready", "ru-weekly": "ready", "en-weekly": "ready"})
            self.assertIn("🤖 Итоги недели для <b>@fintech_daily_ru</b>", "\n".join(m["text"] for m in self.tg.sent))
            results = self.tick(21, 5, 29)
        self.assertEqual((results.get("ru-weekly"), results.get("ru")), ("published", "published"))
        ru_posts = [m["text"] for m in self.tg.sent if m["chat_id"] == "@fintech_daily_ru"]
        self.assertEqual(ru_posts, ["<b>weekly ru</b>", "<b>ru</b>"])
        self.assertNotIn("ru-weekly", self.tick(22, 5, 43))   # Tuesday: no week in review

    def test_no_week_in_review_after_the_digest_went_out(self):
        # the case of 2026-09-28: the feature arrived on a Monday after the morning posts
        self.tick(20, 23, 13)
        self.assertEqual(self.tick(21, 7, 43).get("en"), "published")
        with unittest.mock.patch.object(pipeline, "build_weekly") as weekly:
            results = self.tick(21, 9, 13)
        weekly.assert_not_called()
        self.assertNotIn("en-weekly", results)
        self.assertEqual([m["text"] for m in self.tg.sent if m["chat_id"] == "@fintech_daily"], ["<b>en</b>"])

    def test_early_wake_up_waits_and_publishes_on_the_minute(self):
        self.tick(15, 23, 13)
        waited = []
        when = dt.datetime(2026, 9, 16, 5, 29, 20, tzinfo=dt.timezone.utc)
        self.assertNotIn("ru", pipeline.tick(self.cfg, None, self.tg, self.state, "42", when - dt.timedelta(minutes=9),
                                             log=QUIET, sleep=waited.append))
        results = pipeline.tick(self.cfg, None, self.tg, self.state, "42", when, log=QUIET, sleep=waited.append)
        self.assertEqual(results.get("ru"), "published")
        self.assertEqual(waited, [40.0])
        self.assertEqual(self.state.draft("ru-20260916")["published_at"], "2026-09-16T08:30:00+03:00")

    def test_early_wake_up_does_not_wait_without_a_ready_draft(self):
        waited = []
        when = dt.datetime(2026, 9, 16, 5, 29, tzinfo=dt.timezone.utc)
        self.assertNotIn("ru", pipeline.tick(self.cfg, None, self.tg, self.state, "42", when, log=QUIET, sleep=waited.append))
        self.assertEqual(waited, [])

    def test_manual_drafts_wait_and_then_expire(self):
        self.cfg["moderation"]["mode"] = "manual"
        self.tick(15, 23, 13)
        self.assertEqual(self.tick(16, 1, 43).get("zh"), "waiting")
        self.assertEqual(self.tick(16, 3, 13).get("zh"), "expired")
        self.assertNotIn("zh", self.tick(16, 3, 43))
        self.assertEqual(self.channel_posts(), [])

    def test_nothing_is_built_once_a_window_closed_without_a_draft(self):
        self.assertNotIn("zh", self.tick(16, 4, 13))
        self.assertEqual(self.builds, [])

    def test_a_failing_draft_gets_a_limited_number_of_attempts(self):
        self.failing.add("zh")
        for hour, minute in ((23, 13), (23, 23), (23, 43), (23, 53)):
            self.tick(15, hour, minute)
        self.tick(16, 0, 13)
        self.assertEqual(self.builds.count("zh"), 3)
        self.assertEqual(self.builds.count("ru"), 1)
        self.assertEqual(sum("пока не собран" in m["text"] for m in self.tg.sent), 1)

    def test_before_the_first_publish_date_only_commands_are_read(self):
        self.cfg["moderation"]["first_publish_date"] = "2026-09-20"
        self.tg.updates.append(ModeTest.command("/manual"))
        results = self.tick(15, 23, 13)
        self.assertEqual(self.builds, [])
        self.assertEqual(results["mode"], "manual")
        self.assertIn("только с одобрения", self.tg.sent[-1]["text"])


def http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps([{"error": {"code": code, "message": message}}]).encode()
    return urllib.error.HTTPError("https://example.invalid/chat/completions", code, "error", {}, io.BytesIO(body))


def quota_error(message: str, quota_id: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED",
                                 "details": [{"violations": [{"quotaId": quota_id}]}]}}).encode()
    return urllib.error.HTTPError("https://example.invalid/chat/completions", 429, "Too Many Requests", {}, io.BytesIO(body))


class QuotaTest(unittest.TestCase):
    def llm(self, **kwargs):
        cfg = CFG["llm"]
        return LLM("https://example.invalid/", "key", cfg["models"], steps=cfg["steps"], **kwargs)

    def test_each_step_starts_with_its_own_model(self):
        llm = self.llm()
        seen = []

        def post(path, body, timeout):
            seen.append(body["model"])
            return RetryTest.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            for step in ("selection", "digest", "verdicts", "repair"):
                llm.json_completion("s", "u", {"type": "object"}, step)
        steps = CFG["llm"]["steps"]
        expected = [steps[step][0] for step in ("selection", "digest", "verdicts", "repair")]
        self.assertEqual(seen, expected)
        self.assertEqual([model for _, model in llm.step_log], expected)
        self.assertEqual(llm.preferred("verdicts"), steps["verdicts"][0])

    def test_requests_to_one_model_are_spaced_under_its_rpm(self):
        clock, sleeps = [1000.0], []

        def sleep(seconds):
            sleeps.append(round(seconds, 1))
            clock[0] += seconds

        def post(path, body, timeout):
            clock[0] += 2   # every answer takes 2 seconds
            return RetryTest.answer()

        llm = LLM("https://example.invalid/", "key", ["a"], rpm={"a": 5}, clock=lambda: clock[0], sleep=sleep)
        with unittest.mock.patch.object(llm, "_post", side_effect=post):
            for _ in range(3):
                llm.json_completion("s", "u", {"type": "object"}, "x")
        self.assertEqual(sleeps, [10.5, 10.5])   # 12.5 s between requests: never more than 5 a minute

    def test_daily_quota_skips_the_model_for_the_rest_of_the_run(self):
        llm = LLM("https://example.invalid/", "key", ["a", "b"], retries=3)
        seen = []

        def post(path, body, timeout):
            seen.append(body["model"])
            if body["model"] == "a":
                raise quota_error("You exceeded your current quota. Please retry in 9h12m3s.",
                                  "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            return RetryTest.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post), \
                unittest.mock.patch("newsbot.llm.time.sleep") as slept:
            llm.json_completion("s", "u", {"type": "object"}, "x")
            llm.json_completion("s", "u", {"type": "object"}, "x")
        self.assertEqual(seen, ["a", "b", "b"])
        slept.assert_not_called()
        self.assertIn("a: daily quota used up", llm.notes)

    def test_per_minute_limit_waits_as_asked_and_keeps_the_model(self):
        llm = LLM("https://example.invalid/", "key", ["a", "b"], retries=3)
        seen = []

        def post(path, body, timeout):
            seen.append(body["model"])
            if len(seen) == 1:
                raise quota_error("You exceeded your current quota. Please retry in 7.5s.",
                                  "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
            return RetryTest.answer()

        with unittest.mock.patch.object(llm, "_post", side_effect=post), \
                unittest.mock.patch("newsbot.llm.time.sleep") as slept:
            llm.json_completion("s", "u", {"type": "object"}, "x")
        self.assertEqual(seen, ["a", "a"])
        slept.assert_called_once_with(7.5)

    def test_api_errors_are_summarised_on_one_line(self):
        from newsbot.llm import api_error_summary
        body = ('[{\n  "error": {\n    "code": 503,\n    "message": "This model is currently experiencing high demand.'
                '\\n Please try again later.",\n    "status": "UNAVAILABLE"\n  }\n}\n]')
        self.assertEqual(api_error_summary(body),
                         "This model is currently experiencing high demand. Please try again later.")
        self.assertEqual(api_error_summary("<html>\n Bad   Gateway \n</html>"), "<html> Bad Gateway </html>")

    def test_retry_hints_are_parsed(self):
        from newsbot.llm import retry_hint
        empty = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b""))
        self.assertEqual(retry_hint(empty, "Please retry in 43.2s."), 43.2)
        self.assertEqual(retry_hint(empty, '{"retryDelay": "30s"}'), 30.0)
        self.assertEqual(retry_hint(empty, "Please retry in 9h30m12s."), 34212.0)
        self.assertIsNone(retry_hint(empty, "Quota exceeded."))

    def test_steps_fit_the_free_daily_quota(self):
        steps, rpm = CFG["llm"]["steps"], CFG["llm"]["rpm"]
        self.assertEqual(set(steps), {"selection", "digest", "repair", "verdicts", "weekly", "rewrite"})
        self.assertTrue(all(model in rpm for order in steps.values() for model in order))
        self.assertNotEqual(steps["digest"][0], steps["verdicts"][0])   # facts are checked by another model
        self.assertNotEqual(steps["rewrite"][0], steps["verdicts"][0])  # and a rewrite too
        # worst case, on a Monday: a repair pass, the week in review and every allowed rewrite (each one re-checked)
        rewrites = CFG["moderation"]["max_rewrites"]
        per_digest = {"selection": 1, "digest": 1, "repair": 1, "verdicts": 2 + rewrites, "weekly": 1, "rewrite": rewrites}
        daily = collections.Counter()
        for step, count in per_digest.items():
            daily[steps[step][0]] += count * len(CFG["channels"])
        self.assertTrue(all(count <= 15 for count in daily.values()), daily)   # of 20 a day: room for a rebuild


class UsageTest(unittest.TestCase):
    def test_every_request_is_recorded_with_its_tokens(self):
        llm = LLM("https://example.invalid/", "key", ["m1"], sleep=lambda s: None, round_delay=0, rounds=2)
        answers = [http_error(503, "This model is currently experiencing high demand."),
                   {"choices": [{"message": {"content": '{"items": []}'}}],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1900}}]

        def post(path, body, timeout):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        llm._post = post
        self.assertEqual(llm.json_completion("s", "u", {}, "digest"), {"items": []})
        self.assertEqual([(c["step"], c["outcome"]) for c in llm.usage_log], [("digest", "http_503"), ("digest", "ok")])
        summary = usage_summary(llm.usage_log)
        self.assertEqual((summary["requests"], summary["ok"], summary["prompt_tokens"], summary["completion_tokens"]),
                         (2, 1, 1200, 700))   # 400 thinking tokens count as output
        self.assertEqual(summary["by_model"]["m1"]["requests"], 2)

    def test_draft_keeps_guardrail_counters_and_entries(self):
        draft = BuildDigestTest().build(ScriptedLLM())
        self.assertEqual(draft["checks"], {"candidates": 8, "selected": 5, "written": 5, "code_flagged": 2,
                                           "code_quotes": 1, "code_other": 1, "dropped": 1, "verifier_flagged": 1,
                                           "repaired": 2, "published": 4})
        self.assertEqual(draft["usage"]["requests"], 0)   # the scripted model makes no HTTP requests
        self.assertEqual(len(draft["entries"]), 4)
        first = draft["entries"][0]
        self.assertEqual(set(first["sources"][0]), {"id", "source", "link", "title", "summary"})
        self.assertIn("https://pymnts.com/clarity", first["keys"])


def published_draft(day, channel="ru", minutes_late=0, checks=None, usage=None, report=None, items=7):
    channel_cfg = CFG["channels"][channel]
    entries = [{"headline": f"Story {n}", "summary": "Text.", "why": "Why.", "hashtags": [],
                "sources": [{"source": "Example", "link": f"https://example.com/{day:%m%d}/{n}"}]} for n in range(items)]
    start, _ = pipeline.publish_window(CFG, channel_cfg, day)
    draft = {"id": f"{channel}-{day:%Y%m%d}", "channel": channel, "date": day.isoformat(), "status": "published",
             "html": render.render_digest(channel_cfg, day, entries), "report": report or [],
             "published_at": (start + dt.timedelta(minutes=minutes_late)).isoformat()}
    if checks is not None:
        draft["checks"] = checks
    if usage is not None:
        draft["usage"] = usage
    return draft


class MetricsTest(unittest.TestCase):
    def test_metrics_from_new_and_legacy_drafts(self):
        usage = {"requests": 6, "ok": 5, "prompt_tokens": 100_000, "completion_tokens": 10_000,
                 "by_model": {"gemini-3.8-flash": {"requests": 6, "ok": 5, "prompt_tokens": 100_000,
                                                   "completion_tokens": 10_000}}, "by_step": {}}
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.save_draft(published_draft(dt.date(2026, 10, 8), checks={
                "written": 9, "repaired": 2, "dropped": 1, "code_flagged": 2, "code_sources": 1, "code_quotes": 1,
                "verifier_flagged": 1, "repeat_verifier": 1}, usage=usage))
            state.save_draft(published_draft(dt.date(2026, 10, 9), minutes_late=13, report=[
                "🔧 «A»: исправлено — ссылается на источники не из этой новости: ['c7']",
                "🔧 «B»: исправлено — Источник не подтверждает факт",
                "❌ «C»: модель не написала текст",
                "⚠️ Часть работы сделала запасная модель: отбор — gemini-3.1-flash-lite, проверка — gemini-3.8-flash "
                "(причины: x)"]))
            data = metrics.collect(CFG, state, days=2, today=dt.date(2026, 10, 9))
        reliability, guards, models, cost = data["reliability"], data["guardrails"], data["models"], data["cost"]
        self.assertEqual((reliability["published"], reliability["on_time"], reliability["max_delay_minutes"]), (2, 1, 13.0))
        self.assertEqual((guards["written"], guards["repaired"], guards["dropped"]), (16, 4, 1))
        self.assertEqual((guards["code_flagged"], guards["verifier_flagged"], guards["code_share"]), (3, 2, 0.6))
        self.assertEqual(guards["repeats_verifier"], 1)
        self.assertEqual((models["fallback_any"], models["fallback_selection"]), (1, 1))
        # 0.1M input × $0.75 + 0.01M output × $3.75 for the one post with token usage
        self.assertAlmostEqual(cost["cost_per_post_usd"], 0.1125)
        self.assertEqual((cost["requests"], cost["failed_requests"]), (6, 1))
        self.assertIn("| Опубликовано | 2 из 2 |", metrics.markdown(data))


class EvaluateTest(unittest.TestCase):
    def test_offline_checks_find_long_texts_bad_style_and_unknown_tags(self):
        day = dt.date(2026, 10, 9)
        draft = published_draft(day, items=3)
        draft["html"] = draft["html"].replace("Story 0</b>\nText.", "Story 0</b>\n" + "Длинный текст. " * 30)
        draft["html"] = draft["html"].replace("Story 1</b>\nText.", "Story 1</b>\nНовость!")
        draft["html"] = draft["html"].rsplit("\n\n", 1)[0] + "\n\n#главное #неизвестный"
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.save_draft(draft)
            result = evaluate.offline(CFG, state, days=1, today=day)
        checks = result["checks"]
        self.assertEqual(checks["limits"], {"passed": 2, "total": 3})
        self.assertEqual(checks["style"], {"passed": 2, "total": 3})
        self.assertEqual(checks["hashtags"], {"passed": 0, "total": 1})
        self.assertEqual(checks["sources"], {"passed": 3, "total": 3})
        self.assertTrue(any("#неизвестный" in f for f in result["failures"]))
        self.assertIn("Запросов к модели: 0.", "\n".join(evaluate.offline_markdown(result)))

    def test_language_share(self):
        self.assertGreater(evaluate.script_share("Stripe купила стартап Bridge за $1,1 млрд", "ru"), 0.5)
        self.assertLess(evaluate.script_share("Stripe buys Bridge", "ru"), 0.5)
        self.assertGreater(evaluate.script_share("蚂蚁国际与 HSBC 试点代币化存款", "zh"), 0.3)

    def test_live_eval_judges_both_versions_blind(self):
        day = dt.date(2026, 10, 9)
        original = articles.fetch_many
        articles.fetch_many = lambda links, limit: {link: {"status": 200, "text": "Story text"} for link in links}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state = State(pathlib.Path(tmp))
                state.save_draft(published_draft(day, items=3))
                state.save_draft(published_draft(day - dt.timedelta(days=1), items=3))
                result = evaluate.live(CFG, RU, FakeLLM(), state, days=3)
        finally:
            articles.fetch_many = original
        self.assertEqual((result["cases"], result["requests"]), (2, 3))   # one writer request per day + one judge
        self.assertEqual(result["code"]["stories"], 6)
        self.assertEqual(result["scores"]["published"]["accuracy"], 4.0)
        self.assertEqual(result["scores"]["current"]["accuracy"], 4.0)
        lines = evaluate.live_markdown(result, {"scores": {"current": {"accuracy": 3.5}}})
        self.assertIn("| точность по источникам | 4.0 | 4.0 | 3.5 |", lines)


class RewriteLLM:
    """Changes the first entry (a supported number), removes the second, leaves the rest; the fact-check passes."""

    last_model = "scripted"

    def __init__(self, quote="$50 million", remove=(1,)):
        self.quote, self.remove, self.calls = quote, remove, []

    def json_completion(self, system, user, schema, schema_name):
        payload = json.loads(user[user.index("{"):])
        self.calls.append(schema_name)
        if schema_name == "rewrite":
            self.request = payload["request"]
            items = []
            for entry in payload["entries"]:
                item = {k: entry[k] for k in ("index", "headline", "summary", "why", "hashtags", "source_quotes")}
                item["keep"] = entry["index"] not in self.remove
                if entry["index"] == 0:
                    item.update(headline="Компания 0 привлекла $50 млн", source_quotes=[self.quote])
                items.append(item)
            return {"items": items}
        if schema_name == "verdicts":
            return {"verdicts": [{"index": e["index"], "ok": True, "issues": []} for e in payload["entries"]]}
        raise AssertionError(schema_name)


class RewriteTest(unittest.TestCase):
    DAY = dt.date(2026, 9, 16)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))
        entries = [{"headline": f"Новость {n}", "summary": f"Текст новости {n}.", "why": "Почему важно.",
                    "hashtags": ["#сделки"], "source_quotes": [],
                    "sources": [{"id": f"c{n}", "source": "Example", "link": f"https://example.com/{n}",
                                 "title": f"Company {n} raises $5{n} million", "summary": "Round led by VC."}],
                    "keys": [f"https://example.com/{n}"]} for n in range(4)]
        self.state.save_draft({
            "id": "ru-20260916", "channel": "ru", "date": "2026-09-16", "status": "ready", "report": ["🔧 старое"],
            "html": render.render_digest(RU, self.DAY, entries), "entries": entries,
            "stories": [{"headline": e["headline"], "hashtags": e["hashtags"], "titles": [e["sources"][0]["title"]],
                         "links": [e["sources"][0]["link"]]} for e in entries],
            "keys": [k for e in entries for k in e["keys"]], "preview_message_id": 1,
            "sent_at": "2026-09-16T04:00:00+00:00", "checks": {"written": 4}})
        self.original = articles.fetch_many
        articles.fetch_many = lambda links, limit: {link: {"status": 200, "text": ""} for link in links}
        self.now = dt.datetime(2026, 9, 16, 5, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        articles.fetch_many = self.original
        self.tmp.cleanup()

    def update(self, update_id, **event):
        return {"update_id": update_id, **event}

    def reply(self, update_id, to, text="Убери новость 2, первую сделай про сумму"):
        return self.update(update_id, message={"message_id": 100 + update_id, "from": {"id": 42},
                                              "chat": {"id": 42, "type": "private"}, "text": text,
                                              "reply_to_message": {"message_id": to}})

    def test_digest_drafts_get_the_button_and_the_week_in_review_does_not(self):
        texts = [b["text"] for row in pipeline.moderation_buttons("ru-20260916", "auto") for b in row]
        self.assertEqual(texts, ["⛔ Не публиковать", "✏️ Переписать"])
        self.assertNotIn("✏️ Переписать", [b["text"] for row in pipeline.moderation_buttons("weekly-ru-20260916") for b in row])

    def test_button_asks_what_to_change_and_a_reply_is_remembered(self):
        press = self.update(1, callback_query={"id": "cb", "from": {"id": 42}, "data": "rewrite:ru-20260916",
                                               "message": {"chat": {"id": 42}, "message_id": 1}})
        tg = FakeTelegram([press])
        decisions = pipeline.process_callbacks(tg, self.state, "42", CFG, self.now, QUIET)
        self.assertEqual(decisions, {})                          # pressing «Переписать» decides nothing
        question = tg.sent[-1]
        self.assertEqual(question["force_reply"], "Что изменить?")
        self.assertEqual(self.state.telegram()["rewrite_prompts"], {str(len(tg.sent)): "ru-20260916"})
        tg.updates.append(self.reply(2, to=len(tg.sent)))
        pipeline.process_callbacks(tg, self.state, "42", CFG, self.now, QUIET)
        request = self.state.telegram()["rewrites"]["ru-20260916"]
        self.assertEqual(request["text"], "Убери новость 2, первую сделай про сумму")
        self.assertIn("Принято", tg.sent[-1]["text"])

    def test_a_reply_to_the_draft_itself_works_too_and_strangers_are_ignored(self):
        stranger = self.reply(1, to=1)
        stranger["message"]["from"]["id"] = 7
        tg = FakeTelegram([stranger, self.reply(2, to=1, text="короче")])
        pipeline.process_callbacks(tg, self.state, "42", CFG, self.now, QUIET)
        self.assertEqual(self.state.telegram()["rewrites"]["ru-20260916"]["text"], "короче")

    def test_rewrite_changes_removes_and_sends_a_new_version(self):
        self.state.save_telegram({"offset": None, "decisions": {}, "rewrites": {"ru-20260916": {"text": "убери 2"}}})
        tg, llm = FakeTelegram(), RewriteLLM()
        self.assertEqual(pipeline.apply_rewrites(CFG, llm, tg, self.state, "42", self.now, QUIET), ["ru-20260916"])
        self.assertEqual(llm.calls, ["rewrite", "verdicts"])
        draft = self.state.draft("ru-20260916")
        self.assertIn("Компания 0 привлекла $50 млн", draft["html"])
        self.assertNotIn("Новость 1", draft["html"])
        self.assertIn("<b>2. Новость 2</b>", draft["html"])     # renumbered
        self.assertEqual([s["headline"] for s in draft["stories"]], ["Компания 0 привлекла $50 млн", "Новость 2", "Новость 3"])
        self.assertNotIn("https://example.com/1", draft["keys"])
        self.assertEqual(draft["rewrites"], 1)
        self.assertEqual((draft["checks"]["rewrite_changed"], draft["checks"]["rewrite_removed"]), (1, 1))
        self.assertTrue(draft["report"][0].startswith("✏️ Переписано по вашей просьбе: изменено 1, убрано 1"))
        self.assertEqual(tg.buttons[0][0][0]["text"], "Заменён новой версией ↓")
        self.assertNotEqual(draft["preview_message_id"], 1)       # the new version went to the editor
        self.assertEqual(self.state.telegram()["rewrites"], {})

    def test_a_change_that_fails_the_checks_keeps_the_previous_text(self):
        self.state.save_telegram({"offset": None, "decisions": {}, "rewrites": {"ru-20260916": {"text": "сумма"}}})
        tg = FakeTelegram()
        pipeline.apply_rewrites(CFG, RewriteLLM(quote="$999 million", remove=()), tg, self.state, "42", self.now, QUIET)
        draft = self.state.draft("ru-20260916")
        self.assertIn("Новость 0", draft["html"])
        self.assertEqual(draft["preview_message_id"], 1)          # nothing changed: the same draft stays in force
        self.assertNotIn("rewrites", draft)
        self.assertIn("В силе прежняя версия", tg.sent[-1]["text"])

    def test_a_rewrite_never_leaves_fewer_stories_than_the_minimum(self):
        self.state.save_telegram({"offset": None, "decisions": {}, "rewrites": {"ru-20260916": {"text": "убери 2 и 3"}}})
        tg = FakeTelegram()
        pipeline.apply_rewrites(CFG, RewriteLLM(remove=(1, 2)), tg, self.state, "42", self.now, QUIET)
        self.assertIn("Новость 1", self.state.draft("ru-20260916")["html"])
        self.assertIn("минимум", tg.sent[-1]["text"])

    def test_auto_publication_waits_for_a_pending_rewrite(self):
        self.state.save_telegram({"offset": None, "decisions": {}, "rewrites": {"ru-20260916": {"text": "убери 2"}}})
        tg = FakeTelegram()
        at_publish = dt.datetime(2026, 9, 16, 8, 32, tzinfo=zoneinfo.ZoneInfo("Europe/Moscow"))
        self.assertEqual(pipeline.publish(CFG, RU, RewriteLLM(), tg, self.state, "42", at_publish, log=QUIET), "waiting")
        self.assertEqual(tg.channel_posts(), [])
        self.assertEqual(self.state.draft("ru-20260916")["rewrites"], 1)


if __name__ == "__main__":
    unittest.main()
