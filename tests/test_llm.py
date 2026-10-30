import asyncio
import hashlib
import logging
from unittest.mock import AsyncMock

import groq
import pytest
from groq_fakes import FakeGroq, completion, connection_error, rate_limited, status_error

from lisovyi.answer import texts
from lisovyi.answer.llm import (
    DEFAULT_RETRY_AFTER,
    RETRY_MARGIN,
    LLMBroken,
    LLMBusy,
    LLMClient,
    OutputRejected,
    Usage,
    retry_after_seconds,
)
from lisovyi.answer.prompt import RESPONSE_FORMAT, RESPONSE_SCHEMA, SYSTEM_PROMPT, build_messages
from lisovyi.search.retrieve import retrieve

MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
OK = completion({"kind": "answer", "text": "Так.", "sources": [1]})


def _client(fake: FakeGroq, **kwargs) -> tuple[LLMClient, AsyncMock]:
    sleep = AsyncMock()
    return LLMClient(fake, "main-model", sleep=sleep, **kwargs), sleep


# --- промпт і запит


def test_system_prompt_is_first_and_identical_between_questions(archive_search):
    index, resolver = archive_search
    first = build_messages(
        "Які гриби біля сосон?", retrieve("Які гриби біля сосон?", index, resolver)
    )
    second = build_messages(
        "Коли збори в ліщині?", retrieve("Коли збори в ліщині?", index, resolver)
    )
    assert first[0] == second[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert first[0]["content"].encode() == second[0]["content"].encode()
    assert first[1]["role"] == "user"
    assert first[1]["content"].startswith("Питання звіра: Які гриби біля сосон?\n\n")
    assert first[1]["content"] != second[1]["content"]


def test_system_prompt_digest():
    # Зміна промпту змінює поведінку моделі й ламає кеш Groq: лише свідомо, разом із цим хешем.
    digest = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert digest == "4d63c6e0c8c1dd36ad0ff3dbb31a506345f90e710b78bed472538be4dce7de04"


def test_system_prompt_has_no_placeholders():
    # Жодних змінних: будь-яка різниця між запитами в цьому тексті ламає кеш Groq.
    assert "{" not in SYSTEM_PROMPT.split("Відповідай JSON")[0]


async def test_request_parameters():
    fake = FakeGroq(OK)
    llm, _ = _client(fake, reasoning_effort="low", max_completion_tokens=1024)
    result = await llm.complete(MESSAGES)

    [call] = fake.calls
    assert call == {
        "model": "main-model",
        "messages": MESSAGES,
        "response_format": RESPONSE_FORMAT,
        "reasoning_effort": "low",
        "max_completion_tokens": 1024,
        "temperature": 0.2,
    }
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"] == RESPONSE_SCHEMA
    assert result.content == OK.choices[0].message.content
    assert (result.model, result.finish_reason) == ("main-model", "stop")
    assert result.usage == Usage(prompt_tokens=1000, cached_tokens=600, completion_tokens=100)


def test_response_schema_matches_the_brief():
    assert RESPONSE_SCHEMA == {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "text", "sources"],
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["answer", "variants", "ask_place", "partial", "not_found"],
            },
            "text": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "integer"}},
        },
    }


async def test_usage_without_cache_details():
    fake = FakeGroq(completion("{}", cached_tokens=None))
    result = await _client(fake)[0].complete(MESSAGES)
    assert result.usage == Usage(prompt_tokens=1000, cached_tokens=None, completion_tokens=100)


def test_usage_sum():
    assert Usage(10, None, 1) + Usage(5, None, 2) == Usage(15, None, 3)
    assert Usage(10, 4, 1) + Usage(5, None, 2) == Usage(15, 4, 3)


# --- 429


async def test_429_with_fallback_goes_straight_to_the_fallback_model():
    fake = FakeGroq(rate_limited("5"), OK)
    llm, sleep = _client(fake, fallback_model="spare-model")
    notify = AsyncMock()

    result = await llm.complete(MESSAGES, notify)

    assert fake.models == ["main-model", "spare-model"]
    assert result.model == "spare-model"
    notify.assert_not_called()
    sleep.assert_not_called()


async def test_429_without_fallback_waits_and_repeats_once():
    fake = FakeGroq(rate_limited("5"), OK)
    llm, sleep = _client(fake)
    notify = AsyncMock()

    result = await llm.complete(MESSAGES, notify)

    assert fake.models == ["main-model", "main-model"]
    assert result.content == OK.choices[0].message.content
    notify.assert_awaited_once_with(texts.WAIT)
    sleep.assert_awaited_once_with(5.0 + RETRY_MARGIN)


async def test_429_with_long_retry_after_is_busy_at_once():
    fake = FakeGroq(rate_limited("60"))
    llm, sleep = _client(fake)
    notify = AsyncMock()

    with pytest.raises(LLMBusy) as caught:
        await llm.complete(MESSAGES, notify)

    assert caught.value.reply == texts.BUSY
    assert len(fake.calls) == 1
    notify.assert_not_called()
    sleep.assert_not_called()


async def test_429_on_the_repeat_is_busy():
    fake = FakeGroq(rate_limited("5"), rate_limited("5"))
    llm, sleep = _client(fake)
    with pytest.raises(LLMBusy):
        await llm.complete(MESSAGES, AsyncMock())
    assert len(fake.calls) == 2
    sleep.assert_awaited_once_with(5.0 + RETRY_MARGIN)


async def test_429_from_the_fallback_model_is_busy():
    fake = FakeGroq(rate_limited("5"), rate_limited("5"))
    llm, sleep = _client(fake, fallback_model="spare-model")
    with pytest.raises(LLMBusy):
        await llm.complete(MESSAGES, AsyncMock())
    assert fake.models == ["main-model", "spare-model"]
    sleep.assert_not_called()


