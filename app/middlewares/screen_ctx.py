"""Нажатое сообщение — это и есть экран.

Если кнопку нажали на сообщении, которое бот не помнит как текущий экран
(старое меню выше по чату, хранилище забыло экран), оно становится экраном,
а прежний убирается. Кнопки уведомлений (лайки, совпадения, напоминания,
админка) сюда не входят: такие сообщения должны оставаться в переписке.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services import screen

NAV_PREFIXES = ("m:", "st:", "pr:", "edit:", "br:", "reg:", "onb:", "cap:", "ver:")
# Пожаловаться можно и из уведомления о лайке — его не превращаем в экран
NOT_NAV = ("br:report:",)


class ScreenMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if isinstance(event, CallbackQuery) and state is not None:
            callback = event.data or ""
            message = event.message
            if (isinstance(message, Message) and callback.startswith(NAV_PREFIXES)
                    and not callback.startswith(NOT_NAV)):
                await screen.adopt(event.bot, message.chat.id, state, message)
        return await handler(event, data)
