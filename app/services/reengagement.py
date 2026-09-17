"""Напоминания тем, кто давно не заходил.

Логика простая и уважительная к людям:

* пишем не раньше, чем через `INACTIVE_HOURS` молчания;
* не чаще одного раза в `REMINDER_COOLDOWN_HOURS`;
* после `REMINDER_MAX_COUNT` напоминаний подряд без ответа — замолкаем
  совсем (вернулся человек — счётчик обнуляется сам);
* ночью не беспокоим: местный час считаем по долготе из анкеты;
* в каждом сообщении есть кнопка «Не напоминать».

Текст подбирается по ситуации: есть непросмотренные лайки — зовём смотреть их,
появились новые анкеты — говорим сколько, иначе шлём короткое напоминание.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Mapping

from aiogram import Bot

from app.config import Settings
from app.db import moderation as mod_repo
from app.db.database import db
from app.keyboards import inline as kb
from app.services.notify import safe_send

log = logging.getLogger(__name__)

BATCH_SIZE = 200
SEND_DELAY = 0.05          # ~20 сообщений в секунду
LOOP_INTERVAL = 15 * 60    # проверяем очередь раз в 15 минут
DEFAULT_UTC_OFFSET = 3     # если координат нет, считаем по Москве

GENERIC = [
    "💤 <b>Вы давно не заглядывали</b>\n\nЗа это время здесь появились новые "
    "люди. Пара минут — и, может быть, найдётся тот самый человек.",

    "👀 <b>Ваша анкета на месте и ждёт</b>\n\nНовые анкеты приходят каждый день. "
    "Загляните — вдруг сегодня повезёт.",

    "☕️ <b>Скучали?</b>\n\nПока вас не было, здесь прибавилось людей. "
    "Посмотрите пару анкет — это быстро.",

    "✨ <b>Кто-то сейчас листает анкеты</b>\n\nВозможно, именно вашу. "
    "Ответьте взаимностью первыми.",
]


def local_hour(lon: float | None, now: datetime | None = None) -> int:
    """Приблизительный местный час по долготе — чтобы не писать ночью."""
    now = now or datetime.now(timezone.utc)
    offset = DEFAULT_UTC_OFFSET if lon is None else round(lon / 15)
    return (now.hour + offset) % 24


def is_quiet(hour: int, settings: Settings) -> bool:
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end     # интервал через полночь


async def candidates(settings: Settings, limit: int = BATCH_SIZE) -> list:
    """Кому пора напомнить о себе. Владельцев бота не тревожим."""
    rows = await db.fetchall(
        """
        SELECT id, name, lon, notify_count, last_active,
               (SELECT COUNT(*) FROM reactions r
                JOIN users au ON au.id = r.from_id
                WHERE r.to_id = u.id AND r.kind = 'like'
                  AND au.is_banned = 0 AND au.is_active = 1
                  AND NOT EXISTS (SELECT 1 FROM reactions r2
                                  WHERE r2.from_id = u.id AND r2.to_id = r.from_id)
               ) AS pending_likes,
               (SELECT COUNT(*) FROM users nu
                WHERE nu.registered = 1 AND nu.is_active = 1 AND nu.is_banned = 0
                  AND nu.id != u.id
                  AND (u.looking_for = 'any' OR nu.gender = u.looking_for)
                  AND nu.created_at > u.last_active
               ) AS fresh_profiles
        FROM users u
        WHERE registered = 1 AND is_banned = 0 AND is_active = 1
          AND notify_enabled = 1
          AND notify_count < :max_count
          AND last_active <= datetime('now', :inactive)
          AND (last_notify_at IS NULL
               OR last_notify_at <= datetime('now', :cooldown))
        ORDER BY last_active
        LIMIT :limit
        """,
        {
            "max_count": settings.reminder_max_count,
            "inactive": f"-{settings.inactive_hours} hours",
            "cooldown": f"-{settings.reminder_cooldown_hours} hours",
            "limit": limit,
        },
    )
    return [row for row in rows if not settings.is_admin(int(row["id"]))]


def compose(row: Mapping[str, Any]) -> str:
    """Текст под ситуацию: лайки > новые анкеты > общее напоминание."""
    name = row["name"] or "Привет"
    likes = int(row["pending_likes"] or 0)
    fresh = int(row["fresh_profiles"] or 0)

    if likes:
        word = "человеку" if likes % 10 == 1 and likes % 100 != 11 else "людям"
        return (
            f"❤️ <b>{name}, вы понравились {likes} {word}!</b>\n\n"
            "Их анкеты ждут вашего ответа. Ответите взаимностью — "
            "сразу обменяетесь контактами."
        )
    if fresh >= 3:
        return (
            f"🔥 <b>{name}, пока вас не было, появилось {fresh} новых анкет</b>\n\n"
            "Посмотрите — самые активные получают больше всего симпатий."
        )
    return random.choice(GENERIC)


async def send_batch(bot: Bot, settings: Settings) -> int:
    """Одна волна напоминаний. Возвращает, скольким написали."""
    rows = await candidates(settings)
    sent = 0

    for row in rows:
        if is_quiet(local_hour(row["lon"]), settings):
            continue          # у человека ночь — вернёмся к нему днём

        ok = await safe_send(bot, int(row["id"]), compose(row),
                             kb.reminder_actions())
        if ok:
            sent += 1
            await db.execute(
                "UPDATE users SET last_notify_at = datetime('now'), "
                "notify_count = notify_count + 1 WHERE id = ?",
                (row["id"],),
            )
        else:
            # Бот заблокирован — больше не тревожим и прячем анкету
            await db.execute(
                "UPDATE users SET notify_enabled = 0, is_active = 0 WHERE id = ?",
                (row["id"],),
            )
        await asyncio.sleep(SEND_DELAY)

    if sent:
        await mod_repo.log_event("reengagement", None, sent=sent)
        await mod_repo.bump_counter("reminders_sent", sent)
        log.info("Напоминания отправлены: %s", sent)
    return sent


async def loop(bot: Bot, settings: Settings) -> None:
    """Фоновая задача: раз в 15 минут проверяет, кому пора напомнить."""
    if not settings.reengagement_enabled:
        log.info("Напоминания выключены (REENGAGEMENT_ENABLED=0)")
        return
    while True:
        try:
            await send_batch(bot, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Напоминания: %s", exc)
        await asyncio.sleep(LOOP_INTERVAL)
