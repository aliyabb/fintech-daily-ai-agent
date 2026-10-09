"""Telegram Bot API calls. The token never appears in errors or logs."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class TelegramError(Exception):
    def __init__(self, message: str, retry_after: float | None = None, transient: bool = False,
                 unknown_outcome: bool = False):
        super().__init__(message)
        self.retry_after = retry_after          # Telegram asked to wait (HTTP 429)
        self.transient = transient              # safe to repeat: the request was not processed
        self.unknown_outcome = unknown_outcome  # the request may have been processed, but no answer came back


class Telegram:
    MAX_WAIT = 60

    def __init__(self, token: str, timeout: int = 40, retries: int = 3, sleep=time.sleep):
        self._token = token
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    def call(self, method: str, **params):
        for attempt in range(self.retries):
            try:
                return self._call_once(method, params)
            except TelegramError as e:
                if attempt == self.retries - 1:
                    raise
                if e.retry_after is not None and e.retry_after <= self.MAX_WAIT:
                    self._sleep(e.retry_after + 1)
                elif e.transient:
                    self._sleep(2 * (attempt + 1))
                else:
                    raise
        raise TelegramError(f"{method}: no attempts made")

    def _call_once(self, method: str, params: dict):
        # A message can be delivered even though the answer is lost, so unanswered sends are never repeated.
        repeatable = method != "sendMessage"
        try:
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self._token}/{method}",
                data=json.dumps(params).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode("utf-8"))
            except (ValueError, OSError):
                raise TelegramError(f"{method}: HTTP {e.code}", transient=e.code >= 500 and repeatable,
                                    unknown_outcome=e.code >= 500 and not repeatable) from None
        except urllib.error.URLError as e:
            # The request did not get through: DNS, refused connection, TLS, connection timeout.
            raise TelegramError(f"{method}: network error {type(e.reason).__name__}", transient=True) from None
        except Exception as e:
            # Sent without an answer (read timeout, dropped connection) or a malformed URL: never show the URL.
            raise TelegramError(f"{method}: no answer ({type(e).__name__})", transient=repeatable,
                                unknown_outcome=not repeatable) from None
        if not data.get("ok"):
            code = data.get("error_code") or 0
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}", retry_after=retry_after,
                                transient=code >= 500 and repeatable, unknown_outcome=code >= 500 and not repeatable)
        return data["result"]

    def send_message(self, chat_id, text: str, buttons: list[list[dict]] | None = None, silent: bool = False,
                     force_reply: str | None = None):
        """force_reply: the editor's app opens a reply to this message at once, with this placeholder."""
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}, "disable_notification": silent}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        elif force_reply is not None:
            params["reply_markup"] = {"force_reply": True, "input_field_placeholder": force_reply[:64]}
        return self.call("sendMessage", **params)

    def edit_message(self, chat_id, message_id: int, text: str):
        """Replace the text of a message the bot sent; an edit that changes nothing counts as done."""
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                             link_preview_options={"is_disabled": True})
        except TelegramError as e:
            if "message is not modified" in str(e):
                return None
            raise

    def get_updates(self, offset: int | None = None, allowed: list[str] | None = None):
        """allowed=None → only messages and button presses; allowed=[] → every update type (diagnostics)."""
        params = {"timeout": 0, "allowed_updates": ["message", "callback_query"] if allowed is None else allowed}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def answer_callback(self, callback_id: str, text: str):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def set_buttons(self, chat_id, message_id: int, buttons: list[list[dict]] | None):
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": buttons or []})

    def set_commands(self, commands: list[dict], chat_id):
        """Command menu shown only in this chat."""
        return self.call("setMyCommands", commands=commands, scope={"type": "chat", "chat_id": chat_id})

    def pinned_message(self, chat_id) -> dict | None:
        """The channel's pinned message (the latest one, if several), with its text and formatting entities."""
        return self.call("getChat", chat_id=chat_id).get("pinned_message")

    def forward_message(self, chat_id, from_chat_id, message_id: int):
        return self.call("forwardMessage", chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id,
                         disable_notification=True)

    def delete_message(self, chat_id, message_id: int):
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def edit_formatted(self, chat_id, message_id: int, text: str, entities: list[dict], caption: bool = False):
        """Replace a message's text (or a media post's caption) keeping its formatting as entities."""
        if caption:
            return self.call("editMessageCaption", chat_id=chat_id, message_id=message_id,
                             caption=text, caption_entities=entities)
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, entities=entities)

    def pin(self, chat_id, message_id: int):
        return self.call("pinChatMessage", chat_id=chat_id, message_id=message_id, disable_notification=True)
