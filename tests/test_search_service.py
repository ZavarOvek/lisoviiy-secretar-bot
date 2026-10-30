from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Beast, Document, ForestMapEntry
from lisovyi.domain import DocumentKind, DocumentStatus, TextSource
from lisovyi.ingest.importer import import_archive
from lisovyi.search.service import SearchService

pytestmark = pytest.mark.db


async def _imported(sessionmaker) -> None:
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )


async def _reloaded(sessionmaker) -> SearchService:
    service = SearchService()
    async with sessionmaker() as session:
        await service.reload(session)
    return service


def _filenames(service: SearchService) -> set[str]:
    return {document.filename for document in service.state.index.documents}


async def test_index_holds_active_documents_without_the_map(sessionmaker):
    await _imported(sessionmaker)
    service = await _reloaded(sessionmaker)
    assert len(service.state.index) == 77
    assert "Карта лісу.pdf" not in _filenames(service)
    assert len(service.state.resolver.gloves) == 8


async def test_replaced_and_deleted_documents_are_not_searched(sessionmaker):
    await _imported(sessionmaker)
    async with sessionmaker() as session, session.begin():
        for filename, status in [
            ("Правила толоки.txt", DocumentStatus.DELETED),
            ("Календар сезонів.txt", DocumentStatus.REPLACED),
        ]:
            await session.execute(
                update(Document).where(Document.filename == filename).values(status=status)
            )

    service = await _reloaded(sessionmaker)

    assert {"Правила толоки.txt", "Календар сезонів.txt"}.isdisjoint(_filenames(service))
    pack = service.search("Коли буде осіння толока?")
    assert "Правила толоки.txt" not in {doc.filename for doc in pack.docs}


async def test_active_beast_document_is_searched(sessionmaker):
    await _imported(sessionmaker)
    async with sessionmaker() as session, session.begin():
        beast = Beast(telegram_id=42, name="Їжак")
        session.add(beast)
        await session.flush()
        session.add(
            Document(
                filename="мед.txt",
                title="Де брати мед",
                kind=DocumentKind.GLOVE,
                scope="Під дубом",
                topic="med",
                text="Мед диких бджіл шукайте в дуплі старого дуба.",
                text_source=TextSource.TEXT,
                sha256="2" * 64,
                author_id=beast.id,
                created_at=datetime(2026, 10, 29, 12, 0, tzinfo=UTC),
            )
        )

    pack = (await _reloaded(sessionmaker)).search("Де брати мед під дубом?")

    [beast_doc] = [doc for doc in pack.docs if doc.filename == "мед.txt"]
    assert beast_doc.author_name == "Їжак"
    assert "[Джерело: мед.txt | рукавичка «Під дубом» | надіслав: Їжак, 29.10.2026]" in (
        pack.render()
    )


async def test_reload_picks_up_a_new_glove(sessionmaker):
    await _imported(sessionmaker)
    service = await _reloaded(sessionmaker)
    assert service.search("Що робити біля клена?").gloves == []

    async with sessionmaker() as session, session.begin():
        map_id = await session.scalar(select(ForestMapEntry.map_document_id).limit(1))
        session.add(
            ForestMapEntry(glove="Біля старого клена", parts=["lystianyi"], map_document_id=map_id)
        )
    async with sessionmaker() as session:
        await service.reload(session)

    pack = service.search("Що робити біля клена?")
    assert pack.gloves == ["Біля старого клена"]
    assert [part.value for part in pack.allowed_parts] == ["lystianyi"]


async def test_followup_is_merged_with_the_previous_question(sessionmaker):
    await _imported(sessionmaker)
    service = await _reloaded(sessionmaker)
    pack = service.search("У соснах", previous="Коли збори мешканців?")
    assert (pack.mode, pack.gloves) == ("placed", ["У соснах"])


def test_search_before_reload_is_an_error():
    with pytest.raises(RuntimeError, match="reload"):
        SearchService().search("Які гриби біля сосон?")
