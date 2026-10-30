"""Нова версія наявного документа чи окрема порада: тема і схожість за змістом.

Остаточно вирішує звір кнопкою; тут лише кандидат, якого варто запропонувати.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from lisovyi.search.index import Bm25Index, Scored, SearchDocument
from lisovyi.search.morph import bag

# Перевірка на архіві (бриф 04, §2): нова версія з 40% речень дає відношення першого
# рахунку до другого щонайменше 2,15, а незв'язані поради — 1,02–1,52.
VERSION_RATIO = 1.8
# Для місця з одним документом відношення немає: тоді потрібні спільні рідкісні леми.
RARE_DF = 30
MIN_SHARED_RARE = 3


@dataclass(frozen=True)
class Candidate:
    document: SearchDocument
    # "topic" — та сама тема в тому самому місці; "similarity" — схожий зміст.
    reason: str


def find_version_candidate(
    new_text: str,
    scope: str,
    topic: str,
    active_docs: Iterable[SearchDocument],
    index: Bm25Index,
    *,
    topic_from_rules: bool,
) -> Candidate | None:
    """Активний документ того самого місця, новою версією якого схожа нова порада.

    Тема збігається точно лише тоді, коли її дали правила (тип документа рукавички чи
    ключове слово назви), а не транслітерація назви файлу.
    """
    if topic_from_rules:
        for document in active_docs:
            if document.scope == scope and document.topic == topic:
                return Candidate(document, "topic")

    ranked = rank_in_scope(new_text, scope, index)
    if not ranked:
        return None
    best = ranked[0]
    if len(ranked) == 1:
        shared = bag(new_text).keys() & bag(_indexed_text(best.document)).keys()
        rare = [lemma for lemma in shared if index.document_frequency(lemma) <= RARE_DF]
        if best.score > 0 and len(rare) >= MIN_SHARED_RARE:
            return Candidate(best.document, "similarity")
        return None
    second = ranked[1].score
    if best.score > 0 and (second == 0 or best.score / second >= VERSION_RATIO):
        return Candidate(best.document, "similarity")
    return None


def rank_in_scope(
    new_text: str, scope: str, index: Bm25Index, exclude: Iterable[int] = ()
) -> list[Scored]:
    """Документи місця `scope` за схожістю на `new_text`, найсхожіший перший.

    BM25 нового тексту з idf пошукового індексу, без синонімів: синоніми допомагають питанню
    знайти пораду, а тут порівнюються дві поради між собою.
    """
    excluded = set(exclude)
    scored = [
        s
        for s in index.score_all(bag(new_text))
        if s.document.scope == scope and s.document.document_id not in excluded
    ]
    return sorted(scored, key=lambda s: (-s.score, s.document.document_id))


def _indexed_text(document: SearchDocument) -> str:
    # Так документ потрапляє в індекс (Bm25Index.build): назва + текст.
    return document.title + "\n" + document.text
