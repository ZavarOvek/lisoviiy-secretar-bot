"""Вимога 1: усі роутери бота, крім групового, — під одним фільтром «лише особисті чати».

Фільтр стоїть на батьківському роутері, тож aiogram не пускає подію з іншого чату до жодного
з дочірніх. Новий роутер, підключений сюди, приватний без жодних додаткових рядків.
"""

from aiogram import F, Router
from aiogram.enums import ChatType

from lisovyi.bot.handlers import router
from lisovyi.bot.library import library_router
from lisovyi.bot.uploads import uploads_router

private_router = Router(name="private")
private_router.message.filter(F.chat.type == ChatType.PRIVATE)
private_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
# Завантаження першими: поки бот чекає ім'я звіра, текст — це ім'я, а не питання.
private_router.include_routers(uploads_router, library_router, router)
