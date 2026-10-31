"""Telegram без мережі: справжній Dispatcher, запити бота перехоплює RecordingSession."""

from datetime import UTC, datetime
from typing import Any

from aiogram import Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User

from lisovyi.bot.handlers import router
from lisovyi.bot.library import library_router
from lisovyi.bot.uploads import uploads_router

CHAT = Chat(id=42, type="private")
USER = User(id=42, is_bot=False, first_name="Telegram-Їжак", username="hedgehog")

# Роутери модульні й приєднуються до диспетчера лише раз; порядок — як у __main__.
DISPATCHER = Dispatcher()
DISPATCHER.include_router(uploads_router)
DISPATCHER.include_router(library_router)
DISPATCHER.include_router(router)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    def sent(self, kind: type) -> list[Any]:
        return [request for request in self.requests if isinstance(request, kind)]

    def texts(self) -> list[str]:
        return [request.text for request in self.sent(SendMessage)]


class NoUploads:
    """Заглушка діалогу завантажень для тестів, що його не стосуються."""

    def awaiting_name(self, chat_id: int, telegram_id: int | None = None) -> bool:
        return False


def message_update(text: str, user: User = USER) -> Update:
    return Update(
        update_id=1,
        message=Message(message_id=1, date=datetime.now(UTC), chat=CHAT, from_user=user, text=text),
    )


def document_update(
    file_name: str, *, file_id: str = "file-1", mime_type: str | None = None,
    file_size: int | None = 100,
) -> Update:  # fmt: skip
    document = Document(
        file_id=file_id,
        file_unique_id=file_id,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )
    message = Message(
        message_id=3, date=datetime.now(UTC), chat=CHAT, from_user=USER, document=document
    )
    return Update(update_id=3, message=message)


def photo_update() -> Update:
    photo = PhotoSize(file_id="p", file_unique_id="p", width=10, height=10)
    message = Message(
        message_id=4, date=datetime.now(UTC), chat=CHAT, from_user=USER, photo=[photo]
    )
    return Update(update_id=4, message=message)


def callback_update(data: str) -> Update:
    message = Message(message_id=7, date=datetime.now(UTC), chat=CHAT, text="Кнопки")
    callback = CallbackQuery(
        id="cb1", from_user=USER, chat_instance="ci", message=message, data=data
    )
    return Update(update_id=2, callback_query=callback)
