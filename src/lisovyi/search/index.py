"""BM25 по цілих документах."""

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from lisovyi.domain import DocumentKind
from lisovyi.search.morph import bag

K1 = 1.5
B = 0.75


@dataclass(frozen=True)
class SearchDocument:
    """Активний документ у тому вигляді, який потрібен пошуку."""

    document_id: int
    filename: str
    title: str
    kind: DocumentKind
    scope: str
    topic: str
    text: str
    author_id: int | None = None
    author_name: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class Scored:
    document: SearchDocument
    score: float


@dataclass(frozen=True)
class Bm25Index:
    """Незмінний індекс: перебудова означає новий об'єкт (Bm25Index.build)."""

    documents: tuple[SearchDocument, ...]
    _bags: tuple[Counter[str], ...]
    _lengths: tuple[int, ...]
    _avg_length: float
    _idf: dict[str, float]
    _df: dict[str, int]

    @classmethod
    def build(cls, documents: Iterable[SearchDocument]) -> "Bm25Index":
        # Карта працює через forest_map, а не як текст для пошуку.
        docs = tuple(d for d in documents if d.kind != DocumentKind.MAP)
        bags = tuple(bag(d.title + "\n" + d.text) for d in docs)
        lengths = tuple(sum(b.values()) for b in bags)
        n = len(docs)
        df: Counter[str] = Counter()
        for document_bag in bags:
            df.update(document_bag.keys())
        idf = {
            lemma: math.log(1 + (n - count + 0.5) / (count + 0.5)) for lemma, count in df.items()
        }
        avg_length = sum(lengths) / n if n else 0.0
        return cls(docs, bags, lengths, avg_length, idf, dict(df))

    def __len__(self) -> int:
        return len(self.documents)

    @property
    def vocabulary(self) -> frozenset[str]:
        return frozenset(self._idf)

    def document_frequency(self, lemma: str) -> int:
        """У скількох документах індексу є лема."""
        return self._df.get(lemma, 0)

    def score_all(self, query: Counter[str]) -> list[Scored]:
        """Рахунок кожного документа індексу; вага леми в питанні множить її внесок."""
        scored = []
        for document, document_bag, length in zip(
            self.documents, self._bags, self._lengths, strict=True
        ):
            score = 0.0
            for lemma, weight in query.items():
                tf = document_bag.get(lemma, 0)
                if not tf:
                    continue
                norm = K1 * (1 - B + B * length / self._avg_length)
                score += weight * self._idf[lemma] * tf * (K1 + 1) / (tf + norm)
            scored.append(Scored(document, score))
        return scored
