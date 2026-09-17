"""Лайки, дизлайки и взаимные симпатии."""
from __future__ import annotations

from app.db.database import db


async def add_reaction(from_id: int, to_id: int, kind: str,
                       note: str | None = None) -> bool:
    """Сохраняет реакцию. Возвращает True, если случилось совпадение."""
    await db.execute(
        "INSERT INTO reactions (from_id, to_id, kind, note) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(from_id, to_id) DO UPDATE SET kind = excluded.kind, "
        "note = COALESCE(excluded.note, reactions.note), "
        "created_at = datetime('now')",
        (from_id, to_id, kind, note),
    )
    await db.execute("UPDATE users SET views_count = views_count + 1 WHERE id = ?", (to_id,))

    if kind != "like":
        return False

    await db.execute(
        "UPDATE users SET likes_received = likes_received + 1 WHERE id = ?", (to_id,)
    )
    mutual = await db.fetchone(
        "SELECT 1 FROM reactions WHERE from_id = ? AND to_id = ? AND kind = 'like'",
        (to_id, from_id),
    )
    if mutual is None:
        return False

    a, b = sorted((from_id, to_id))
    await db.execute(
        "INSERT OR IGNORE INTO matches (user_a, user_b) VALUES (?, ?)", (a, b)
    )
    await db.execute(
        "UPDATE users SET matches_count = matches_count + 1 WHERE id IN (?, ?)",
        (from_id, to_id),
    )
    await db.execute(
        "UPDATE reactions SET is_seen = 1 WHERE (from_id = ? AND to_id = ?) "
        "OR (from_id = ? AND to_id = ?)",
        (from_id, to_id, to_id, from_id),
    )
    return True


async def has_reacted(from_id: int, to_id: int) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM reactions WHERE from_id = ? AND to_id = ?", (from_id, to_id)
    )
    return row is not None


async def liked_me(user_id: int, other_id: int) -> bool:
    """Лайкнул ли `other_id` нашего пользователя.

    Если да, ответный лайк — это не поиск, а ответ на чужую симпатию,
    и суточный лимит на него не распространяется.
    """
    row = await db.fetchone(
        "SELECT 1 FROM reactions WHERE from_id = ? AND to_id = ? AND kind = 'like'",
        (other_id, user_id),
    )
    return row is not None


async def get_note(from_id: int, to_id: int) -> str | None:
    """Текст, который отправитель приложил к лайку."""
    return await db.fetchval(
        "SELECT note FROM reactions WHERE from_id = ? AND to_id = ?",
        (from_id, to_id),
    )


async def mark_like_seen(to_id: int, from_id: int) -> None:
    await db.execute(
        "UPDATE reactions SET is_seen = 1 WHERE from_id = ? AND to_id = ?",
        (from_id, to_id),
    )


async def reset_dislikes(user_id: int, older_than_days: int = 14) -> int:
    """Возвращает в выдачу тех, кого пропустили давно — анкеты не бесконечны."""
    cur = await db.conn.execute(
        "DELETE FROM reactions WHERE from_id = ? AND kind = 'dislike' "
        "AND created_at < datetime('now', ?)",
        (user_id, f"-{int(older_than_days)} days"),
    )
    await db.conn.commit()
    return cur.rowcount or 0
