"""Фильтры доступа к служебным разделам.

Роли:
* **админ** — перечислен в ADMIN_IDS. Полный доступ, включая рассылку,
  рекламу, настройки бота и назначение модераторов.
* **модератор** — назначается админом прямо в боте. Ему доступны жалобы,
  верификация, баны и поиск пользователя — и ничего больше.

Значения приходят из UserContextMiddleware, поэтому лишних запросов в базу нет.
"""
from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class IsAdmin(BaseFilter):
    """Только владельцы бота из ADMIN_IDS."""

    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        return is_admin


class IsStaff(BaseFilter):
    """Админы и модераторы."""

    async def __call__(self, event: TelegramObject, is_staff: bool = False) -> bool:
        return is_staff
