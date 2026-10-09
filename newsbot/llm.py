"""Minimal client for OpenAI-compatible chat APIs (Gemini, DeepSeek, OpenRouter, LM Studio...)."""
from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request

from .config import redact

DAILY_QUOTA = re.compile(r"per\s*day|PerDay|daily", re.I)
RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')
RETRY_IN = re.compile(r"retry in\s+((?:\d+(?:\.\d+)?\s*(?:h|ms|m|s)\s*)+)", re.I)
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(h|ms|m|s)", re.I)


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, models: list[str], temperature: float = 0.3,
                 timeout: int = 180, retries: int = 3, steps: dict | None = None, rpm: dict | None = None,
                 clock=None, sleep=None, rounds: int = 3, round_delay: float = 15.0,
                 last_resort: list[str] | None = None, patient_steps: list[str] | None = None,
                 reserve_seconds: float = 240.0):
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.models = list(models)
        self.steps = {step: list(order) for step, order in (steps or {}).items() if order}
        self.rpm = dict(rpm or {})
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries
        self.rounds = rounds              # how many times to go round every model when they are all busy
        self.round_delay = round_delay
        # In patient steps the weakest fallback answers only after the main models stayed busy for every round:
        # it tends to ignore instructions such as "do not repeat a published story".
        self.last_resort = set(last_resort or [])
        self.patient_steps = set(patient_steps or [])
        self.reserve_seconds = reserve_seconds   # stop waiting when less than this is left of the time budget
        self.last_model = None
        self.models_used: list[str] = []
        self.step_log: list[tuple[str, str]] = []   # (step, model that answered)
        self.exhausted: set[str] = set()            # models whose daily quota ran out during this run
        self.deadline: float | None = None
        self.notes: list[str] = []   # errors from attempts that were followed by a successful answer
        # Every HTTP request with its outcome and token usage: the cost and quota of each step can be measured.
        # Failed requests count too: Google appears to charge "overloaded" answers to the daily quota.
        self.usage_log: list[dict] = []
        self._clock = clock
        self._sleep_fn = sleep
        self._last_request: dict[str, float] = {}

    def models_for(self, step: str) -> list[str]:
        return self.steps.get(step) or self.models

    def preferred(self, step: str) -> str:
        return self.models_for(step)[0]

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    def set_budget(self, seconds: float | None) -> None:
        """Limit the total time of the following calls (one digest), so a hanging API cannot use up the whole job."""
        self.deadline = self._now() + seconds if seconds else None

    def _remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - self._now()

    def _check_budget(self, errors: list[str]) -> None:
        remaining = self._remaining()
        if remaining is not None and remaining < 5:
            raise LLMError(redact("time budget exhausted: " + " | ".join(errors[-4:])))

    def _sleep(self, seconds: float) -> None:
        remaining = self._remaining()
        (self._sleep_fn or time.sleep)(max(0.0, seconds if remaining is None else min(seconds, remaining)))

    def _pace(self, model: str) -> None:
        """Stay under the model's requests-per-minute limit by spacing its requests evenly."""
        limit = self.rpm.get(model)
        last = self._last_request.get(model)
        if limit and last is not None:
            wait = last + 60.0 / limit + 0.5 - self._now()
            if wait > 0:
                self._sleep(wait)
        self._last_request[model] = self._now()

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def list_models(self) -> list[str]:
        request = urllib.request.Request(self.base_url + "models",
                                         headers={"Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(m.get("id", "").replace("models/", "") for m in data.get("data", []))

    def json_completion(self, system: str, user: str, schema: dict, schema_name: str) -> dict:
        """Ask for JSON that follows `schema`, trying the step's models in order with retries."""
        errors = []
        round_number = 0
        while True:
            final = round_number >= self.rounds - 1
            patient = schema_name in self.patient_steps and not final
            busy = False          # a model was overloaded (HTTP 5xx): worth coming back to it
            for model in self.models_for(schema_name):
                if patient and model in self.last_resort:
                    continue
                if model in self.exhausted:
                    errors.append(f"{model}: daily quota used up")
                    continue
                for mode in ("json_schema", "json_object"):
                    self._check_budget(errors)
                    body = {
                        "model": model,
                        "temperature": self.temperature,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    }
                    if mode == "json_schema":
                        body["response_format"] = {"type": "json_schema",
                                                   "json_schema": {"name": schema_name, "schema": schema}}
                    else:
                        body["response_format"] = {"type": "json_object"}
                        body["messages"][0]["content"] += "\n\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False)
                    outcome = self._try(body, errors, model, mode, schema_name)
                    if outcome == "unsupported":
                        continue          # this response_format is not supported: try json_object
                    if outcome in ("next_model", "busy"):
                        busy = busy or outcome == "busy"
                        break
                    self.last_model = model
                    self.models_used.append(model)
                    self.step_log.append((schema_name, model))
                    self.notes = (self.notes + errors)[-20:]
                    return outcome
            if final:
                break
            delay = self.round_delay * (round_number + 1)
            remaining = self._remaining()
            short = remaining is not None and remaining < self.reserve_seconds + delay
            if not busy or short:
                if not patient:
                    break
                round_number = self.rounds - 1   # nothing to wait for, or no time: last pass, fallback included
                continue
            self._check_budget(errors)
            self._sleep(delay)   # every model is busy: wait and try them again
            round_number += 1
        raise LLMError(redact("all models failed: " + " | ".join(errors[-6:])))

    def _record(self, step: str, model: str, outcome: str, usage: dict | None = None) -> None:
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        # Thinking tokens are billed as output: when the API leaves them out of completion_tokens,
        # they still show in total_tokens.
        output = max(int(usage.get("completion_tokens") or 0), int(usage.get("total_tokens") or 0) - prompt_tokens)
        self.usage_log.append({"step": step, "model": model, "outcome": outcome,
                               "prompt_tokens": prompt_tokens, "completion_tokens": output})

    def _try(self, body: dict, errors: list[str], model: str, mode: str, step: str = ""):
        for attempt in range(self.retries):
            self._check_budget(errors)
            self._pace(model)
            remaining = self._remaining()
            timeout = self.timeout if remaining is None else max(5.0, min(self.timeout, remaining))
            data = None
            try:
                data = self._post("chat/completions", body, timeout)
                content = data["choices"][0]["message"]["content"] or ""
                result = parse_json(content)
                self._record(step, model, "ok", data.get("usage"))
                return result
            except urllib.error.HTTPError as e:
                self._record(step, model, f"http_{e.code}")
                try:
                    detail = e.read().decode("utf-8", "ignore")[:4000]
                except OSError:
                    detail = ""
                errors.append(f"{model}/{mode}: HTTP {e.code} {api_error_summary(detail)}")
                if e.code == 400 and mode == "json_schema":
                    return "unsupported"
                if e.code == 429:
                    hint = retry_hint(e, detail)
                    if DAILY_QUOTA.search(detail) or (hint is not None and hint > 90):
                        self.exhausted.add(model)      # the daily limit: no point asking this model again today
                        errors.append(f"{model}: daily quota used up")
                        return "next_model"
                    self._sleep(min(60.0, hint) if hint is not None else min(60.0, 5.0 * 2 ** attempt))
                    continue
                if e.code >= 500:
                    return "busy"      # overloaded: another model may answer right now
                return "next_model"
            except (TimeoutError, socket.timeout):
                self._record(step, model, "timeout")
                errors.append(f"{model}/{mode}: no answer in {timeout:.0f}s")
                return "next_model"            # a hanging model: switch instead of waiting again
            except urllib.error.URLError as e:
                self._record(step, model, "network")
                if isinstance(e.reason, (TimeoutError, socket.timeout)):
                    errors.append(f"{model}/{mode}: no connection in {timeout:.0f}s")
                    return "next_model"
                errors.append(f"{model}/{mode}: network error {type(e.reason).__name__}")
                self._sleep(3 * (attempt + 1))
            except (OSError, http.client.HTTPException) as e:   # dropped connection, TLS error
                self._record(step, model, "network")
                errors.append(f"{model}/{mode}: {type(e).__name__}")
                self._sleep(3 * (attempt + 1))
            except (KeyError, IndexError, TypeError, ValueError) as e:   # malformed or unexpected answer
                # an answer that was not valid JSON still used tokens
                self._record(step, model, "bad_answer", data.get("usage") if isinstance(data, dict) else None)
                errors.append(f"{model}/{mode}: {type(e).__name__} {redact(str(e))[:200]}")
                self._sleep(3 * (attempt + 1))
        return "next_model"


def api_error_summary(body: str, limit: int = 160) -> str:
    """The API's own error message on one line: the raw body is JSON spread over many lines."""
    message = body
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            error = data.get("error", data)
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
    except ValueError:
        pass
    return redact(re.sub(r"\s+", " ", message).strip())[:limit]


def duration_seconds(text: str) -> float:
    factors = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(value) * factors[unit.lower()] for value, unit in DURATION_PART.findall(text))


def retry_hint(error: urllib.error.HTTPError, body: str) -> float | None:
    """Seconds the API asks to wait: a Retry-After header, "retryDelay": "43s" or "Please retry in 43.2s"."""
    header = error.headers.get("Retry-After") if error.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = RETRY_DELAY.search(body)
    if match:
        return float(match.group(1))
    match = RETRY_IN.search(body)
    return duration_seconds(match.group(1)) if match else None


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    """Honour Retry-After when the API sends it, otherwise back off exponentially."""
    hint = retry_hint(error, "")
    return min(60.0, hint) if hint is not None else min(60.0, 5.0 * 2 ** attempt)


def parse_json(content: str) -> dict:
    content = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.S)
    if fenced:
        content = fenced.group(1)
    return json.loads(content)


SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "repeat_of": {"type": "string"},
                },
                "required": ["candidate_ids", "reason", "repeat_of"],
            },
        }
    },
    "required": ["stories"],
}

WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["top", "deals", "regulation", "trends"]},
                    "story_ids": {"type": "array", "items": {"type": "string"}},
                    "text": {"type": "string"},
                },
                "required": ["section", "story_ids", "text"],
            },
        },
    },
    "required": ["intro", "items"],
}

EDITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "why": {"type": "string"},
                    "source_quotes": {"type": "array", "items": {"type": "string"}},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "source_ids", "headline", "summary", "why", "source_quotes", "hashtags"],
            },
        }
    },
    "required": ["items"],
}

VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "ok": {"type": "boolean"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "repeat_of": {"type": "string"},
                },
                "required": ["index", "ok", "issues"],
            },
        }
    },
    "required": ["verdicts"],
}

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "why": {"type": "string"},
                    "source_quotes": {"type": "array", "items": {"type": "string"}},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "keep", "headline", "summary", "why", "source_quotes", "hashtags"],
            },
        }
    },
    "required": ["items"],
}

JUDGE_CRITERIA = ("accuracy", "tone", "why_value", "clarity")

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "version": {"type": "string", "enum": ["A", "B"]},
                    **{name: {"type": "integer", "minimum": 1, "maximum": 5} for name in JUDGE_CRITERIA},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "version", *JUDGE_CRITERIA, "issues"],
            },
        }
    },
    "required": ["scores"],
}


