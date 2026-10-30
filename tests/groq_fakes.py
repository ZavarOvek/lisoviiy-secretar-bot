"""Підробний `AsyncGroq` для тестів: черга відповідей і винятків замість мережі."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import groq
import httpx
from groq.types.chat import ChatCompletion

URL = "https://api.groq.com/openai/v1/chat/completions"


def completion(
    content: str | dict[str, Any],
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 1000,
    cached_tokens: int | None = 600,
    completion_tokens: int = 100,
) -> ChatCompletion:
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }
    )


def rate_limited(retry_after: str | None) -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", URL))
    return groq.RateLimitError("rate limited", response=response, body=None)


def status_error(cls: type[groq.APIStatusError], status: int, body: Any = None):
    response = httpx.Response(status, request=httpx.Request("POST", URL))
    return cls("error", response=response, body=body)


def connection_error() -> groq.APIConnectionError:
    return groq.APIConnectionError(request=httpx.Request("POST", URL))


class FakeGroq:
    """`client.chat.completions.create(**kwargs)`: віддає `outcomes` по черзі.

    `delay` — пауза всередині виклику, щоб ловити паралельні виклики.
    """

    def __init__(self, *outcomes: Any, delay: float = 0.0):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            outcome = self.outcomes.pop(0)
        finally:
            self.active -= 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]
