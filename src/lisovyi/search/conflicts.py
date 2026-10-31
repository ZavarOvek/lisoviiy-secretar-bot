"""Вимога 8: поради частин лісу для рукавички з кількох частин розходяться.

Чи є конфлікт, вирішує цей детектор, а не модель: модель потім лише заповнює окремі поля
для кожної частини, тож обрати одну з порад їй нема де.
"""

from dataclasses import dataclass

from lisovyi.domain import DocumentKind, ForestPart
from lisovyi.search.index import Bm25Index
from lisovyi.search.morph import bag
from lisovyi.search.places import PlaceResolver
from lisovyi.search.retrieve import ContextDoc, ContextPack, Layer, Mode, strip_places
from lisovyi.search.synonyms import expand


@dataclass(frozen=True)
class PartConflict:
    glove: str
    topic: str
    # Найкращий документ кожної частини рукавички, у порядку карти.
    docs: dict[ForestPart, ContextDoc]


def detect_part_conflict(
    question: str, pack: ContextPack, index: Bm25Index, resolver: PlaceResolver
) -> PartConflict | None:
    """Конфлікт, якщо виконано все:

    - пакет «placed», названа рівно одна рукавичка, частини лісу прямо не названі;
    - за картою рукавичка має щонайменше дві частини;
    - для кожної частини в пакеті є документ шару «частина» з ненульовим змістовним рахунком,
      і найкращі документи частин мають однакову тему;
    - жоден активний документ самої рукавички не відповідає на питання краще за слабшу
      з частин.

    Змістовний рахунок — BM25 питання без назв місць (`strip_places`) з синонімами: інакше
    «Статут ОСМД Під вербою» вигравав би лише завдяки слову «верба».
    """
    if pack.mode != Mode.PLACED or len(pack.gloves) != 1 or pack.parts:
        return None
    glove = pack.gloves[0]
    parts = resolver.parts_of(glove)
    if len(parts) < 2:
        return None
    query = expand(bag(strip_places(question, resolver)))
    scored = {s.document.document_id: s for s in index.score_all(query)}

    best: dict[ForestPart, ContextDoc] = {}
    for part in parts:
        candidates = [
            doc for doc in pack.docs if doc.layer == Layer.PART and doc.scope == part.value
        ]
        if not candidates:
            return None
        top = max(candidates, key=lambda doc: (_score(scored, doc.document_id), -doc.document_id))
        if _score(scored, top.document_id) <= 0:
            return None
        best[part] = top

    topics = {scored[doc.document_id].document.topic for doc in best.values()}
    if len(topics) != 1:
        return None
    weakest_part = min(_score(scored, doc.document_id) for doc in best.values())
    glove_best = max(
        (
            s.score
            for s in scored.values()
            if s.document.kind == DocumentKind.GLOVE and s.document.scope == glove
        ),
        default=0.0,
    )
    if glove_best >= weakest_part:
        return None
    return PartConflict(glove, topics.pop(), best)


def _score(scored: dict, document_id: int) -> float:
    entry = scored.get(document_id)
    return entry.score if entry is not None else 0.0
