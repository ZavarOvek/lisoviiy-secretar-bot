import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Beast, Document, ForestMapEntry
from lisovyi.domain import DocumentKind, DocumentStatus, TextSource
from lisovyi.ingest.importer import ArchiveImportError, ImportStats, import_archive

pytestmark = pytest.mark.db

ARCHIVE = PROJECT_ROOT / "data" / "archive"
TRANSCRIPTS = PROJECT_ROOT / "data" / "transcripts"

EXPECTED_FOREST_MAP = {
    "Під ялиною": ["khvoinyi"],
    "У соснах": ["khvoinyi"],
    "Біля мурашника": ["khvoinyi"],
    "Під дубом": ["lystianyi"],
    "У ліщині": ["lystianyi"],
    "Під вербою": ["lystianyi", "richka"],
    "Біля бобрової греблі": ["richka"],
    "На узліссі": [],
}


async def _import(sessionmaker, archive: Path = ARCHIVE) -> ImportStats:
    async with sessionmaker() as session:
        return await import_archive(session, archive, TRANSCRIPTS)


async def _active_counts(sessionmaker, column) -> dict[str, int]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(column, func.count())
            .where(Document.status == DocumentStatus.ACTIVE)
            .group_by(column)
        )
        return {value.value: count for value, count in rows}


async def _documents(sessionmaker, **filters) -> list[Document]:
    async with sessionmaker() as session:
        return list(
            await session.scalars(select(Document).filter_by(**filters).order_by(Document.id))
        )


def _archive_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "archive"
    shutil.copytree(ARCHIVE, copy)
    return copy


async def test_first_import(sessionmaker):
    assert await _import(sessionmaker) == ImportStats(inserted=78, replaced=0, skipped=0)

    assert await _active_counts(sessionmaker, Document.kind) == {
        "glove": 56,
        "part": 15,
        "general": 6,
        "map": 1,
    }
    assert await _active_counts(sessionmaker, Document.text_source) == {
        "text": 59,
        "transcript": 19,
    }
    async with sessionmaker() as session:
        entries = list(await session.scalars(select(ForestMapEntry)))
        map_document = await session.scalar(
            select(Document).where(Document.kind == DocumentKind.MAP)
        )
    assert {entry.glove: entry.parts for entry in entries} == EXPECTED_FOREST_MAP
    assert {entry.map_document_id for entry in entries} == {map_document.id}
    documents = await _documents(sessionmaker)
    assert {(d.ocr_confirmed, d.author_id, d.replaces_id) for d in documents} == {
        (None, None, None)
    }


async def test_second_import_skips_everything(sessionmaker):
    await _import(sessionmaker)
    assert await _import(sessionmaker) == ImportStats(inserted=0, replaced=0, skipped=78)
    assert len(await _documents(sessionmaker)) == 78


async def test_deleted_document_stays_deleted(sessionmaker):
    await _import(sessionmaker)
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Document)
            .where(Document.filename == "Правила толоки.txt")
            .values(status=DocumentStatus.DELETED)
        )

    assert await _import(sessionmaker) == ImportStats(inserted=0, replaced=0, skipped=78)
    [document] = await _documents(sessionmaker, scope="general", topic="toloka")
    assert document.status == DocumentStatus.DELETED


async def test_changed_archive_file_replaces_active_version(sessionmaker, tmp_path):
    await _import(sessionmaker)
    archive = _archive_copy(tmp_path)
    changed = archive / "Правила толоки.txt"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nНове правило: приходити з власним кошиком.\n",
        encoding="utf-8",
    )

    assert await _import(sessionmaker, archive) == ImportStats(inserted=0, replaced=1, skipped=77)
    old, new = await _documents(sessionmaker, scope="general", topic="toloka")
    assert old.status == DocumentStatus.REPLACED
    assert new.status == DocumentStatus.ACTIVE
    assert new.replaces_id == old.id
    assert "власним кошиком" in new.text
    assert sum((await _active_counts(sessionmaker, Document.kind)).values()) == 78


async def test_new_glove_document_uses_gloves_of_the_imported_map(sessionmaker, tmp_path):
    await _import(sessionmaker)
    archive = _archive_copy(tmp_path)
    (archive / "Статут ОСМД Під ялиною.docx").unlink()
    (archive / "Статут ОСМД Під ялиною.txt").write_text(
        "Статут ОСМД Під ялиною\nНовий статут.", encoding="utf-8"
    )

    # Карта пропущена (її sha256 уже є), тож рукавички беруться з forest_map у БД.
    assert await _import(sessionmaker, archive) == ImportStats(inserted=0, replaced=1, skipped=77)
    [active] = await _documents(
        sessionmaker, scope="Під ялиною", topic="statut", status=DocumentStatus.ACTIVE
    )
    assert (active.kind, active.filename) == (DocumentKind.GLOVE, "Статут ОСМД Під ялиною.txt")


