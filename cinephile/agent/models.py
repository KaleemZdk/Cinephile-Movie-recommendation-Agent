"""Choosing and calling the Gemini model.

Free-tier quotas are per model, so a short chain of models keeps the app usable when one
runs out. A model that returns 429 or 503 is skipped for a while and the next one is
tried at once, instead of retrying for minutes.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import (
    GoogleAPIError,
    GoogleAuthenticationError,
    GoogleInvalidRequestError,
    GoogleModelNotFoundError,
    GooglePermissionDeniedError,
    GoogleRateLimitError,
)

log = logging.getLogger(__name__)

QUOTA_COOLDOWN = (60.0, 1800.0)  # min, max seconds to skip a model after a 429
UNAVAILABLE_COOLDOWN = 45.0  # seconds to skip a model after a 503 or a timeout
RECOVERABLE = frozenset({"quota", "unavailable", "missing"})

# Thinking is the main latency cost of the flash models; it is switched off where allowed.
# These models reject thinking_budget=0 (more are discovered and added at runtime).
REJECTS_THINKING_BUDGET = frozenset({"gemini-3.5-flash-lite"})


class LLMUnavailableError(Exception):
    """Every configured model failed in a way that waiting may fix (or the key is wrong)."""

    def __init__(self, kind: str, retry_in: int | None = None) -> None:
        self.kind = kind
        self.retry_in = retry_in
        if kind == "quota":
            message = "The Gemini models are out of free quota right now."
        elif kind == "auth":
            message = "The Gemini API key was rejected. Check GEMINI_API_KEY in .env."
        else:
            message = "The Gemini models are overloaded or not responding right now."
        if retry_in and kind != "auth":
            minutes = max(1, round(retry_in / 60))
            message += f" Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
        super().__init__(message)


def classify_error(exc: BaseException) -> str:
    """quota | unavailable | missing | auth | invalid | other"""
    if isinstance(exc, GoogleRateLimitError):
        return "quota"
    if isinstance(exc, GoogleModelNotFoundError):
        return "missing"
    if isinstance(exc, GoogleInvalidRequestError):
        return "invalid"
    if isinstance(exc, GoogleAuthenticationError | GooglePermissionDeniedError):
        return "auth"
    if isinstance(exc, GoogleAPIError | TimeoutError) or "Timeout" in type(exc).__name__:
        return "unavailable"
    return "other"


def retry_after_seconds(message: str) -> float | None:
    """Read "'retryDelay': '21746s'" or "retry in 6h2m26.1s" from a Gemini error message."""
    if match := re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", message):
        return float(match.group(1))
    match = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", message)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


class _ModelSkippedError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ModelRouter:
    """Calls the first usable model of a chain, with tools bound."""

    def __init__(self, primary: str, fallbacks: Sequence[str], api_key: str | None, timeout: float) -> None:
        self.primary = primary
        self.fallbacks = tuple(fallbacks)
        self._api_key = api_key
        self._timeout = timeout
        self._tools: list = []
        self._bound: dict[str, object] = {}
        self._cooldown: dict[str, float] = {}
        self._rejects_thinking: set[str] = set(REJECTS_THINKING_BUDGET)

    def bind_tools(self, tools: Iterable) -> None:
        self._tools = list(tools)
        self._bound.clear()

    def chain(self, preferred: str | None = None) -> list[str]:
        models = [preferred.strip()] if preferred and preferred.strip() else []
        return list(dict.fromkeys([*models, self.primary, *self.fallbacks]))

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "name": model,
                "available": self._cooldown.get(model, 0) <= now,
                "retry_in": max(0, int(self._cooldown.get(model, 0) - now)),
            }
            for model in self.chain()
        ]

    def _create_llm(self, model: str) -> ChatGoogleGenerativeAI:
        if not self._api_key:
            raise LLMUnavailableError("auth")
        options: dict = {"model": model, "google_api_key": self._api_key, "max_retries": 0, "timeout": self._timeout}
        if model not in self._rejects_thinking:
            options["thinking_budget"] = 0
        return ChatGoogleGenerativeAI(**options)

    def _llm_for(self, model: str):
        if model not in self._bound:
            self._bound[model] = self._create_llm(model).bind_tools(self._tools)
        return self._bound[model]

    def _start_cooldown(self, model: str, kind: str, exc: BaseException) -> float:
        if kind == "quota":
            low, high = QUOTA_COOLDOWN
            wait = min(max(retry_after_seconds(str(exc)) or low, low), high)
        else:
            wait = UNAVAILABLE_COOLDOWN
        self._cooldown[model] = time.monotonic() + wait
        return wait

    def _soonest_retry(self) -> int | None:
        now = time.monotonic()
        pending = [until - now for until in self._cooldown.values() if until > now]
        return int(min(pending)) if pending else None

    async def _invoke_model(self, model: str, messages: list[BaseMessage]) -> AIMessage:
        started = time.perf_counter()
        try:
            response = await self._llm_for(model).ainvoke(messages)
        except Exception as exc:
            kind = classify_error(exc)
            if kind == "invalid" and model not in self._rejects_thinking:
                log.info("%s rejected thinking_budget=0; retrying without it", model)
                self._rejects_thinking.add(model)
                self._bound.pop(model, None)
                return await self._invoke_model(model, messages)
            if kind in RECOVERABLE:
                wait = self._start_cooldown(model, kind, exc)
                log.warning(
                    "model %s %s after %.1fs; skipping it for %.0fs", model, kind, time.perf_counter() - started, wait
                )
                raise _ModelSkippedError(kind) from exc
            if kind == "auth":
                raise LLMUnavailableError("auth") from exc
            raise
        calls = [call["name"] for call in response.tool_calls]
        log.info("llm model=%s ms=%d tool_calls=%s", model, (time.perf_counter() - started) * 1000, calls)
        return response

    async def invoke(self, messages: list[BaseMessage], preferred: str | None = None) -> AIMessage:
        """Answer with the first model that works. Raises LLMUnavailableError if none does."""
        now = time.monotonic()
        chain = self.chain(preferred)
        ready = [m for m in chain if self._cooldown.get(m, 0) <= now]
        # If every model is cooling down, still try the one that recovers first.
        candidates = ready or sorted(chain, key=lambda m: self._cooldown.get(m, 0))[:1]

        last_kind = "unavailable"
        for model in candidates:
            try:
                return await self._invoke_model(model, messages)
            except _ModelSkippedError as skipped:
                last_kind = skipped.kind
        raise LLMUnavailableError(last_kind, self._soonest_retry())
