"""Детектор конфлікту частин лісу для вимоги 8 (бриф 05, §2.6) на корпусі архіву без БД."""

import pytest

from lisovyi.domain import ForestPart
from lisovyi.search import synonyms
from lisovyi.search.conflicts import detect_part_conflict
from lisovyi.search.retrieve import retrieve

CASES = [
    ("Чим утеплити рукавичку під вербою?", "uteplennia"),
    ("Коли утеплювати рукавичку під вербою?", "uteplennia"),
    ("Коли йти по гриби під вербою?", "hryby"),
    ("Які гриби шукати під вербою?", "hryby"),
    ("Де біля верби ростуть опеньки?", None),
    ("Які небезпеки під вербою?", None),
    ("Що їсти під вербою?", None),
    ("Коли збори під вербою?", None),
    ("Хто скарбник у рукавичці під вербою?", None),
    ("Як перейти річку від верби?", None),
    ("Як дістатися від верби до узлісся?", None),
    ("Коли утеплювати рукавичку під ялиною?", None),
    ("Чим утеплити рукавичку біля бобрової греблі?", None),
    # Бриф 06: частини відповідають на різні теми — це не конфлікт (len(topics) != 1).
    ("Які звірі небезпечні під вербою?", None),
    ("Де брати воду під вербою?", None),
]


def _detect(question, archive_search):
    index, resolver = archive_search
    return detect_part_conflict(question, retrieve(question, index, resolver), index, resolver)


@pytest.mark.parametrize(("question", "topic"), CASES, ids=[q for q, _ in CASES])
def test_brief_cases(question, topic, archive_search):
    conflict = _detect(question, archive_search)
    if topic is None:
        assert conflict is None
        return
    assert (conflict.glove, conflict.topic) == ("Під вербою", topic)
    assert list(conflict.docs) == [ForestPart.LYSTIANYI, ForestPart.RICHKA]
    assert [doc.scope for doc in conflict.docs.values()] == ["lystianyi", "richka"]


def test_named_part_means_the_beast_already_chose(archive_search):
    assert _detect("Чим утеплити рукавичку під вербою біля річки?", archive_search) is None


def test_eat_needs_its_synonym(archive_search, monkeypatch):
    # Без «їсти» → «поживитися» документ рукавички «Чим поживитись…» програє частинам,
    # і детектор хибно бачить конфлікт.
    table = {k: v for k, v in synonyms.SYNONYMS.items() if k != "їсти"}
    monkeypatch.setattr(synonyms, "SYNONYMS", table)
    synonyms.lemmatized_synonyms.cache_clear()
    try:
        assert _detect("Що їсти під вербою?", archive_search) is not None
    finally:
        monkeypatch.undo()
        synonyms.lemmatized_synonyms.cache_clear()
    assert _detect("Що їсти під вербою?", archive_search) is None
