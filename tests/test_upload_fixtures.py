"""Закомічені тестові поради з tests/fixtures/uploads: що з кожною робить бот."""

import pytest

from lisovyi.bot.uploads import map_preview
from lisovyi.config import PROJECT_ROOT
from lisovyi.domain import DocumentKind, ForestPart, TextSource
from lisovyi.ingest.classify import classify_upload, upload_topic
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.uploads.reading import EMPTY, TOO_LONG, UploadRejected, read_upload
from lisovyi.uploads.versions import find_version_candidate

UPLOADS = PROJECT_ROOT / "tests" / "fixtures" / "uploads"


async def _read(name: str):
    return await read_upload((UPLOADS / name).read_bytes(), name)


async def _place_and_candidate(name: str, archive_search):
    index, resolver = archive_search
    extracted = await _read(name)
    title = name.rsplit(".", 1)[0]
    place = classify_upload(title, extracted.text, resolver.gloves)
    if place is None:
        return None, None
    candidate = find_version_candidate(
        extracted.text,
        place.scope,
        place.topic,
        index.documents,
        index,
        topic_from_rules=place.topic_from_rules,
    )
    return place, candidate


async def test_advice_for_a_glove(archive_search):
    place, candidate = await _place_and_candidate("порада.txt", archive_search)
    assert (place.kind, place.scope) == (DocumentKind.GLOVE, "Під вербою")
    assert candidate is None


async def test_new_version_of_the_mushroom_guide(archive_search):
    place, candidate = await _place_and_candidate("гриби_нова_версія.docx", archive_search)
    assert (place.kind, place.scope, place.topic) == (DocumentKind.PART, "khvoinyi", "hryby")
    assert candidate.document.title == "Грибний путівник хвойного лісу"


async def test_advice_without_a_place(archive_search):
    place, _ = await _place_and_candidate("без_місця.txt", archive_search)
    assert place is None
    assert upload_topic("без_місця") == ("bez_mistsia", False)


@pytest.mark.ocr
async def test_scan_goes_through_ocr_with_numbers_to_check():
    extracted = await _read("скан_з_числами.pdf")
    assert extracted.source == TextSource.OCR
    assert extracted.review_items
    by_line = {item.line.split()[0]: item for item in extracted.review_items}
    assert by_line["До"].token == "300"
    # «3 шишки» tesseract читає то як цифру, то як кириличну «З» — саме для цього звіра
    # й перепитують.
    assert by_line["Внесок"].token in {"3", "З"}


async def test_map_with_the_edge_in_the_conifers(archive_search):
    _, resolver = archive_search
    extracted = await _read("Карта лісу.txt")
    assert classify_upload("Карта лісу", extracted.text, resolver.gloves).kind == DocumentKind.MAP
    forest_map = parse_forest_map(extracted.text)
    assert forest_map["На узліссі"] == (ForestPart.KHVOINYI,)
    current = {glove: resolver.parts_of(glove) for glove in resolver.gloves}
    assert map_preview(forest_map, current).split("\n")[-1] == "На узліссі: жодна → хвойний ліс"


async def test_too_long_advice():
    text = (UPLOADS / "задовга.txt").read_text(encoding="utf-8")
    assert len(text) == len(text.strip()) == 4001
    with pytest.raises(UploadRejected) as caught:
        await _read("задовга.txt")
    assert caught.value.reason == TOO_LONG


@pytest.mark.ocr
async def test_pdf_without_text_or_images():
    with pytest.raises(UploadRejected) as caught:
        await _read("порожня.pdf")
    assert caught.value.reason == EMPTY
