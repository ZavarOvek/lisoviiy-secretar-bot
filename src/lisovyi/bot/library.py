"""/help, /docs, /my з видаленням, /subscribe і /unsubscribe (бриф 05)."""

import logging
from collections.abc import Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from lisovyi.bot import texts
from lisovyi.bot.places import Place, all_places, find_place, human_place, place_keyboard
from lisovyi.bot.uploads import UploadDialogue
from lisovyi.domain import PART_NAMES, DocumentKind, ForestPart
from lisovyi.library import DocumentRow, Library
from lisovyi.notify import Notifier
from lisovyi.search.retrieve import display_date
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import ConflictError, MapRequired, NotFound

log = logging.getLogger(__name__)

library_router = Router(name="library")

DOCS_PREFIX = "docs:"
DELETE_PREFIX = "del:"
DELETE_OK_PREFIX = "delok:"
DELETE_NO_PREFIX = "delno:"
# Telegram приймає до 4096 символів; ділимо по межі рядка із запасом.
MAX_MESSAGE_CHARS = 4000
DELETE_LABEL_CHARS = 30
# Скільки документів (і кнопок 🗑) в одному повідомленні /my.
MY_PER_MESSAGE = 40
# Назва в рядку /my: довша обрізається, щоб навіть один рядок влазив у повідомлення.
MY_TITLE_CHARS = 200


# --- /help


@library_router.message(Command("help"))
async def help_command(message: Message, bot: Bot) -> None:
    await bot.send_message(message.chat.id, texts.HELP, parse_mode=None)


# --- /docs


@library_router.message(Command("docs"))
async def docs_command(message: Message, bot: Bot, search: SearchService) -> None:
    await bot.send_message(
        message.chat.id, texts.DOCS_ASK, parse_mode=None, reply_markup=_groups_keyboard(search)
    )


@library_router.callback_query(F.data.startswith(DOCS_PREFIX))
async def docs_group(
    callback: CallbackQuery, bot: Bot, search: SearchService, library: Library
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    places = all_places(search.state.resolver.gloves)
    place = find_place((callback.data or "").removeprefix(DOCS_PREFIX), places)
    if place is None:
        log.warning("docs button for a place no longer on the map: %r", callback.data)
        await bot.send_message(
            chat_id, texts.UPLOAD_PLACE_GONE, parse_mode=None, reply_markup=_groups_keyboard(search)
        )
        return
    rows = await library.group(place.kind, place.scope)
    text = group_text(place, rows, search.state.resolver.parts_of(place.scope)
                      if place.kind == DocumentKind.GLOVE else ())  # fmt: skip
    for chunk in split_lines(text):
        await bot.send_message(chat_id, chunk, parse_mode=None)


def _groups_keyboard(search: SearchService) -> InlineKeyboardMarkup | None:
    return place_keyboard(all_places(search.state.resolver.gloves), prefix=DOCS_PREFIX)


def group_text(place: Place, rows: Sequence[DocumentRow], parts: Sequence[ForestPart]) -> str:
    """Заголовок групи, документи за назвою, а для рукавички — які ще поради на неї діють."""
    lines = [f"{_group_title(place)}:"]
    lines += [f"— {row.title} ({_origin(row)})" for row in rows] or [texts.DOCS_EMPTY]
    if place.kind == DocumentKind.GLOVE:
        lines += ["", texts.DOCS_ALSO.format(others=_also_applies(parts))]
    return "\n".join(lines)


def _group_title(place: Place) -> str:
    if place.kind == DocumentKind.GLOVE:
        return f"Рукавичка «{place.scope}»"
    return place.label


def _origin(row: DocumentRow) -> str:
    if row.author_id is None:
        return texts.DOCS_ARCHIVE
    return f"автор: {row.author_name}, {display_date(row.created_at)}"


def _also_applies(parts: Sequence[ForestPart]) -> str:
    """«листяного лісу, тих, хто біля річки, і всього лісу» — за картою."""
    names = [human_place(DocumentKind.PART, part.value) for part in parts]
    if not names:
        return texts.DOCS_ALL_FOREST
    if len(names) == 1:
        return f"{names[0]} і {texts.DOCS_ALL_FOREST}"
    return f"{', '.join(names)}, і {texts.DOCS_ALL_FOREST}"


def split_lines(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Повідомлення не довші за `limit`, розрізані по межі рядка."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


# --- /my і видалення


@library_router.message(Command("my"))
async def my_command(message: Message, bot: Bot, library: Library) -> None:
    assert message.from_user is not None
    rows = await library.by_author(message.from_user.id)
    if not rows:
        await bot.send_message(message.chat.id, texts.MY_NONE, parse_mode=None)
        return
    for chunk in _my_chunks(rows):
        lines = [_my_line(row) for row in chunk]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🗑 {_short(row.title)}", callback_data=f"{DELETE_PREFIX}{row.id}"
                    )
                ]
                for row in chunk
            ]
        )
        await bot.send_message(
            message.chat.id, "\n".join(lines), parse_mode=None, reply_markup=keyboard
        )


