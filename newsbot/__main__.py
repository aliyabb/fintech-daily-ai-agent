"""Command line: python -m newsbot <command> [--channel ru|en|zh]."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import yaml

from . import evaluate, feeds, metrics, pipeline, render
from .config import ROOT, channel as get_channel, load_config, load_dotenv, redact, secret, state_dir
from .llm import LLM, FakeLLM
from .state import State
from .telegram import Telegram, TelegramError


def step_summary(lines: list[str]) -> None:
    """Show a readable result on the GitHub Actions run page (no-op when run locally)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")


def make_llm(cfg: dict, fake: bool):
    if fake:
        return FakeLLM(cfg["max_items"])
    llm_cfg = cfg["llm"]
    return LLM(llm_cfg["base_url"], secret(llm_cfg["api_key_env"]), llm_cfg["models"],
               llm_cfg.get("temperature", 0.3), llm_cfg.get("timeout_seconds", 180),
               steps=llm_cfg.get("steps"), rpm=llm_cfg.get("rpm"),
               rounds=llm_cfg.get("rounds", 3), round_delay=llm_cfg.get("round_delay_seconds", 15),
               last_resort=llm_cfg.get("last_resort"), patient_steps=llm_cfg.get("patient_steps"))


def make_eval_llm(cfg: dict, fake: bool):
    """The live evaluation runs on its own key (another Google project): it never uses the digests' quota."""
    if fake:
        return FakeLLM(cfg["max_items"])
    llm_cfg, eval_cfg = cfg["llm"], cfg.get("eval") or {}
    key = secret(eval_cfg.get("api_key_env", "EVAL_GEMINI_API_KEY"), required=False)
    if not key:
        return None
    return LLM(llm_cfg["base_url"], key, llm_cfg["models"], llm_cfg.get("temperature", 0.3),
               llm_cfg.get("timeout_seconds", 180), steps=eval_cfg.get("steps"), rpm=llm_cfg.get("rpm"),
               rounds=llm_cfg.get("rounds", 3), round_delay=llm_cfg.get("round_delay_seconds", 15))


def configured_models(cfg: dict) -> list[str]:
    llm_cfg = cfg["llm"]
    return list(dict.fromkeys(llm_cfg["models"] + [m for order in (llm_cfg.get("steps") or {}).values() for m in order]))


