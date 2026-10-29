from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lisovyi.config import PROJECT_ROOT


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def run_migrations(url: str) -> None:
    """`alembic upgrade head` проти `url`.

    Синхронна: `migrations/env.py` сам запускає власний цикл подій, тож викликати її треба
    поза запущеним циклом (з асинхронного коду — через `asyncio.to_thread`).
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