def usage_summary(calls: list[dict]) -> dict:
    """Requests and tokens of a run, in total and per model and per step (what a draft stores)."""
    def bucket() -> dict:
        return {"requests": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0}

    total, by_model, by_step = bucket(), {}, {}
    for call in calls:
        for target in (total, by_model.setdefault(call["model"], bucket()), by_step.setdefault(call["step"] or "?", bucket())):
            target["requests"] += 1
            target["ok"] += call["outcome"] == "ok"
            target["prompt_tokens"] += call["prompt_tokens"]
            target["completion_tokens"] += call["completion_tokens"]
    return {**total, "by_model": by_model, "by_step": by_step}


NUMBER = re.compile(r"[$€£]?\d[\d.,]*\s?(?:%|[MBK]\b|million|billion)?")


class FakeLLM:
    """Offline stand-in for dry runs: picks the first candidates, writes from their titles, approves everything."""

    last_model = "fake"

    def __init__(self, max_items: int = 5):
        self.max_items = max_items

    def json_completion(self, system: str, user: str, schema: dict, schema_name: str) -> dict:
        payload = json.loads(user[user.index("{"):]) if "{" in user else {}
        if schema_name == "selection":
            return {"stories": [{"candidate_ids": [c["id"]], "reason": "offline test", "repeat_of": ""}
                                for c in payload.get("candidates", [])[: self.max_items + 2]]}
        if schema_name == "digest":
            items = []
            for story in payload.get("stories", []):
                source = story["sources"][0]
                items.append({"index": story["index"], "source_ids": [source["id"]],
                              "headline": source["title"][:90],
                              "summary": (source["summary"] or source["title"])[:200],
                              "why": "Test entry generated offline.",
                              "source_quotes": [n.strip() for n in NUMBER.findall(source["title"])][:2],
                              "hashtags": []})
            return {"items": items}
        if schema_name == "weekly":
            stories = payload.get("stories", [])
            return {"intro": "Offline test.", "items": [
                {"section": ("top", "deals", "regulation")[n % 3], "story_ids": [s["id"]], "text": s["headline"]}
                for n, s in enumerate(stories[:9])] + [
                {"section": "trends", "story_ids": [s["id"] for s in stories[9:11]], "text": "Offline trend."}]}
        if schema_name == "rewrite":
            return {"items": [{**{k: e[k] for k in ("index", "headline", "summary", "why", "hashtags", "source_quotes")},
                               "keep": True} for e in payload.get("entries", [])]}
        if schema_name == "judge":
            return {"scores": [{"index": e["index"], "version": version, **{name: 4 for name in JUDGE_CRITERIA},
                                "issues": []} for e in payload.get("entries", []) for version in ("A", "B")]}
        if schema_name == "repair":
            return {"items": [{**{k: e[k] for k in ("index", "source_ids", "headline", "summary", "why", "hashtags")},
                               "source_quotes": []} for e in payload.get("entries", [])]}
        return {"verdicts": [{"index": e["index"], "ok": True, "issues": []} for e in payload.get("entries", [])]}