PINES = "Як не заблукати серед однакових сосен.txt"


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


async def test_archive_file_with_changed_scope_supersedes_its_old_version(
    sessionmaker, tmp_path, caplog
):
    await _import(sessionmaker)
    archive = _archive_copy(tmp_path)
    _rewrite(archive / PINES, "мешканців хвойного лісу", "мешканців листяного лісу")

    with caplog.at_level("WARNING"):
        stats = await _import(sessionmaker, archive)

    assert stats == ImportStats(inserted=0, replaced=1, skipped=77)
    old, new = await _documents(sessionmaker, filename=PINES)
    assert (old.scope, old.status) == ("khvoinyi", DocumentStatus.REPLACED)
    assert (new.scope, new.topic, new.status) == (
        "lystianyi",
        "oriientuvannia",
        DocumentStatus.ACTIVE,
    )
    assert new.replaces_id == old.id
    assert await _documents(sessionmaker, scope="khvoinyi", status=DocumentStatus.ACTIVE) != []
    assert (
        await _documents(
            sessionmaker, scope="khvoinyi", topic="oriientuvannia", status=DocumentStatus.ACTIVE
        )
        == []
    )
    # Інший документ не постраждав, тож попереджати нема про що.
    assert caplog.records == []


async def test_changed_file_landing_on_another_files_place_replaces_both(
    sessionmaker, tmp_path, caplog
):
    await _import(sessionmaker)
    archive = _archive_copy(tmp_path)
    # Порада хвойного лісу стає порадою листяного: місце (lystianyi, yizha) уже зайняте.
    _rewrite(archive / "Що їсти в хвойному лісі.txt", "хвойного лісу.", "листяного лісу.")

    with caplog.at_level("WARNING"):
        stats = await _import(sessionmaker, archive)

    assert stats == ImportStats(inserted=0, replaced=1, skipped=77)
    [old, new] = await _documents(sessionmaker, filename="Що їсти в хвойному лісі.txt")
    [other] = await _documents(sessionmaker, filename="Що їсти в листяному лісі.pdf")
    assert old.status == other.status == DocumentStatus.REPLACED
    assert (new.scope, new.topic, new.status) == ("lystianyi", "yizha", DocumentStatus.ACTIVE)
    assert new.replaces_id == old.id
    [record] = caplog.records
    assert record.levelname == "WARNING"
    assert "Що їсти в хвойному лісі.txt" in record.message
    assert "Що їсти в листяному лісі.pdf" in record.message


async def test_beast_document_is_never_matched_by_filename(sessionmaker, tmp_path):
    await _import(sessionmaker)
    async with sessionmaker() as session, session.begin():
        beast = Beast(telegram_id=1, name="Їжак")
        session.add(beast)
        await session.flush()
        session.add(
            Document(
                filename=PINES,
                title="Моя порада",
                kind=DocumentKind.GLOVE,
                scope="Під ялиною",
                topic="moia_porada",
                text="...",
                text_source=TextSource.TEXT,
                sha256="1" * 64,
                author_id=beast.id,
            )
        )
    archive = _archive_copy(tmp_path)
    _rewrite(archive / PINES, "мешканців хвойного лісу", "мешканців листяного лісу")

    assert await _import(sessionmaker, archive) == ImportStats(inserted=0, replaced=1, skipped=77)
    [beast_document] = await _documents(sessionmaker, filename=PINES, author_id=beast.id)
    assert beast_document.status == DocumentStatus.ACTIVE


async def test_failing_file_rolls_back_the_whole_import(sessionmaker, tmp_path):
    archive = _archive_copy(tmp_path)
    (archive / "Порада.doc").write_bytes(b"old Word format")

    with pytest.raises(ArchiveImportError, match=r"Порада\.doc: .*unsupported file type"):
        await _import(sessionmaker, archive)

    assert await _documents(sessionmaker) == []
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ForestMapEntry)) == 0


async def test_map_with_too_few_gloves_fails(sessionmaker, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "Карта лісу.txt").write_text(
        "Карта лісу\nЛегенда карти\nПід ялиною — хвойний ліс.\nПід дубом — листяний ліс.\n"
        "Дві особливі рукавички\n",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveImportError, match="only 2 gloves"):
        await _import(sessionmaker, archive)


async def test_two_archive_files_with_same_scope_and_topic_fail(sessionmaker, tmp_path):
    archive = _archive_copy(tmp_path)
    (archive / "Правила толоки, друга редакція.txt").write_text(
        "Правила толоки, друга редакція\nЦі правила стосуються всіх мешканців лісу.",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveImportError, match="same scope and topic"):
        await _import(sessionmaker, archive)


async def test_archive_without_map_fails(sessionmaker, tmp_path):
    archive = _archive_copy(tmp_path)
    (archive / "Карта лісу.pdf").unlink()
    with pytest.raises(ArchiveImportError, match="exactly one 'Карта лісу'"):
        await _import(sessionmaker, archive)
