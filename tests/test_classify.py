import csv
from pathlib import Path

import pytest

from lisovyi.config import PROJECT_ROOT
from lisovyi.domain import DocumentKind
from lisovyi.ingest.classify import Classification, classify, slugify, topic_for
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.parsers import TranscriptStore, extract_text

ARCHIVE = PROJECT_ROOT / "data" / "archive"
REFERENCE = Path(__file__).parent / "fixtures" / "classification.csv"

with REFERENCE.open(encoding="utf-8", newline="") as f:
    REFERENCE_ROWS = list(csv.DictReader(f))

GLOVES = ["Під ялиною", "У соснах", "Під вербою", "На узліссі"]


@pytest.fixture(scope="module")
def transcripts():
    return TranscriptStore.load(PROJECT_ROOT / "data" / "transcripts")


@pytest.fixture(scope="module")
def archive_gloves():
    name = "Карта лісу.pdf"
    text = extract_text((ARCHIVE / name).read_bytes(), name, None).text
    return list(parse_forest_map(text))


def test_reference_covers_the_whole_archive():
    assert sorted(row["filename"] for row in REFERENCE_ROWS) == sorted(
        p.name for p in ARCHIVE.iterdir()
    )


@pytest.mark.parametrize("row", REFERENCE_ROWS, ids=[row["filename"] for row in REFERENCE_ROWS])
def test_archive_document_matches_reference(row, transcripts, archive_gloves):
    filename = row["filename"]
    extracted = extract_text((ARCHIVE / filename).read_bytes(), filename, transcripts)
    classification = classify(Path(filename).stem, extracted.text, archive_gloves)
    assert classification is not None
    assert (
        classification.kind.value,
        classification.scope,
        classification.topic,
        extracted.source.value,
    ) == (row["kind"], row["scope"], row["topic"], row["text_source"])


def _text(*sentences: str) -> str:
    return "Заголовок\n" + " ".join(sentences)


def test_glove_document_needs_a_glove_from_the_map():
    assert classify("Статут ОСМД Під ялиною", "...", GLOVES) == Classification(
        DocumentKind.GLOVE, "Під ялиною", "statut"
    )
    # Рукавички немає на карті: далі працюють правила для частин лісу.
    assert classify("Статут ОСМД Під кленом", "...", GLOVES) is None


def test_scope_comes_from_text_before_title():
    text = _text("Ця порада для мешканців листяного лісу.")
    assert classify("Як не заблукати серед сосен хвойного лісу", text, GLOVES) == Classification(
        DocumentKind.PART, "lystianyi", "oriientuvannia"
    )


def test_sentence_with_for_but_without_scope_is_skipped():
    text = _text("Осінь — гарна пора для прогулянок.", "Цей путівник для всіх мешканців лісу.")
    assert classify("Осінній путівник лісу", text, GLOVES).kind == DocumentKind.GENERAL


def test_scope_marker_without_addressee_is_skipped():
    text = _text("Біля річки утеплення має свої особливості.", "Ця порада для хвойного лісу.")
    assert classify("Як утеплити рукавичку", text, GLOVES).scope == "khvoinyi"


def test_only_first_three_sentences_count():
    text = _text("Раз.", "Два.", "Три.", "Ця порада для хвойного лісу.")
    assert classify("Порада", text, GLOVES) is None


@pytest.mark.parametrize(
    ("sentence", "scope"),
    [
        ("Ці правила стосуються мешканців хвойного лісу.", "khvoinyi"),
        ("Це звернення до всіх мешканців лісу.", "general"),
        ("Для тих, хто живе при річці.", "richka"),
        ("Для мешканців прибережжя.", "richka"),
        ("Ця пам'ятка для тих,\nхто біля\nрічки.", "richka"),
    ],
)
def test_addressee_and_scope_markers(sentence, scope):
    assert classify("Порада", _text(sentence), GLOVES).scope == scope


def test_title_is_the_fallback():
    text = _text("Любі сусіди!", "Цей путівник для вас.")
    assert classify("Грибний путівник листяного лісу", text, GLOVES).scope == "lystianyi"


def test_sentence_naming_two_scopes_is_not_guessed():
    text = _text("Ця порада для хвойного й листяного лісу.")
    assert classify("Порада", text, GLOVES) is None


def test_nothing_found_returns_none():
    assert classify("Порада", _text("Цей путівник для вас."), GLOVES) is None


def test_map_is_recognised_by_title():
    assert classify("Карта лісу", "", []) == Classification(DocumentKind.MAP, "general", "karta")


@pytest.mark.parametrize(
    ("title", "topic"),
    [
        ("Їстівне й отруйне", "yistivne_otruine"),
        ("Що їсти біля річки", "yizha"),
        ("Грибний путівник біля річки", "hryby"),
        ("Осінній путівник лісу", "osinnii_putivnyk"),
        ("Як кричати, коли бачиш мисливців", "myslyvtsi"),
        ("Як рахувати зірки", "yak_rakhuvaty_zirky"),
    ],
)
def test_topic_for(title, topic):
    assert topic_for(title) == topic


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Ялинкова пам'ятка", "yalynkova_pamiatka"),
        ("Їжак і Юля", "yizhak_i_yulia"),
        ("Щедрий  ґанок!", "shchedryi_ganok"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug
