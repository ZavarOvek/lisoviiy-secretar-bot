"""Звірі й їхні поради в БД: реєстрація, збереження з заміною, переприв'язка версії."""

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lisovyi.db.models import Beast, Document
from lisovyi.domain import GENERAL_SCOPE, DocumentKind, DocumentStatus, ForestPart, TextSource
from lisovyi.ingest.classify import MAP_TITLE, MAP_TOPIC
from lisovyi.ingest.importer import rebuild_forest_map
from lisovyi.ingest.parsers import Extracted
from lisovyi.search.service import SearchService

log = logging.getLogger(__name__)

NAME_MIN, NAME_MAX = 2, 40
_NAME_EXTRA = frozenset(" -'’ʼ")


class ConflictError(Exception):
    """Документ, який звір замінює, уже не активний: його щойно замінив хтось інший."""


@dataclass(frozen=True)
class BeastInfo:
    id: int
    telegram_id: int
    name: str


@dataclass(frozen=True)
class SavedDocument:
    id: int
    title: str
    kind: DocumentKind
    scope: str
    topic: str
    # Назва документа, який новий замінив; None — окрема порада.
    replaced_title: str | None = None


def valid_name(raw: str) -> str | None:
    """Ім'я звіра: 2–40 символів після strip, лише літери, пробіл, дефіс, апостроф."""
    name = unicodedata.normalize("NFC", raw).strip()
    if not NAME_MIN <= len(name) <= NAME_MAX:
        return None
    if not all(ch.isalpha() or ch in _NAME_EXTRA for ch in name):
        return None
    # Ім'я з самих розділових знаків («--») — не ім'я.
    if not any(ch.isalpha() for ch in name):
        return None
    return name


