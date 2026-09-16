"""Рекламные посты, которые бот показывает между анкетами."""
from __future__ import annotations

import aiosqlite

from app.db.database import db


async def create(*, title: str, src_chat_id: int, src_message_id: int,
                 preview: str, button_text: str | None, button_url: str | None,
                 every_n: int, created_by: int) -> int:
    return await db.insert(
        "INSERT INTO ads (title, src_chat_id, src_message_id, preview, "
        "button_text, button_url, every_n, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, src_chat_id, src_message_id, preview, button_text, button_url,
         every_n, created_by),
    )


async def get(ad_id: int) -> aiosqlite.Row | None:
    return await db.fetchone("SELECT * FROM ads WHERE id = ?", (ad_id,))


async def list_all() -> list[aiosqlite.Row]:
    return await db.fetchall("SELECT * FROM ads ORDER BY is_active DESC, id DESC")


async def set_active(ad_id: int, active: bool) -> None:
    await db.execute("UPDATE ads SET is_active = ? WHERE id = ?",
                     (int(active), ad_id))


async def delete(ad_id: int) -> None:
    await db.execute("DELETE FROM ads WHERE id = ?", (ad_id,))


async def pick_next() -> aiosqlite.Row | None:
    """Следующий пост в ротации — тот, что показывали реже остальных."""
    return await db.fetchone(
        "SELECT * FROM ads WHERE is_active = 1 ORDER BY shows, id LIMIT 1"
    )


async def bump_shows(ad_id: int) -> None:
    await db.execute("UPDATE ads SET shows = shows + 1 WHERE id = ?", (ad_id,))


async def total_shows() -> int:
    return int(await db.fetchval("SELECT COALESCE(SUM(shows), 0) FROM ads",
                                 default=0) or 0)


async def count_active() -> int:
    return int(await db.fetchval("SELECT COUNT(*) FROM ads WHERE is_active = 1",
                                 default=0) or 0)
