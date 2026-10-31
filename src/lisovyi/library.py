"""Перелік активних документів для /docs і /my (вимога 16)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lisovyi.db.models import Beast, Document
from lisovyi.domain import GENERAL_SCOPE, DocumentKind, DocumentStatus


@dataclass(frozen=True)
class DocumentRow:
    id: int
    title: str
    kind: DocumentKind
    scope: str
    # None — архівний документ.
    author_id: int | None
    author_name: str | None
    created_at: datetime


class Library:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sessionmaker = sessionmaker

    async def group(self, kind: DocumentKind, scope: str) -> list[DocumentRow]:
        """Активні документи місця за назвою. «Увесь ліс» — поради всього лісу і карта."""
        if kind == DocumentKind.GENERAL:
            where = or_(Document.kind == DocumentKind.MAP, Document.scope == GENERAL_SCOPE)
        else:
            where = Document.scope == scope
        return await self._rows(where, order=(Document.title, Document.id))

    async def by_author(self, telegram_id: int) -> list[DocumentRow]:
        """Активні документи звіра, від нових до старих."""
        return await self._rows(
            Beast.telegram_id == telegram_id, order=(Document.created_at.desc(), Document.id.desc())
        )

    async def get(self, document_id: int) -> DocumentRow | None:
        rows = await self._rows(Document.id == document_id, order=(Document.id,))
        return rows[0] if rows else None

    async def _rows(self, where, order) -> list[DocumentRow]:
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(Document, Beast.name)
                .outerjoin(Beast, Document.author_id == Beast.id)
                .where(where, Document.status == DocumentStatus.ACTIVE)
                .order_by(*order)
            )
            return [
                DocumentRow(d.id, d.title, d.kind, d.scope, d.author_id, name, d.created_at)
                for d, name in result
            ]
