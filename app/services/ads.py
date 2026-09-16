"""Показ рекламных постов в ленте анкет.

Пост хранится не текстом, а ссылкой на исходное сообщение в чате с админом:
copy_message переносит его как есть — с фото, видео, форматированием и
эмодзи. Кнопку со ссылкой при этом можно подставить свою.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import ads as ads_repo

log = logging.getLogger(__name__)


def markup(ad: Mapping[str, Any]) -> InlineKeyboardMarkup | None:
    if not (ad["button_text"] and ad["button_url"]):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ad["button_text"], url=ad["button_url"])
    ]])


async def send(bot: Bot, chat_id: int, ad: Mapping[str, Any]) -> list[int]:
    """Показывает пост. Возвращает id сообщений — их убирают вместе с анкетой."""
    try:
        sent = await bot.copy_message(
            chat_id, ad["src_chat_id"], ad["src_message_id"],
            reply_markup=markup(ad),
        )
    except TelegramBadRequest as exc:
        # Исходное сообщение удалили — выключаем пост, чтобы не ломать ленту
        log.warning("Реклама #%s недоступна, выключаю: %s", ad["id"], exc)
        await ads_repo.set_active(ad["id"], False)
        return []
    except Exception as exc:
        log.warning("Реклама #%s не отправилась: %s", ad["id"], exc)
        return []

    await ads_repo.bump_shows(ad["id"])
    return [sent.message_id]


async def maybe_send(bot: Bot, chat_id: int, shown: int) -> tuple[list[int], int]:
    """Решает, пора ли показать рекламу.

    `shown` — сколько анкет человек посмотрел с прошлого поста.
    Возвращает (id сообщений, новый счётчик).
    """
    ad = await ads_repo.pick_next()
    if ad is None or shown < int(ad["every_n"] or 10):
        return [], shown
    message_ids = await send(bot, chat_id, ad)
    return message_ids, 0 if message_ids else shown
