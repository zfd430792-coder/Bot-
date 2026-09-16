"""Состояние капчи: попытки и временные блокировки."""
from __future__ import annotations

import aiosqlite

from app.db.database import db


async def get_state(user_id: int) -> aiosqlite.Row | None:
    return await db.fetchone("SELECT * FROM captcha_state WHERE user_id = ?", (user_id,))


async def blocked_seconds(user_id: int) -> int:
    """Сколько секунд осталось до конца блокировки (0 — не заблокирован)."""
    value = await db.fetchval(
        "SELECT CAST(strftime('%s', blocked_until) - strftime('%s', 'now') AS INTEGER) "
        "FROM captcha_state WHERE user_id = ? AND blocked_until > datetime('now')",
        (user_id,), default=0,
    )
    return max(0, int(value or 0))


async def register_fail(user_id: int, max_attempts: int, block_minutes: int) -> int:
    """Учитывает провал. Возвращает количество оставшихся попыток."""
    await db.execute(
        "INSERT INTO captcha_state (user_id, fails, total_fails) VALUES (?, 1, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET fails = fails + 1, "
        "total_fails = total_fails + 1, updated_at = datetime('now')",
        (user_id,),
    )
    fails = int(await db.fetchval(
        "SELECT fails FROM captcha_state WHERE user_id = ?", (user_id,), default=0
    ))
    left = max(0, max_attempts - fails)
    if left == 0:
        await db.execute(
            "UPDATE captcha_state SET blocked_until = datetime('now', ?), fails = 0 "
            "WHERE user_id = ?",
            (f"+{int(block_minutes)} minutes", user_id),
        )
    return left


async def register_pass(user_id: int) -> None:
    await db.execute(
        "INSERT INTO captcha_state (user_id, passes) VALUES (?, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET passes = passes + 1, fails = 0, "
        "blocked_until = NULL, updated_at = datetime('now')",
        (user_id,),
    )


async def attempts_used(user_id: int) -> int:
    return int(await db.fetchval(
        "SELECT fails FROM captcha_state WHERE user_id = ?", (user_id,), default=0
    ))
