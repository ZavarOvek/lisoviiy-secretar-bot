"""`python -m lisovyi`: міграції → імпорт архіву → бот."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from groq import AsyncGroq

from lisovyi.answer.llm import LLMClient
from lisovyi.answer.service import AnswerService
from lisovyi.bot.groups import groups_router
from lisovyi.bot.handlers import COMMANDS, router
from lisovyi.bot.library import library_router
from lisovyi.bot.uploads import UploadDialogue, uploads_router
from lisovyi.config import Settings
from lisovyi.db.session import make_engine, make_sessionmaker, run_migrations
from lisovyi.ingest.importer import import_archive
from lisovyi.library import Library
from lisovyi.notify import Notifier
from lisovyi.search.service import SearchService
from lisovyi.uploads.service import UploadService

log = logging.getLogger("lisovyi")

NOTIFY_DRAIN_TIMEOUT = 10.0


def make_llm(settings: Settings) -> LLMClient:
    # Повтори SDK вимкнені: 429 обробляє LLMClient, щоб звір бачив, що відбувається.
    client = AsyncGroq(api_key=settings.groq_api_key, max_retries=0, timeout=30)
    return LLMClient(
        client,
        settings.groq_model,
        fallback_model=settings.groq_fallback_model,
        reasoning_effort=settings.groq_reasoning_effort,
        max_completion_tokens=settings.groq_max_completion_tokens,
    )


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

        search = SearchService(sessionmaker, settings.context_char_budget)
        await search.reload()
        log.info(
            "search index: %d documents, %d gloves",
            len(search.state.index),
            len(search.state.resolver.gloves),
        )

        # Токени перевіряються після імпорту: без них база все одно має бути готова.
        if not settings.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set: archive imported, bot not started")
        if not settings.groq_api_key:
            raise SystemExit("GROQ_API_KEY is not set: archive imported, bot not started")
        answers = AnswerService(
            search, make_llm(settings), dialogue_ttl=settings.dialogue_ttl_seconds
        )
        log.info(
            "llm: model=%s fallback=%s reasoning_effort=%s max_completion_tokens=%d",
            settings.groq_model,
            settings.groq_fallback_model or "-",
            settings.groq_reasoning_effort,
            settings.groq_max_completion_tokens,
        )

        bot = Bot(settings.telegram_bot_token)
        await bot.set_my_commands(COMMANDS)
        log.info("bot commands: %s", ", ".join(command.command for command in COMMANDS))
        notifier = Notifier(bot, sessionmaker)
        uploads = UploadDialogue(UploadService(sessionmaker, search, notifier))
        dispatcher = Dispatcher()
        # Завантаження першими: поки бот чекає ім'я звіра, текст — це ім'я, а не питання.
        # Групи першими: там бот відповідає лише на /start і /help (вимога 1).
        dispatcher.include_router(groups_router)
        dispatcher.include_router(uploads_router)
        dispatcher.include_router(library_router)
        dispatcher.include_router(router)
        try:
            await dispatcher.start_polling(
                bot,
                search=search,
                answers=answers,
                uploads=uploads,
                library=Library(sessionmaker),
                notifier=notifier,
            )
        finally:
            # Розсилка, почата перед зупинкою, має дійти до кінця, поки БД ще відкрита.
            await notifier.drain(timeout=NOTIFY_DRAIN_TIMEOUT)
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
