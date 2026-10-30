import asyncio

import pytest
from sqlalchemy import select

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.models import Document, ForestMapEntry
from lisovyi.domain import DocumentKind, DocumentStatus, ForestPart, TextSource
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.importer import import_archive
from lisovyi.ingest.parsers import Extracted
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import ConflictError, UploadService, valid_name

GUIDE = "Грибний путівник хвойного лісу.pdf"


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("  Їжак  ", "Їжак"),
        ("Пані Сова-Сипуха", "Пані Сова-Сипуха"),
        ("Вовк д'Артаньян", "Вовк д'Артаньян"),
        ("Ведмідь Михайло", "Ведмідь Михайло"),
        ("Я", None),
        ("а" * 41, None),
        ("Їжак 2", None),
        ("Їжак!", None),
        ("--", None),
        ("", None),
    ],
)
def test_valid_name(raw, name):
    assert valid_name(raw) == name


@pytest.fixture
async def uploads(sessionmaker):
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    search = SearchService(sessionmaker)
    await search.reload()
    return UploadService(sessionmaker, search)


def _text(text: str, source: TextSource = TextSource.TEXT) -> Extracted:
    return Extracted(text, source)


async def _by_filename(sessionmaker, filename: str) -> list[Document]:
    async with sessionmaker() as session:
        return list(
            await session.scalars(
                select(Document).where(Document.filename == filename).order_by(Document.id)
            )
        )


@pytest.mark.db
async def test_register_and_rename(uploads):
    assert await uploads.beast(7) is None
    assert await uploads.rename(7, "Їжак") is None
    beast = await uploads.register(7, "Їжак")
    assert (await uploads.beast(7)) == beast
    renamed = await uploads.rename(7, "Їжачок")
    assert (renamed.id, renamed.name) == (beast.id, "Їжачок")


