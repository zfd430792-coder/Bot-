"""Автоматическая защита от накрутки лайков.

Два признака машинного поведения:

1. **Скорость.** Живой человек смотрит фото и читает описание. Если реакции
   идут одна за другой быстрее, чем за секунду с небольшим, и так много раз
   подряд — анкеты никто не смотрит, кнопку жмут вслепую.
2. **Только лайки.** Даже уложившись в суточный лимит, нормальный человек
   кого-то пропускает. Доля лайков около 100% на длинной серии означает
   «лайкаю всех подряд» — обычно чтобы собрать взаимности и рассылать спам.

Наказание нарастает, потому что оба признака иногда даёт и живой человек:

| Нарушение | Что происходит |
|-----------|----------------|
| первое    | просим заново пройти капчу — боту это дороже, чем человеку |
| второе    | автобан на сутки (срок настраивается) |
| третье    | бессрочный бан |

О каждом срабатывании уходит уведомление администратору с командой разбана —
последнее слово всегда за человеком.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot

from app.config import Settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.db.database import db
from app.keyboards import reply as rkb
from app.services.notify import appeal_contact, notify_admins, safe_send

log = logging.getLogger(__name__)

# Метка времени с миллисекундами — datetime('now') округляет до секунды,
# а нам нужно ловить интервалы меньше секунды
NOW_MS = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

REASON_TEXT = {
    "speed": "слишком быстрые реакции — анкеты не просматриваются",
    "ratio": "лайки без единого пропуска — похоже на массовую накрутку",
}

WARNING = (
    "⚠️ <b>Похоже, анкеты листают слишком быстро</b>\n\n"
    "Так делают спам-боты, поэтому нужно ещё раз подтвердить, что вы человек.\n"
    "Нажмите /start и пройдите проверку.\n\n"
    "<i>Если повторится — доступ будет заблокирован автоматически.</i>"
)

BAN_REASON = "автоматическая блокировка: накрутка лайков"


@dataclass(slots=True)
class Verdict:
    reason: str      # 'speed' | 'ratio'
    strikes: int
    action: str      # 'captcha' | 'ban_temp' | 'ban_permanent'
    detail: str


async def _seconds_since_previous(user_id: int) -> float | None:
    """Сколько секунд прошло с прошлой реакции (None — она первая)."""
    value = await db.fetchval(
        "SELECT (julianday('now') - julianday(af_last_reaction)) * 86400.0 "
        "FROM users WHERE id = ? AND af_last_reaction IS NOT NULL",
        (user_id,),
    )
    return float(value) if value is not None else None


async def _like_ratio(user_id: int, window: int, after: int) -> tuple[int, float]:
    """Доля лайков среди последних реакций, сделанных после прошлого нарушения.

    Отсечка `after` обязательна: без неё уже сработавшая серия продолжала бы
    считаться, и следующий страйк прилетал бы с первого же нажатия.
    """
    row = await db.fetchone(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN kind = 'like' THEN 1 ELSE 0 END) AS likes "
        "FROM (SELECT kind FROM reactions WHERE from_id = ? AND rowid > ? "
        "      ORDER BY rowid DESC LIMIT ?)",
        (user_id, after, window),
    )
    total = int(row["total"] or 0) if row else 0
    likes = int(row["likes"] or 0) if row else 0
    return total, (likes / total if total else 0.0)


async def inspect(user_id: int, settings: Settings) -> Verdict | None:
    """Записывает реакцию и возвращает вердикт, если сработала защита."""
    # На владельца бота защита не распространяется
    if not settings.antifraud_enabled or settings.is_admin(user_id):
        await db.execute(
            f"UPDATE users SET af_last_reaction = {NOW_MS} WHERE id = ?", (user_id,)
        )
        return None

    elapsed = await _seconds_since_previous(user_id)
    too_fast = elapsed is not None and elapsed < settings.af_fast_seconds

    await db.execute(
        f"UPDATE users SET af_last_reaction = {NOW_MS}, "
        "af_fast_streak = CASE WHEN ? THEN af_fast_streak + 1 ELSE 0 END "
        "WHERE id = ?",
        (1 if too_fast else 0, user_id),
    )

    row = await db.fetchone(
        "SELECT af_fast_streak, af_ratio_after FROM users WHERE id = ?", (user_id,)
    )
    streak = int(row["af_fast_streak"] or 0) if row else 0
    ratio_after = int(row["af_ratio_after"] or 0) if row else 0

    reason = detail = ""
    if streak >= settings.af_fast_streak:
        reason = "speed"
        detail = (f"{streak} реакций подряд быстрее "
                  f"{settings.af_fast_seconds:g} сек")
    else:
        total, ratio = await _like_ratio(user_id, settings.af_ratio_window,
                                         ratio_after)
        if total >= settings.af_ratio_window and ratio >= settings.af_ratio_threshold:
            reason = "ratio"
            detail = f"{ratio * 100:.0f}% лайков на последних {total} анкетах"

    if not reason:
        return None

    strikes = int(await db.fetchval(
        "SELECT af_strikes FROM users WHERE id = ?", (user_id,), default=0
    ) or 0) + 1
    # Сдвигаем отсечку: для следующего страйка нужна новая полная серия
    last_rowid = int(await db.fetchval(
        "SELECT COALESCE(MAX(rowid), 0) FROM reactions WHERE from_id = ?",
        (user_id,), default=0,
    ) or 0)
    await db.execute(
        "UPDATE users SET af_strikes = ?, af_fast_streak = 0, af_ratio_after = ? "
        "WHERE id = ?",
        (strikes, last_rowid, user_id),
    )

    if strikes == 1:
        action = "captcha"
    elif strikes == 2:
        action = "ban_temp"
    else:
        action = "ban_permanent"

    await mod_repo.log_event("antifraud", user_id, reason=reason, detail=detail,
                             strikes=strikes, action=action)
    await mod_repo.bump_counter("af_triggers")
    if action != "captcha":
        await mod_repo.bump_counter("af_autobans")
    return Verdict(reason=reason, strikes=strikes, action=action, detail=detail)


async def punish(bot: Bot, user_id: int, verdict: Verdict,
                 settings: Settings) -> None:
    """Применяет наказание и сообщает об этом администраторам."""
    user = await users_repo.get_user(user_id)
    username = (user["username"] if user else None) or "—"

    if verdict.action == "captcha":
        # Мягкая мера: сбрасываем проверку, бот попросит пройти капчу заново
        await users_repo.update_user(user_id, captcha_passed=0)
        await safe_send(bot, user_id, WARNING, rkb.RECHECK)
        note = "🔁 сброшена капча"
    else:
        until = None
        if verdict.action == "ban_temp":
            until = await users_repo.ban_until(f"+{settings.af_ban_hours} hours")
        await mod_repo.ban_user(user_id, None, BAN_REASON, until)
        await safe_send(
            bot, user_id,
            "🚫 <b>Доступ заблокирован автоматически</b>\n\n"
            f"Причина: {REASON_TEXT[verdict.reason]}.\n"
            + (f"Срок: до {until} (UTC).\n" if until else "Срок: бессрочно.\n")
            + f"\nЕсли считаете это ошибкой — {await appeal_contact()}.",
            rkb.REMOVE,
        )
        note = (f"🚫 бан на {settings.af_ban_hours} ч" if until
                else "🚫 бессрочный бан")

    await notify_admins(
        bot,
        f"🤖 <b>Антинакрутка</b> — {note}\n\n"
        f"Пользователь: <code>{user_id}</code> @{username}\n"
        f"Признак: {REASON_TEXT[verdict.reason]}\n"
        f"Детали: {verdict.detail}\n"
        f"Нарушение по счёту: <b>{verdict.strikes}</b>\n\n"
        f"Разбанить: <code>/unban {user_id}</code> · карточка: <code>/find {user_id}</code>",
    )
    log.info("Антинакрутка: %s -> %s (%s)", user_id, verdict.action, verdict.detail)


async def check(bot: Bot, user_id: int, settings: Settings) -> str | None:
    """Полный цикл: проверка и, если нужно, наказание. Возвращает действие."""
    verdict = await inspect(user_id, settings)
    if verdict is None:
        return None
    await punish(bot, user_id, verdict, settings)
    return verdict.action
