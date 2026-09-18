"""Нажатия, которые работают откуда угодно: /start, /menu и «🏠 Меню».

Роутер подключается первым: иначе кнопку «🏠 Меню», нажатую посреди ввода
(например, пока бот ждёт текст сообщения к лайку), перехватил бы хендлер
этого ввода и принял бы за ответ.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.handlers import browse
from app.handlers import menu as menu_handlers
from app.handlers import onboarding
from app.keyboards import reply as rkb
from app.services import screen

router = Router(name="nav")


@router.message(CommandStart())
@router.message(F.text == rkb.START_OVER)
async def start(message: Message, state: FSMContext, bot: Bot, user: Mapping[str, Any],
                settings: Settings, is_admin: bool) -> None:
    await screen.drop(message)          # сама команда в чате не нужна
    await onboarding.begin(bot, message.chat.id, state, user, settings, is_admin,
                           first_name=message.from_user.first_name or "")


@router.message(Command("menu"))
@router.message(F.text == rkb.HOME)
@router.message(F.text.regexp(rkb.LEGACY_MENU_RE))
async def home(message: Message, state: FSMContext, user: Mapping[str, Any],
               is_admin: bool) -> None:
    await screen.drop(message)
    await menu_handlers.show_menu(message.bot, message.chat.id, state, user, is_admin)


@router.message(F.text.regexp(rkb.LEGACY_LIKES_RE))
async def legacy_likes(message: Message, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    """«Кто меня лайкнул» со старой клавиатуры: такие анкеты теперь первыми в ленте."""
    await screen.drop(message)
    await browse.open_feed(bot, message.chat.id, state, user, settings)
