"""Telegram: /start і /help, питання звичайним текстом, кнопки рукавичок."""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from lisovyi.answer.service import AnswerService
from lisovyi.bot import texts
from lisovyi.bot.places import PLACE_PREFIX, find_place, glove_places, place_keyboard
from lisovyi.search.service import SearchService

log = logging.getLogger(__name__)

router = Router()

COMMANDS = [
    BotCommand(command="start", description="Що я вмію"),
    BotCommand(command="help", description="Довідка"),
]

TYPING_REPEAT = 5.0

_NUMBER_WORDS = {8: "восьми"}


def start_text(gloves: int) -> str:
    """Привітання дослівно з брифу 03; кількість рукавичок — з Карти лісу."""
    count = _NUMBER_WORDS.get(gloves, str(gloves))
    return (
        "Привіт! Я Лісовий секретар 🌲\n"
        "\n"
        f"Я бережу «Лісовий архів»: статути, правила й поради всіх {count} рукавичок, "
        "а ще поради, які надсилають самі звірі. Відповідаю тільки за цими записами "
        "і завжди кажу, звідки взяв відповідь.\n"
        "\n"
        "Питай звичайним повідомленням і називай місце — так я знайду пораду саме для твоєї "
        "рукавички. Наприклад:\n"
        "— Які гриби біля сосон?\n"
        "— Коли утеплювати рукавичку під вербою?\n"
        "\n"
        "Маєш корисну пораду? Надішли її файлом: PDF, Word (.docx) або TXT."
    )


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, search: SearchService) -> None:
    await message.answer(start_text(len(search.state.resolver.gloves)))


@router.message(F.text & ~F.text.startswith("/"))
async def question(message: Message, bot: Bot, answers: AnswerService) -> None:
    await _answer(bot, message.chat.id, message.text or "", answers)


@router.callback_query(F.data.startswith(PLACE_PREFIX))
async def place_chosen(callback: CallbackQuery, bot: Bot, answers: AnswerService) -> None:
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    key = (callback.data or "").removeprefix(PLACE_PREFIX)
    place = find_place(key, glove_places(answers.search.state.resolver.gloves))
    if place is None:
        log.warning("place button for a glove no longer on the map: %r", callback.data)
        await bot.send_message(chat_id, texts.PLACE_GONE, parse_mode=None)
        return
    # Натиск кнопки — те саме, що звір написав назву рукавички.
    await _answer(bot, chat_id, place.label, answers)


async def _answer(bot: Bot, chat_id: int, text: str, answers: AnswerService) -> None:
    async def notify(intermediate: str) -> None:
        await bot.send_message(chat_id, intermediate, parse_mode=None)

    # «Друкує…» одразу, а поки Groq думає чи бот чекає після 429 — повторювати кожні 5 с:
    # Telegram сам гасить статус через 5 с.
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    async with ChatActionSender.typing(chat_id=chat_id, bot=bot, initial_sleep=TYPING_REPEAT):
        reply = await answers.answer(chat_id, text, notify)
    # Без parse_mode: текст моделі не екранується, тож розмітка в ньому була б небезпечна.
    await bot.send_message(
        chat_id,
        reply.text,
        parse_mode=None,
        reply_markup=place_keyboard(glove_places(reply.keyboard)),
    )
