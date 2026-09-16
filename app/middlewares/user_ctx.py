"""Подгружает пользователя из базы и кладёт его в data['user']."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.config import get_settings
from app.db import users as users_repo


class UserContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        row = await users_repo.ensure_user(
            tg_user.id, tg_user.username, tg_user.full_name
        )
        settings = get_settings()
        data["user"] = row
        data["settings"] = settings
        data["is_admin"] = settings.is_admin(tg_user.id)
        return await handler(event, data)
