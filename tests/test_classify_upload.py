import pytest

from lisovyi.config import PROJECT_ROOT
from lisovyi.domain import GENERAL_SCOPE, DocumentKind
from lisovyi.ingest.classify import (
    FALLBACK_TOPIC,
    MAP_TITLE,
    classify,
    classify_upload,
    glove_from_intro,
    upload_topic,
)
from lisovyi.ingest.parsers import TranscriptStore, extract_text

ARCHIVE = PROJECT_ROOT / "data" / "archive"


@pytest.fixture(scope="module")
def archive(archive_search):
    """(title, text, kind, scope) кожного архівного документа, разом із картою."""
    index, resolver = archive_search
    docs = [(d.title, d.text, d.kind, d.scope) for d in index.documents]
    [map_path] = [p for p in ARCHIVE.iterdir() if p.stem == MAP_TITLE]
    transcripts = TranscriptStore.load(PROJECT_ROOT / "data" / "transcripts")
    map_text = extract_text(map_path.read_bytes(), map_path.name, transcripts).text
    docs.append((MAP_TITLE, map_text, DocumentKind.MAP, GENERAL_SCOPE))
    return docs, resolver.gloves


def test_archive_has_56_glove_documents_and_22_others(archive):
    docs, _ = archive
    gloves = [d for d in docs if d[2] == DocumentKind.GLOVE]
    assert (len(gloves), len(docs) - len(gloves)) == (56, 22)


def test_glove_documents_titled_porada_find_their_glove(archive):
    docs, gloves = archive
    wrong = []
    for title, text, kind, scope in docs:
        if kind != DocumentKind.GLOVE:
            continue
        result = classify_upload("порада", text, gloves)
        if result is None or (result.kind, result.scope) != (DocumentKind.GLOVE, scope):
            wrong.append((title, result))
    assert wrong == []


def test_other_documents_never_trigger_the_quoted_glove_rule(archive):
    docs, gloves = archive
    others = [d for d in docs if d[2] != DocumentKind.GLOVE]
    assert [title for title, text, *_ in others if glove_from_intro(text, gloves)] == []
    for title, text, kind, _scope in others:
        upload = classify_upload("порада", text, gloves)
        if kind == DocumentKind.MAP:
            # Карту впізнає лише її назва (правило 1). Під назвою «порада» її вступ
            # («для всіх мешканців лісу») дає весь ліс, а не карту.
            assert (upload.kind, upload.scope) == (DocumentKind.GENERAL, GENERAL_SCOPE)
            continue
        archive_result = classify(title, text, gloves)
        assert upload is None or (upload.kind, upload.scope) == (
            archive_result.kind,
            archive_result.scope,
        ), title


GLOVES = ["Під ялиною", "Під вербою", "Під дубом", "Біля бобрової греблі"]


def test_quoted_glove_in_a_one_line_advice():
    text = "Мешканцям рукавички «Під вербою». Навесні латаття цвіте. Малим бабок не ловити."
    result = classify_upload("порада", text, GLOVES)
    assert (result.kind, result.scope) == (DocumentKind.GLOVE, "Під вербою")


@pytest.mark.parametrize(
    "intro",
    [
        "Увага, мешканці «Під ялиною»!",
        "Шановні сусіди з «Під ялиною», ось новина.",
        "Любі звірі «Під ялиною», слухайте.",
        "Дорогі друзі з «Під ялиною», привіт.",
        "До відома всіх у «Під ялиною».",
        "У рукавичці «Під ялиною» новий порядок.",
        "Це стосується рукавички «Під ялиною».",
        "Звернення до рукавички «Під ялиною».",
        "Порада для рукавички «Під\n ялиною».",
    ],
)
def test_every_addressee_marker(intro):
    text = f"Заголовок\n{intro} Решта поради."
    assert glove_from_intro(text, GLOVES) == "Під ялиною"


def test_quoted_glove_without_an_addressee_is_not_enough():
    assert glove_from_intro("Заголовок\nБіля «Під ялиною» росте мох.", GLOVES) is None


def test_two_quoted_gloves_in_one_sentence_skip_the_rule():
    text = "Заголовок\nДля мешканців «Під ялиною» і «Під дубом»: ходіть разом. Решта."
    assert glove_from_intro(text, GLOVES) is None
    # Далі вирішують інші правила: тут — жодне, тож питаємо звіра.
    assert classify_upload("порада", text, GLOVES) is None


def test_same_glove_twice_is_still_one_glove():
    text = "Заголовок\nДля мешканців «Під дубом»: «Під дубом» ростуть жолуді."
    assert glove_from_intro(text, GLOVES) == "Під дубом"


def test_only_the_first_three_sentences_count():
    text = "Заголовок\nРаз. Два. Три. Для мешканців «Під дубом» щось."
    assert glove_from_intro(text, GLOVES) is None


def test_map_title_and_archive_like_title_come_first():
    text = "Для мешканців «Під дубом» щось."
    assert classify_upload(MAP_TITLE, text, GLOVES).kind == DocumentKind.MAP
    result = classify_upload("Статут ОСМД Під вербою", text, GLOVES)
    assert (result.kind, result.scope, result.topic, result.topic_from_rules) == (
        DocumentKind.GLOVE,
        "Під вербою",
        "statut",
        True,
    )


def test_part_from_the_intro_and_from_the_title():
    text = "Гриби\nЦей путівник написано для звірів, що живуть у хвойному лісі."
    result = classify_upload("гриби_нова_версія", text, GLOVES)
    assert (result.kind, result.scope, result.topic) == (DocumentKind.PART, "khvoinyi", "hryby")
    result = classify_upload("Поради листяного лісу", "Без вступу.", GLOVES)
    assert (result.kind, result.scope) == (DocumentKind.PART, "lystianyi")


def test_nothing_found_asks_the_beast():
    assert classify_upload("без_місця", "Корисна порада. Ягоди збирайте зранку.", GLOVES) is None


@pytest.mark.parametrize(
    ("title", "topic", "from_rules"),
    [
        ("Грибна порада", "hryby", True),
        ("Моя пам'ятка", "moia_pamiatka", False),
        ("!!!", FALLBACK_TOPIC, False),
        ("—", FALLBACK_TOPIC, False),
    ],
)
def test_upload_topic(title, topic, from_rules):
    assert upload_topic(title) == (topic, from_rules)


@pytest.mark.parametrize(
    "text",
    [
        "Для звірів, що живуть у хвойному лісі. Гриби збирайте після дощу.",
        "Гриби\nДля звірів, що живуть у хвойному лісі. Гриби збирайте після дощу.",
    ],
    ids=["one-line", "with-heading"],
)
def test_rule_4_uses_the_same_intro_as_rule_3(text):
    result = classify_upload("порада", text, GLOVES)
    assert (result.kind, result.scope) == (DocumentKind.PART, "khvoinyi")


def test_first_line_that_is_a_sentence_is_not_a_heading():
    result = classify_upload(
        "порада", "Мешканцям рукавички «Під вербою».\nНавесні латаття цвіте.", GLOVES
    )
    assert (result.kind, result.scope) == (DocumentKind.GLOVE, "Під вербою")


def test_archive_classify_still_skips_the_first_line():
    # Архівний classify не змінювався: однорядковий вступ без заголовка він не бачить.
    one_line = "Для звірів, що живуть у хвойному лісі. Гриби збирайте після дощу."
    assert classify("порада", one_line, GLOVES) is None
