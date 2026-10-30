"""Хендлери через справжній Dispatcher; запити до Telegram перехоплює RecordingSession."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendChatAction, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from lisovyi.answer.service import Reply
from lisovyi.bot.handlers import (
    CALLBACK_DATA_BYTES,
    PLACE_PREFIX,
    glove_from_callback,
    place_keyboard,
    router,
    start_text,
)

GLOVES = ["Біля бобрової греблі", "Біля мурашника", "На узліссі", "Під вербою", "Під дубом",
          "Під ялиною", "У ліщині", "У соснах"]  # fmt: skip
CHAT = Chat(id=42, type="private")
USER = User(id=42, is_bot=False, first_name="Їжак")

START_TEXT = """\
Привіт! Я Лісовий секретар 🌲

Я бережу «Лісовий архів»: статути, правила й поради всіх восьми рукавичок, а ще поради, які надсилають самі звірі. Відповідаю тільки за цими записами і завжди кажу, звідки взяв відповідь.

Питай звичайним повідомленням і називай місце — так я знайду пораду саме для твоєї рукавички. Наприклад:
— Які гриби біля сосон?
— Коли утеплювати рукавичку під вербою?

Маєш корисну пораду? Надішли її файлом: PDF, Word (.docx) або TXT."""  # noqa: E501


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


class FakeAnswers:
    def __init__(self, reply: Reply, gloves: list[str] = GLOVES):
        self.reply = reply
        self.calls: list[tuple[int, str]] = []
        resolver = SimpleNamespace(gloves=gloves)
        self.search = SimpleNamespace(state=SimpleNamespace(resolver=resolver))

    async def answer(self, chat_id, message, notify=None):
        self.calls.append((chat_id, message))
        await notify("зачекай")
        return self.reply


@pytest.fixture
def session():
    return RecordingSession()


@pytest.fixture
def bot(session):
    return Bot("42:TEST", session=session)


def _reply(keyboard: list[str] | None = None) -> Reply:
    return Reply("Маслюки.\n\nДжерела:\n— Грибний путівник", keyboard or [], "answer", [], None)


# Роутер модульний і приєднується до диспетчера лише раз.
DISPATCHER = Dispatcher()
DISPATCHER.include_router(router)


async def _feed(bot: Bot, update: Update, answers: FakeAnswers) -> None:
    await DISPATCHER.feed_update(bot, update, answers=answers, search=answers.search)


def _message(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(message_id=1, date=datetime.now(UTC), chat=CHAT, from_user=USER, text=text),
    )


def _callback(data: str) -> Update:
    message = Message(message_id=7, date=datetime.now(UTC), chat=CHAT, text="Про яку?")
    callback = CallbackQuery(
        id="cb1", from_user=USER, chat_instance="ci", message=message, data=data
    )
    return Update(update_id=2, callback_query=callback)


# --- тексти


def test_start_text_is_the_brief_text():
    assert start_text(8) == START_TEXT


def test_start_text_uses_digits_for_other_counts():
    assert "поради всіх 9 рукавичок" in start_text(9)


# --- хендлери


@pytest.mark.parametrize("command", ["/start", "/help"])
async def test_start_and_help(command, bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _message(command), answers)
    [sent] = session.sent(SendMessage)
    assert sent.text == START_TEXT
    assert "Які гриби біля сосон?" in sent.text
    assert "PDF, Word (.docx) або TXT" in sent.text
    assert answers.calls == []


async def test_text_message_is_a_question(bot, session):
    answers = FakeAnswers(_reply(keyboard=GLOVES))
    await _feed(bot, _message("Які гриби біля сосон?"), answers)

    assert answers.calls == [(42, "Які гриби біля сосон?")]
    assert session.sent(SendChatAction)[0].action == "typing"
    intermediate, final = session.sent(SendMessage)
    assert intermediate.text == "зачекай"
    assert final.text == _reply().text
    assert final.parse_mode is None
    buttons = [b for row in final.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == GLOVES
    assert buttons[0].callback_data == PLACE_PREFIX + GLOVES[0]


async def test_answer_without_keyboard_has_no_markup(bot, session):
    await _feed(bot, _message("Які гриби біля сосон?"), FakeAnswers(_reply()))
    assert session.sent(SendMessage)[-1].reply_markup is None


async def test_unknown_command_is_not_a_question(bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _message("/list"), answers)
    assert answers.calls == []
    assert session.sent(SendMessage) == []


@pytest.mark.parametrize(
    "data", [PLACE_PREFIX + "У соснах", PLACE_PREFIX + str(GLOVES.index("У соснах"))]
)
async def test_place_button_is_a_followup(data, bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _callback(data), answers)
    assert len(session.sent(AnswerCallbackQuery)) == 1
    assert answers.calls == [(42, "У соснах")]
    assert session.sent(SendMessage)[-1].text == _reply().text


async def test_index_button_of_a_too_long_name_round_trips(bot, session):
    long_name = "Біля найстарішого дуба на південному схилі"
    gloves = ["Під дубом", long_name]
    keyboard = place_keyboard(gloves)
    [button] = [b for row in keyboard.inline_keyboard for b in row if b.text == long_name]
    assert button.callback_data == "place:1"

    answers = FakeAnswers(_reply(), gloves=gloves)
    await _feed(bot, _callback(button.callback_data), answers)
    assert answers.calls == [(42, long_name)]


async def test_button_for_a_vanished_glove_is_ignored(bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _callback(PLACE_PREFIX + "Під кленом"), answers)
    assert len(session.sent(AnswerCallbackQuery)) == 1
    assert answers.calls == []


# --- кнопки


def test_archive_glove_names_fit_into_callback_data():
    longest = max(GLOVES, key=lambda glove: len(glove.encode()))
    assert len((PLACE_PREFIX + longest).encode()) <= CALLBACK_DATA_BYTES
    keyboard = place_keyboard(GLOVES)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == [PLACE_PREFIX + glove for glove in GLOVES]
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)


def test_too_long_glove_name_switches_to_indexes():
    long_name = "Біля найстарішого дуба на південному схилі"  # > 58 байт
    assert len((PLACE_PREFIX + long_name).encode()) > CALLBACK_DATA_BYTES
    gloves = ["Під дубом", long_name]
    keyboard = place_keyboard(gloves)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["place:0", "place:1"]
    assert all(len(d.encode()) <= CALLBACK_DATA_BYTES for d in data)
    assert glove_from_callback("place:1", gloves) == long_name


def test_glove_from_callback():
    assert glove_from_callback("place:Під дубом", GLOVES) == "Під дубом"
    assert glove_from_callback("place:0", GLOVES) == GLOVES[0]
    assert glove_from_callback("place:8", GLOVES) is None
    assert glove_from_callback("place:Під кленом", GLOVES) is None


def test_no_gloves_no_keyboard():
    assert place_keyboard([]) is None
