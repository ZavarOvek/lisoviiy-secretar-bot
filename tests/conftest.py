import asyncio
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lisovyi.config import PROJECT_ROOT
from lisovyi.db.session import make_engine, make_sessionmaker, run_migrations
from lisovyi.search.index import Bm25Index
from lisovyi.search.offline import load_archive
from lisovyi.search.places import PlaceResolver


@pytest.fixture(scope="session")
def archive_search() -> tuple[Bm25Index, PlaceResolver]:
    """Пошуковий індекс і резолвер, побудовані з data/archive без БД."""
    return load_archive(PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts")


async def _recreate_database(url: URL) -> None:
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    quoted = '"' + url.database.replace('"', '""') + '"'
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {quoted}")
    finally:
        await conn.close()


@pytest.fixture
async def db_url() -> str:
    """Окрема база з DATABASE_URL, щоразу створена наново й доведена міграціями до head."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set: db tests need a disposable PostgreSQL database")
    parsed = make_url(url)
    bot_db = os.environ.get("POSTGRES_DB")
    if bot_db and parsed.database == bot_db:
        pytest.fail(f"DATABASE_URL points at the bot's own database {bot_db!r}; tests drop it")
    await _recreate_database(parsed)
    await asyncio.to_thread(run_migrations, url)
    return url


@pytest.fixture
async def sessionmaker(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(db_url)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()
