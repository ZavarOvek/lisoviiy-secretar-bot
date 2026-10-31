"""Діалог завантаження порад через справжній Dispatcher і справжню БД."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetFile, SendChatAction, SendMessage
from aiogram.types import User
from sqlalchemy import select
from telegram_fakes import (
    DISPATCHER,
    RecordingSession,
    callback_update,
    document_update,
    message_update,
    photo_update,
)

from lisovyi.answer.service import Reply
from lisovyi.bot import texts
from lisovyi.bot.handlers import COMMANDS
from lisovyi.bot.places import place_hash
from lisovyi.bot.uploads import UploadDialogue
from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Beast, Document
from lisovyi.domain import DocumentStatus, TextSource
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.importer import import_archive
from lisovyi.ingest.ocr import find_review_items
from lisovyi.ingest.parsers import Extracted
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import UploadService

pytestmark = pytest.mark.db

VERBA_ADVICE = (
    "Мешканцям рукавички «Під вербою». Навесні латаття біля берега цвіте, і туди прилітають "
    "бабки. Малим їх не ловити."
)
MUSHROOMS = (
    "Гриби\nЦей путівник написано для звірів, що живуть у хвойному лісі. "
    "Маслюки ростуть під молодими соснами, де багато сонця."
)
NO_PLACE = "Корисна порада. Ягоди збирайте зранку, поки роса."


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Questions:
    """AnswerService для питань: лише записує, що текст пішов як питання."""

    def __init__(self, search):
        self.search = search
        self.calls: list[str] = []

    async def answer(self, chat_id, message, notify=None):
        self.calls.append(message)
        return Reply("Відповідь.", [], "answer", [], None)


@pytest.fixture
async def world(sessionmaker):
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    search = SearchService(sessionmaker)
    await search.reload()
    files: dict[str, bytes] = {}

    async def download(bot, file_id):
        return files[file_id]

    clock = Clock()
    dialogue = UploadDialogue(UploadService(sessionmaker, search), download=download, clock=clock)
    session = RecordingSession()
    return SimpleNamespace(
        dialogue=dialogue,
        files=files,
        clock=clock,
        session=session,
        bot=Bot("42:TEST", session=session),
        questions=Questions(search),
        sessionmaker=sessionmaker,
    )


async def feed(world, update) -> None:
    await DISPATCHER.feed_update(
        world.bot,
        update,
        uploads=world.dialogue,
        answers=world.questions,
        search=world.dialogue.service.search,
    )


async def send_file(world, name: str, content: bytes, file_id: str = "file-1", **kwargs) -> None:
    world.files[file_id] = content
    await feed(world, document_update(name, file_id=file_id, **kwargs))


async def press(world, label: str) -> None:
    """Натискає кнопку з останнього повідомлення, де вона є."""
    for message in reversed(world.session.sent(SendMessage)):
        if message.reply_markup is None:
            continue
        for row in message.reply_markup.inline_keyboard:
            for button in row:
                if button.text == label:
                    await feed(world, callback_update(button.callback_data))
                    return
    raise AssertionError(f"no button {label!r}")


def last_text(world) -> str:
    return world.session.texts()[-1]


async def documents(world, **where) -> list[Document]:
    async with world.sessionmaker() as session:
        query = select(Document).filter_by(**where).order_by(Document.id)
        return list(await session.scalars(query))


async def registered(world, name: str = "Їжак Колючий") -> None:
    await world.dialogue.service.register(42, name)


# --- повний потік


async def test_new_beast_sends_a_file_names_itself_and_the_advice_is_saved(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    assert last_text(world) == texts.ASK_NAME
    assert world.dialogue.awaiting_name(42)

    await feed(world, message_update("  Їжак Колючий "))

    sent = world.session.texts()
    assert "Приємно познайомитись, Їжак Колючий!" in sent
    # Кандидата на заміну не було: під записом — підказка до кнопки «Це нова версія…».
    assert sent[-1] == (
        "Дякую, Їжак Колючий! Записав пораду «порада» для рукавички «Під вербою».\n\n"
        "Якщо це нова версія поради, яка вже є, натисніть кнопку нижче — "
        "і звірі бачитимуть вашу замість старої."
    )
    assert world.questions.calls == []
    assert world.session.sent(SendChatAction)[0].action == "upload_document"
    [saved] = await documents(world, filename="порада.txt")
    assert (saved.scope, saved.status, saved.text) == (
        "Під вербою",
        DocumentStatus.ACTIVE,
        VERBA_ADVICE,
    )
    async with world.sessionmaker() as session:
        beast = await session.scalar(select(Beast).where(Beast.id == saved.author_id))
    # Ім'я — те, що звір написав, а не ім'я з Telegram.
    assert (beast.telegram_id, beast.name) == (42, "Їжак Колючий")
    # Пошук уже бачить нову пораду.
    assert "порада.txt" in {d.filename for d in world.dialogue.service.search.state.index.documents}


async def test_text_is_a_name_only_while_the_name_is_awaited(world):
    await feed(world, message_update("Що росте під вербою?"))
    assert world.questions.calls == ["Що росте під вербою?"]

    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    await feed(world, message_update("Сова"))
    assert world.questions.calls == ["Що росте під вербою?"]

    await feed(world, message_update("Що росте під вербою?"))
    assert world.questions.calls == ["Що росте під вербою?", "Що росте під вербою?"]


async def test_bad_name_is_asked_again(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    await feed(world, message_update("Я"))
    assert last_text(world) == texts.BAD_NAME
    assert world.dialogue.awaiting_name(42)
    await feed(world, message_update("Їжак 007"))
    assert last_text(world) == texts.BAD_NAME
    await feed(world, message_update("Їжак"))
    assert last_text(world).startswith("Дякую, Їжак!")


async def test_registered_beast_is_not_asked_for_a_name(world):
    await registered(world)
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    assert texts.ASK_NAME not in world.session.texts()
    assert last_text(world).startswith("Дякую, Їжак Колючий! Записав пораду")


# --- скасування й протухлі кнопки


async def test_cancel_while_the_name_is_awaited(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    await press(world, texts.CANCEL)
    assert last_text(world) == texts.CANCELLED
    assert not world.dialogue.awaiting_name(42)
    await feed(world, message_update("Сова"))
    assert world.questions.calls == ["Сова"]
    assert await documents(world, filename="порада.txt") == []


async def test_new_file_shelves_the_previous_one(world):
    await send_file(world, "перша.txt", VERBA_ADVICE.encode(), file_id="a")
    old_buttons = world.session.sent(SendMessage)[-1].reply_markup
    await send_file(world, "друга.txt", VERBA_ADVICE.encode(), file_id="b")
    assert world.session.texts()[-2:] == [texts.PREVIOUS_SHELVED, texts.ASK_NAME]

    # Кнопка першого файлу вже протухла.
    await feed(world, callback_update(old_buttons.inline_keyboard[0][0].callback_data))
    assert last_text(world) == texts.STALE
    assert world.dialogue.awaiting_name(42)


async def test_expired_upload_buttons_are_stale(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    world.clock.now += 30 * 60 + 1
    await press(world, texts.CANCEL)
    assert last_text(world) == texts.STALE
    await feed(world, message_update("Сова"))
    assert world.questions.calls == ["Сова"]


async def test_foreign_or_forged_upload_callback_is_stale(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    for data in ["up:deadbeef:cancel", "up:", "up:x:y:z:w"]:
        await feed(world, callback_update(data))
        assert last_text(world) == texts.STALE
    assert world.dialogue.awaiting_name(42)


# --- прийом


@pytest.mark.parametrize(
    ("name", "kwargs", "text"),
    [
        ("Порада.doc", {}, texts.DOC_FORMAT),
        ("Порада.DOC", {}, texts.DOC_FORMAT),
        ("фото.jpg", {"mime_type": "image/jpeg"}, texts.FORMATS),
        ("фото.heic", {}, texts.FORMATS),
        ("таблиця.xlsx", {}, texts.FORMATS),
        ("порада.txt", {"mime_type": "image/png"}, texts.FORMATS),
        ("порада.pdf", {"file_size": 21 * 1024 * 1024}, texts.TOO_BIG),
    ],
)
async def test_files_that_are_not_accepted(world, name, kwargs, text):
    await send_file(world, name, b"x", **kwargs)
    assert world.session.texts() == [text]
    assert world.dialogue.live(42) is None


async def test_photo_gets_the_formats_text(world):
    await feed(world, photo_update())
    assert world.session.texts() == [texts.FORMATS]


async def test_upper_case_extension_is_accepted(world):
    await registered(world)
    await send_file(world, "ПОРАДА.TXT", VERBA_ADVICE.encode())
    assert last_text(world).startswith("Дякую, Їжак Колючий! Записав пораду «ПОРАДА»")


@pytest.mark.parametrize(
    ("content", "text"),
    [
        (b"   \n ", texts.READ_REJECTED["empty"]),
        (("а" * 4001).encode(), texts.READ_REJECTED["too_long"]),
    ],
)
async def test_unreadable_contents_are_refused(world, content, text):
    await registered(world)
    await send_file(world, "порада.txt", content)
    assert last_text(world) == text
    assert world.dialogue.live(42) is None


async def test_broken_docx_is_refused(world):
    await registered(world)
    await send_file(world, "порада.docx", b"not a zip")
    assert last_text(world) == texts.READ_REJECTED["unreadable"]


# --- «поганий зір»

SCAN_TEXT = "Порада\nДля мешканців рукавички «Під дубом». До дуба — 300 кроків. Внесок — 3 шишки."


def _fake_ocr(world):
    async def read(data, filename):
        return Extracted(SCAN_TEXT, TextSource.OCR, find_review_items(SCAN_TEXT))

    world.dialogue._read = read


async def test_scan_numbers_confirmed(world):
    await registered(world)
    _fake_ocr(world)
    await send_file(world, "скан.pdf", b"%PDF")
    review = last_text(world)
    assert review.startswith(texts.OCR_INTRO)
    assert "300 кроків" in review and "3 шишки" in review
    assert review.endswith(texts.OCR_QUESTION)

    await press(world, texts.OCR_YES)

    assert last_text(world).startswith("Дякую, Їжак Колючий! Записав пораду «скан»")
    [saved] = await documents(world, filename="скан.pdf")
    assert (saved.text_source, saved.ocr_confirmed) == (TextSource.OCR, True)


async def test_scan_numbers_wrong(world):
    await registered(world)
    _fake_ocr(world)
    await send_file(world, "скан.pdf", b"%PDF")
    await press(world, texts.OCR_NO)
    assert last_text(world) == texts.OCR_REJECTED
    assert await documents(world, filename="скан.pdf") == []
    # Виправити числа текстом не можна: текст — це питання.
    await feed(world, message_update("До дуба 350 кроків"))
    assert world.questions.calls == ["До дуба 350 кроків"]


# --- місце й версія


async def test_place_buttons_when_the_rules_give_up(world):
    await registered(world)
    await send_file(world, "без_місця.txt", NO_PLACE.encode())
    message = world.session.sent(SendMessage)[-1]
    assert message.text == texts.ASK_PLACE
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]
    assert labels[:8] == world.dialogue.service.search.state.resolver.gloves
    assert labels[8:] == ["Хвойний ліс", "Листяний ліс", "Біля річки", "Увесь ліс"]
    data = message.reply_markup.inline_keyboard[0][0].callback_data
    assert data.endswith(":place:" + place_hash(labels[0]))

    await press(world, "Увесь ліс")
    text = last_text(world)
    assert "усього лісу" in text


async def test_similar_advice_offers_replacement_and_separate_keeps_both(world):
    await registered(world)
    await send_file(world, "без_місця.txt", NO_PLACE.encode())
    await press(world, "Хвойний ліс")
    assert last_text(world) == (
        "Схоже, це нова версія поради «Що їсти в хвойному лісі» (хвойного лісу).\n\n"
        "• Замінити стару — звірі бачитимуть лише вашу версію. "
        "Якщо потім видалите її через /my, стара повернеться.\n"
        "• Залишити обидві — звірі бачитимуть обидві поради."
    )
    message = world.session.sent(SendMessage)[-1]
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]
    assert labels == ["Замінити стару", "Залишити обидві"]
    await press(world, texts.SEPARATE)
    # Звір сам вибрав «Залишити обидві»: підказка зайва, кнопка «Це нова версія…» лишається.
    assert last_text(world) == "Дякую, Їжак Колючий! Записав пораду «без_місця» для хвойного лісу."
    assert world.session.sent(SendMessage)[-1].reply_markup is not None
    [old] = await documents(world, filename="Що їсти в хвойному лісі.txt")
    assert old.status == DocumentStatus.ACTIVE


async def test_new_version_replaces_the_old_document(world):
    await registered(world)
    await send_file(world, "гриби_нова_версія.txt", MUSHROOMS.encode())
    assert last_text(world) == (
        "Схоже, це нова версія поради «Грибний путівник хвойного лісу» (хвойного лісу).\n\n"
        "• Замінити стару — звірі бачитимуть лише вашу версію. "
        "Якщо потім видалите її через /my, стара повернеться.\n"
        "• Залишити обидві — звірі бачитимуть обидві поради."
    )
    await press(world, texts.REPLACE)
    assert last_text(world) == (
        "Дякую, Їжак Колючий! Замінив «Грибний путівник хвойного лісу» вашою новою версією "
        "для хвойного лісу. Тепер звірі бачитимуть її."
    )
    [old] = await documents(world, filename="Грибний путівник хвойного лісу.pdf")
    [new] = await documents(world, filename="гриби_нова_версія.txt")
    assert (old.status, new.replaces_id, new.topic) == (DocumentStatus.REPLACED, old.id, "hryby")
    assert world.dialogue.live(42) is None


async def test_replacement_conflict(world):
    await registered(world)
    await send_file(world, "гриби_нова_версія.txt", MUSHROOMS.encode())
    # Поки звір думає, інший звір замінює ту саму пораду.
    [old] = await documents(world, filename="Грибний путівник хвойного лісу.pdf")
    owl = await world.dialogue.service.register(99, "Сова")
    await world.dialogue.service.save(
        owl, Extracted("Гриби.", TextSource.TEXT), "0" * 64, "сова", "сова.txt",
        "khvoinyi", old.kind, "hryby", old.id,
    )  # fmt: skip
    await press(world, texts.REPLACE)
    assert last_text(world) == texts.CONFLICT
    assert await documents(world, filename="гриби_нова_версія.txt") == []


async def test_other_version_button_rebinds_the_new_advice(world):
    await registered(world)
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    await press(world, texts.OTHER_VERSION)
    message = world.session.sent(SendMessage)[-1]
    assert (
        message.text
        == texts.PICK_OTHER
        == (
            "Новою версією якої поради є ваша? Оберіть її — і звірі бачитимуть вашу замість неї. "
            "Якщо потім видалите свою через /my, та повернеться."
        )
    )
    labels = [row[0].text for row in message.reply_markup.inline_keyboard]
    verba = {
        d.title
        for d in world.dialogue.service.search.state.index.documents
        if d.scope == "Під вербою" and d.filename != "порада.txt"
    }
    assert set(labels) == verba and len(labels) <= 20
    target = labels[0]

    await press(world, target)

    assert last_text(world) == (
        f"Дякую, Їжак Колючий! Замінив «{target}» вашою новою версією для рукавички «Під вербою». "
        "Тепер звірі бачитимуть її."
    )
    [new] = await documents(world, filename="порада.txt")
    [old] = await documents(world, title=target)
    assert (old.status, new.replaces_id, new.topic) == (DocumentStatus.REPLACED, old.id, old.topic)


async def test_pick_that_was_not_offered_is_stale(world):
    await registered(world)
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    token = world.session.sent(SendMessage)[-1].reply_markup.inline_keyboard[0][0].callback_data
    token = token.split(":")[1]
    await press(world, texts.OTHER_VERSION)
    await feed(world, callback_update(f"up:{token}:pick:1"))
    assert last_text(world) == texts.STALE


# --- Карта лісу

ON_EDGE = "На узліссі — ні хвойний, ні листяний ліс, і до річки далеко."


async def test_map_replacement_through_the_dialogue(world, archive_map_text):
    await registered(world)
    text = archive_map_text.replace(ON_EDGE, "На узліссі — хвойний ліс.")
    await send_file(world, "Карта лісу.txt", text.encode())

    preview = last_text(world)
    lines = preview.split("\n")
    assert lines[0] == texts.MAP_PREVIEW
    assert "Під ялиною — хвойний ліс" in lines
    assert "Під вербою — листяний ліс і біля річки" in lines
    assert "На узліссі — хвойний ліс" in lines
    assert lines[-2:] == [texts.MAP_DIFF, "На узліссі: жодна → хвойний ліс"]
    assert world.dialogue.service.search.state.resolver.parts_of("На узліссі") == ()

    await press(world, texts.REPLACE_MAP)

    assert last_text(world) == texts.MAP_REPLACED
    resolver = world.dialogue.service.search.state.resolver
    assert [p.value for p in resolver.parts_of("На узліссі")] == ["khvoinyi"]
    [new] = await documents(world, filename="Карта лісу.txt")
    assert new.status == DocumentStatus.ACTIVE


async def test_map_preview_lists_gloves_without_parts_and_added_removed(world, archive_map_text):
    await registered(world)
    text = archive_map_text.replace("У соснах — хвойний ліс.", "Під кленом — листяний ліс.")
    await send_file(world, "Карта лісу.txt", text.encode())
    lines = last_text(world).split("\n")
    assert "На узліссі — жодна частина" in lines
    assert "Додано: Під кленом" in lines
    assert "Прибрано: У соснах" in lines


async def test_same_map_says_nothing_changed(world, archive_map_text):
    await registered(world)
    await send_file(world, "Карта лісу.txt", archive_map_text.encode())
    assert last_text(world).split("\n")[-1] == texts.MAP_SAME


async def test_map_cancel_keeps_the_old_map(world, archive_map_text):
    await registered(world)
    text = archive_map_text.replace(ON_EDGE, "На узліссі — хвойний ліс.")
    await send_file(world, "Карта лісу.txt", text.encode())
    await press(world, texts.CANCEL)
    assert last_text(world) == texts.CANCELLED
    assert world.dialogue.service.search.state.resolver.parts_of("На узліссі") == ()
    assert await documents(world, filename="Карта лісу.txt") == []


@pytest.mark.parametrize(
    "text",
    [
        "Карта лісу\nТут немає легенди.",
        "Карта лісу\nЛегенда карти\nПід ялиною — хвойний ліс.\nДві особливі рукавички",
        "Карта лісу\nЛегенда карти\nПід ялиною - хвойний ліс.\nУ соснах — хвойний ліс.\n"
        "Дві особливі рукавички",
    ],
    ids=["no-legend", "one-glove", "hyphen-instead-of-dash"],
)
async def test_unreadable_map(world, text):
    await registered(world)
    await send_file(world, "Карта лісу.txt", text.encode())
    assert last_text(world) == texts.MAP_UNREADABLE
    assert world.dialogue.live(42) is None


# --- /name


async def test_name_without_argument(world):
    await feed(world, message_update("/name"))
    assert last_text(world) == texts.NOT_NAMED
    await registered(world)
    await feed(world, message_update("/name"))
    assert last_text(world) == "Вас звати Їжак Колючий. Щоб змінити ім'я, напишіть: /name Нове Ім'я"
    assert world.questions.calls == []


async def test_name_change_reaches_saved_documents(world):
    await registered(world)
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    await feed(world, message_update("/name Пан Їжак"))
    assert last_text(world) == "Готово, тепер ви — Пан Їжак."
    [document] = [
        d for d in world.dialogue.service.search.state.index.documents if d.filename == "порада.txt"
    ]
    assert document.author_name == "Пан Їжак"


@pytest.mark.parametrize("raw", ["Я", "Їжак123", "a" * 41])
async def test_name_validation(world, raw):
    await registered(world)
    await feed(world, message_update(f"/name {raw}"))
    assert last_text(world) == texts.BAD_NAME
    assert (await world.dialogue.service.beast(42)).name == "Їжак Колючий"


async def test_name_cannot_register_a_new_beast(world):
    await feed(world, message_update("/name Сова"))
    assert last_text(world) == texts.NOT_NAMED
    assert await world.dialogue.service.beast(42) is None


def test_name_is_in_the_bot_commands():
    assert [(c.command, c.description) for c in COMMANDS][-1] == ("name", "Змінити своє ім'я")


# --- рев'ю брифу 04: читання, чужий текст, записані поради, подвійні натиски


async def test_text_sent_while_the_file_is_read_is_a_question(world):
    await registered(world)
    release = asyncio.Event()

    async def slow_read(data, filename):
        await release.wait()
        return Extracted(VERBA_ADVICE, TextSource.TEXT)

    world.dialogue._read = slow_read
    upload = asyncio.create_task(send_file(world, "порада.txt", b"x"))
    await asyncio.sleep(0.05)
    await feed(world, message_update("Які гриби біля сосон?"))
    assert world.questions.calls == ["Які гриби біля сосон?"]
    release.set()
    await upload
    assert last_text(world).startswith("Дякую, Їжак Колючий! Записав пораду")


async def test_failed_download_ends_the_upload_and_keeps_the_name(world):
    await registered(world)

    async def broken_download(bot, file_id):
        raise ConnectionError("network is down")

    world.dialogue._download = broken_download
    await send_file(world, "порада.txt", b"x")
    assert last_text(world) == texts.READ_REJECTED["unreadable"]
    assert world.dialogue.live(42) is None
    # Наступний текст — питання, а не нове ім'я звіра.
    await feed(world, message_update("Коли збирати жолуді"))
    assert world.questions.calls == ["Коли збирати жолуді"]
    assert (await world.dialogue.service.beast(42)).name == "Їжак Колючий"


async def test_file_refused_by_telegram_as_too_big(world):
    await registered(world)

    async def refused(bot, file_id):
        raise TelegramBadRequest(GetFile(file_id=file_id), "Bad Request: file is too big")

    world.dialogue._download = refused
    await send_file(world, "порада.pdf", b"x", file_size=None)
    assert last_text(world) == texts.TOO_BIG


async def test_text_of_another_member_is_not_the_name(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    owl = User(id=99, is_bot=False, first_name="Сова")
    await feed(world, message_update("Сова Мудра", user=owl))
    assert world.questions.calls == ["Сова Мудра"]
    assert world.dialogue.awaiting_name(42)


async def test_button_under_saved_advice_survives_a_new_file(world):
    await registered(world)
    await send_file(world, "порада.txt", VERBA_ADVICE.encode(), file_id="a")
    saved_message = world.session.sent(SendMessage)[-1]
    await send_file(world, "без_місця.txt", NO_PLACE.encode(), file_id="b")
    assert texts.PREVIOUS_SHELVED not in world.session.texts()

    await feed(
        world, callback_update(saved_message.reply_markup.inline_keyboard[0][0].callback_data)
    )
    assert last_text(world) == texts.PICK_OTHER


async def test_second_tap_on_a_finished_upload_is_ignored(world):
    await registered(world)
    await send_file(world, "гриби_нова_версія.txt", MUSHROOMS.encode())
    await press(world, texts.REPLACE)
    sent = len(world.session.texts())
    await press(world, texts.REPLACE)
    assert len(world.session.texts()) == sent


async def test_upload_place_removed_from_the_map_offers_fresh_buttons(world, archive_map_text):
    await registered(world)
    await send_file(world, "без_місця.txt", NO_PLACE.encode())
    old_keyboard = world.session.sent(SendMessage)[-1].reply_markup
    [edge] = [b for row in old_keyboard.inline_keyboard for b in row if b.text == "На узліссі"]

    # Поки звір вибирає, інший звір прибирає «На узліссі» з карти.
    lines = archive_map_text.splitlines()
    text = "\n".join(line for line in lines if not line.startswith("На узліссі —"))
    owl = await world.dialogue.service.register(99, "Сова")
    await world.dialogue.service.replace_map(
        owl, Extracted(text, TextSource.TEXT), "1" * 64, "Карта лісу.txt", parse_forest_map(text)
    )
    await feed(world, callback_update(edge.callback_data))

    message = world.session.sent(SendMessage)[-1]
    assert message.text == texts.UPLOAD_PLACE_GONE
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]
    assert "На узліссі" not in labels and "Увесь ліс" in labels
    await press(world, "Увесь ліс")
    assert "усього лісу" in last_text(world)


async def test_expired_uploads_of_other_chats_are_forgotten(world):
    await send_file(world, "порада.txt", VERBA_ADVICE.encode())
    world.clock.now += 30 * 60 + 1
    world.dialogue.live(7)
    assert world.dialogue._pending == {}
