"""Поради файлами: прийом, ім'я звіра, «поганий зір», місце, нова версія чи окрема порада.

Стан незавершеного завантаження живе в пам'яті процесу (PendingUpload, 30 хв). Після
рестарту звір просто надсилає файл ще раз.
"""

import asyncio
import hashlib
import logging
import secrets
import time
import unicodedata
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePath

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from lisovyi.answer.service import AnswerService
from lisovyi.bot import texts
from lisovyi.bot.handlers import answer_question
from lisovyi.bot.places import all_places, find_place, human_place, place_keyboard
from lisovyi.domain import PART_NAMES, DocumentKind, ForestPart, TextSource
from lisovyi.ingest.classify import UploadClassification, classify_upload, upload_topic
from lisovyi.ingest.forest_map import MapParseError, parse_forest_map
from lisovyi.ingest.parsers import Extracted
from lisovyi.uploads.reading import UNREADABLE, UploadRejected, read_upload
from lisovyi.uploads.service import (
    BeastInfo,
    ConflictError,
    SavedDocument,
    UploadService,
    valid_name,
)
from lisovyi.uploads.versions import find_version_candidate, rank_in_scope

log = logging.getLogger(__name__)

uploads_router = Router(name="uploads")

ACCEPTED = frozenset({".pdf", ".docx", ".txt"})
# Telegram віддає ботам файли до 20 МБ.
MAX_FILE_BYTES = 20 * 1024 * 1024
PENDING_TTL = 30 * 60
OCR_REVIEW_LINES = 10
OTHER_VERSIONS = 20
# Карта з меншою кількістю рукавичок означає, що легенду розібрано неправильно.
MIN_MAP_GLOVES = 2
CALLBACK_PREFIX = "up:"
# Довжина фрагмента рядка навколо числа в «поганому зорі».
SNIPPET_RADIUS = 30
TYPING_REPEAT = 5.0

Download = Callable[[Bot, str], Awaitable[bytes]]


class Stage(StrEnum):
    # Бот завантажує й читає файл: текст звіра в цей час — питання, як завжди.
    READING = "reading"
    # Бот попросив ім'я: наступний текст цього звіра — ім'я (єдиний виняток з вимоги 3).
    NAME = "name"
    OCR = "ocr"
    PLACE = "place"
    VERSION = "version"
    MAP = "map"
    # Порада записана; лишається лише кнопка «Це нова версія іншої поради?».
    SAVED = "saved"


@dataclass
class PendingUpload:
    token: str
    chat_id: int
    telegram_id: int
    file_id: str
    filename: str
    title: str
    started: float
    stage: Stage
    beast: BeastInfo | None = None
    # Сам файл не зберігається: лише його sha256 для БД.
    sha256: str = ""
    extracted: Extracted | None = None
    ocr_confirmed: bool = False
    place: UploadClassification | None = None
    candidate_id: int | None = None
    saved: SavedDocument | None = None
    # Документи, запропоновані кнопками «Це нова версія іншої поради?».
    offered: list[int] = field(default_factory=list)
    # Розібрана легенда нової «Карти лісу».
    forest_map: dict[str, tuple[ForestPart, ...]] | None = None


async def download_from_telegram(bot: Bot, file_id: str) -> bytes:
    buffer = await bot.download(file_id)
    if buffer is None:
        raise RuntimeError(f"Telegram returned no content for file {file_id}")
    return buffer.getvalue()


