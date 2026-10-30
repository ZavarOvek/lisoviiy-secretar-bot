"""Виклик Groq: не більше одного одночасно на процес, 429 обробляється одним повтором."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import groq

from lisovyi.answer import texts
from lisovyi.answer.prompt import RESPONSE_FORMAT

log = logging.getLogger(__name__)

TEMPERATURE = 0.2
# Скільки чекати на 429, якщо Groq не прислав retry-after.
DEFAULT_RETRY_AFTER = 20.0
# Довше звір чекати не має: тоді одразу «забагато питань».
MAX_WAIT = 30.0

Notify = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    # None, якщо Groq не повернув prompt_tokens_details.
    cached_tokens: int | None
    completion_tokens: int

    def __add__(self, other: "Usage") -> "Usage":
        cached = (
            None
            if self.cached_tokens is None and other.cached_tokens is None
            else (self.cached_tokens or 0) + (other.cached_tokens or 0)
        )
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            cached,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True)
class Completion:
    model: str
    content: str
    finish_reason: str
    usage: Usage | None
    seconds: float


class LLMError(Exception):
    """Відповіді моделі не буде; `reply` — що сказати звірові."""

    reply = texts.BROKEN


class LLMBusy(LLMError):
    """429, на який не допоміг ні повтор, ні запасна модель."""

    reply = texts.BUSY


class LLMBroken(LLMError):
    """Мережа, таймаут, 5xx чи інша помилка API: без повторів."""


class OutputRejected(Exception):
    """Groq відкинув згенерований JSON (400 json_validate_failed): як невалідний JSON."""


class _RateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after


class LLMClient:
    """Обгортка над `AsyncGroq`. Один екземпляр на процес: семафор живе в ньому.

    `client` створюється з `max_retries=0`: повтори тут, щоб звір бачив, що відбувається.
    """

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        fallback_model: str = "",
        reasoning_effort: str = "low",
        max_completion_tokens: int = 1024,
        max_wait: float = MAX_WAIT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self.model = model
        self.fallback_model = fallback_model
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self.max_wait = max_wait
        self._sleep = sleep
        # Паралельні виклики при 8K TPM нічого не прискорюють, лише плодять 429.
        self._lock = asyncio.Semaphore(1)

    async def complete(
        self, messages: list[dict[str, str]], notify: Notify | None = None
    ) -> Completion:
        """Один запит до моделі; на 429 — не більше одного повтору.

        Повтор — одразу запасною моделлю, якщо вона задана. Інакше, якщо Groq просить
        чекати не довше `max_wait`, — `notify(WAIT)`, пауза й той самий запит ще раз.
        """
        async with self._lock:
            try:
                return await self._call(self.model, messages)
            except _RateLimited as limited:
                retry_after = limited.retry_after
            if self.fallback_model:
                return await self._call_or_busy(self.fallback_model, messages)
            if retry_after > self.max_wait:
                raise LLMBusy
            if notify is not None:
                await _notify_quietly(notify, texts.WAIT)
            await self._sleep(retry_after)
            return await self._call_or_busy(self.model, messages)

    async def _call_or_busy(self, model: str, messages: list[dict[str, str]]) -> Completion:
        try:
            return await self._call(model, messages)
        except _RateLimited:
            raise LLMBusy from None

    async def _call(self, model: str, messages: list[dict[str, str]]) -> Completion:
        started = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                response_format=RESPONSE_FORMAT,
                reasoning_effort=self.reasoning_effort,
                max_completion_tokens=self.max_completion_tokens,
                temperature=TEMPERATURE,
            )
        except groq.RateLimitError as error:
            retry_after = retry_after_seconds(error.response.headers)
            log.warning("groq 429: model=%s retry_after=%.0fs", model, retry_after)
            raise _RateLimited(retry_after) from error
        except groq.BadRequestError as error:
            if _error_code(error) == "json_validate_failed":
                log.warning("groq rejected the generated JSON: model=%s", model)
                raise OutputRejected from error
            _log_failure(model, error)
            raise LLMBroken from error
        except groq.APIError as error:
            _log_failure(model, error)
            raise LLMBroken from error
        choice = response.choices[0]
        return Completion(
            model=model,
            content=choice.message.content or "",
            finish_reason=choice.finish_reason or "",
            usage=_usage(response.usage),
            seconds=time.monotonic() - started,
        )


def retry_after_seconds(headers: Mapping[str, str] | None) -> float:
    """Секунди з заголовка retry-after; без нього чи з датою замість числа — 20."""
    value = headers.get("retry-after") if headers is not None else None
    try:
        seconds = float(value) if value is not None else math.nan
    except ValueError:
        seconds = math.nan
    return seconds if math.isfinite(seconds) and seconds >= 0 else DEFAULT_RETRY_AFTER


async def _notify_quietly(notify: Notify, text: str) -> None:
    # Проміжне повідомлення — лише ввічливість: якщо воно не дійшло, відповідь усе одно буде.
    try:
        await notify(text)
    except Exception as error:
        log.warning("intermediate message failed: %s", type(error).__name__)


def _error_code(error: groq.APIStatusError) -> str | None:
    body = error.body
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            return inner.get("code")
    return None


def _log_failure(model: str, error: groq.APIError) -> None:
    # Лише тип і статус: текст помилки API не потрібен, а ключ туди потрапляти не має.
    log.error(
        "groq call failed: %s model=%s status=%s",
        type(error).__name__,
        model,
        getattr(error, "status_code", None),
    )


def _usage(usage: Any) -> Usage | None:
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    return Usage(
        prompt_tokens=usage.prompt_tokens,
        cached_tokens=getattr(details, "cached_tokens", None),
        completion_tokens=usage.completion_tokens,
    )
