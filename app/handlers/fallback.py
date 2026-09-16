"""Последний роутер: ловит всё, что не разобрали остальные."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.handlers import menu as menu_handlers

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(call: CallbackQuery) -> None:
    """Кнопка из старого сообщения — не оставляем «часики» висеть."""
    await call.answer("Кнопка устарела. Откройте меню заново: /start", show_alert=True)


@router.message(F.text)
async def unknown_text(message: Message, state: FSMContext,
                       user: Mapping[str, Any], is_admin: bool) -> None:
    if not user["registered"]:
        await message.answer("Чтобы начать, нажмите /start")
        return
    await menu_handlers.show_main_menu(message, user, is_admin, text=texts.UNKNOWN)


@router.message()
async def unknown_other(message: Message, user: Mapping[str, Any]) -> None:
    if not user["registered"]:
        await message.answer("Чтобы начать, нажмите /start")
        return
    await message.answer(texts.UNKNOWN)
