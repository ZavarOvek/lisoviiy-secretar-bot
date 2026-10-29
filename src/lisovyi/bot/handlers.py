from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lisovyi.db.models import Document
from lisovyi.domain import DocumentStatus

router = Router()


def documents_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "документ"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "документи"
    return "документів"


def start_text(active_documents: int) -> str:
    return (
        "Лісовий архів скоро відкриється. "
        f"У архіві зараз {active_documents} {documents_word(active_documents)}."
    )


@router.message(CommandStart())
async def start(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.status == DocumentStatus.ACTIVE)
        )
    await message.answer(start_text(count or 0))
