"""Простейший антифлуд: не даём дёргать бота быстрее, чем раз в N секунд."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

# Действия, которые намеренно делают быстрыми подряд (клетки капчи, лента)
FAST_PREFIXES = ("cap:tok:", "cap:done", "br:")


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

        limit = self.rate
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(FAST_PREFIXES):
            limit = self.fast_rate

        now = time.monotonic()
        last = self._last.get(user.id, 0.0)
        if now - last < limit:
            if isinstance(event, CallbackQuery):
                await event.answer()     # просто гасим «часики», без текста
            return None
        self._last[user.id] = now

        # Изредка подчищаем словарь, чтобы он не рос бесконечно
        if len(self._last) > 20_000:
            cutoff = now - 3600
            self._last = {k: v for k, v in self._last.items() if v > cutoff}

        return await handler(event, data)