@pytest.mark.db
async def test_beast_replaces_an_archive_document(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    [old] = await _by_filename(sessionmaker, GUIDE)

    saved = await uploads.save(
        beast,
        _text("Гриби\nМаслюки ростуть під молодими соснами після дощу."),
        b"new version",
        "гриби_нова_версія",
        "гриби_нова_версія.docx",
        "khvoinyi",
        DocumentKind.PART,
        "hryby",
        old.id,
    )

    [old_now] = await _by_filename(sessionmaker, GUIDE)
    [new] = await _by_filename(sessionmaker, "гриби_нова_версія.docx")
    assert old_now.status == DocumentStatus.REPLACED
    assert (new.status, new.topic, new.replaces_id, new.author_id) == (
        DocumentStatus.ACTIVE,
        old.topic,
        old.id,
        beast.id,
    )
    assert (saved.replaced_title, saved.topic) == ("Грибний путівник хвойного лісу", "hryby")
    assert new.ocr_confirmed is None
    pack = uploads.search.search("Які гриби біля сосон?")
    filenames = [doc.filename for doc in pack.docs]
    assert "гриби_нова_версія.docx" in filenames
    assert GUIDE not in filenames


@pytest.mark.db
async def test_separate_advice_on_a_taken_topic_gets_a_suffix(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    kwargs = {"scope": "khvoinyi", "kind": DocumentKind.PART, "topic": "hryby", "replaces": None}
    first = await uploads.save(beast, _text("Опеньки."), b"1", "a", "a.txt", **kwargs)
    second = await uploads.save(beast, _text("Рижики."), b"2", "b", "b.txt", **kwargs)

    assert (first.topic, second.topic) == ("hryby_2", "hryby_3")
    async with sessionmaker() as session:
        active = set(
            await session.scalars(
                select(Document.topic).where(
                    Document.scope == "khvoinyi", Document.status == DocumentStatus.ACTIVE
                )
            )
        )
    assert {"hryby", "hryby_2", "hryby_3"} <= active


@pytest.mark.db
async def test_parallel_replacement_of_the_same_document(uploads, sessionmaker):
    hedgehog = await uploads.register(7, "Їжак")
    owl = await uploads.register(8, "Сова")
    [old] = await _by_filename(sessionmaker, GUIDE)

    def replace(beast, name):
        return uploads.save(
            beast, _text("Гриби."), name.encode(), name, f"{name}.txt", "khvoinyi",
            DocumentKind.PART, "hryby", old.id,
        )  # fmt: skip

    results = await asyncio.gather(
        replace(hedgehog, "їжак"), replace(owl, "сова"), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["ConflictError", "SavedDocument"]


@pytest.mark.db
async def test_replacing_an_already_replaced_document_is_a_conflict(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    [old] = await _by_filename(sessionmaker, GUIDE)
    args = ("khvoinyi", DocumentKind.PART, "hryby", old.id)
    await uploads.save(beast, _text("Гриби."), b"1", "a", "a.txt", *args)
    with pytest.raises(ConflictError):
        await uploads.save(beast, _text("Гриби."), b"2", "b", "b.txt", *args)


@pytest.mark.db
async def test_rebind_makes_the_new_advice_a_version_of_another(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    saved = await uploads.save(
        beast, _text("Опеньки ростуть на пнях."), b"1", "опеньки", "опеньки.txt",
        "khvoinyi", DocumentKind.PART, "openky", None,
    )  # fmt: skip
    [old] = await _by_filename(sessionmaker, GUIDE)

    rebound = await uploads.rebind(saved.id, old.id)

    [old_now] = await _by_filename(sessionmaker, GUIDE)
    [new] = await _by_filename(sessionmaker, "опеньки.txt")
    assert old_now.status == DocumentStatus.REPLACED
    assert (new.topic, new.replaces_id, new.status) == ("hryby", old.id, DocumentStatus.ACTIVE)
    assert rebound.replaced_title == "Грибний путівник хвойного лісу"
    assert GUIDE not in {d.filename for d in uploads.search.state.index.documents}


@pytest.mark.db
async def test_rebind_across_places_is_refused(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    saved = await uploads.save(
        beast, _text("Порада."), b"1", "п", "п.txt", "Під дубом", DocumentKind.GLOVE, "p", None
    )
    [old] = await _by_filename(sessionmaker, GUIDE)
    with pytest.raises(ValueError, match="different places"):
        await uploads.rebind(saved.id, old.id)
    [old_now] = await _by_filename(sessionmaker, GUIDE)
    assert old_now.status == DocumentStatus.ACTIVE


@pytest.mark.db
async def test_ocr_confirmation_is_stored(uploads, sessionmaker):
    beast = await uploads.register(7, "Їжак")
    common = ("Під дубом", DocumentKind.GLOVE, "p", None)
    await uploads.save(
        beast, _text("300 кроків.", TextSource.OCR), b"1", "a", "a.pdf", *common,
        ocr_confirmed=True,
    )  # fmt: skip
    await uploads.save(beast, _text("3 шишки.", TextSource.OCR), b"2", "b", "b.pdf", *common)
    [a] = await _by_filename(sessionmaker, "a.pdf")
    [b] = await _by_filename(sessionmaker, "b.pdf")
    assert (a.ocr_confirmed, b.ocr_confirmed) == (True, False)


ON_EDGE = "На узліссі — ні хвойний, ні листяний ліс, і до річки далеко."


@pytest.mark.db
async def test_map_replacement(uploads, sessionmaker, archive_map_text):
    beast = await uploads.register(7, "Їжак")
    text = archive_map_text.replace(ON_EDGE, "На узліссі — хвойний ліс.")
    forest_map = parse_forest_map(text)
    [old] = await _by_filename(sessionmaker, "Карта лісу.pdf")

    orphans = await uploads.replace_map(
        beast, _text(text), text.encode(), "Карта лісу.txt", forest_map
    )

    assert orphans == []
    [old_now] = await _by_filename(sessionmaker, "Карта лісу.pdf")
    [new] = await _by_filename(sessionmaker, "Карта лісу.txt")
    assert old_now.status == DocumentStatus.REPLACED
    assert (new.kind, new.status, new.replaces_id, new.author_id) == (
        DocumentKind.MAP, DocumentStatus.ACTIVE, old.id, beast.id,
    )  # fmt: skip
    async with sessionmaker() as session:
        entries = list(
            await session.scalars(select(ForestMapEntry).order_by(ForestMapEntry.position))
        )
    assert [e.glove for e in entries] == list(forest_map)
    assert {e.map_document_id for e in entries} == {new.id}
    # Пошук одразу відповідає за новою картою.
    resolver = uploads.search.state.resolver
    assert resolver.parts_of("На узліссі") == (ForestPart.KHVOINYI,)
    assert uploads.search.search("Які гриби на узліссі?").allowed_parts == [ForestPart.KHVOINYI]


@pytest.mark.db
async def test_gloves_missing_from_the_new_map_keep_their_documents(
    uploads, sessionmaker, archive_map_text, caplog
):
    beast = await uploads.register(7, "Їжак")
    text = "\n".join(
        line for line in archive_map_text.splitlines() if not line.startswith("У соснах —")
    )
    forest_map = parse_forest_map(text)
    assert "У соснах" not in forest_map

    with caplog.at_level("WARNING"):
        orphans = await uploads.replace_map(
            beast, _text(text), text.encode(), "Карта лісу.txt", forest_map
        )

    assert orphans == ["У соснах"]
    assert "not on the new map: У соснах" in caplog.text
    async with sessionmaker() as session:
        active = await session.scalar(
            select(Document.id)
            .where(Document.scope == "У соснах", Document.status == DocumentStatus.ACTIVE)
            .limit(1)
        )
    assert active is not None
    assert "У соснах" not in uploads.search.state.resolver.gloves
