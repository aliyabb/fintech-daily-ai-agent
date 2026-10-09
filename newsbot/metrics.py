"""Product metrics computed from the drafts the bot keeps: reliability, guardrails, model fallbacks, cost.

Drafts made before the guardrail counters and token usage were stored are read back from their report lines,
so the numbers cover every draft still in the state branch (14 days).
"""
from __future__ import annotations

import collections
import datetime as dt
import re

from . import pipeline, render

FALLBACK = re.compile(r"Часть работы сделала запасная модель: (.*?)(?: \(причины|$)")
STEP_NAMES = {"отбор": "selection", "написание": "digest", "проверка": "verdicts", "исправление": "repair"}


def legacy_checks(report: list[str], published: int) -> dict:
    """Guardrail counts of a draft built before draft["checks"] existed, from its report lines."""
    checks = collections.Counter(published=published)
    for line in report or []:
        if line.startswith("🔧"):
            checks["repaired"] += 1
            if "источники не из этой" in line:
                checks.update(code_flagged=1, code_sources=1)
            elif "нет фрагментов" in line:
                checks.update(code_flagged=1, code_quotes=1)
            else:
                checks["verifier_flagged"] += 1
        elif line.startswith("❌"):
            checks["dropped"] += 1
            if "не написала" in line:
                checks["not_written"] += 1
                checks["dropped"] -= 1
        elif line.startswith("🔁"):
            checks["repeat_verifier" if "пропущено" in line else "repeat_selection"] += 1
    checks["written"] = checks["published"] + checks["dropped"]
    return dict(checks)


def fallback_steps(report: list[str], model: str = "flash-lite") -> set[str]:
    """Steps of a draft that the given fallback model did (as the report states)."""
    steps = set()
    for line in report or []:
        match = FALLBACK.search(line)
        if match:
            for part in match.group(1).split(", "):
                step, _, used = part.partition(" — ")
                if model in used:
                    steps.add(STEP_NAMES.get(step.strip(), step.strip()))
    return steps


def cost(usage: dict | None, pricing: dict) -> float | None:
    """What the draft's model calls would cost on the paid tier, in dollars; None without usage or prices."""
    if not usage or not usage.get("by_model") or not pricing:
        return None
    total = 0.0
    for model, numbers in usage["by_model"].items():
        price = pricing.get(model)
        if not price:
            return None
        total += numbers.get("prompt_tokens", 0) / 1e6 * price["input"] + numbers.get("completion_tokens", 0) / 1e6 * price["output"]
    return total


def channel_drafts(cfg: dict, state, since: dt.date, until: dt.date) -> list[tuple[dict, dict]]:
    rows = []
    for channel in cfg["channels"].values():
        for draft in state.drafts(channel["key"]) + state.drafts(f"weekly-{channel['key']}"):
            try:
                day = dt.date.fromisoformat(draft.get("date", ""))
            except ValueError:
                continue
            if since <= day <= until:
                rows.append((channel, draft))
    return rows


