"""Последний роутер: ловит всё, что не разобрали остальные."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.handlers import menu as menu_handlers
from app.handlers import onboarding
from app.services import screen

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(call: CallbackQuery, state: FSMContext, user: Mapping[str, Any],
                         settings: Settings, is_admin: bool) -> None:
    """Кнопка под сообщением из прежней версии бота: теперь управление —
    нижними кнопками, поэтому просто показываем актуальный экран."""
    await call.answer()
    if call.message is not None:
        await onboarding.begin(call.bot, call.message.chat.id, state, user, settings,
                               is_admin, first_name=call.from_user.first_name or "")


@router.message()
async def unknown(message: Message, state: FSMContext, user: Mapping[str, Any],
                  is_admin: bool) -> None:
    """Непонятное сообщение убираем, а меню напоминает, где кнопки."""
    await screen.drop(message)
    # Посреди диалога (капча, лента) экран уже подсказывает, что нажать, —
    # не сбиваем его ради случайного сообщения
    if await state.get_state() is not None:
        return
    await menu_handlers.show_menu(message.bot, message.chat.id, state, user, is_admin,
                                  note=texts.UNKNOWN if user["registered"] else None)
