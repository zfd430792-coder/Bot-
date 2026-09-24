"""Баны, жалобы, верификация и журнал событий."""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from app.db.database import db


# ─────────────────────────────── События ────────────────────────────────────

async def log_event(kind: str, user_id: int | None = None, **payload: Any) -> None:
    await db.execute(
        "INSERT INTO events (kind, user_id, payload) VALUES (?, ?, ?)",
        (kind, user_id, json.dumps(payload, ensure_ascii=False) if payload else None),
    )


# ──────────────────────────────── Баны ──────────────────────────────────────

async def ban_user(user_id: int, admin_id: int | None, reason: str,
                   until: str | None = None) -> None:
    await db.execute(
        "UPDATE users SET is_banned = 1, ban_reason = ?, banned_until = ?, "
        "is_active = 0 WHERE id = ?",
        (reason, until, user_id),
    )
    await db.execute(
        "INSERT INTO bans (user_id, admin_id, reason, until) VALUES (?, ?, ?, ?)",
        (user_id, admin_id, reason, until),
    )
    await log_event("ban", user_id, admin_id=admin_id, reason=reason, until=until)


async def unban_user(user_id: int, admin_id: int | None = None) -> None:
    await db.execute(
        "UPDATE users SET is_banned = 0, ban_reason = NULL, banned_until = NULL, "
        "is_active = 1 WHERE id = ?",
        (user_id,),
    )
    await db.execute("UPDATE bans SET is_active = 0 WHERE user_id = ?", (user_id,))
    await log_event("unban", user_id, admin_id=admin_id)


async def expire_temporary_bans() -> list[int]:
    """Снимает временные баны, у которых истёк срок. Возвращает разбаненных."""
    rows = await db.fetchall(
        "SELECT id FROM users WHERE is_banned = 1 AND banned_until IS NOT NULL "
        "AND banned_until <= datetime('now')"
    )
    ids = [int(r["id"]) for r in rows]
    for user_id in ids:
        await unban_user(user_id)
    return ids


# ──────────────────────────────── Жалобы ────────────────────────────────────

async def add_report(from_id: int, target_id: int, reason: str,
                     comment: str | None = None) -> int:
    report_id = await db.insert(
        "INSERT INTO reports (from_id, target_id, reason, comment) VALUES (?, ?, ?, ?)",
        (from_id, target_id, reason, comment),
    )
    await db.execute(
        "UPDATE users SET reports_count = reports_count + 1 WHERE id = ?", (target_id,)
    )
    await log_event("report", target_id, from_id=from_id, reason=reason)
    return report_id


async def already_reported(from_id: int, target_id: int) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM reports WHERE from_id = ? AND target_id = ? AND status = 'open'",
        (from_id, target_id),
    )
    return row is not None


async def close_report(report_id: int, admin_id: int, status: str) -> None:
    await db.execute(
        "UPDATE reports SET status = ?, handled_by = ?, handled_at = datetime('now') "
        "WHERE id = ?",
        (status, admin_id, report_id),
    )


async def close_reports_for(target_id: int, admin_id: int, status: str = "done") -> None:
    await db.execute(
        "UPDATE reports SET status = ?, handled_by = ?, handled_at = datetime('now') "
        "WHERE target_id = ? AND status = 'open'",
        (status, admin_id, target_id),
    )


async def get_report(report_id: int) -> aiosqlite.Row | None:
    return await db.fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))


async def open_reports(limit: int = 20) -> list[aiosqlite.Row]:
    return await db.fetchall(
        "SELECT r.*, u.username AS target_username, u.name AS target_name "
        "FROM reports r LEFT JOIN users u ON u.id = r.target_id "
        "WHERE r.status = 'open' ORDER BY r.created_at LIMIT ?",
        (limit,),
    )


async def count_open_reports() -> int:
    return int(await db.fetchval(
        "SELECT COUNT(*) FROM reports WHERE status = 'open'", default=0
    ))


