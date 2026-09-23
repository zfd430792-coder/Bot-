"""Нажатое сообщение — это и есть экран.

Если кнопку нажали на сообщении, которое бот не помнит как текущий экран
(старое меню выше по чату, хранилище забыло экран), оно становится экраном,
а прежний убирается — в чате снова ровно один экран.

Кнопки уведомлений (n: — лайки, напоминания, жалобы и заявки для админа)
сюда не входят: такие сообщения должны оставаться в переписке, а открытый
по ним раздел встаёт на место прежнего экрана ниже.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services import screen

NAV_PREFIXES = ("m:", "cap:", "onb:", "reg:", "br:", "rep:", "pr:", "ver:", "adm:")


class ScreenMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if isinstance(event, CallbackQuery) and state is not None:
            message = event.message
            if isinstance(message, Message) and (event.data or "").startswith(NAV_PREFIXES):
                await screen.adopt(event.bot, message.chat.id, state, message)
        return await handler(event, data)
