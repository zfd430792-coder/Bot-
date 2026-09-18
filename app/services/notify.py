"""Уведомления администратору и безопасная отправка сообщений."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.config import get_settings

log = logging.getLogger(__name__)

Markup = ReplyKeyboardMarkup | ReplyKeyboardRemove | None


async def safe_send(bot: Bot, chat_id: int, text: str,
                    markup: Markup = None,
                    disable_notification: bool = False) -> bool:
    """Отправка, которая не роняет бота из-за блокировки или флуд-лимита."""
    try:
        await bot.send_message(chat_id, text, reply_markup=markup,
                               disable_notification=disable_notification)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
            return True
        except Exception:
            return False
    except TelegramForbiddenError:
        return False           # пользователь заблокировал бота
    except TelegramBadRequest as exc:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
        return False


async def admin_log(bot: Bot, text: str, markup: Markup = None) -> None:
    """Пишет в журнал администратора (LOG_CHAT_ID или первому админу)."""
    settings = get_settings()
    ok = await safe_send(bot, settings.log_target, text, markup,
                         disable_notification=True)
    if not ok and settings.log_chat_id:
        # Резерв: если канал недоступен — пишем владельцу напрямую
        await safe_send(bot, settings.admin_ids[0], text, markup)


async def notify_admins(bot: Bot, text: str, markup: Markup = None) -> None:
    """Важные события — всем админам сразу."""
    for admin_id in get_settings().admin_ids:
        await safe_send(bot, admin_id, text, markup)