def cmd_check(cfg, args) -> int:
    ok = True
    rows = []

    def row(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok &= good
        rows.append(f"| {'✅' if good else '❌'} | {name} | {detail} |")
        print(f"{name}: {detail}")

    row("Каналы", True, ", ".join(c["chat_id"] for c in cfg["channels"].values()))
    row("RSS-ленты", True, str(len(feeds.parse_opml(ROOT / cfg["feeds_file"]))))
    for name in ("TELEGRAM_BOT_TOKEN", cfg["llm"]["api_key_env"], "ADMIN_CHAT_ID"):
        present = bool(secret(name, required=False))
        row(f"Секрет {name}", present, "задан" if present else "не задан")
    if secret(cfg["llm"]["api_key_env"], required=False):
        try:
            available = set(make_llm(cfg, False).list_models())
            configured = configured_models(cfg)
            usable = [m for m in configured if m in available]
            for model in configured:
                print(f"model {model}: {'available' if model in available else 'NOT available for this key'}")
            row("Модели из настроек", usable == configured, ("доступны: " + ", ".join(usable)) if usable == configured
                else "недоступны: " + ", ".join(m for m in configured if m not in available))
            flash = sorted(m for m in available if "flash" in m)
            row("Все Flash-модели для ключа", True, ", ".join(flash)[:400] or "нет")
        except Exception as e:
            row("Модели Gemini", False, f"не удалось проверить ключ ({type(e).__name__})")
    token = secret("TELEGRAM_BOT_TOKEN", required=False)
    if token:
        tg = Telegram(token)
        try:
            me = tg.call("getMe")
            row("Бот", True, f"@{me['username']}")
            for ch in cfg["channels"].values():
                try:
                    member = tg.call("getChatMember", chat_id=ch["chat_id"], user_id=me["id"])
                    can_post = member.get("status") == "administrator" and bool(member.get("can_post_messages"))
                    row(ch["chat_id"], can_post, "бот может публиковать" if can_post else
                        "бот не администратор с правом публикации")
                except TelegramError as e:
                    row(ch["chat_id"], False, str(e))
        except TelegramError as e:
            row("Бот", False, str(e))
    print("OK" if ok else "Some checks failed")
    step_summary([f"### Проверка: {'всё в порядке ✅' if ok else 'есть проблемы ❌'}", "",
                  "| | Что | Результат |", "|---|---|---|", *rows])
    return 0 if ok else 1


MODEL_GROUPS = [
    ("Gemini Pro", lambda m: m.startswith("gemini") and "pro" in m),
    ("Gemini Flash", lambda m: m.startswith("gemini") and "flash" in m and "lite" not in m),
    ("Gemini Flash-Lite", lambda m: m.startswith("gemini") and "lite" in m),
    ("Прочие Gemini", lambda m: m.startswith("gemini")),
    ("Gemma (открытые модели)", lambda m: m.startswith("gemma")),
    ("Эмбеддинги", lambda m: "embedding" in m),
    ("Картинки, видео, звук", lambda m: any(k in m for k in ("imagen", "veo", "tts", "image", "audio", "lyria"))),
    ("Другое", lambda m: True),
]


def cmd_models(cfg, args) -> int:
    available = make_llm(cfg, False).list_models()
    groups: dict[str, list[str]] = {}
    for model in available:
        for name, match in MODEL_GROUPS:
            if match(model):
                groups.setdefault(name, []).append(model)
                break
    lines = [f"### Модели, доступные ключу ({len(available)})", "",
             "| Группа | Модели |", "|---|---|"]
    for name, _ in MODEL_GROUPS:
        if groups.get(name):
            print(f"{name}: {', '.join(groups[name])}")
            lines.append(f"| {name} | {', '.join(f'`{m}`' for m in groups[name])} |")
    lines += ["", "В списке всё, что ключ видит; у части моделей на бесплатном тарифе лимит может быть нулевым "
              "(это видно в AI Studio → Rate limits)."]
    step_summary(lines)
    return 0


def cmd_whoami(cfg, args) -> int:
    tg = Telegram(secret("TELEGRAM_BOT_TOKEN"))
    bot = tg.call("getMe").get("username", "your_bot")
    webhook = tg.call("getWebhookInfo")
    if webhook.get("url"):
        hint = (f"У бота @{bot} включён вебхук: сообщения забирает другой сервис, и бот их не видит. "
                "Если вы не подключали бота к другим сервисам, отключите вебхук и запустите whoami снова.")
        print(hint, f"(pending updates: {webhook.get('pending_update_count')})")
        step_summary(["### Telegram ID не найден: включён вебхук", "", hint])
        return 0

    updates = tg.get_updates(allowed=[])
    kinds = {}
    chats = {}
    for update in updates:
        for kind, event in update.items():
            if kind == "update_id" or not isinstance(event, dict):
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            where = (event.get("chat") or (event.get("message") or {}).get("chat") or {})
            print(f"update {update['update_id']}: {kind} in {where.get('type', '?')} {where.get('title') or ''}".rstrip())
            sender = event.get("from")
            if kind in ("message", "callback_query") and where.get("type", "private") == "private" \
                    and sender and not sender.get("is_bot"):
                chats[sender["id"]] = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) \
                    or sender.get("username", "")
    print(f"bot @{bot}; updates by type: {kinds or 'none'}")

    if not chats:
        seen = ", ".join(f"{k}: {v}" for k, v in kinds.items()) or "никаких событий"
        if "channel_post" in kinds:
            advice = "Похоже, сообщение написано в канал. Нужно написать боту в личный чат."
        else:
            advice = (f"Проверьте, что пишете именно @{bot} (у ботов с похожими именами другой владелец), "
                      "в личном чате с ним, а не в канале.")
        hint = (f"Бот @{bot} пока не получил от вас личных сообщений (события за последние 24 часа: {seen}). "
                f"{advice} Отправьте /start и сразу снова запустите whoami.")
        print(hint)
        step_summary(["### Telegram ID пока не найден", "", hint])
        return 0
    for chat_id, name in chats.items():
        print(f"ADMIN_CHAT_ID candidate: {chat_id}  ({name})")
    step_summary(["### Ваш Telegram ID", "", "| ID | Имя в Telegram |", "|---|---|",
                  *[f"| `{chat_id}` | {name} |" for chat_id, name in chats.items()], "",
                  "Скопируйте своё число и сохраните его как секрет `ADMIN_CHAT_ID`: "
                  "Settings → Secrets and variables → Actions → New repository secret."])
    return 0


