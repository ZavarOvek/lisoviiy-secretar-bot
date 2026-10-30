"""Telegram: /start і /help, питання звичайним текстом, кнопки рукавичок."""

import logging
from collections.abc import Sequence

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from lisovyi.answer.service import AnswerService
from lisovyi.search.service import SearchService

log = logging.getLogger(__name__)

router = Router()

COMMANDS = [
    BotCommand(command="start", description="Що я вмію"),
    BotCommand(command="help", description="Довідка"),
]

PLACE_PREFIX = "place:"
# Telegram обмежує callback_data 64 байтами UTF-8.
CALLBACK_DATA_BYTES = 64
BUTTONS_PER_ROW = 2
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


def place_keyboard(gloves: Sequence[str]) -> InlineKeyboardMarkup | None:
    """Кнопка на кожну рукавичку. callback_data — назва, а якщо найдовша не влазить
    у 64 байти, то для всіх кнопок індекс у карті."""
    if not gloves:
        return None
    by_name = all(_fits(PLACE_PREFIX + glove) for glove in gloves)
    buttons = [
        InlineKeyboardButton(
            text=glove, callback_data=PLACE_PREFIX + (glove if by_name else str(index))
        )
        for index, glove in enumerate(gloves)
    ]
    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def glove_from_callback(data: str, gloves: Sequence[str]) -> str | None:
    """Назва рукавички з callback_data; None, якщо її вже немає на карті."""
    value = data.removeprefix(PLACE_PREFIX)
    if value in gloves:
        return value
    if value.isdigit() and int(value) < len(gloves):
        return gloves[int(value)]
    return None


def _fits(data: str) -> bool:
    return len(data.encode("utf-8")) <= CALLBACK_DATA_BYTES


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
    glove = glove_from_callback(callback.data or "", answers.search.state.resolver.gloves)
    if glove is None or callback.message is None:
        log.warning("place button for a glove no longer on the map: %r", callback.data)
        return
    # Натиск кнопки — те саме, що звір написав назву рукавички.
    await _answer(bot, callback.message.chat.id, glove, answers)


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
        chat_id, reply.text, parse_mode=None, reply_markup=place_keyboard(reply.keyboard)
    )