def _my_line(row: DocumentRow) -> str:
    title = row.title if len(row.title) <= MY_TITLE_CHARS else row.title[: MY_TITLE_CHARS - 1] + "…"
    return f"— {title} ({_place_label(row)}, {display_date(row.created_at)})"


def _my_chunks(rows: Sequence[DocumentRow]) -> list[list[DocumentRow]]:
    """Не більше MY_PER_MESSAGE документів і MAX_MESSAGE_CHARS символів на повідомлення:
    назва — це назва файлу звіра, і довжину її ніщо не обмежує."""
    chunks: list[list[DocumentRow]] = [[]]
    size = 0
    for row in rows:
        line = len(_my_line(row)) + 1
        if chunks[-1] and (len(chunks[-1]) >= MY_PER_MESSAGE or size + line > MAX_MESSAGE_CHARS):
            chunks.append([])
            size = 0
        chunks[-1].append(row)
        size += line
    return chunks


def _place_label(row: DocumentRow) -> str:
    if row.kind == DocumentKind.MAP:
        return "Карта лісу"
    if row.kind == DocumentKind.GLOVE:
        return f"рукавичка «{row.scope}»"
    if row.kind == DocumentKind.PART:
        return PART_NAMES[ForestPart(row.scope)]
    return "увесь ліс"


def _short(title: str) -> str:
    return title if len(title) <= DELETE_LABEL_CHARS else title[: DELETE_LABEL_CHARS - 1] + "…"


@library_router.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_ask(callback: CallbackQuery, bot: Bot, library: Library) -> None:
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    document_id = _document_id(callback.data, DELETE_PREFIX)
    row = await library.get(document_id) if document_id is not None else None
    if row is None:
        await bot.send_message(chat_id, texts.ALREADY_GONE, parse_mode=None)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.DELETE_YES, callback_data=f"{DELETE_OK_PREFIX}{row.id}"
                ),
                InlineKeyboardButton(
                    text=texts.DELETE_NO, callback_data=f"{DELETE_NO_PREFIX}{row.id}"
                ),
            ]
        ]
    )
    await bot.send_message(
        chat_id, texts.DELETE_ASK.format(title=row.title), parse_mode=None, reply_markup=keyboard
    )


@library_router.callback_query(F.data.startswith(DELETE_NO_PREFIX))
async def delete_no(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if callback.message is not None:
        await bot.send_message(callback.message.chat.id, texts.DELETE_CANCELLED, parse_mode=None)


@library_router.callback_query(F.data.startswith(DELETE_OK_PREFIX))
async def delete_ok(callback: CallbackQuery, bot: Bot, uploads: UploadDialogue) -> None:
    """Власника перевіряє сервіс: callback-дані може підробити будь-хто."""
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    document_id = _document_id(callback.data, DELETE_OK_PREFIX)
    beast = await uploads.service.beast(callback.from_user.id)
    if beast is None or document_id is None:
        await bot.send_message(chat_id, texts.NOT_YOURS, parse_mode=None)
        return
    try:
        deleted = await uploads.service.delete(beast, document_id)
    except PermissionError:
        log.warning("beast %d tried to delete document %d", beast.telegram_id, document_id)
        text = texts.NOT_YOURS
    except NotFound:
        text = texts.ALREADY_GONE
    except MapRequired:
        text = texts.MAP_REQUIRED
    except ConflictError:
        text = texts.DELETE_CONFLICT
    else:
        text = texts.DELETED.format(title=deleted.title)
        if deleted.restored_title is not None:
            text += " " + texts.RESTORED.format(title=deleted.restored_title)
    await bot.send_message(chat_id, text, parse_mode=None)


def _document_id(data: str | None, prefix: str) -> int | None:
    value = (data or "").removeprefix(prefix)
    return int(value) if value.isdigit() else None


# --- підписки


@library_router.message(Command("subscribe"))
async def subscribe(message: Message, bot: Bot, notifier: Notifier) -> None:
    added = await notifier.subscribe(message.chat.id)
    text = texts.SUBSCRIBED if added else texts.ALREADY_SUBSCRIBED
    await bot.send_message(message.chat.id, text, parse_mode=None)


@library_router.message(Command("unsubscribe"))
async def unsubscribe(message: Message, bot: Bot, notifier: Notifier) -> None:
    removed = await notifier.unsubscribe(message.chat.id)
    text = texts.UNSUBSCRIBED if removed else texts.NOT_SUBSCRIBED
    await bot.send_message(message.chat.id, text, parse_mode=None)
