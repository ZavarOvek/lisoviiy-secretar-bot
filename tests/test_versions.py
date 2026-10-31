import random
import re

import pytest

from lisovyi.domain import DocumentKind
from lisovyi.search.index import Bm25Index, SearchDocument
from lisovyi.uploads.versions import VERSION_RATIO, find_version_candidate, rank_in_scope

NEW_SENTENCE = "Цю пораду дописав звір із сусідньої рукавички."
# Незв'язані нові поради в місцях, де вже є документи.
UNRELATED = [
    (
        "Під вербою",
        "Мешканцям рукавички «Під вербою». Навесні латаття біля берега цвіте, і туди "
        "прилітають бабки. Малим їх не ловити.",
    ),
    (
        "Під дубом",
        "Для мешканців рукавички «Під дубом». Взимку не забувайте підгодовувати синиць "
        "насінням соняшника.",
    ),
    (
        "lystianyi",
        "Для мешканців листяного лісу. Восени біля струмка світяться світлячки, малим їх "
        "не ловити.",
    ),
]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s]


def _new_version(text: str, rng: random.Random, share: float = 0.4) -> str:
    """Частина речень оригіналу в їхньому порядку плюс одне нове речення."""
    sentences = _sentences(text)
    keep = max(1, round(share * len(sentences)))
    picked = sorted(rng.sample(range(len(sentences)), keep))
    return " ".join(sentences[i] for i in picked) + " " + NEW_SENTENCE


def _candidate(text, scope, index, topic="nova_porada", topic_from_rules=False):
    return find_version_candidate(
        text, scope, topic, index.documents, index, topic_from_rules=topic_from_rules
    )


def test_every_archive_document_is_found_from_40_percent_of_it(archive_search):
    index, _ = archive_search
    rng = random.Random(2)
    missed = []
    for document in index.documents:
        found = _candidate(_new_version(document.text, rng), document.scope, index)
        if found is None or found.document.document_id != document.document_id:
            missed.append(document.filename)
    assert (len(index.documents), missed) == (77, [])


def test_version_detection_over_ten_seeds(archive_search):
    # Зерно 2 вище дає 77/77, але поріг 1,8 стоїть близько: на інших зернах буває 76/77.
    # Тут — що промахи поодинокі, а не систематичні.
    index, _ = archive_search
    hits = total = 0
    for seed in range(10):
        rng = random.Random(seed)
        for document in index.documents:
            found = _candidate(_new_version(document.text, rng), document.scope, index)
            hits += found is not None and found.document.document_id == document.document_id
            total += 1
    assert hits / total >= 0.99


@pytest.mark.parametrize(("scope", "text"), UNRELATED, ids=[s for s, _ in UNRELATED])
def test_unrelated_advice_has_no_candidate(scope, text, archive_search):
    index, _ = archive_search
    ranked = rank_in_scope(text, scope, index)
    assert len(ranked) > 1
    assert ranked[0].score / ranked[1].score < VERSION_RATIO
    assert _candidate(text, scope, index) is None


def test_same_topic_from_the_rules_is_an_exact_candidate(archive_search):
    index, _ = archive_search
    found = _candidate("Будь-що про гриби.", "khvoinyi", index, "hryby", topic_from_rules=True)
    assert (found.reason, found.document.title) == ("topic", "Грибний путівник хвойного лісу")
    # Та сама тема з транслітерації назви файлу точним збігом не вважається.
    assert _candidate("Будь-що.", "khvoinyi", index, "hryby", topic_from_rules=False) is None


def _doc(document_id, scope, text) -> SearchDocument:
    return SearchDocument(
        document_id=document_id,
        filename=f"{document_id}.txt",
        title=f"Документ {document_id}",
        kind=DocumentKind.GLOVE,
        scope=scope,
        topic=f"t{document_id}",
        text=text,
    )


def test_place_with_a_single_document():
    # Решта корпусу дає рідкісним лемам df ≤ 30, як в архіві.
    filler = [_doc(i, "Під ялиною", f"шишки хвоя мох {i}") for i in range(2, 40)]
    only = _doc(1, "Під дубом", "Жолуді під старим дубом збирають після першого морозу в жовтні.")
    index = Bm25Index.build([only, *filler])

    similar = "Жолуді під дубом збирай після першого морозу."
    assert _candidate(similar, "Під дубом", index).document.document_id == 1
    # Одна спільна рідкісна лема — замало.
    assert _candidate("Морозу не боїмося, співаємо пісні.", "Під дубом", index) is None
    # Нічого спільного — рахунок 0.
    assert _candidate("Латаття цвіте біля берега.", "Під дубом", index) is None


def test_empty_place_has_no_candidate():
    index = Bm25Index.build([_doc(1, "Під ялиною", "шишки хвоя")])
    assert _candidate("шишки хвоя", "Під дубом", index) is None


def test_rank_in_scope_excludes_and_orders(archive_search):
    index, _ = archive_search
    ranked = rank_in_scope("гриби після дощу", "khvoinyi", index)
    assert {s.document.scope for s in ranked} == {"khvoinyi"}
    assert ranked == sorted(ranked, key=lambda s: -s.score)
    first = ranked[0].document.document_id
    assert first not in {
        s.document.document_id for s in rank_in_scope("гриби", "khvoinyi", index, [first])
    }


def test_runner_up_with_zero_score_needs_shared_rare_lemmas():
    filler = [_doc(i, "Під ялиною", f"шишки хвоя мох {i}") for i in range(3, 40)]
    first = _doc(1, "Під дубом", "Жолуді під старим дубом збирають після першого морозу.")
    second = _doc(2, "Під дубом", "Синиці прилітають до годівниці взимку.")
    index = Bm25Index.build([first, second, *filler])
    # Одне спільне слово з першим документом і жодного з другим — це не нова версія.
    assert _candidate("Морозу не боїмося, співаємо пісні.", "Під дубом", index) is None
    assert (
        _candidate(
            "Жолуді під дубом збирай після першого морозу.", "Під дубом", index
        ).document.document_id
        == 1
    )
