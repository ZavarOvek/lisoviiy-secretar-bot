"""Пошук поверх поточного стану БД: індекс активних документів і резолвер з forest_map."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lisovyi.db.models import Beast, Document, ForestMapEntry
from lisovyi.domain import DocumentKind, DocumentStatus
from lisovyi.search.index import Bm25Index, SearchDocument
from lisovyi.search.places import PlaceResolver
from lisovyi.search.retrieve import DEFAULT_CHAR_BUDGET, ContextPack, merge_followup, retrieve
from lisovyi.search.synonyms import lemmatized_synonyms


@dataclass(frozen=True)
class _State:
    index: Bm25Index
    resolver: PlaceResolver


class SearchService:
    def __init__(self, char_budget: int = DEFAULT_CHAR_BUDGET):
        self.char_budget = char_budget
        self._state: _State | None = None

    async def reload(self, session: AsyncSession) -> None:
        """Перебудовує індекс і резолвер з активних документів і forest_map.

        Документи й карта читаються в одній транзакції REPEATABLE READ, тож бачать один
        знімок бази. Індекс будується в окремому потоці, щоб не тримати цикл подій бота.
        Обидва підміняються одним присвоєнням: читач бачить або старий стан, або новий.
        """
        # Окрема транзакція, бо рівень ізоляції задається до першого запиту в ній.
        async with session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            rows = await session.execute(
                select(Document, Beast.name)
                .outerjoin(Beast, Document.author_id == Beast.id)
                .where(Document.status == DocumentStatus.ACTIVE, Document.kind != DocumentKind.MAP)
                .order_by(Document.id)
            )
            documents = [
                SearchDocument(
                    document_id=document.id,
                    filename=document.filename,
                    title=document.title,
                    kind=document.kind,
                    scope=document.scope,
                    topic=document.topic,
                    text=document.text,
                    author_id=document.author_id,
                    author_name=author_name,
                    created_at=document.created_at,
                )
                for document, author_name in rows
            ]
            entries = await session.scalars(select(ForestMapEntry).order_by(ForestMapEntry.glove))
            forest_map = {entry.glove: list(entry.parts) for entry in entries}
        if not forest_map:
            raise RuntimeError("forest_map is empty: import the forest map before searching")
        self._state = await asyncio.to_thread(_build_state, documents, forest_map)

    @property
    def state(self) -> _State:
        if self._state is None:
            raise RuntimeError("search index is not loaded: call SearchService.reload() first")
        return self._state

    def search(self, question: str, previous: str | None = None) -> ContextPack:
        state = self.state
        merged = merge_followup(previous, question, state.resolver)
        return retrieve(merged, state.index, state.resolver, char_budget=self.char_budget)


def _build_state(
    documents: list[SearchDocument], forest_map: Mapping[str, Sequence[str]]
) -> _State:
    """Уся морфологія і BM25: сотні мілісекунд CPU, тому поза циклом подій."""
    resolver = PlaceResolver(forest_map)
    # Зіпсована таблиця синонімів має зупинити старт, а не перше питання звіра.
    lemmatized_synonyms()
    return _State(Bm25Index.build(documents), resolver)