class UploadDialogue:
    def __init__(
        self,
        service: UploadService,
        *,
        ttl: float = PENDING_TTL,
        clock: Callable[[], float] = time.monotonic,
        download: Download = download_from_telegram,
        read: Callable[[bytes, str], Awaitable[Extracted]] = read_upload,
    ):
        self.service = service
        self.ttl = ttl
        self._clock = clock
        self._download = download
        self._read = read
        # Незавершене завантаження чату: новий файл його скасовує.
        self._pending: dict[int, PendingUpload] = {}
        # Записані окремі поради чату, під якими ще живе «Це нова версія іншої поради?».
        self._saved: dict[int, dict[str, PendingUpload]] = {}
        # Токени завантажень, які вже завершилися: повторний натиск їхніх кнопок
        # (подвійний клік на повільному зв'язку) просто ігнорується.
        self._finished: dict[str, float] = {}
        # Повідомлення й кнопки одного чату обробляються по черзі.
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    def live(self, chat_id: int) -> PendingUpload | None:
        self._sweep()
        return self._pending.get(chat_id)

    def awaiting_name(self, chat_id: int, telegram_id: int | None = None) -> bool:
        pending = self.live(chat_id)
        return (
            pending is not None
            and pending.stage == Stage.NAME
            and (telegram_id is None or pending.telegram_id == telegram_id)
        )

    def _sweep(self) -> None:
        """Забуває все, що старше TTL, у всіх чатах: інакше покинуті завантаження жили б
        до кінця процесу."""
        now = self._clock()
        for chat_id in [c for c, p in self._pending.items() if now - p.started > self.ttl]:
            del self._pending[chat_id]
        for chat_id, saved in list(self._saved.items()):
            for token in [t for t, p in saved.items() if now - p.started > self.ttl]:
                del saved[token]
            if not saved:
                del self._saved[chat_id]
        for token in [t for t, at in self._finished.items() if now - at > self.ttl]:
            del self._finished[token]

    # --- вхідні події

    async def on_document(self, bot: Bot, message: Message) -> None:
        document = message.document
        assert document is not None and message.from_user is not None
        chat_id = message.chat.id
        filename = unicodedata.normalize("NFC", document.file_name or "")
        suffix = PurePath(filename).suffix.lower()
        if suffix == ".doc":
            await _send(bot, chat_id, texts.DOC_FORMAT)
            return
        if suffix not in ACCEPTED or (document.mime_type or "").startswith("image/"):
            await _send(bot, chat_id, texts.FORMATS)
            return
        if document.file_size is not None and document.file_size > MAX_FILE_BYTES:
            await _send(bot, chat_id, texts.TOO_BIG)
            return

        if self.live(chat_id) is not None:
            await _send(bot, chat_id, texts.PREVIOUS_SHELVED)
        pending = PendingUpload(
            token=secrets.token_hex(4),
            chat_id=chat_id,
            telegram_id=message.from_user.id,
            file_id=document.file_id,
            filename=filename,
            title=PurePath(filename).stem,
            started=self._clock(),
            stage=Stage.READING,
        )
        self._pending[chat_id] = pending
        pending.beast = await self.service.beast(pending.telegram_id)
        if pending.beast is None:
            pending.stage = Stage.NAME
            await _send(
                bot, chat_id, texts.ASK_NAME, _keyboard(pending, [(texts.CANCEL, "cancel")])
            )
            return
        await self._read_file(bot, pending)

    async def on_name(self, bot: Bot, message: Message) -> bool:
        """Текст як ім'я. False — бот уже не чекає імені від цього звіра (крок змінився,
        поки повідомлення стояло в черзі), і текст треба обробити як питання."""
        sender = message.from_user.id if message.from_user else None
        if not self.awaiting_name(message.chat.id, sender):
            return False
        pending = self._pending[message.chat.id]
        name = valid_name(message.text or "")
        if name is None:
            await _send(bot, pending.chat_id, texts.BAD_NAME)
            return True
        pending.stage = Stage.READING
        pending.beast = await self.service.register(pending.telegram_id, name)
        await _send(bot, pending.chat_id, texts.NICE_TO_MEET.format(name=name))
        await self._read_file(bot, pending)
        return True

    async def on_callback(self, bot: Bot, callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is None:
            return
        chat_id = callback.message.chat.id
        parts = (callback.data or "").split(":", 3)
        _, token, action, argument = parts + [""] * (4 - len(parts))
        pending = self.live(chat_id)
        if pending is None or pending.token != token:
            pending = self._saved.get(chat_id, {}).get(token)
        if pending is None and token in self._finished:
            log.info("button of a finished upload pressed again: %r", callback.data)
            return
        if pending is None or not _expected(pending.stage, action):
            log.info("stale upload callback %r in chat %d", callback.data, chat_id)
            await _send(bot, chat_id, texts.STALE)
            return
        if action == "cancel":
            self._finish(pending)
            await _send(bot, chat_id, texts.CANCELLED)
        elif action == "ocr_yes":
            pending.ocr_confirmed = True
            await self._choose_place(bot, pending)
        elif action == "ocr_no":
            self._finish(pending)
            await _send(bot, chat_id, texts.OCR_REJECTED)
        elif action == "place":
            await self._place_chosen(bot, pending, argument)
        elif action == "replace":
            await self._save(bot, pending, replaces=pending.candidate_id)
        elif action == "separate":
            await self._save(bot, pending, replaces=None)
        elif action == "other":
            await self._offer_other_versions(bot, pending)
        elif action == "pick":
            await self._rebind(bot, pending, int(argument))
        elif action == "map_yes":
            await self._replace_map(bot, pending)

    # --- кроки

    async def _read_file(self, bot: Bot, pending: PendingUpload) -> None:
        pending.stage = Stage.READING
        await bot.send_chat_action(pending.chat_id, ChatAction.UPLOAD_DOCUMENT)
        try:
            async with ChatActionSender.upload_document(
                chat_id=pending.chat_id, bot=bot, initial_sleep=TYPING_REPEAT
            ):
                data = await self._download(bot, pending.file_id)
                pending.sha256 = hashlib.sha256(data).hexdigest()
                pending.extracted = await self._read(data, pending.filename)
        except UploadRejected as rejected:
            self._drop(pending)
            await _send(bot, pending.chat_id, texts.READ_REJECTED[rejected.reason])
            return
        except TelegramBadRequest as exc:
            # Без file_size у повідомленні межу 20 МБ перевіряє вже сам Telegram.
            self._drop(pending)
            log.info("download of %r refused: %s", pending.filename, exc.message)
            too_big = "too big" in exc.message.lower()
            await _send(
                bot, pending.chat_id, texts.TOO_BIG if too_big else texts.READ_REJECTED[UNREADABLE]
            )
            return
        except Exception:
            # Будь-яка інша помилка (мережа, OCR) не має лишити завантаження висіти:
            # інакше наступний текст звіра став би його «ім'ям».
            self._drop(pending)
            log.exception("reading upload %r failed", pending.filename)
            await _send(bot, pending.chat_id, texts.READ_REJECTED[UNREADABLE])
            return
        extracted = pending.extracted
        if extracted.source == TextSource.OCR and extracted.review_items:
            pending.stage = Stage.OCR
            await _send(
                bot,
                pending.chat_id,
                _ocr_review(extracted),
                _keyboard(pending, [(texts.OCR_YES, "ocr_yes"), (texts.OCR_NO, "ocr_no")]),
            )
            return
        await self._choose_place(bot, pending)

    async def _choose_place(self, bot: Bot, pending: PendingUpload) -> None:
        assert pending.extracted is not None
        gloves = self.service.search.state.resolver.gloves
        pending.place = classify_upload(pending.title, pending.extracted.text, gloves)
        if pending.place is not None and pending.place.kind == DocumentKind.MAP:
            await self._preview_map(bot, pending)
            return
        if pending.place is None:
            pending.stage = Stage.PLACE
            keyboard = place_keyboard(all_places(gloves), prefix=_callback(pending, "place:"))
            await _send(bot, pending.chat_id, texts.ASK_PLACE, keyboard)
            return
        await self._check_version(bot, pending)

    async def _place_chosen(self, bot: Bot, pending: PendingUpload, key: str) -> None:
        place = find_place(key, all_places(self.service.search.state.resolver.gloves))
        if place is None:
            log.warning("upload place button for a place no longer on the map: %r", key)
            gloves = self.service.search.state.resolver.gloves
            keyboard = place_keyboard(all_places(gloves), prefix=_callback(pending, "place:"))
            await _send(bot, pending.chat_id, texts.UPLOAD_PLACE_GONE, keyboard)
            return
        topic, from_rules = upload_topic(pending.title)
        pending.place = UploadClassification(place.kind, place.scope, topic, from_rules)
        await self._check_version(bot, pending)

    async def _check_version(self, bot: Bot, pending: PendingUpload) -> None:
        assert pending.extracted is not None and pending.place is not None
        index = self.service.search.state.index
        place = pending.place
        candidate = find_version_candidate(
            pending.extracted.text,
            place.scope,
            place.topic,
            index.documents,
            index,
            topic_from_rules=place.topic_from_rules,
        )
        if candidate is None:
            await self._save(bot, pending, replaces=None)
            return
        pending.stage = Stage.VERSION
        pending.candidate_id = candidate.document.document_id
        question = texts.ASK_REPLACE.format(
            title=candidate.document.title, place=human_place(place.kind, place.scope)
        )
        buttons = [(texts.REPLACE, "replace"), (texts.SEPARATE, "separate")]
        await _send(bot, pending.chat_id, question, _keyboard(pending, buttons))

    async def _save(self, bot: Bot, pending: PendingUpload, *, replaces: int | None) -> None:
        assert pending.extracted is not None and pending.place is not None
        assert pending.beast is not None
        place = pending.place
        try:
            saved = await self.service.save(
                pending.beast,
                pending.extracted,
                pending.sha256,
                pending.title,
                pending.filename,
                place.scope,
                place.kind,
                place.topic,
                replaces,
                ocr_confirmed=pending.ocr_confirmed,
            )
        except ConflictError:
            self._drop(pending)
            await _send(bot, pending.chat_id, texts.CONFLICT)
            return
        if saved.replaced_title is not None:
            self._finish(pending)
            await _send(bot, pending.chat_id, _replaced_text(pending.beast, saved))
            return
        pending.stage = Stage.SAVED
        pending.saved = saved
        # Записана порада більше не «незавершена»: новий файл її кнопку не скасовує.
        self._drop(pending)
        self._saved.setdefault(pending.chat_id, {})[pending.token] = pending
        text = texts.SAVED_NEW.format(
            name=pending.beast.name,
            title=saved.title,
            place=human_place(saved.kind, saved.scope),
        )
        await _send(
            bot, pending.chat_id, text, _keyboard(pending, [(texts.OTHER_VERSION, "other")])
        )

    async def _offer_other_versions(self, bot: Bot, pending: PendingUpload) -> None:
        assert pending.saved is not None and pending.extracted is not None
        saved = pending.saved
        ranked = rank_in_scope(
            pending.extracted.text,
            saved.scope,
            self.service.search.state.index,
            exclude=[saved.id],
        )[:OTHER_VERSIONS]
        if not ranked:
            await _send(
                bot,
                pending.chat_id,
                texts.NO_OTHER.format(place=human_place(saved.kind, saved.scope)),
            )
            return
        pending.offered = [s.document.document_id for s in ranked]
        buttons = [(s.document.title, f"pick:{s.document.document_id}") for s in ranked]
        await _send(bot, pending.chat_id, texts.PICK_OTHER, _keyboard(pending, buttons, per_row=1))

    async def _rebind(self, bot: Bot, pending: PendingUpload, target_id: int) -> None:
        assert pending.saved is not None and pending.beast is not None
        if target_id not in pending.offered:
            await _send(bot, pending.chat_id, texts.STALE)
            return
        try:
            saved = await self.service.rebind(pending.saved.id, target_id)
        except ConflictError:
            self._drop(pending)
            await _send(bot, pending.chat_id, texts.CONFLICT)
            return
        self._finish(pending)
        await _send(bot, pending.chat_id, _replaced_text(pending.beast, saved))

    async def _preview_map(self, bot: Bot, pending: PendingUpload) -> None:
        assert pending.extracted is not None
        try:
            forest_map = parse_forest_map(pending.extracted.text)
        except MapParseError as exc:
            log.info("uploaded map %r is unreadable: %s", pending.filename, exc)
            forest_map = {}
        if len(forest_map) < MIN_MAP_GLOVES:
            self._drop(pending)
            await _send(bot, pending.chat_id, texts.MAP_UNREADABLE)
            return
        pending.stage = Stage.MAP
        pending.forest_map = forest_map
        resolver = self.service.search.state.resolver
        current = {glove: resolver.parts_of(glove) for glove in resolver.gloves}
        buttons = [(texts.REPLACE_MAP, "map_yes"), (texts.CANCEL, "cancel")]
        await _send(
            bot, pending.chat_id, map_preview(forest_map, current), _keyboard(pending, buttons)
        )

    async def _replace_map(self, bot: Bot, pending: PendingUpload) -> None:
        assert pending.extracted is not None and pending.forest_map is not None
        assert pending.beast is not None
        try:
            await self.service.replace_map(
                pending.beast,
                pending.extracted,
                pending.sha256,
                pending.filename,
                pending.forest_map,
                ocr_confirmed=pending.ocr_confirmed,
            )
        except ConflictError:
            self._drop(pending)
            await _send(bot, pending.chat_id, texts.CONFLICT)
            return
        self._finish(pending)
        await _send(bot, pending.chat_id, texts.MAP_REPLACED)

    def _drop(self, pending: PendingUpload) -> None:
        if self._pending.get(pending.chat_id) is pending:
            del self._pending[pending.chat_id]
        self._saved.get(pending.chat_id, {}).pop(pending.token, None)

    def _finish(self, pending: PendingUpload) -> None:
        self._drop(pending)
        self._finished[pending.token] = self._clock()


# --- кнопки й тексти

# Яка дія має сенс на якому кроці: кнопка зі старого кроку того самого файлу — теж протухла.
_ACTIONS: dict[Stage, set[str]] = {
    Stage.READING: set(),
    Stage.NAME: {"cancel"},
    Stage.OCR: {"ocr_yes", "ocr_no"},
    Stage.PLACE: {"place"},
    Stage.VERSION: {"replace", "separate"},
    Stage.SAVED: {"other", "pick"},
    Stage.MAP: {"map_yes", "cancel"},
}


def _expected(stage: Stage, action: str) -> bool:
    return action in _ACTIONS[stage]


def _callback(pending: PendingUpload, action: str) -> str:
    return f"{CALLBACK_PREFIX}{pending.token}:{action}"


def _keyboard(
    pending: PendingUpload, buttons: list[tuple[str, str]], *, per_row: int = 2
) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=label, callback_data=_callback(pending, action))
        for label, action in buttons
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[row[i : i + per_row] for i in range(0, len(row), per_row)]
    )


