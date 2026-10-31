"""М'яке видалення власних порад із поверненням попередньої версії (бриф 05, §2.2)."""

import hashlib

import pytest
from sqlalchemy import select, update

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Document, ForestMapEntry
from lisovyi.domain import DocumentKind, DocumentStatus, ForestPart, TextSource
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.importer import import_archive
from lisovyi.ingest.parsers import Extracted
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import MapRequired, NotFound, UploadService

pytestmark = pytest.mark.db

GUIDE = "Грибний путівник хвойного лісу.pdf"
ON_EDGE = "На узліссі — ні хвойний, ні листяний ліс, і до річки далеко."


@pytest.fixture
async def uploads(sessionmaker):
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    search = SearchService(sessionmaker)
    await search.reload()
    return UploadService(sessionmaker, search)


def _text(text: str) -> Extracted:
    return Extracted(text, TextSource.TEXT)


async def _doc(sessionmaker, **where) -> Document:
    async with sessionmaker() as session:
        return await session.scalar(select(Document).filter_by(**where))


async def _replace_guide(uploads, beast, filename: str, replaces: int):
    return await uploads.save(
        beast, _text(f"Гриби від {beast.name}."), hashlib.sha256(filename.encode()).hexdigest(),
        filename.removesuffix(".txt"), filename, "khvoinyi", DocumentKind.PART, "hryby", replaces,
    )  # fmt: skip


def _indexed(uploads) -> set[str]:
    return {d.filename for d in uploads.search.state.index.documents}


async def test_own_separate_advice_is_deleted_and_nothing_returns(uploads, sessionmaker):
    hedgehog = await uploads.register(7, "Їжак")
    saved = await uploads.save(
        hedgehog, _text("Латаття цвіте."), "1" * 64, "латаття", "латаття.txt",
        "Під вербою", DocumentKind.GLOVE, "latattia", None,
    )  # fmt: skip
    assert "латаття.txt" in _indexed(uploads)

    deleted = await uploads.delete(hedgehog, saved.id)

    assert (deleted.restored_id, deleted.restored_title) == (None, None)
    assert (await _doc(sessionmaker, id=saved.id)).status == DocumentStatus.DELETED
    assert "латаття.txt" not in _indexed(uploads)


async def test_deleting_a_replacement_brings_back_the_archive_document(uploads, sessionmaker):
    hedgehog = await uploads.register(7, "Їжак")
    old = await _doc(sessionmaker, filename=GUIDE)
    saved = await _replace_guide(uploads, hedgehog, "їжак.txt", old.id)
    assert GUIDE not in _indexed(uploads)

    deleted = await uploads.delete(hedgehog, saved.id)

    assert (deleted.restored_id, deleted.restored_title) == (
        old.id,
        "Грибний путівник хвойного лісу",
    )
    assert (await _doc(sessionmaker, id=old.id)).status == DocumentStatus.ACTIVE
    assert GUIDE in _indexed(uploads)
    pack = uploads.search.search("Які гриби біля сосон?")
    assert GUIDE in {d.filename for d in pack.docs}


async def test_chain_restores_only_the_immediate_previous_version(uploads, sessionmaker):
    squirrel = await uploads.register(7, "Білочка")
    hedgehog = await uploads.register(8, "Їжак")
    archive = await _doc(sessionmaker, filename=GUIDE)
    first = await _replace_guide(uploads, squirrel, "білочка.txt", archive.id)
    second = await _replace_guide(uploads, hedgehog, "їжак.txt", first.id)

    deleted = await uploads.delete(hedgehog, second.id)

    # Повертається порада Білочки (чужа для Їжака), а не архівна.
    assert deleted.restored_id == first.id
    assert (await _doc(sessionmaker, id=first.id)).status == DocumentStatus.ACTIVE
    assert (await _doc(sessionmaker, id=archive.id)).status == DocumentStatus.REPLACED
    assert "білочка.txt" in _indexed(uploads) and GUIDE not in _indexed(uploads)


