"""Последний роутер: ловит всё, что не разобрали остальные."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.handlers import menu as menu_handlers
from app.keyboards import inline as kb
from app.services import screen

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(call: CallbackQuery) -> None:
    """Кнопка из старого сообщения — не оставляем «часики» висеть."""
    await call.answer("Кнопка устарела. Откройте меню заново: /start", show_alert=True)


@router.message()
async def unknown(message: Message, state: FSMContext, user: Mapping[str, Any],
                  is_admin: bool) -> None:
    """Непонятное сообщение убираем, а меню напоминает, где кнопки."""
    await screen.drop(message)
    # Посреди диалога (капча, лента) экран уже подсказывает, что нажать, —
    # не сбиваем его ради случайного сообщения
    if await state.get_state() is not None:
        return
    if not user["registered"]:
        await screen.show(message.bot, message.chat.id, state,
                          "Чтобы начать, нажмите /start или кнопку ниже.", kb.START_OVER)
        return
    await menu_handlers.show_menu(message.bot, message.chat.id, state, user, is_admin,
                                  note=texts.UNKNOWN)
