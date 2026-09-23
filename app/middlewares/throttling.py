"""Простейший антифлуд: не даём дёргать бота быстрее, чем раз в N секунд."""
from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import get_settings

# Нажатия, которые намеренно делают быстро подряд: клетки капчи и кнопки ленты
FAST_CALLBACKS = ("cap:tok:", "br:like:", "br:dislike:")
# То же с нижней клавиатуры прежней версии — она приходит обычным текстом
FAST_TEXTS = re.compile(r"^(\d{1,2}|❤️(\s*\d+)?|👎|✅ Готово)$")


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.4, fast_rate: float = 0.12) -> None:
        self.rate = rate
        self.fast_rate = fast_rate
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Владельца не тормозим: он тестирует бота и жмёт кнопки подряд
        if get_settings().is_admin(user.id):
            return await handler(event, data)

        limit = self.rate
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(FAST_CALLBACKS):
            limit = self.fast_rate
        elif isinstance(event, Message) and FAST_TEXTS.match(event.text or ""):
            limit = self.fast_rate

        now = time.monotonic()
        last = self._last.get(user.id, 0.0)
        if now - last < limit:
            if isinstance(event, CallbackQuery):
                await event.answer()     # просто гасим «часики», без текста
            elif isinstance(event, Message):
                # Лишнее нажатие не должно остаться висеть в чате
                try:
                    await event.delete()
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            return None
        self._last[user.id] = now

        # Изредка подчищаем словарь, чтобы он не рос бесконечно
        if len(self._last) > 20_000:
            cutoff = now - 3600
            self._last = {k: v for k, v in self._last.items() if v > cutoff}

        return await handler(event, data)
