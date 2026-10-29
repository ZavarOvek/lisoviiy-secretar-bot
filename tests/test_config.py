import pytest

from lisovyi.config import Settings


def test_db_url_is_built_from_postgres_settings():
    settings = Settings(
        postgres_user="lisovyi",
        postgres_password="p@ss/word",
        postgres_db="lisovyi",
        database_url="",
    )
    assert settings.db_url() == "postgresql+asyncpg://lisovyi:p%40ss%2Fword@db:5432/lisovyi"


def test_database_url_takes_priority():
    settings = Settings(postgres_db="lisovyi", database_url="postgresql+asyncpg://u:p@h/test")
    assert settings.db_url() == "postgresql+asyncpg://u:p@h/test"


def test_missing_settings_are_named():
    settings = Settings(
        postgres_user="lisovyi", postgres_password="", postgres_db="", database_url=""
    )
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD, POSTGRES_DB"):
        settings.db_url()