# ───────────────────────────── Верификация ──────────────────────────────────

async def create_verification(user_id: int, forced: bool,
                              requested_by: int | None) -> int:
    """Новая заявка. Задание (код) выдаётся позже, когда человек
    садится записывать кружок: код живёт недолго и не должен истечь, пока
    человек ещё не открыл бота."""
    await db.execute(
        "UPDATE verifications SET status = 'rejected', review_note = 'заменена новой' "
        "WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )
    return await db.insert(
        "INSERT INTO verifications (user_id, code, forced, requested_by, status) "
        "VALUES (?, '', ?, ?, 'pending')",
        (user_id, int(forced), requested_by),
    )


async def current_verification(user_id: int) -> aiosqlite.Row | None:
    """Открытая заявка человека. task_age — сколько секунд назад выдано
    задание (NULL, пока не выдано)."""
    return await db.fetchone(
        "SELECT *, CAST(strftime('%s', 'now') - strftime('%s', issued_at) AS INTEGER) "
        "AS task_age FROM verifications WHERE user_id = ? AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


async def awaiting_review(user_id: int) -> bool:
    """Кружок прислан и ждёт администратора."""
    row = await db.fetchone(
        "SELECT 1 FROM verifications WHERE user_id = ? AND status = 'pending' "
        "AND media_id IS NOT NULL",
        (user_id,),
    )
    return row is not None


async def issue_verification_task(verification_id: int, code: str) -> None:
    await db.execute(
        "UPDATE verifications SET code = ?, action = NULL, issued_at = datetime('now') "
        "WHERE id = ?",
        (code, verification_id),
    )


async def attach_verification_media(verification_id: int, media_type: str,
                                    media_id: str) -> None:
    await db.execute(
        "UPDATE verifications SET media_type = ?, media_id = ? WHERE id = ?",
        (media_type, media_id, verification_id),
    )


async def get_verification(verification_id: int) -> aiosqlite.Row | None:
    return await db.fetchone("SELECT * FROM verifications WHERE id = ?", (verification_id,))


async def review_verification(verification_id: int, admin_id: int, approved: bool,
                              note: str | None = None) -> None:
    await db.execute(
        "UPDATE verifications SET status = ?, reviewed_by = ?, review_note = ?, "
        "reviewed_at = datetime('now') WHERE id = ?",
        ("approved" if approved else "rejected", admin_id, note, verification_id),
    )


async def pending_verifications(limit: int = 20) -> list[aiosqlite.Row]:
    return await db.fetchall(
        "SELECT v.*, u.username, u.name FROM verifications v "
        "JOIN users u ON u.id = v.user_id "
        "WHERE v.status = 'pending' AND v.media_id IS NOT NULL "
        "ORDER BY v.created_at LIMIT ?",
        (limit,),
    )


async def count_pending_verifications() -> int:
    return int(await db.fetchval(
        "SELECT COUNT(*) FROM verifications WHERE status = 'pending' AND media_id IS NOT NULL",
        default=0,
    ))


# ───────────────────────── Настройки бота (runtime) ─────────────────────────

async def get_setting(key: str, default: str | None = None) -> str | None:
    value = await db.fetchval("SELECT value FROM bot_settings WHERE key = ?", (key,))
    return default if value is None else str(value)


async def set_setting(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def bump_counter(key: str, amount: int = 1) -> None:
    """Накопительный счётчик для статистики (автобаны, напоминания)."""
    await db.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(CAST(bot_settings.value AS INTEGER) + ? AS TEXT)",
        (key, str(amount), amount),
    )


async def get_int_setting(key: str, default: int) -> int:
    raw = await get_setting(key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


async def support_username() -> str:
    """Контакт поддержки без «@». Пустая строка — ещё не указан."""
    return (await get_setting("support", "") or "").strip().lstrip("@")


async def verify_example() -> str:
    """file_id кружка-примера для верификации. Пустая строка — не загружен."""
    return (await get_setting("verify_example", "") or "").strip()
