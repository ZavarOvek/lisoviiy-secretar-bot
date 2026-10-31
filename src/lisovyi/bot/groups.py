"""Вимога 1: бот працює лише в особистих повідомленнях.

Решта роутерів обробляє тільки приватні чати. Цей роутер, підключений першим, у групах
і супергрупах відповідає лише на /start і /help, а на все інше мовчить. Канали шлють
channel_post, а не message, тож їх не бачить жоден хендлер.
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from lisovyi.bot import texts

groups_router = Router(name="groups")
groups_router.message.filter(F.chat.type != ChatType.PRIVATE)
groups_router.callback_query.filter(F.message.chat.type != ChatType.PRIVATE)

# Фільтр для всіх інших роутерів.
PRIVATE_MESSAGE = F.chat.type == ChatType.PRIVATE
PRIVATE_CALLBACK = F.message.chat.type == ChatType.PRIVATE


@groups_router.message(CommandStart())
@groups_router.message(Command("help"))
async def private_only(message: Message, bot: Bot) -> None:
    await bot.send_message(message.chat.id, texts.PRIVATE_ONLY, parse_mode=None)


@groups_router.callback_query()
async def ignore_group_button(callback: CallbackQuery) -> None:
    # Бот кнопок у групах не надсилає; чужу чи стару кнопку просто «гасимо».
    await callback.answer()