async def test_deleted_previous_version_never_comes_back(uploads, sessionmaker):
    squirrel = await uploads.register(7, "Білочка")
    hedgehog = await uploads.register(8, "Їжак")
    archive = await _doc(sessionmaker, filename=GUIDE)
    first = await _replace_guide(uploads, squirrel, "білочка.txt", archive.id)
    second = await _replace_guide(uploads, hedgehog, "їжак.txt", first.id)
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Document).where(Document.id == first.id).values(status=DocumentStatus.DELETED)
        )

    deleted = await uploads.delete(hedgehog, second.id)

    assert deleted.restored_id is None
    assert (await _doc(sessionmaker, id=first.id)).status == DocumentStatus.DELETED
    assert (await _doc(sessionmaker, id=archive.id)).status == DocumentStatus.REPLACED


@pytest.mark.parametrize("target", ["archive", "foreign", "already_deleted", "missing"])
async def test_only_own_active_documents_can_be_deleted(target, uploads, sessionmaker):
    hedgehog = await uploads.register(7, "Їжак")
    owl = await uploads.register(8, "Сова")
    archive = await _doc(sessionmaker, filename=GUIDE)
    owls = await _replace_guide(uploads, owl, "сова.txt", archive.id)
    mine = await uploads.save(
        hedgehog, _text("Моя."), "2" * 64, "моя", "моя.txt", "Під дубом", DocumentKind.GLOVE,
        "moia", None,
    )  # fmt: skip
    await uploads.delete(hedgehog, mine.id)
    document_id, error = (
        {
            "archive": (await _doc(sessionmaker, filename="Статут ОСМД Під дубом.txt")).id,
            "foreign": owls.id,
            "already_deleted": mine.id,
            "missing": 10**6,
        }[target],
        (PermissionError if target in ("archive", "foreign") else NotFound),
    )
    before = await _doc(sessionmaker, id=document_id)

    with pytest.raises(error):
        await uploads.delete(hedgehog, document_id)

    after = await _doc(sessionmaker, id=document_id)
    assert (before and before.status) == (after and after.status)
    assert (await _doc(sessionmaker, id=archive.id)).status == DocumentStatus.REPLACED


async def test_deleting_a_beast_map_restores_the_previous_map(
    uploads, sessionmaker, archive_map_text
):
    hedgehog = await uploads.register(7, "Їжак")
    text = archive_map_text.replace(ON_EDGE, "На узліссі — хвойний ліс.")
    await uploads.replace_map(
        hedgehog, _text(text), "3" * 64, "Карта лісу.txt", parse_forest_map(text)
    )
    assert uploads.search.state.resolver.parts_of("На узліссі") == (ForestPart.KHVOINYI,)
    beast_map = await _doc(sessionmaker, filename="Карта лісу.txt")
    archive_map = await _doc(sessionmaker, filename="Карта лісу.pdf")

    deleted = await uploads.delete(hedgehog, beast_map.id)

    assert deleted.restored_id == archive_map.id
    assert (await _doc(sessionmaker, id=archive_map.id)).status == DocumentStatus.ACTIVE
    assert uploads.search.state.resolver.parts_of("На узліссі") == ()
    async with sessionmaker() as session:
        ids = set(await session.scalars(select(ForestMapEntry.map_document_id)))
    assert ids == {archive_map.id}


async def test_map_without_a_previous_map_cannot_be_deleted(
    uploads, sessionmaker, archive_map_text
):
    hedgehog = await uploads.register(7, "Їжак")
    await uploads.replace_map(
        hedgehog, _text(archive_map_text), "4" * 64, "Карта лісу.txt",
        parse_forest_map(archive_map_text),
    )  # fmt: skip
    archive_map = await _doc(sessionmaker, filename="Карта лісу.pdf")
    beast_map = await _doc(sessionmaker, filename="Карта лісу.txt")
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Document)
            .where(Document.id == archive_map.id)
            .values(status=DocumentStatus.DELETED)
        )

    with pytest.raises(MapRequired):
        await uploads.delete(hedgehog, beast_map.id)
    assert (await _doc(sessionmaker, id=beast_map.id)).status == DocumentStatus.ACTIVE
