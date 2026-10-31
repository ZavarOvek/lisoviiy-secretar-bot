"""/help, /docs, /my з видаленням, підписки — через справжній Dispatcher і БД (бриф 05)."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import User
from sqlalchemy import select
from telegram_fakes import DISPATCHER, RecordingSession, callback_update, message_update

from lisovyi.bot import texts
from lisovyi.bot.handlers import COMMANDS
from lisovyi.bot.library import group_text, split_lines
from lisovyi.bot.places import Place, place_hash
from lisovyi.bot.uploads import UploadDialogue
from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Document
from lisovyi.domain import DocumentKind, DocumentStatus, ForestPart, TextSource
from lisovyi.ingest.importer import import_archive
from lisovyi.ingest.parsers import Extracted
from lisovyi.library import DocumentRow, Library
from lisovyi.notify import Notifier
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import UploadService

GUIDE = "Грибний путівник хвойного лісу.pdf"
HELP = """\
Що я вмію:
— питайте звичайним повідомленням, і я відповім за записами архіву (назвіть місце — так відповідь буде точнішою);
— надішліть пораду файлом (PDF, Word .docx або TXT), і я додам її до архіву;
/docs — усі поради архіву за місцями
/my — ваші поради (тут їх можна видалити)
/subscribe — повідомляти про нові поради
/unsubscribe — більше не повідомляти
/name — змінити своє ім'я"""  # noqa: E501


@pytest.fixture
async def world(sessionmaker):
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    search = SearchService(sessionmaker)
    await search.reload()
    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    service = UploadService(sessionmaker, search)
    return SimpleNamespace(
        bot=bot,
        session=session,
        search=search,
        service=service,
        uploads=UploadDialogue(service),
        library=Library(sessionmaker),
        notifier=Notifier(bot, sessionmaker),
        sessionmaker=sessionmaker,
    )


async def feed(world, update) -> None:
    await DISPATCHER.feed_update(
        world.bot,
        update,
        search=world.search,
        uploads=world.uploads,
        library=world.library,
        notifier=world.notifier,
        answers=None,
    )


def last(world) -> SendMessage:
    return world.session.sent(SendMessage)[-1]


async def press(world, label: str) -> None:
    for message in reversed(world.session.sent(SendMessage)):
        for row in message.reply_markup.inline_keyboard if message.reply_markup else []:
            for button in row:
                if button.text == label:
                    await feed(world, callback_update(button.callback_data))
                    return
    raise AssertionError(f"no button {label!r}")


# --- /help і команди


def test_help_and_bot_commands_list_all_seven_commands():
    assert [c.command for c in COMMANDS] == [
        "start", "help", "docs", "my", "subscribe", "unsubscribe", "name",
    ]  # fmt: skip
    assert all(c.description for c in COMMANDS)
    assert texts.HELP == HELP
    for command in ["/docs", "/my", "/subscribe", "/unsubscribe", "/name"]:
        assert command in texts.HELP


@pytest.mark.db
async def test_help_command(world):
    await feed(world, message_update("/help"))
    assert last(world).text == HELP


# --- /docs


@pytest.mark.db
async def test_docs_offers_groups_in_map_order(world):
    await feed(world, message_update("/docs"))
    message = last(world)
    assert message.text == texts.DOCS_ASK
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]
    assert labels == [
        *world.search.state.resolver.gloves, "Хвойний ліс", "Листяний ліс", "Біля річки",
        "Увесь ліс",
    ]  # fmt: skip
    data = message.reply_markup.inline_keyboard[0][0].callback_data
    assert data == "docs:" + place_hash(labels[0])


@pytest.mark.db
async def test_docs_group_of_a_glove_with_two_parts(world):
    await world.service.save(
        await world.service.register(7, "Жабка"), Extracted("Латаття.", TextSource.TEXT),
        "1" * 64, "Порада про латаття", "латаття.txt", "Під вербою", DocumentKind.GLOVE,
        "latattia", None,
    )  # fmt: skip
    await feed(world, message_update("/docs"))
    await press(world, "Під вербою")
    lines = last(world).text.split("\n")
    assert lines[0] == "Рукавичка «Під вербою»:"
    assert "— Статут ОСМД Під вербою (архів)" in lines
    today = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y")
    assert f"— Порада про латаття (автор: Жабка, {today})" in lines
    items = [line for line in lines if line.startswith("— ")]
    assert items == sorted(items)
    assert lines[-1] == (
        "Для цієї рукавички діють ще поради: листяного лісу, тих, хто біля річки, і всього лісу"
    )


@pytest.mark.db
async def test_docs_group_of_a_glove_without_parts(world):
    await feed(world, message_update("/docs"))
    await press(world, "На узліссі")
    assert last(world).text.split("\n")[-1] == "Для цієї рукавички діють ще поради: всього лісу"


@pytest.mark.db
async def test_docs_whole_forest_includes_the_map(world):
    await feed(world, message_update("/docs"))
    await press(world, "Увесь ліс")
    lines = last(world).text.split("\n")
    assert lines[0] == "Увесь ліс:"
    assert "— Карта лісу (архів)" in lines
    assert "— Календар сезонів (архів)" in lines


@pytest.mark.db
async def test_docs_button_of_a_vanished_place(world):
    await feed(world, callback_update("docs:" + place_hash("Під кленом")))
    assert last(world).text == texts.UPLOAD_PLACE_GONE
    assert last(world).reply_markup is not None


