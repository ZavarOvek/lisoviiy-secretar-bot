"""Ідемпотентний імпорт архіву в БД на старті бота."""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from lisovyi.db.models import Document, ForestMapEntry
from lisovyi.domain import DocumentStatus, ForestPart, TextSource
from lisovyi.ingest.classify import MAP_TITLE, classify
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.parsers import TranscriptStore, extract_text

log = logging.getLogger(__name__)

# Менше рукавичок в архівній карті означає, що легенду розібрано неправильно.
MIN_ARCHIVE_GLOVES = 8


class ArchiveImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportStats:
    # Кожен файл архіву потрапляє рівно в одну з трьох груп.
    inserted: int  # новий файл для нової пари (scope, topic)
    # нова версія, що замінила активний документ того самого архівного файлу
    # та/або тієї ж пари (scope, topic)
    replaced: int
    skipped: int  # такий sha256 уже є в БД з будь-яким статусом


async def import_archive(
    session: AsyncSession, archive_dir: Path, transcripts_dir: Path
) -> ImportStats:
    """Імпортує архів в одній транзакції: помилка будь-якого файлу відкочує все."""
    files = sorted(path for path in archive_dir.iterdir() if path.is_file())
    maps = [path for path in files if path.stem == MAP_TITLE]
    if len(maps) != 1:
        raise ArchiveImportError(
            f"expected exactly one {MAP_TITLE!r} file in {archive_dir}, found {len(maps)}"
        )
    map_path = maps[0]
    importer = _Importer(session, TranscriptStore.load(transcripts_dir), map_path)
    async with session.begin():
        for path in [map_path, *(path for path in files if path != map_path)]:
            try:
                await importer.import_file(path)
            except Exception as exc:
                raise ArchiveImportError(f"{path.name}: {exc}") from exc
    return ImportStats(importer.inserted, importer.replaced, importer.skipped)


class _Importer:
    def __init__(self, session: AsyncSession, transcripts: TranscriptStore, map_path: Path):
        self.session = session
        self.transcripts = transcripts
        self.map_path = map_path
        self.gloves: list[str] | None = None
        # (scope, topic) → файл, що їх отримав у цьому запуску.
        self.claimed: dict[tuple[str, str], str] = {}
        self.inserted = self.replaced = self.skipped = 0

    async def import_file(self, path: Path) -> None:
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        # Відомий sha256 пропускається за будь-якого статусу: інакше документ, який звір
        # замінив або видалив, «воскресав» би після кожного перезапуску.
        if await self.session.scalar(select(exists().where(Document.sha256 == sha256))):
            self.skipped += 1
            return

        extracted = extract_text(data, path.name, self.transcripts)
        forest_map = None
        if path == self.map_path:
            forest_map = parse_forest_map(extracted.text)
            if len(forest_map) < MIN_ARCHIVE_GLOVES:
                raise ArchiveImportError(
                    f"only {len(forest_map)} gloves parsed from the map legend, "
                    f"expected at least {MIN_ARCHIVE_GLOVES}"
                )
            self.gloves = list(forest_map)
        elif self.gloves is None:
            self.gloves = await self._gloves_from_db()

        classification = classify(path.stem, extracted.text, self.gloves)
        if classification is None:
            raise ArchiveImportError("cannot determine the document's scope")
        key = (classification.scope, classification.topic)
        if key in self.claimed:
            raise ArchiveImportError(
                f"same scope and topic {key} as {self.claimed[key]!r} in this archive"
            )
        self.claimed[key] = path.name

        document = Document(
            filename=path.name,
            title=path.stem,
            kind=classification.kind,
            scope=classification.scope,
            topic=classification.topic,
            text=extracted.text,
            text_source=extracted.source,
            sha256=sha256,
            ocr_confirmed=False if extracted.source == TextSource.OCR else None,
            status=DocumentStatus.ACTIVE,
        )
        # Попередня версія того самого архівного файлу. Документи звірів за іменем
        # файлу не зіставляються: звір міг назвати свій файл так само випадково.
        same_file = await self._active(Document.filename == path.name, Document.author_id.is_(None))
        # Активний документ того самого місця й теми, якщо класифікація збіглася.
        same_place = await self._active(
            Document.scope == classification.scope, Document.topic == classification.topic
        )
        if same_file is None and same_place is None:
            self.inserted += 1
        else:
            # Спершу знімаємо старі версії з активних, інакше вставка порушить
            # частковий унікальний індекс (scope, topic) WHERE status = 'active'.
            if same_file is not None:
                same_file.status = DocumentStatus.REPLACED
            if same_place is not None and same_place is not same_file:
                # Замінюється документ іншого файлу: варто знати, що саме зникло з активних.
                same_place.status = DocumentStatus.REPLACED
                log.warning(
                    "%s replaces %s, which was active for the same scope and topic %s",
                    path.name,
                    same_place.filename,
                    key,
                )
            await self.session.flush()
            # replaces_id веде до попередньої версії того самого файлу, якщо вона є.
            document.replaces_id = (same_file or same_place).id
            self.replaced += 1
        self.session.add(document)
        await self.session.flush()

        if forest_map is not None:
            await self._rebuild_forest_map(forest_map, document.id)

    async def _active(self, *conditions) -> Document | None:
        return await self.session.scalar(
            select(Document)
            .where(*conditions, Document.status == DocumentStatus.ACTIVE)
            .with_for_update()
        )

    async def _gloves_from_db(self) -> list[str]:
        gloves = list(
            await self.session.scalars(
                select(ForestMapEntry.glove).order_by(ForestMapEntry.position, ForestMapEntry.glove)
            )
        )
        if not gloves:
            raise ArchiveImportError(
                "forest_map is empty: the map was imported earlier but its legend is not in the DB"
            )
        return gloves

    async def _rebuild_forest_map(
        self, forest_map: dict[str, tuple[ForestPart, ...]], map_document_id: int
    ) -> None:
        await self.session.execute(delete(ForestMapEntry))
        self.session.add_all(
            ForestMapEntry(
                glove=glove,
                parts=[part.value for part in parts],
                map_document_id=map_document_id,
                position=position,
            )
            for position, (glove, parts) in enumerate(forest_map.items())
        )
        await self.session.flush()
        log.info("forest map rebuilt: %d gloves", len(forest_map))