async def test_429_without_retry_after_waits_the_default():
    fake = FakeGroq(rate_limited(None), OK)
    llm, sleep = _client(fake)
    await llm.complete(MESSAGES)
    sleep.assert_awaited_once_with(DEFAULT_RETRY_AFTER + RETRY_MARGIN)


async def test_failed_intermediate_message_does_not_stop_the_answer(caplog):
    fake = FakeGroq(rate_limited("1"), OK)
    llm, _ = _client(fake)
    notify = AsyncMock(side_effect=RuntimeError("telegram is down"))
    with caplog.at_level(logging.WARNING):
        result = await llm.complete(MESSAGES, notify)
    assert result.model == "main-model"
    assert "intermediate message failed: RuntimeError" in caplog.text


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"retry-after": "2.5"}, 2.5),
        ({}, DEFAULT_RETRY_AFTER),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, DEFAULT_RETRY_AFTER),
        ({"retry-after": "-3"}, DEFAULT_RETRY_AFTER),
        ({"retry-after": "nan"}, DEFAULT_RETRY_AFTER),
        (None, DEFAULT_RETRY_AFTER),
    ],
)
def test_retry_after_seconds(headers, seconds):
    assert retry_after_seconds(headers) == seconds


# --- інші помилки


@pytest.mark.parametrize(
    "error",
    [
        connection_error(),
        groq.APITimeoutError(request=connection_error().request),
        status_error(groq.InternalServerError, 503),
        status_error(groq.AuthenticationError, 401),
    ],
    ids=["network", "timeout", "5xx", "401"],
)
async def test_api_failures_are_not_repeated(error, caplog):
    fake = FakeGroq(error, OK)
    llm, sleep = _client(fake, fallback_model="spare-model")

    with caplog.at_level(logging.ERROR), pytest.raises(LLMBroken) as caught:
        await llm.complete(MESSAGES, AsyncMock())

    assert caught.value.reply == texts.BROKEN
    assert len(fake.calls) == 1
    sleep.assert_not_called()
    assert f"groq call failed: {type(error).__name__}" in caplog.text


async def test_rejected_json_is_reported_as_invalid_output():
    body = {"error": {"code": "json_validate_failed", "message": "bad"}}
    fake = FakeGroq(status_error(groq.BadRequestError, 400, body))
    with pytest.raises(OutputRejected):
        await _client(fake)[0].complete(MESSAGES)


async def test_other_bad_request_is_broken():
    body = {"error": {"code": "model_not_found"}}
    fake = FakeGroq(status_error(groq.BadRequestError, 400, body))
    with pytest.raises(LLMBroken):
        await _client(fake)[0].complete(MESSAGES)


# --- семафор


async def test_calls_never_overlap():
    fake = FakeGroq(OK, OK, OK, delay=0.02)
    llm, _ = _client(fake)
    await asyncio.gather(*(llm.complete(MESSAGES) for _ in range(3)))
    assert len(fake.calls) == 3
    assert fake.max_active == 1


async def test_wait_after_429_holds_the_queue():
    # Поки один запит чекає retry-after, інший не йде до Groq: він отримав би той самий 429.
    fake = FakeGroq(rate_limited("5"), OK, OK)
    calls_after_pause = []

    async def sleep(seconds):
        await asyncio.sleep(0.01)
        calls_after_pause.append(len(fake.calls))

    llm = LLMClient(fake, "main-model", sleep=sleep)
    await asyncio.gather(llm.complete(MESSAGES), llm.complete(MESSAGES))
    assert calls_after_pause == [1]
    assert len(fake.calls) == 3
    assert fake.max_active == 1


# --- пауза після 429 і черга


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_after_a_long_429_later_questions_do_not_call_groq():
    fake = FakeGroq(rate_limited("600"), OK)
    clock = Clock()
    llm = LLMClient(fake, "main-model", sleep=AsyncMock(), clock=clock)
    with pytest.raises(LLMBusy):
        await llm.complete(MESSAGES)

    clock.now += 300
    with pytest.raises(LLMBusy):
        await llm.complete(MESSAGES)
    assert len(fake.calls) == 1

    clock.now += 301
    await llm.complete(MESSAGES)
    assert len(fake.calls) == 2


async def test_rate_limited_main_model_sends_later_questions_to_the_fallback():
    fake = FakeGroq(rate_limited("600"), OK, OK)
    llm = LLMClient(fake, "main-model", fallback_model="spare-model", clock=Clock())
    await llm.complete(MESSAGES)
    await llm.complete(MESSAGES)
    assert fake.models == ["main-model", "spare-model", "spare-model"]


async def test_short_remaining_pause_is_waited_out_by_the_next_question():
    fake = FakeGroq(rate_limited("60"), OK)
    clock = Clock()
    sleep = AsyncMock()
    llm = LLMClient(fake, "main-model", sleep=sleep, clock=clock)
    with pytest.raises(LLMBusy):
        await llm.complete(MESSAGES)

    clock.now += 50
    notify = AsyncMock()
    await llm.complete(MESSAGES, notify)

    notify.assert_awaited_once_with(texts.WAIT)
    sleep.assert_awaited_once_with(10.0 + RETRY_MARGIN)
    assert len(fake.calls) == 2


async def test_queue_wait_is_bounded():
    fake = FakeGroq(OK, OK, delay=0.2)
    llm = LLMClient(fake, "main-model", max_wait=0.05)
    first, second = await asyncio.gather(
        llm.complete(MESSAGES), llm.complete(MESSAGES), return_exceptions=True
    )
    assert first.model == "main-model"
    assert isinstance(second, LLMBusy)
    assert len(fake.calls) == 1
