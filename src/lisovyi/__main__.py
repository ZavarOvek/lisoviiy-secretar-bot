"""`python -m lisovyi`: міграції → імпорт архіву → бот."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from lisovyi.bot.handlers import router
from lisovyi.config import Settings
from lisovyi.db.session import make_engine, make_sessionmaker, run_migrations
from lisovyi.ingest.importer import import_archive

log = logging.getLogger("lisovyi")


async def run(settings: Settings, db_url: str) -> None:
    engine = make_engine(db_url)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            stats = await import_archive(session, settings.archive_dir, settings.transcripts_dir)
        log.info(
            "archive import: inserted=%d replaced=%d skipped=%d",
            stats.inserted,
            stats.replaced,
            stats.skipped,
        )

        # Токен перевіряється після імпорту: без нього база все одно має бути готова.
        if not settings.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set: archive imported, bot not started")
        bot = Bot(settings.telegram_bot_token)
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        await dispatcher.start_polling(bot, sessionmaker=sessionmaker)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    db_url = settings.db_url()
    run_migrations(db_url)
    asyncio.run(run(settings, db_url))


if __name__ == "__main__":
    main()