class UploadService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], search: SearchService):
        self._sessionmaker = sessionmaker
        self.search = search

    # --- звірі

    async def beast(self, telegram_id: int) -> BeastInfo | None:
        async with self._sessionmaker() as session:
            beast = await session.scalar(select(Beast).where(Beast.telegram_id == telegram_id))
            return _beast_info(beast) if beast else None

    async def register(self, telegram_id: int, name: str) -> BeastInfo:
        """Запам'ятовує звіра з ім'ям; повторна реєстрація просто міняє ім'я."""
        async with self._sessionmaker() as session, session.begin():
            beast = await session.scalar(
                select(Beast).where(Beast.telegram_id == telegram_id).with_for_update()
            )
            if beast is None:
                beast = Beast(telegram_id=telegram_id, name=name)
                session.add(beast)
            else:
                beast.name = name
            await session.flush()
            info = _beast_info(beast)
        log.info("beast %d registered as %r", telegram_id, name)
        return info

    async def rename(self, telegram_id: int, name: str) -> BeastInfo | None:
        """Нове ім'я; None — звір ще не реєструвався. Ім'я на документах береться з beasts
        під час рендеру, тож після reload нове ім'я видно й на старих порадах."""
        async with self._sessionmaker() as session, session.begin():
            beast = await session.scalar(
                select(Beast).where(Beast.telegram_id == telegram_id).with_for_update()
            )
            if beast is None:
                return None
            beast.name = name
            info = _beast_info(beast)
        await self.search.reload()
        return info

    # --- документи

    async def save(
        self,
        beast: BeastInfo,
        extracted: Extracted,
        data: bytes,
        title: str,
        filename: str,
        scope: str,
        kind: DocumentKind,
        topic: str,
        replaces: int | None,
        *,
        ocr_confirmed: bool = False,
    ) -> SavedDocument:
        """Записує пораду в одній транзакції й перебудовує пошук.

        `replaces` — id документа, який звір замінює (будь-який активний, архівний чи чужий).
        Новий переймає його тему й отримує replaces_id, старий стає replaced. Без заміни тема
        запропонована; якщо в цьому місці вона вже зайнята, додається суфікс _2, _3, …
        """
        async with self._sessionmaker() as session:
            try:
                async with session.begin():
                    replaced_title = None
                    if replaces is not None:
                        old = await _active_for_update(session, replaces)
                        old.status = DocumentStatus.REPLACED
                        # Спершу знімаємо стару версію: інакше вставка порушить частковий
                        # унікальний індекс (scope, topic) WHERE status = 'active'.
                        await session.flush()
                        topic, replaced_title = old.topic, old.title
                    else:
                        topic = await _free_topic(session, scope, topic)
                    document = Document(
                        filename=filename,
                        title=title,
                        kind=kind,
                        scope=scope,
                        topic=topic,
                        text=extracted.text,
                        text_source=extracted.source,
                        sha256=hashlib.sha256(data).hexdigest(),
                        ocr_confirmed=_ocr_confirmed(extracted, ocr_confirmed),
                        author_id=beast.id,
                        status=DocumentStatus.ACTIVE,
                        replaces_id=replaces,
                    )
                    session.add(document)
                    await session.flush()
                    saved = SavedDocument(document.id, title, kind, scope, topic, replaced_title)
            except IntegrityError as exc:
                # Хтось паралельно зайняв ту саму пару (scope, topic).
                raise ConflictError(
                    f"scope {scope!r}, topic {topic!r} was taken meanwhile"
                ) from exc
        log.info(
            "beast %d saved %r as %s/%s topic=%s replaces=%s",
            beast.telegram_id,
            filename,
            kind,
            scope,
            topic,
            replaces,
        )
        await self.search.reload()
        return saved

    async def rebind(self, document_id: int, target_id: int) -> SavedDocument:
        """«Це нова версія іншої поради»: щойно доданий документ замінює `target_id`.

        В одній транзакції вибраний стає replaced, а новий переймає його тему й отримує
        replaces_id = target_id (на ньому бриф 05 повертатиме попередню версію).
        """
        async with self._sessionmaker() as session:
            try:
                async with session.begin():
                    target = await _active_for_update(session, target_id)
                    document = await _active_for_update(session, document_id)
                    if target.scope != document.scope:
                        raise ValueError(
                            f"document {document_id} and {target_id} are in different places"
                        )
                    target.status = DocumentStatus.REPLACED
                    await session.flush()
                    document.topic = target.topic
                    document.replaces_id = target.id
                    await session.flush()
                    saved = SavedDocument(
                        document.id,
                        document.title,
                        document.kind,
                        document.scope,
                        document.topic,
                        target.title,
                    )
            except IntegrityError as exc:
                raise ConflictError(f"topic of document {target_id} was taken meanwhile") from exc
        log.info("document %d rebound as a new version of %d", document_id, target_id)
        await self.search.reload()
        return saved

    async def replace_map(
        self,
        beast: BeastInfo,
        extracted: Extracted,
        data: bytes,
        filename: str,
        forest_map: dict[str, tuple[ForestPart, ...]],
        *,
        ocr_confirmed: bool = False,
    ) -> list[str]:
        """Нова «Карта лісу» в одній транзакції: стара карта replaced, нова active,
        forest_map перебудовано. Далі пошук перебудовується й відповідає за новою картою.

        Повертає рукавички, чиї активні документи лишилися поза новою картою. Їх не
        деактивують: карту можуть виправити назад, і тоді нічого не загубиться.
        """
        async with self._sessionmaker() as session:
            try:
                async with session.begin():
                    old = await session.scalar(
                        select(Document)
                        .where(
                            Document.kind == DocumentKind.MAP,
                            Document.status == DocumentStatus.ACTIVE,
                        )
                        .with_for_update()
                    )
                    if old is not None:
                        old.status = DocumentStatus.REPLACED
                        await session.flush()
                    document = Document(
                        filename=filename,
                        title=MAP_TITLE,
                        kind=DocumentKind.MAP,
                        scope=GENERAL_SCOPE,
                        topic=MAP_TOPIC,
                        text=extracted.text,
                        text_source=extracted.source,
                        sha256=hashlib.sha256(data).hexdigest(),
                        ocr_confirmed=_ocr_confirmed(extracted, ocr_confirmed),
                        author_id=beast.id,
                        status=DocumentStatus.ACTIVE,
                        replaces_id=old.id if old is not None else None,
                    )
                    session.add(document)
                    await session.flush()
                    await rebuild_forest_map(session, forest_map, document.id)
                    orphans = sorted(
                        set(
                            await session.scalars(
                                select(Document.scope).where(
                                    Document.kind == DocumentKind.GLOVE,
                                    Document.status == DocumentStatus.ACTIVE,
                                    Document.scope.not_in(list(forest_map)),
                                )
                            )
                        )
                    )
            except IntegrityError as exc:
                raise ConflictError("the forest map was replaced meanwhile") from exc
        log.info("beast %d replaced the forest map: %d gloves", beast.telegram_id, len(forest_map))
        if orphans:
            log.warning(
                "gloves with active documents are not on the new map: %s", ", ".join(orphans)
            )
        await self.search.reload()
        return orphans


async def _active_for_update(session: AsyncSession, document_id: int) -> Document:
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None or document.status != DocumentStatus.ACTIVE:
        raise ConflictError(f"document {document_id} is no longer active")
    return document


async def _free_topic(session: AsyncSession, scope: str, topic: str) -> str:
    """Перша вільна в цьому місці тема: topic, topic_2, topic_3, …"""
    taken = set(
        await session.scalars(
            select(Document.topic).where(
                Document.scope == scope,
                Document.status == DocumentStatus.ACTIVE,
                Document.topic.startswith(topic, autoescape=True),
            )
        )
    )
    if topic not in taken:
        return topic
    suffix = re.compile(rf"^{re.escape(topic)}_(\d+)$")
    used = {int(m[1]) for t in taken if (m := suffix.match(t))}
    n = 2
    while n in used:
        n += 1
    return f"{topic}_{n}"


def _ocr_confirmed(extracted: Extracted, confirmed: bool) -> bool | None:
    # NULL — не OCR; True — звір підтвердив числа; False — OCR без підтвердження.
    if extracted.source != TextSource.OCR:
        return None
    return confirmed


def _beast_info(beast: Beast) -> BeastInfo:
    return BeastInfo(beast.id, beast.telegram_id, beast.name)
