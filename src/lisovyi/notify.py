"""Підписки й сповіщення про нові та замінені поради (вимога 18).

За замовчуванням бот нікому не пише сам: лише тим, хто підписався командою /subscribe.
Автор документа сповіщення про нього не отримує. Про видалення не сповіщаємо.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lisovyi.bot.places import human_place
from lisovyi.db.models import Subscription
from lisovyi.uploads.service import BeastInfo, SavedDocument

log = logging.getLogger(__name__)

# Telegram дозволяє ботові ~30 повідомлень на секунду; 50 мс — із запасом.
PAUSE = 0.05

NEW_ADVICE = "🌿 Нова порада для {place}: «{title}» (автор: {name})."
REPLACED_ADVICE = "🌿 Оновлено пораду «{old}» для {place}: тепер діє версія від {name} — «{title}»."
NEW_MAP = "🗺 Оновлено Карту лісу (автор: {name}). Тепер я відповідаю за новою картою."


def saved_text(saved: SavedDocument, author: BeastInfo) -> str:
    place = human_place(saved.kind, saved.scope)
    if saved.replaced_title is None:
        return NEW_ADVICE.format(place=place, title=saved.title, name=author.name)
    return REPLACED_ADVICE.format(
        old=saved.replaced_title, place=place, name=author.name, title=saved.title
    )


class Notifier:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        pause: float = PAUSE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._bot = bot
        self._sessionmaker = sessionmaker
        self._pause = pause
        self._sleep = sleep
        # Посилання на фонові розсилки: інакше asyncio може зібрати задачу посеред роботи.
        self._tasks: set[asyncio.Task[None]] = set()

    # --- підписки

    async def subscribe(self, chat_id: int) -> bool:
        """False — чат уже був підписаний."""
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                insert(Subscription).values(chat_id=chat_id).on_conflict_do_nothing()
            )
            return result.rowcount == 1

    async def unsubscribe(self, chat_id: int) -> bool:
        """False — підписки й не було."""
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                delete(Subscription).where(Subscription.chat_id == chat_id)
            )
            return result.rowcount == 1

    async def subscribers(self) -> list[int]:
        async with self._sessionmaker() as session:
            return list(
                await session.scalars(select(Subscription.chat_id).order_by(Subscription.chat_id))
            )

    # --- сповіщення

    def document_saved(self, saved: SavedDocument, author: BeastInfo) -> None:
        """Нова порада чи заміна. Розсилка — фонова: завантажувач на неї не чекає."""
        self._spawn(self.broadcast(saved_text(saved, author), exclude=author.telegram_id))

    def map_replaced(self, author: BeastInfo) -> None:
        self._spawn(self.broadcast(NEW_MAP.format(name=author.name), exclude=author.telegram_id))

    async def broadcast(self, text: str, *, exclude: int) -> None:
        """Послідовно всім підписникам, крім автора (його приватний чат — його telegram_id)."""
        recipients = [chat_id for chat_id in await self.subscribers() if chat_id != exclude]
        for i, chat_id in enumerate(recipients):
            if i:
                await self._sleep(self._pause)
            await self._send(chat_id, text)
        log.info("notified %d subscribers", len(recipients))

    async def _send(self, chat_id: int, text: str, *, retried: bool = False) -> None:
        try:
            await self._bot.send_message(chat_id, text, parse_mode=None)
        except TelegramForbiddenError:
            log.info("chat %d blocked the bot: unsubscribing it", chat_id)
            await self.unsubscribe(chat_id)
        except TelegramRetryAfter as exc:
            if retried:
                log.warning("chat %d: still rate limited, skipping", chat_id)
                return
            await self._sleep(exc.retry_after)
            await self._send(chat_id, text, retried=True)
        except Exception as exc:
            log.warning("chat %d: notification failed: %s", chat_id, type(exc).__name__)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Виняток фонової задачі інакше зник би мовчки.
            log.error("notification task failed", exc_info=task.exception())

    async def drain(self) -> None:
        """Дочекатися всіх фонових розсилок (для тестів і зупинки бота)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
