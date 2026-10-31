"""Хендлери через справжній Dispatcher; запити до Telegram перехоплює RecordingSession."""

from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendChatAction, SendMessage
from aiogram.types import Update
from telegram_fakes import (
    DISPATCHER,
    NoUploads,
    RecordingSession,
    callback_update,
    message_update,
)

from lisovyi.answer.service import AnswerService, Reply
from lisovyi.bot import texts as bot_texts
from lisovyi.bot.handlers import start_text
from lisovyi.bot.places import (
    PLACE_PREFIX,
    all_places,
    find_place,
    glove_places,
    place_hash,
    place_keyboard,
)
from lisovyi.search import service as search_module
from lisovyi.search.service import SearchService

GLOVES = ["Біля бобрової греблі", "Біля мурашника", "На узліссі", "Під вербою", "Під дубом",
          "Під ялиною", "У ліщині", "У соснах"]  # fmt: skip
START_TEXT = """\
Вітаю! Я Лісовий секретар 🌲

Я бережу «Лісовий архів»: статути, правила й поради всіх восьми рукавичок, а ще поради, які надсилають самі звірі. Відповідаю тільки за цими записами і завжди кажу, звідки взяв відповідь.

Питайте звичайним повідомленням і називайте місце — так я знайду пораду саме для вашої рукавички. Наприклад:
— Які гриби біля сосон?
— Коли утеплювати рукавичку під вербою?

Маєте корисну пораду? Надішліть її файлом: PDF, Word (.docx) або TXT."""  # noqa: E501


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


async def _feed(bot: Bot, update: Update, answers: FakeAnswers) -> None:
    await DISPATCHER.feed_update(
        bot, update, answers=answers, search=answers.search, uploads=NoUploads()
    )


def _message(text: str) -> Update:
    return message_update(text)


def _callback(data: str) -> Update:
    return callback_update(data)


# --- тексти


def test_start_text_is_the_brief_text():
    assert start_text(8) == START_TEXT


def test_start_text_uses_digits_for_other_counts():
    assert "поради всіх 9 рукавичок" in start_text(9)


# --- хендлери


async def test_start(bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _message("/start"), answers)
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
    assert buttons[0].callback_data == PLACE_PREFIX + place_hash(GLOVES[0])


async def test_answer_without_keyboard_has_no_markup(bot, session):
    await _feed(bot, _message("Які гриби біля сосон?"), FakeAnswers(_reply()))
    assert session.sent(SendMessage)[-1].reply_markup is None


async def test_unknown_command_is_not_a_question(bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _message("/list"), answers)
    assert answers.calls == []
    assert session.sent(SendMessage) == []


async def test_place_button_is_a_followup(bot, session):
    answers = FakeAnswers(_reply())
    await _feed(bot, _callback(PLACE_PREFIX + place_hash("У соснах")), answers)
    assert len(session.sent(AnswerCallbackQuery)) == 1
    assert answers.calls == [(42, "У соснах")]
    assert session.sent(SendMessage)[-1].text == _reply().text


async def test_button_of_a_glove_removed_from_the_map(bot, session, caplog):
    # Кнопку надіслано за старою картою; нова карта рукавичку «Під дубом» прибрала.
    old_button = PLACE_PREFIX + place_hash("Під дубом")
    answers = FakeAnswers(_reply(), gloves=[g for g in GLOVES if g != "Під дубом"])
    await _feed(bot, _callback(old_button), answers)
    assert len(session.sent(AnswerCallbackQuery)) == 1
    assert answers.calls == []
    assert session.sent(SendMessage)[-1].text == bot_texts.PLACE_GONE
    assert "no longer on the map" in caplog.text


async def test_long_glove_name_still_fits_into_callback_data(bot, session):
    long_name = "Біля найстарішого дуба на південному схилі за ярком і струмком"
    gloves = ["Під дубом", long_name]
    keyboard = place_keyboard(glove_places(gloves))
    [button] = [b for row in keyboard.inline_keyboard for b in row if b.text == long_name]
    assert len(button.callback_data.encode()) <= 64

    answers = FakeAnswers(_reply(), gloves=gloves)
    await _feed(bot, _callback(button.callback_data), answers)
    assert answers.calls == [(42, long_name)]


# --- кнопки


def test_answer_keyboard_uses_hashes_two_per_row():
    keyboard = place_keyboard(glove_places(GLOVES))
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == [PLACE_PREFIX + place_hash(glove) for glove in GLOVES]
    assert all(len(d) == len("place:") + 10 for d in data)
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)


def test_archive_places_have_distinct_hashes(archive_search):
    _, resolver = archive_search
    places = all_places(resolver.gloves)
    assert len(places) == 8 + 3 + 1
    assert [p.label for p in places[8:]] == [
        "Хвойний ліс",
        "Листяний ліс",
        "Біля річки",
        "Увесь ліс",
    ]
    assert len({place.key for place in places}) == len(places)


def test_find_place():
    places = all_places(GLOVES)
    assert find_place(place_hash("Під дубом"), places).scope == "Під дубом"
    assert find_place(place_hash("Хвойний ліс"), places).scope == "khvoinyi"
    assert find_place(place_hash("Увесь ліс"), places).scope == "general"
    assert find_place(place_hash("Під кленом"), places) is None


def test_no_places_no_keyboard():
    assert place_keyboard([]) is None


async def test_old_glove_button_after_restart_asks_for_the_question(bot, session, archive_search):
    class NoLLM:
        async def complete(self, messages, notify=None):
            raise AssertionError("the model must not be called")

    search = SearchService(None)
    search._state = search_module._State(*archive_search)
    # Свіжий сервіс — порожня пам'ять, як після рестарту; кнопка лишилась в історії чату.
    answers = AnswerService(search, NoLLM(), dialogue_ttl=900)
    await DISPATCHER.feed_update(
        bot,
        _callback(PLACE_PREFIX + place_hash("Під дубом")),
        answers=answers,
        search=search,
        uploads=NoUploads(),
    )
    assert session.sent(SendMessage)[-1].text.startswith(
        "Що саме ви хочете дізнатися про рукавичку «Під дубом»?"
    )
