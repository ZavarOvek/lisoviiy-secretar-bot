import pytest
from sqlalchemy import func, select

from lisovyi.__main__ import run
from lisovyi.config import Settings
from lisovyi.db.models import Document

pytestmark = pytest.mark.db


async def test_without_token_archive_is_imported_before_exit(db_url, sessionmaker, caplog):
    settings = Settings(telegram_bot_token="", database_url=db_url)

    with caplog.at_level("INFO"), pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN is not set"):
        await run(settings, db_url)

    assert "archive import: inserted=78 replaced=0 skipped=0" in caplog.text
    assert "search index: 77 documents, 8 gloves" in caplog.text
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 78


async def test_without_groq_key_the_bot_does_not_start(db_url):
    settings = Settings(telegram_bot_token="42:TEST", groq_api_key="", database_url=db_url)
    with pytest.raises(SystemExit, match="GROQ_API_KEY is not set"):
        await run(settings, db_url)