def collect(cfg: dict, state, days: int = 14, today: dt.date | None = None) -> dict:
    """Every metric as plain numbers (also what `metrics --json` prints)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=days - 1)
    pricing = cfg["llm"].get("pricing") or {}
    statuses = collections.Counter()
    checks = collections.Counter()
    delays, items, lite_any, lite_selection, digests = [], [], 0, 0, 0
    usage_drafts, requests, ok, prompt_tokens, completion_tokens = 0, 0, 0, 0, 0
    costs, published_with_cost = [], 0
    weekly = collections.Counter()
    for channel, draft in channel_drafts(cfg, state, since, today):
        if draft.get("kind") == "weekly":
            weekly[draft.get("status")] += 1
        else:
            digests += 1
            statuses[draft.get("status")] += 1
            entries = len(render.split_digest(draft.get("html", ""))[1]) if draft.get("html") else 0
            checks.update(draft.get("checks") or legacy_checks(draft.get("report"), entries if draft.get("status") in
                                                                  ("ready", "published") else 0))
            steps = fallback_steps(draft.get("report"))
            lite_any += bool(steps)
            lite_selection += "selection" in steps
            if draft.get("status") == "published":
                items.append(entries)
                if draft.get("published_at"):
                    start, _ = pipeline.publish_window(cfg, channel, dt.date.fromisoformat(draft["date"]))
                    delays.append((dt.datetime.fromisoformat(draft["published_at"]) - start).total_seconds() / 60)
        usage = draft.get("usage")
        if usage:
            usage_drafts += 1
            requests += usage.get("requests", 0)
            ok += usage.get("ok", 0)
            prompt_tokens += usage.get("prompt_tokens", 0)
            completion_tokens += usage.get("completion_tokens", 0)
            price = cost(usage, pricing)
            if price is not None and draft.get("status") == "published":
                costs.append(price)
                published_with_cost += 1
    written = checks["written"]
    flagged = checks["code_flagged"] + checks["verifier_flagged"]
    published = statuses["published"]

    def share(part, whole):
        return round(part / whole, 4) if whole else None

    return {
        "period": {"from": since.isoformat(), "to": today.isoformat(), "days": days},
        "reliability": {
            "digests": digests, "statuses": dict(statuses), "published": published,
            "on_time": sum(1 for d in delays if 0 <= d < 1), "with_time": len(delays),
            "avg_delay_minutes": round(sum(delays) / len(delays), 1) if delays else None,
            "max_delay_minutes": round(max(delays), 1) if delays else None,
        },
        "content": {"avg_items": round(sum(items) / len(items), 2) if items else None,
                    "full_posts": sum(1 for n in items if n >= cfg["max_items"]), "posts": len(items),
                    "weekly": dict(weekly)},
        "guardrails": {
            "written": written, "repaired": checks["repaired"], "dropped": checks["dropped"],
            "repair_rate": share(checks["repaired"], written), "drop_rate": share(checks["dropped"], written),
            "code_flagged": checks["code_flagged"], "code_sources": checks["code_sources"],
            "code_quotes": checks["code_quotes"], "verifier_flagged": checks["verifier_flagged"],
            "code_share": share(checks["code_flagged"], flagged),
            "repeats_selection": checks["repeat_selection"], "repeats_verifier": checks["repeat_verifier"],
            "rewrites": checks["rewrites"], "rewrite_changed": checks["rewrite_changed"],
            "rewrite_removed": checks["rewrite_removed"], "rewrite_rejected": checks["rewrite_rejected"],
        },
        "models": {"fallback_any": lite_any, "fallback_selection": lite_selection,
                   "fallback_any_rate": share(lite_any, digests), "fallback_selection_rate": share(lite_selection, digests)},
        "cost": {
            "drafts_with_usage": usage_drafts, "requests": requests, "failed_requests": requests - ok,
            "failed_share": share(requests - ok, requests),
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "requests_per_draft": round(requests / usage_drafts, 1) if usage_drafts else None,
            "tokens_per_draft": round((prompt_tokens + completion_tokens) / usage_drafts) if usage_drafts else None,
            "cost_per_post_usd": round(sum(costs) / published_with_cost, 4) if published_with_cost else None,
            "cost_per_month_usd": (round(sum(costs) / published_with_cost * len(cfg["channels"]) * 30, 2)
                                   if published_with_cost else None),
            "priced_posts": published_with_cost,
        },
    }


def percent(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def dollars(value, digits: int) -> str:
    return "—" if value is None else f"${value:.{digits}f}"


def markdown(data: dict) -> list[str]:
    """The metrics as a Markdown report (for the run page and the console)."""
    r, c, g, m, k = data["reliability"], data["content"], data["guardrails"], data["models"], data["cost"]
    p = data["period"]
    usage_note = ("" if k["drafts_with_usage"] else
                  "\n\n_Токены считаются с версии v2: данные появятся после первых выпусков с ней._")
    return [
        f"### Метрики Fintech Daily · {p['from']} — {p['to']}", "",
        "**Надёжность**", "", "| Метрика | Значение |", "|---|---|",
        f"| Выпусков (3 канала) | {r['digests']} |",
        f"| Опубликовано | {r['published']} из {r['digests']} |",
        f"| Вышли минута в минуту | {r['on_time']} из {r['with_time']} |",
        f"| Средняя / максимальная задержка | {r['avg_delay_minutes']} / {r['max_delay_minutes']} мин |",
        f"| Новостей в посте в среднем | {c['avg_items']} (полных постов: {c['full_posts']} из {c['posts']}) |", "",
        "**Защита от ошибок (до публикации)**", "", "| Метрика | Значение |", "|---|---|",
        f"| Написано новостей | {g['written']} |",
        f"| Исправлено проверками | {g['repaired']} ({percent(g['repair_rate'])}) |",
        f"| Убрано, не исправив | {g['dropped']} ({percent(g['drop_rate'])}) |",
        f"| Нашёл код / вторая модель | {g['code_flagged']} / {g['verifier_flagged']} (доля кода {percent(g['code_share'])}) |",
        f"| — чужие источники / числа без цитаты | {g['code_sources']} / {g['code_quotes']} |",
        f"| Пойманные повторы: отбор / проверка | {g['repeats_selection']} / {g['repeats_verifier']} |",
        f"| «Переписать»: раз / изменено / убрано / отклонено | {g['rewrites']} / {g['rewrite_changed']} / "
        f"{g['rewrite_removed']} / {g['rewrite_rejected']} |", "",
        "**Модели**", "", "| Метрика | Значение |", "|---|---|",
        f"| Выпуски, где помогала Flash Lite | {m['fallback_any']} ({percent(m['fallback_any_rate'])}) |",
        f"| Отбор новостей делала Flash Lite | {m['fallback_selection']} ({percent(m['fallback_selection_rate'])}) |", "",
        "**Запросы и стоимость**", "", "| Метрика | Значение |", "|---|---|",
        f"| Черновиков с учётом токенов | {k['drafts_with_usage']} |",
        f"| Запросов к модели / из них неудачных | {k['requests']} / {k['failed_requests']} ({percent(k['failed_share'])}) |",
        f"| Запросов на черновик | {k['requests_per_draft'] if k['requests_per_draft'] is not None else '—'} |",
        f"| Токенов на черновик | {k['tokens_per_draft'] if k['tokens_per_draft'] is not None else '—'} |",
        f"| Стоимость поста по цене платного тарифа (оценка) | {dollars(k['cost_per_post_usd'], 4)} |",
        f"| В месяц на 3 канала (оценка) | {dollars(k['cost_per_month_usd'], 2)} |",
        f"| Фактическая стоимость | $0 (бесплатный тариф) |{usage_note}",
    ]
