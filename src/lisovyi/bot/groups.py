"""Вимога 1: бот працює лише в особистих повідомленнях.

Решту роутерів зібрано під `bot.private.private_router` з фільтром «лише особисті чати».
Цей роутер, підключений першим, у групах і супергрупах відповідає лише на /start і /help,
а на все інше мовчить. Канали шлють channel_post, а не message, тож їх не бачить жоден
хендлер.
"""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message

from lisovyi.bot import texts
from lisovyi.bot.handlers import COMMANDS

groups_router = Router(name="groups")
groups_router.message.filter(F.chat.type != ChatType.PRIVATE)
groups_router.callback_query.filter(F.message.chat.type != ChatType.PRIVATE)

# Меню команд у групах: решта команд там нічого не робить.
GROUP_COMMANDS: list[BotCommand] = [c for c in COMMANDS if c.command in {"start", "help"}]


@groups_router.message(CommandStart())
@groups_router.message(Command("help"))
async def private_only(message: Message) -> None:
    # message.answer, а не bot.send_message: у темі форуму відповідь лишається в темі.
    await message.answer(texts.PRIVATE_ONLY, parse_mode=None)


@groups_router.callback_query()
async def ignore_group_button(callback: CallbackQuery) -> None:
    # Бот кнопок у групах не надсилає; чужу чи стару кнопку просто «гасимо».
    await callback.answer()