def cmd_pipeline(cfg, args) -> int:
    ch = get_channel(cfg, args.channel)
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    state = State(state_dir())
    llm = make_llm(cfg, args.fake_llm)
    if args.command in ("preview", "weekly") or args.dry_run:
        tg = admin = None
        if args.command in ("preview", "weekly") and not args.dry_run:   # check the Telegram secrets before the expensive build
            tg, admin = Telegram(secret("TELEGRAM_BOT_TOKEN")), secret("ADMIN_CHAT_ID")
        build = pipeline.build_weekly if args.command == "weekly" else pipeline.build_digest
        try:
            draft = build(cfg, ch, llm, state, now)
        except Exception as e:
            problem = redact(f"{type(e).__name__}: {e}")[:600]
            print(problem)
            step_summary([f"### Черновик для {ch['chat_id']} не собран", "", problem])
            return 1
        print("model:", draft.get("model") or getattr(llm, "last_model", None))
        print("\n".join(draft["report"]) or "(no report lines)")
        print("-" * 60)
        print(render.visible_length(draft.get("html", "")), "visible characters")
        print(draft.get("html", f"status: {draft['status']}"))
        if tg:
            pipeline.send_preview(tg, admin, cfg, ch, draft, test=True)
            step_summary([f"### Тестовый черновик для {ch['chat_id']}",
                          "Отправлен вам в Telegram." if draft["status"] == "ready" else
                          "Не собран: " + "; ".join(draft["report"][-3:])])
        return 0 if draft["status"] == "ready" else 1
    tg = Telegram(secret("TELEGRAM_BOT_TOKEN"))
    admin = secret("ADMIN_CHAT_ID")
    if args.command == "prepare":
        draft = pipeline.prepare(cfg, ch, llm, tg, state, admin, now)
        return 0 if draft["status"] in ("ready",) + pipeline.FINAL_STATUSES else 1
    result = pipeline.publish(cfg, ch, llm, tg, state, admin, now)
    return 0 if result in ("published", "skipped", "waiting", "expired") else 1


def cmd_tick(cfg, args) -> int:
    """A wake-up from cron-job.org or the GitHub schedule: do whatever is due right now."""
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    results = pipeline.tick(cfg, make_llm(cfg, args.fake_llm), Telegram(secret("TELEGRAM_BOT_TOKEN")),
                            State(state_dir()), secret("ADMIN_CHAT_ID"), now)
    label = "автоматическая публикация" if results["mode"] == "auto" else "только с одобрения"
    lines = [f"### Пробуждение бота · {now:%Y-%m-%d %H:%M} UTC", "", f"Режим: {label}", ""]
    for key, channel in cfg["channels"].items():
        for kind, what in (("weekly", "итоги недели"), ("digest", "черновик")):
            status = results.get("prepared", {}).get(pipeline.result_key(key, kind))
            if status:
                lines.append(f"- {what} {channel['chat_id']}: {status}")
    for key, channel in cfg["channels"].items():
        for kind, what in (("weekly", "итоги недели"), ("digest", "публикация")):
            if pipeline.result_key(key, kind) in results:
                lines.append(f"- {what} {channel['chat_id']}: {results[pipeline.result_key(key, kind)]}")
    if lines[-1] == "":
        lines.append("Сейчас делать было нечего.")
    print("\n".join(lines))
    step_summary(lines)
    if results.get("undelivered"):
        print(f"drafts not delivered to the admin: {', '.join(results['undelivered'])}")
        return 1
    return 0


def cmd_inbox(cfg, args) -> int:
    """Read bot commands (/auto, /manual, /mode) and button presses outside the publishing windows."""
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    mode = pipeline.inbox(cfg, Telegram(secret("TELEGRAM_BOT_TOKEN")), State(state_dir()), secret("ADMIN_CHAT_ID"), now)
    label = "автоматическая публикация" if mode == "auto" else "только с одобрения"
    print(f"publishing mode: {mode}")
    step_summary([f"### Режим публикации: {label}", "", "Команды боту в личном чате: /auto, /manual, /mode."])
    return 0


def cmd_edit(cfg, args) -> int:
    """Remove the entries listed in edits.yaml (repeats) from published posts: a preview, or with --apply the edits."""
    edits = yaml.safe_load((ROOT / "edits.yaml").read_text(encoding="utf-8")) or []
    state = State(state_dir())
    if args.apply and not args.dry_run:
        lines = pipeline.edit_posts(cfg, edits, state, Telegram(secret("TELEGRAM_BOT_TOKEN")), secret("ADMIN_CHAT_ID"))
        title = "### Правка опубликованных постов"
    else:
        lines = pipeline.edit_posts(cfg, edits, state)
        title = "### Предпросмотр правок (в каналах ничего не изменено)"
    print("\n".join(lines) or "edits.yaml is empty")
    step_summary([title, "", *[f"- {line}" for line in lines]])
    return 1 if any(line.startswith("❌") for line in lines) else 0


def cmd_pinned(cfg, args) -> int:
    """Remove the lines listed in pinned.yaml from each channel's intro post: a preview, or with --apply the edit."""
    removals = yaml.safe_load((ROOT / "pinned.yaml").read_text(encoding="utf-8")) or {}
    lines = pipeline.edit_pinned(cfg, removals, Telegram(secret("TELEGRAM_BOT_TOKEN")), secret("ADMIN_CHAT_ID"),
                                 apply=args.apply)
    print("\n\n".join(lines) or "pinned.yaml is empty")
    title = "### Правка закреплённых постов" if args.apply else "### Предпросмотр правки закреплённых постов"
    step_summary([title, "", *[f"- {line}" for line in lines]])
    return 1 if any(line.startswith("❌") for line in lines) else 0