def map_preview(
    new: dict[str, tuple[ForestPart, ...]], current: dict[str, tuple[ForestPart, ...]]
) -> str:
    """Розібрана нова карта і її різниця з чинною: що додано, прибрано, змінено."""
    lines = [texts.MAP_PREVIEW]
    lines += [f"{glove} — {_parts_text(parts, texts.MAP_NO_PART)}" for glove, parts in new.items()]
    added = [glove for glove in new if glove not in current]
    removed = [glove for glove in current if glove not in new]
    changed = [
        texts.MAP_CHANGED.format(
            glove=glove,
            old=_parts_text(current[glove], texts.MAP_NO_PART_SHORT),
            new=_parts_text(parts, texts.MAP_NO_PART_SHORT),
        )
        for glove, parts in new.items()
        if glove in current and set(parts) != set(current[glove])
    ]
    lines.append("")
    if not (added or removed or changed):
        lines.append(texts.MAP_SAME)
        return "\n".join(lines)
    lines.append(texts.MAP_DIFF)
    if added:
        lines.append(texts.MAP_ADDED.format(gloves=", ".join(added)))
    if removed:
        lines.append(texts.MAP_REMOVED.format(gloves=", ".join(removed)))
    lines += changed
    return "\n".join(lines)


def _parts_text(parts: tuple[ForestPart, ...], none: str) -> str:
    return " і ".join(PART_NAMES[part] for part in parts) or none


