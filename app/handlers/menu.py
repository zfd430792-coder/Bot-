"""Главное меню и справка."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import get_settings
from app.db import users as users_repo
from app.keyboards import reply as rkb

router = Router(name="menu")


async def send_main_menu(bot: Bot, chat_id: int, user: Mapping[str, Any],
                         is_admin: bool, text: str | None = None) -> None:
    likes = await users_repo.count_incoming_likes(user["id"])
    await bot.send_message(
        chat_id,
        text or texts.MAIN_MENU,
        reply_markup=rkb.main_menu(is_admin=is_admin, likes_count=likes),
    )


async def show_main_menu(message: Message, user: Mapping[str, Any], is_admin: bool,
                         text: str | None = None) -> None:
    await send_main_menu(message.bot, message.chat.id, user, is_admin, text)


@router.message(Command("help"))
@router.message(F.text == rkb.BTN_HELP)
async def help_handler(message: Message, user, is_admin: bool) -> None:
    settings = get_settings()
    await message.answer(texts.HELP.format(limit=settings.likes_limit_per_day))


@router.message(Command("menu"))
async def menu_command(message: Message, state: FSMContext, user, is_admin: bool) -> None:
    await state.clear()
    if not user["registered"]:
        await message.answer("Сначала нужно заполнить анкету — нажмите /start")
        return
    await show_main_menu(message, user, is_admin)