def cmd_metrics(cfg, args) -> int:
    """Product metrics from the stored drafts; with --json, the raw numbers."""
    data = metrics.collect(cfg, State(state_dir()), days=args.days)
    lines = metrics.markdown(data)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else "\n".join(lines))
    step_summary(lines)
    return 0


def cmd_eval(cfg, args) -> int:
    """Offline checks of the published posts (no model requests); with eval-live, also the prompt A/B by a judge."""
    state = State(state_dir())
    result = evaluate.offline(cfg, state, days=args.days)
    lines = evaluate.offline_markdown(result)
    print("\n".join(lines))
    step_summary(lines)
    if args.command != "eval-live":
        return 0
    llm = make_eval_llm(cfg, args.fake_llm)
    if llm is None:
        name = (cfg.get("eval") or {}).get("api_key_env", "EVAL_GEMINI_API_KEY")
        message = (f"Живая оценка не запущена: нет секрета {name}. Создайте ключ в другом проекте Google AI Studio "
                   "(у него своя бесплатная квота) и сохраните его как секрет репозитория.")
        print(message)
        step_summary([message])
        return 1
    channel = get_channel(cfg, args.channel)
    days = (cfg.get("eval") or {}).get("days", 3)
    try:
        report = evaluate.live(cfg, channel, llm, state, days=days)
    except Exception as e:
        problem = redact(f"{type(e).__name__}: {e}")[:600]
        print(problem)
        step_summary(["### Живая оценка не удалась", "", problem])
        return 1
    history = state.evals(channel["key"])
    lines = evaluate.live_markdown(report, history[-1] if history else None)
    if not args.dry_run:
        state.save_eval(channel["key"], dt.datetime.now(dt.timezone.utc), report)
    print("\n".join(lines))
    step_summary(lines)
    return 0


def cmd_navigation(cfg, args) -> int:
    ch = get_channel(cfg, args.channel)
    text = render.render_navigation(ch)
    if args.dry_run:
        print(text)
        return 0
    tg = Telegram(secret("TELEGRAM_BOT_TOKEN"))
    if args.publish:
        message = tg.send_message(ch["chat_id"], text, silent=True)
        tg.pin(ch["chat_id"], message["message_id"])
        print(f"navigation posted and pinned in {ch['chat_id']}")
        step_summary([f"### Навигация опубликована и закреплена в {ch['chat_id']}"])
    else:
        tg.send_message(secret("ADMIN_CHAT_ID"), text)
        print("navigation preview sent to ADMIN_CHAT_ID")
        step_summary([f"### Предпросмотр навигации для {ch['chat_id']} отправлен вам в Telegram"])
    return 0


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="newsbot", description="Fintech Daily news bot")
    parser.add_argument("command", choices=["check", "models", "whoami", "preview", "prepare", "publish", "inbox", "tick",
                                            "scheduled", "navigation", "edit", "weekly", "pinned", "metrics", "eval",
                                            "eval-live"])
    parser.add_argument("--channel", default="ru")
    parser.add_argument("--cron", help="ignored: kept for runs queued before wake-ups replaced cron tasks")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending anything to Telegram")
    parser.add_argument("--fake-llm", action="store_true", help="offline test without calling the model")
    parser.add_argument("--publish", action="store_true", help="for 'navigation': post and pin in the channel")
    parser.add_argument("--apply", action="store_true", help="for 'edit': edit the posts in the channels")
    parser.add_argument("--now", help="override current time (ISO, for tests)")
    parser.add_argument("--days", type=int, default=14, help="for 'metrics' and 'eval': how many days to cover")
    parser.add_argument("--json", action="store_true", help="for 'metrics': print the raw numbers as JSON")
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.command in ("tick", "scheduled"):   # "scheduled": runs queued before the switch to wake-ups
        return cmd_tick(cfg, args)
    if args.command == "check":
        return cmd_check(cfg, args)
    if args.command == "whoami":
        return cmd_whoami(cfg, args)
    if args.command == "models":
        return cmd_models(cfg, args)
    if args.command == "inbox":
        return cmd_inbox(cfg, args)
    if args.command == "navigation":
        return cmd_navigation(cfg, args)
    if args.command == "edit":
        return cmd_edit(cfg, args)
    if args.command == "pinned":
        return cmd_pinned(cfg, args)
    if args.command == "metrics":
        return cmd_metrics(cfg, args)
    if args.command in ("eval", "eval-live"):
        return cmd_eval(cfg, args)
    return cmd_pipeline(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
