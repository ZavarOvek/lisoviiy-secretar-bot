import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from lisovyi.db.models import Base, Document
from lisovyi.db.session import make_engine
from lisovyi.domain import DocumentKind, DocumentStatus, TextSource

pytestmark = pytest.mark.db


async def test_migration_matches_models(db_url):
    engine = make_engine(db_url)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
    finally:
        await engine.dispose()
    assert diff == []


def _document(status: DocumentStatus) -> Document:
    return Document(
        filename="Правила толоки.txt",
        title="Правила толоки",
        kind=DocumentKind.GENERAL,
        scope="general",
        topic="toloka",
        text="...",
        text_source=TextSource.TEXT,
        sha256="0" * 64,
        status=status,
    )


async def test_partial_unique_index_allows_one_active_version(sessionmaker):
    async with sessionmaker() as session:
        # Неактивні версії тієї ж пари (scope, topic) індекс не обмежує.
        session.add_all(
            [
                _document(DocumentStatus.ACTIVE),
                _document(DocumentStatus.REPLACED),
                _document(DocumentStatus.DELETED),
            ]
        )
        await session.commit()

        session.add(_document(DocumentStatus.ACTIVE))
        with pytest.raises(IntegrityError, match="uq_documents_active_scope_topic"):
            await session.commit()