def _ocr_review(extracted: Extracted) -> str:
    lines = [texts.OCR_INTRO]
    for item in extracted.review_items[:OCR_REVIEW_LINES]:
        note = texts.OCR_DIGIT_LIKE if item.reason == "digit_like_letter" else ""
        lines.append(f"— …{_snippet(item.line, item.token)}…{note}")
    lines.append(texts.OCR_QUESTION)
    return "\n".join(lines)


def _snippet(line: str, token: str) -> str:
    """Фрагмент рядка навколо числа: звір має впізнати місце, а не читати весь рядок."""
    position = max(line.find(token), 0)
    start = max(position - SNIPPET_RADIUS, 0)
    end = min(position + len(token) + SNIPPET_RADIUS, len(line))
    return " ".join(line[start:end].split())


def _replaced_text(beast: BeastInfo, saved: SavedDocument) -> str:
    return texts.SAVED_REPLACED.format(
        name=beast.name, old=saved.replaced_title, place=human_place(saved.kind, saved.scope)
    )


async def _send(
    bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    await bot.send_message(chat_id, text, parse_mode=None, reply_markup=keyboard)


# --- хендлери


class AwaitingName(Filter):
    """Текст — це ім'я лише тоді, коли бот саме його попросив (вимога 12)."""

    async def __call__(self, message: Message, uploads: UploadDialogue) -> bool:
        sender = message.from_user.id if message.from_user else None
        return uploads.awaiting_name(message.chat.id, sender)


@uploads_router.message(Command("name"))
async def name_command(
    message: Message, command: CommandObject, bot: Bot, uploads: UploadDialogue
) -> None:
    """/name — показати ім'я; /name Нове Ім'я — змінити. Зареєструватися так не можна:
    ім'я бот просить разом із першою порадою (вимога 12)."""
    assert message.from_user is not None
    service = uploads.service
    if not command.args:
        beast = await service.beast(message.from_user.id)
        text = texts.NOT_NAMED if beast is None else texts.CURRENT_NAME.format(name=beast.name)
        await _send(bot, message.chat.id, text)
        return
    name = valid_name(command.args)
    if name is None:
        await _send(bot, message.chat.id, texts.BAD_NAME)
        return
    beast = await service.rename(message.from_user.id, name)
    text = texts.NOT_NAMED if beast is None else texts.NAME_CHANGED.format(name=name)
    await _send(bot, message.chat.id, text)


@uploads_router.message(F.document)
async def document_sent(message: Message, bot: Bot, uploads: UploadDialogue) -> None:
    async with uploads.lock(message.chat.id):
        await uploads.on_document(bot, message)


@uploads_router.message(F.photo)
async def photo_sent(message: Message, bot: Bot) -> None:
    await _send(bot, message.chat.id, texts.FORMATS)


@uploads_router.message(F.text & ~F.text.startswith("/"), AwaitingName())
async def name_sent(
    message: Message, bot: Bot, uploads: UploadDialogue, answers: AnswerService
) -> None:
    async with uploads.lock(message.chat.id):
        taken = await uploads.on_name(bot, message)
    if not taken:
        # Фільтр бачив крок «ім'я», але поки повідомлення чекало замка, крок змінився:
        # тоді це звичайне питання.
        await answer_question(bot, message.chat.id, message.text or "", answers)


@uploads_router.callback_query(F.data.startswith(CALLBACK_PREFIX))
async def upload_button(callback: CallbackQuery, bot: Bot, uploads: UploadDialogue) -> None:
    chat_id = callback.message.chat.id if callback.message else 0
    async with uploads.lock(chat_id):
        await uploads.on_callback(bot, callback)
