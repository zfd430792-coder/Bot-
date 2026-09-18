"""Списки команд бота для разных ролей."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

log = logging.getLogger(__name__)

USER = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="search", description="Смотреть анкеты"),
    BotCommand(command="profile", description="Моя анкета"),
]

MODERATOR = USER + [
    BotCommand(command="mod", description="Панель модератора"),
    BotCommand(command="find", description="Найти пользователя"),
    BotCommand(command="ban", description="Забанить"),
    BotCommand(command="unban", description="Разбанить"),
    BotCommand(command="verify", description="Запросить верификацию"),
]

ADMIN = MODERATOR + [
    BotCommand(command="admin", description="Админ-панель"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="broadcast", description="Рассылка"),
    BotCommand(command="ads", description="Рекламные посты"),
    BotCommand(command="mods", description="Модераторы"),
    BotCommand(command="say", description="Написать пользователю"),
]


async def apply(bot: Bot, chat_id: int, commands: list[BotCommand] | None) -> None:
    """Ставит набор команд конкретному человеку (None — вернуть общий)."""
    scope = BotCommandScopeChat(chat_id=chat_id)
    try:
        if commands is None:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(commands, scope=scope)
    except Exception as exc:
        log.warning("Не удалось обновить команды для %s: %s", chat_id, exc)


async def setup(bot: Bot, admin_ids: list[int], moderator_ids: list[int]) -> None:
    try:
        await bot.set_my_commands(USER, scope=BotCommandScopeDefault())
    except Exception as exc:
        log.warning("Не удалось задать общие команды: %s", exc)
    for admin_id in admin_ids:
        await apply(bot, admin_id, ADMIN)
    for moderator_id in moderator_ids:
        if moderator_id not in admin_ids:
            await apply(bot, moderator_id, MODERATOR)