def test_glove_with_one_part_and_empty_group():
    place = Place("Під ялиною", DocumentKind.GLOVE, "Під ялиною")
    text = group_text(place, [], [ForestPart.KHVOINYI])
    assert text.split("\n") == [
        "Рукавичка «Під ялиною»:",
        texts.DOCS_EMPTY,
        "",
        "Для цієї рукавички діють ще поради: хвойного лісу і всього лісу",
    ]


def test_long_list_is_split_on_line_boundaries():
    rows = [
        DocumentRow(i, f"Порада номер {i:03d} " + "ж" * 60, DocumentKind.GLOVE, "Під дубом",
                    None, None, datetime(2026, 10, 31))
        for i in range(120)
    ]  # fmt: skip
    text = group_text(Place("Під дубом", DocumentKind.GLOVE, "Під дубом"), rows, [])
    chunks = split_lines(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "\n".join(chunks) == text


# --- /my і видалення


@pytest.mark.db
async def test_my_without_documents(world):
    await feed(world, message_update("/my"))
    assert last(world).text == texts.MY_NONE
    await world.service.register(42, "Їжак")
    await feed(world, message_update("/my"))
    assert last(world).text == texts.MY_NONE


async def _replace_guide(world, telegram_id: int, name: str, title: str):
    beast = await world.service.register(telegram_id, name)
    async with world.sessionmaker() as session:
        old = await session.scalar(
            select(Document).where(
                Document.scope == "khvoinyi",
                Document.topic == "hryby",
                Document.status == DocumentStatus.ACTIVE,
            )
        )
    return await world.service.save(
        beast, Extracted("Гриби.", TextSource.TEXT), title.ljust(64, "0")[:64], title,
        f"{title}.txt", "khvoinyi", DocumentKind.PART, "hryby", old.id,
    )  # fmt: skip


@pytest.mark.db
async def test_full_delete_flow_restores_the_archive_guide(world):
    await _replace_guide(world, 42, "Їжак", "Мої гриби")
    await feed(world, message_update("/my"))
    message = last(world)
    today = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y")
    assert message.text == f"— Мої гриби (хвойний ліс, {today})"
    assert message.reply_markup.inline_keyboard[0][0].text == "🗑 Мої гриби"

    await press(world, "🗑 Мої гриби")
    assert last(world).text == "Видалити «Мої гриби»?"
    await press(world, texts.DELETE_YES)

    assert last(world).text == (
        "Видалив «Мої гриби». Повернув попередню версію: «Грибний путівник хвойного лісу»."
    )
    assert GUIDE in {d.filename for d in world.search.state.index.documents}
    await feed(world, message_update("/my"))
    assert last(world).text == texts.MY_NONE


@pytest.mark.db
async def test_delete_no_keeps_the_document(world):
    saved = await _replace_guide(world, 42, "Їжак", "Мої гриби")
    await feed(world, message_update("/my"))
    await press(world, "🗑 Мої гриби")
    await press(world, texts.DELETE_NO)
    assert last(world).text == texts.DELETE_CANCELLED
    async with world.sessionmaker() as session:
        assert (await session.get(Document, saved.id)).status == DocumentStatus.ACTIVE


@pytest.mark.db
async def test_forged_delete_of_someone_elses_document_is_refused(world):
    owls = await _replace_guide(world, 99, "Сова", "Совині гриби")
    await world.service.register(42, "Їжак")
    # Їжак підробляє callback з id чужої поради.
    await feed(world, callback_update(f"delok:{owls.id}"))
    assert last(world).text == texts.NOT_YOURS
    async with world.sessionmaker() as session:
        assert (await session.get(Document, owls.id)).status == DocumentStatus.ACTIVE


@pytest.mark.db
async def test_forged_delete_of_an_archive_document_by_an_unregistered_chat(world):
    async with world.sessionmaker() as session:
        archive = await session.scalar(select(Document).where(Document.filename == GUIDE))
    stranger = User(id=500, is_bot=False, first_name="Хтось")
    update = callback_update(f"delok:{archive.id}")
    update.callback_query.__dict__["from_user"] = stranger
    await feed(world, update)
    assert last(world).text == texts.NOT_YOURS
    async with world.sessionmaker() as session:
        assert (await session.get(Document, archive.id)).status == DocumentStatus.ACTIVE


@pytest.mark.db
async def test_second_delete_says_it_is_gone(world):
    saved = await _replace_guide(world, 42, "Їжак", "Мої гриби")
    await feed(world, callback_update(f"delok:{saved.id}"))
    await feed(world, callback_update(f"delok:{saved.id}"))
    assert last(world).text == texts.ALREADY_GONE


# --- підписки


@pytest.mark.db
async def test_subscribe_and_unsubscribe(world):
    await feed(world, message_update("/subscribe"))
    assert last(world).text == texts.SUBSCRIBED
    await feed(world, message_update("/subscribe"))
    assert last(world).text == texts.ALREADY_SUBSCRIBED
    assert await world.notifier.subscribers() == [42]
    await feed(world, message_update("/unsubscribe"))
    assert last(world).text == texts.UNSUBSCRIBED
    await feed(world, message_update("/unsubscribe"))
    assert last(world).text == texts.NOT_SUBSCRIBED
    assert await world.notifier.subscribers() == []
