"""Вимога 1: бот працює лише в особистих повідомленнях (бриф 06)."""

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import Chat, Message, Update
from telegram_fakes import (
    DISPATCHER,
    RecordingSession,
    callback_update,
    document_update,
    message_update,
)

from lisovyi.bot import texts
from lisovyi.bot.groups import GROUP_COMMANDS, groups_router
from lisovyi.bot.private import private_router

GROUP = Chat(id=-100, type="group", title="Лісова рада")
SUPERGROUP = Chat(id=-1001, type="supergroup", title="Усі рукавички")
PRIVATE_ONLY = "Я працюю лише в особистих повідомленнях. Напишіть мені напряму — і я допоможу."


class Forbidden:
    """Залежність, якої в групі не можна торкатися: питання до LLM, файли, підписки."""

    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, attribute: str):
        raise AssertionError(f"{self.name}.{attribute} used in a group chat")


@pytest.fixture
def session():
    return RecordingSession()


async def feed(session: RecordingSession, update: Update) -> list:
    """Подає оновлення; повертає запити бота, крім GetMe (ним Command звіряє «@бот»)."""
    await DISPATCHER.feed_update(
        Bot("42:TEST", session=session),
        update,
        answers=Forbidden("answers"),
        search=Forbidden("search"),
        uploads=Forbidden("uploads"),
        library=Forbidden("library"),
        notifier=Forbidden("notifier"),
    )
    return [request for request in session.requests if not isinstance(request, GetMe)]


def test_private_only_text():
    assert texts.PRIVATE_ONLY == PRIVATE_ONLY


@pytest.mark.parametrize("chat", [GROUP, SUPERGROUP], ids=["group", "supergroup"])
@pytest.mark.parametrize(
    "command", ["/start", "/help", "/start@LisoviyTestBot", "/help@LisoviyTestBot"]
)
async def test_start_and_help_in_a_group_point_to_private_messages(session, chat, command):
    [request] = await feed(session, message_update(command, chat=chat))
    assert isinstance(request, SendMessage)
    assert (request.chat_id, request.text) == (chat.id, PRIVATE_ONLY)
    assert request.reply_markup is None


@pytest.mark.parametrize("chat", [GROUP, SUPERGROUP], ids=["group", "supergroup"])
@pytest.mark.parametrize(
    "text",
    [
        "Які гриби біля сосон?",  # питання — без виклику LLM
        "/subscribe",  # без підписки
        "/unsubscribe",
        "/docs",
        "/my",
        "/name",
        "/start@OtherBot",  # команда іншому ботові
    ],
)
async def test_everything_else_in_a_group_is_silent(session, chat, text):
    assert await feed(session, message_update(text, chat=chat)) == []


async def test_document_in_a_group_is_not_processed(session):
    assert (
        await feed(session, document_update("порада.txt", mime_type="text/plain", chat=GROUP)) == []
    )


async def test_button_in_a_group_is_only_acknowledged(session):
    [request] = await feed(session, callback_update("place:0123456789", chat=SUPERGROUP))
    assert isinstance(request, AnswerCallbackQuery)
    assert request.text is None


async def test_channel_posts_are_ignored(session):
    channel = Chat(id=-1002, type="channel", title="Лісові новини")
    post = Message(message_id=5, date=datetime.now(UTC), chat=channel, text="/start")
    assert await feed(session, Update(update_id=1, channel_post=post)) == []


async def test_reply_in_a_forum_topic_stays_in_the_topic(session):
    forum = Chat(id=-1003, type="supergroup", title="Лісовий форум", is_forum=True)
    start = Message(
        message_id=9,
        date=datetime.now(UTC),
        chat=forum,
        text="/start",
        message_thread_id=17,
        is_topic_message=True,
    )
    [request] = await feed(session, Update(update_id=1, message=start))
    assert (request.text, request.message_thread_id) == (PRIVATE_ONLY, 17)


def test_every_router_but_the_group_one_is_private():
    # Новий роутер, підключений повз private_router, працював би й у групах.
    assert DISPATCHER.sub_routers == [groups_router, private_router]
    assert private_router.sub_routers
    assert all(not router.sub_routers for router in private_router.sub_routers)


def test_group_command_menu_has_only_start_and_help():
    assert [command.command for command in GROUP_COMMANDS] == ["start", "help"]
