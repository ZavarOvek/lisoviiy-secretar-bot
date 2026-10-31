"""Сповіщення підписників про нові й замінені поради (бриф 05, §2.3)."""

import asyncio
import logging

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Document
from lisovyi.domain import DocumentKind, TextSource
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.importer import import_archive
from lisovyi.ingest.parsers import Extracted
from lisovyi.notify import Notifier
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import UploadService

pytestmark = pytest.mark.db

GUIDE = "Грибний путівник хвойного лісу.pdf"
HEDGEHOG, OWL, FOX = 7, 8, 9


class FakeBot:
    """send_message, що записує розсилку; `errors` — що кинути для якого чату."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.errors: dict[int, list[BaseException]] = {}
        self.gate: asyncio.Event | None = None

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.gate is not None:
            await self.gate.wait()
        pending = self.errors.get(chat_id)
        if pending:
            raise pending.pop(0)
        self.sent.append((chat_id, text))


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
async def world(sessionmaker):
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    search = SearchService(sessionmaker)
    await search.reload()
    bot = FakeBot()
    sleeps = Sleeps()
    notifier = Notifier(bot, sessionmaker, sleep=sleeps)
    uploads = UploadService(sessionmaker, search, notifier)
    hedgehog = await uploads.register(HEDGEHOG, "Їжак")
    for chat_id in (HEDGEHOG, OWL, FOX):
        await notifier.subscribe(chat_id)
    return bot, sleeps, notifier, uploads, hedgehog, sessionmaker


def _text(text: str) -> Extracted:
    return Extracted(text, TextSource.TEXT)


async def _new_advice(uploads, beast):
    return await uploads.save(
        beast, _text("Латаття цвіте."), "1" * 64, "Порада про латаття", "латаття.txt",
        "Під вербою", DocumentKind.GLOVE, "latattia", None,
    )  # fmt: skip


async def test_new_advice_reaches_everyone_but_the_author(world):
    bot, sleeps, notifier, uploads, hedgehog, _ = world
    await _new_advice(uploads, hedgehog)
    await notifier.drain()
    text = "🌿 Нова порада для рукавички «Під вербою»: «Порада про латаття» (автор: Їжак)."
    assert bot.sent == [(OWL, text), (FOX, text)]
    assert sleeps.calls == [0.05]


async def test_replacement_text(world):
    bot, _, notifier, uploads, hedgehog, sessionmaker = world
    async with sessionmaker() as session:
        old = await session.scalar(select(Document).where(Document.filename == GUIDE))
    await uploads.save(
        hedgehog, _text("Гриби."), "2" * 64, "Гриби від Їжака", "гриби.txt", "khvoinyi",
        DocumentKind.PART, "hryby", old.id,
    )  # fmt: skip
    await notifier.drain()
    assert bot.sent[0] == (
        OWL,
        "🌿 Оновлено пораду «Грибний путівник хвойного лісу» для хвойного лісу: тепер діє "
        "версія від Їжак — «Гриби від Їжака».",
    )


async def test_map_replacement_text(world, archive_map_text):
    bot, _, notifier, uploads, hedgehog, _ = world
    await uploads.replace_map(
        hedgehog, _text(archive_map_text), "3" * 64, "Карта лісу.txt",
        parse_forest_map(archive_map_text),
    )  # fmt: skip
    await notifier.drain()
    text = "🗺 Оновлено Карту лісу (автор: Їжак). Тепер я відповідаю за новою картою."
    assert bot.sent == [(OWL, text), (FOX, text)]


async def test_rebind_and_delete_send_nothing(world):
    bot, _, notifier, uploads, hedgehog, sessionmaker = world
    saved = await _new_advice(uploads, hedgehog)
    await notifier.drain()
    bot.sent.clear()
    async with sessionmaker() as session:
        target = await session.scalar(
            select(Document).where(Document.filename == "Статут ОСМД Під вербою.docx")
        )
    await uploads.rebind(saved.id, target.id)
    await uploads.delete(hedgehog, saved.id)
    await notifier.drain()
    assert bot.sent == []


async def test_blocked_chat_is_unsubscribed(world, caplog):
    bot, _, notifier, uploads, hedgehog, _ = world
    bot.errors[OWL] = [TelegramForbiddenError(SendMessage(chat_id=OWL, text="x"), "blocked")]
    with caplog.at_level(logging.INFO):
        await _new_advice(uploads, hedgehog)
        await notifier.drain()
    assert [chat for chat, _ in bot.sent] == [FOX]
    assert await notifier.subscribers() == [HEDGEHOG, FOX]
    assert f"chat {OWL} blocked the bot" in caplog.text


async def test_retry_after_is_waited_out_once(world):
    bot, sleeps, notifier, uploads, hedgehog, _ = world
    flood = TelegramRetryAfter(SendMessage(chat_id=OWL, text="x"), "flood", retry_after=3)
    bot.errors[OWL] = [flood]
    await _new_advice(uploads, hedgehog)
    await notifier.drain()
    assert [chat for chat, _ in bot.sent] == [OWL, FOX]
    assert 3 in sleeps.calls


async def test_second_retry_after_skips_the_chat(world, caplog):
    bot, _, notifier, uploads, hedgehog, _ = world
    bot.errors[OWL] = [
        TelegramRetryAfter(SendMessage(chat_id=OWL, text="x"), "flood", retry_after=1)
        for _ in range(2)
    ]
    await _new_advice(uploads, hedgehog)
    await notifier.drain()
    assert [chat for chat, _ in bot.sent] == [FOX]
    assert f"chat {OWL}: still rate limited" in caplog.text


async def test_other_errors_do_not_stop_the_broadcast(world, caplog):
    bot, _, notifier, uploads, hedgehog, _ = world
    bot.errors[OWL] = [RuntimeError("network")]
    await _new_advice(uploads, hedgehog)
    await notifier.drain()
    assert [chat for chat, _ in bot.sent] == [FOX]
    assert f"chat {OWL}: notification failed: RuntimeError" in caplog.text


async def test_exception_in_the_background_task_is_logged(world, caplog, monkeypatch):
    _, _, notifier, uploads, hedgehog, _ = world

    async def broken(self):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Notifier, "subscribers", broken)
    with caplog.at_level(logging.ERROR):
        await _new_advice(uploads, hedgehog)
        await notifier.drain()
    assert "notification task failed" in caplog.text
    assert "database is down" in caplog.text


async def test_save_does_not_wait_for_the_broadcast(world):
    bot, _, notifier, uploads, hedgehog, _ = world
    bot.gate = asyncio.Event()
    saved = await asyncio.wait_for(_new_advice(uploads, hedgehog), timeout=10)
    assert saved.title == "Порада про латаття"
    assert bot.sent == []
    bot.gate.set()
    await notifier.drain()
    assert len(bot.sent) == 2


async def test_subscribe_and_unsubscribe_report_the_previous_state(world):
    _, _, notifier, *_ = world
    assert await notifier.subscribe(100) is True
    assert await notifier.subscribe(100) is False
    assert await notifier.unsubscribe(100) is True
    assert await notifier.unsubscribe(100) is False
