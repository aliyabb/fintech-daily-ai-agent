"""Small JSON state: what was published, today's drafts, moderation decisions."""
from __future__ import annotations

import datetime as dt
import json
import pathlib


class State:
    def __init__(self, directory: pathlib.Path):
        self.dir = pathlib.Path(directory)
        (self.dir / "drafts").mkdir(parents=True, exist_ok=True)

    def _read(self, name: str, default):
        path = self.dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write(self, name: str, data) -> None:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # --- published stories (dedupe across days) ---
    def posted_keys(self, channel: str) -> set[str]:
        return set(self._read(f"posted-{channel}.json", {}))

    def mark_posted(self, channel: str, keys: list[str], day: dt.date, keep_days: int = 21) -> None:
        posted = self._read(f"posted-{channel}.json", {})
        for key in keys:
            posted[key] = day.isoformat()
        cutoff = (day - dt.timedelta(days=keep_days)).isoformat()
        self._write(f"posted-{channel}.json", {k: v for k, v in posted.items() if v >= cutoff})

    # --- drafts ---
    def draft(self, draft_id: str) -> dict | None:
        return self._read(f"drafts/{draft_id}.json", None)

    def drafts(self, channel: str) -> list[dict]:
        """Every stored draft of a channel, oldest first."""
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((self.dir / "drafts").glob(f"{channel}-*.json"))]

    def save_draft(self, draft: dict) -> None:
        self._write(f"drafts/{draft['id']}.json", draft)

    def prune_drafts(self, today: dt.date, keep_days: int = 14) -> None:
        cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
        for path in (self.dir / "drafts").glob("*.json"):
            if json.loads(path.read_text(encoding="utf-8")).get("date", "9999") < cutoff:
                path.unlink()

    # --- telegram moderation ---
    def telegram(self) -> dict:
        return self._read("telegram.json", {"offset": None, "decisions": {}})

    def save_telegram(self, data: dict) -> None:
        self._write("telegram.json", data)

    # --- prompt evaluations (python -m newsbot eval-live) ---
    def evals(self, channel: str) -> list[dict]:
        """Saved live evaluations of a channel, oldest first."""
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((self.dir / "evals").glob(f"{channel}-*.json"))]

    def save_eval(self, channel: str, moment: dt.datetime, result: dict) -> None:
        self._write(f"evals/{channel}-{moment.strftime('%Y%m%dT%H%M')}.json", {**result, "at": moment.isoformat()})

    # --- settings changed through the bot (publishing mode) ---
    def settings(self) -> dict:
        return self._read("settings.json", {})

    def save_settings(self, data: dict) -> None:
        self._write("settings.json", data)
