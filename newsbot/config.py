"""Configuration and environment loading."""
from __future__ import annotations

import os
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_dotenv(path: pathlib.Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from a local .env file without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: pathlib.Path = ROOT / "config.yaml") -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, channel in cfg["channels"].items():
        channel["key"] = key
        channel["hashtags"] = dict(channel.get("hashtags") or {})
    return cfg


def channel(cfg: dict, key: str) -> dict:
    try:
        return cfg["channels"][key]
    except KeyError:
        raise SystemExit(f"unknown channel '{key}'; configured: {', '.join(cfg['channels'])}")


# A value that does not match was pasted with extra characters; the messages never include the value.
SECRET_FORMATS = {
    "TELEGRAM_BOT_TOKEN": (r"\d{5,15}:[A-Za-z0-9_-]{30,}", "digits, a colon and a long code, as issued by @BotFather"),
    "ADMIN_CHAT_ID": (r"-?\d{3,20}", "a number"),
}
REDACTED_ENV = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "EVAL_GEMINI_API_KEY")
TOKEN_PATTERNS = (re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,}"), re.compile(r"AIza[0-9A-Za-z_-]{20,}"))


def secret(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise SystemExit(f"environment variable {name} is not set (add it to .env or GitHub Secrets)")
        return value
    if any(ch.isspace() for ch in value) or value[0] in "\"'" or value[-1] in "\"'":
        raise SystemExit(f"{name} contains spaces, line breaks or quotes: save the secret again without them")
    pattern, expected = SECRET_FORMATS.get(name, (None, None))
    if pattern and not re.fullmatch(pattern, value):
        raise SystemExit(f"{name} has an unexpected format: expected {expected}")
    return value


def redact(text) -> str:
    """Remove secret values from text before it is logged, stored in bot-state or sent to Telegram."""
    text = str(text)
    for name in REDACTED_ENV:
        for part in os.environ.get(name, "").split():
            if len(part) >= 8:
                text = text.replace(part, "***")
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return text


def state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("STATE_DIR") or ROOT / "state")
