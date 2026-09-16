"""Работа с пользователями и подбор анкет."""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

import aiosqlite

from app.db.database import db

# Поля, которые разрешено обновлять через update_user() — защита от опечаток
UPDATABLE = {
    "username", "tg_name", "captcha_passed", "rules_accepted", "registered",
    "name", "gender", "looking_for", "age", "about", "media_type", "media_id",
    "city", "region", "country", "lat", "lon", "geo_source", "search_scope",
    "search_radius", "age_min", "age_max", "is_active", "is_banned",
    "ban_reason", "banned_until", "verify_status", "verify_code", "verified_at",
    "verify_forced", "likes_today", "likes_date", "likes_received",
    "matches_count", "views_count", "reports_count",
    "notify_enabled", "notify_count", "last_notify_at",
    "af_strikes", "af_fast_streak", "af_ratio_after",
}


def today() -> str:
    return dt.date.today().isoformat()


async def ensure_user(user_id: int, username: str | None, tg_name: str | None) -> aiosqlite.Row:
    """Создаёт запись при первом обращении и всегда освежает username/активность."""
    # notify_count = 0: человек вернулся, серия напоминаний начинается заново
    await db.execute(
        "INSERT INTO users (id, username, tg_name) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, "
        "tg_name = excluded.tg_name, last_active = datetime('now'), "
        "notify_count = 0",
        (user_id, username, tg_name),
    )
    row = await get_user(user_id)
    assert row is not None
    return row


async def get_user(user_id: int) -> aiosqlite.Row | None:
    return await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


async def get_by_username(username: str) -> aiosqlite.Row | None:
    return await db.fetchone(
        "SELECT * FROM users WHERE lower(username) = lower(?)", (username.lstrip("@"),)
    )


async def find_user(query: str) -> aiosqlite.Row | None:
    """Поиск пользователя по ID или @username — используется в админке."""
    query = query.strip()
    if query.lstrip("-").isdigit():
        row = await get_user(int(query))
        if row:
            return row
    return await get_by_username(query)


async def update_user(user_id: int, **fields: Any) -> None:
    payload = {k: v for k, v in fields.items() if k in UPDATABLE}
    if not payload:
        return
    assignments = ", ".join(f"{k} = ?" for k in payload)
    await db.execute(
        f"UPDATE users SET {assignments} WHERE id = ?",
        (*payload.values(), user_id),
    )


async def mark_verified(user_id: int) -> None:
    await db.execute(
        "UPDATE users SET verify_status = 'verified', verify_forced = 0, "
        "verify_code = NULL, verified_at = datetime('now') WHERE id = ?",
        (user_id,),
    )


async def ban_until(offset: str) -> str | None:
    """Превращает смещение вида '+72 hours' в конкретную дату для бана."""
    return await db.fetchval("SELECT datetime('now', ?)", (offset,))


async def touch(user_id: int) -> None:
    await db.execute(
        "UPDATE users SET last_active = datetime('now') WHERE id = ?", (user_id,)
    )


async def delete_profile(user_id: int) -> None:
    """Удаляет анкету, но оставляет запись пользователя (чтобы бан/капча помнились)."""
    await db.execute(
        "UPDATE users SET registered = 0, is_active = 0, name = NULL, gender = NULL, "
        "looking_for = NULL, age = NULL, about = NULL, media_type = NULL, "
        "media_id = NULL, city = NULL, region = NULL, country = NULL, lat = NULL, "
        "lon = NULL, geo_source = NULL, verify_status = CASE WHEN verify_status = "
        "'verified' THEN 'none' ELSE verify_status END, verified_at = NULL "
        "WHERE id = ?",
        (user_id,),
    )
    await db.execute("DELETE FROM reactions WHERE from_id = ? OR to_id = ?", (user_id, user_id))
    await db.execute("DELETE FROM matches WHERE user_a = ? OR user_b = ?", (user_id, user_id))


# ─────────────────────────────── Лимит лайков ───────────────────────────────

async def likes_left(user: Mapping[str, Any], limit: int) -> int:
    if user["likes_date"] != today():
        return limit
    return max(0, limit - int(user["likes_today"] or 0))


async def consume_like(user_id: int, limit: int) -> bool:
    """Списывает один лайк из суточного лимита. False — лимит исчерпан."""
    row = await db.fetchone(
        "SELECT likes_today, likes_date FROM users WHERE id = ?", (user_id,)
    )
    if row is None:
        return False
    used = 0 if row["likes_date"] != today() else int(row["likes_today"] or 0)
    if used >= limit:
        return False
    await db.execute(
        "UPDATE users SET likes_today = ?, likes_date = ? WHERE id = ?",
        (used + 1, today(), user_id),
    )
    return True


# ───────────────────────────── Подбор анкет ─────────────────────────────────

def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Грубая рамка вокруг точки — чтобы SQLite не считал расстояние для всех."""
    import math

    d_lat = radius_km / 111.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    d_lon = radius_km / (111.0 * cos_lat)
    return lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon


async def search_candidates(user: Mapping[str, Any], limit: int = 25) -> list[aiosqlite.Row]:
    """Возвращает подходящие анкеты с учётом пола, возраста и географии.

    Порядок: сначала те, кто уже поставил нам ❤️ (быстрее случаются совпадения),
    затем — по расстоянию (режим «рядом») или по свежести активности.
    """
    scope = user["search_scope"] or "city"
    params: dict[str, Any] = {
        "me": user["id"],
        "my_gender": user["gender"],
        "want": user["looking_for"] or "any",
        "age_min": user["age_min"] or 18,
        "age_max": user["age_max"] or 99,
        "my_age": user["age"],
        "lat": user["lat"],
        "lon": user["lon"],
        "limit": limit,
    }

    where = [
        "u.id != :me",
        "u.registered = 1",
        "u.is_active = 1",
        "u.is_banned = 0",
        "u.verify_forced = 0",
        "(:want = 'any' OR u.gender = :want)",
        "(u.looking_for = 'any' OR u.looking_for = :my_gender)",
        "u.age BETWEEN :age_min AND :age_max",
        ":my_age BETWEEN COALESCE(u.age_min, 18) AND COALESCE(u.age_max, 99)",
        "NOT EXISTS (SELECT 1 FROM reactions r WHERE r.from_id = :me AND r.to_id = u.id)",
    ]

    order_geo = "u.last_active DESC"
    if scope == "near" and user["lat"] is not None and user["lon"] is not None:
        radius = int(user["search_radius"] or 50)
        lat_min, lat_max, lon_min, lon_max = _bbox(user["lat"], user["lon"], radius)
        params.update(
            lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,
            radius=radius,
        )
        where += [
            "u.lat BETWEEN :lat_min AND :lat_max",
            "u.lon BETWEEN :lon_min AND :lon_max",
            "dist_km(u.lat, u.lon, :lat, :lon) <= :radius",
        ]
        order_geo = "distance ASC"
    elif scope == "region" and user["region"]:
        params.update(region=user["region"], country=user["country"])
        where += ["u.region = :region", "u.country IS :country"]
    elif user["city"]:
        params.update(city=user["city"], country=user["country"])
        where += ["u.city = :city", "u.country IS :country"]

    sql = f"""
        SELECT u.*,
               EXISTS(SELECT 1 FROM reactions r2
                      WHERE r2.from_id = u.id AND r2.to_id = :me AND r2.kind = 'like')
                   AS liked_me,
               dist_km(u.lat, u.lon, :lat, :lon) AS distance
        FROM users u
        WHERE {' AND '.join(where)}
        ORDER BY liked_me DESC, {order_geo}
        LIMIT :limit
    """
    return await db.fetchall(sql, params)


async def incoming_likes(user_id: int, limit: int = 25) -> list[aiosqlite.Row]:
    """Анкеты тех, кто лайкнул нас, а мы ещё не ответили."""
    return await db.fetchall(
        """
        SELECT u.*, r.created_at AS liked_at,
               dist_km(u.lat, u.lon,
                       (SELECT lat FROM users WHERE id = :me),
                       (SELECT lon FROM users WHERE id = :me)) AS distance
        FROM reactions r
        JOIN users u ON u.id = r.from_id
        WHERE r.to_id = :me AND r.kind = 'like'
          AND u.is_banned = 0 AND u.is_active = 1 AND u.registered = 1
          AND NOT EXISTS (SELECT 1 FROM reactions r2
                          WHERE r2.from_id = :me AND r2.to_id = u.id)
        ORDER BY r.created_at DESC
        LIMIT :limit
        """,
        {"me": user_id, "limit": limit},
    )


async def count_incoming_likes(user_id: int) -> int:
    return int(await db.fetchval(
        """
        SELECT COUNT(*) FROM reactions r
        JOIN users u ON u.id = r.from_id
        WHERE r.to_id = ? AND r.kind = 'like' AND u.is_banned = 0 AND u.is_active = 1
          AND NOT EXISTS (SELECT 1 FROM reactions r2
                          WHERE r2.from_id = ? AND r2.to_id = u.id)
        """,
        (user_id, user_id), default=0,
    ))


async def get_matches(user_id: int, limit: int = 50) -> list[aiosqlite.Row]:
    return await db.fetchall(
        """
        SELECT u.*, m.created_at AS matched_at
        FROM matches m
        JOIN users u ON u.id = CASE WHEN m.user_a = :me THEN m.user_b ELSE m.user_a END
        WHERE (m.user_a = :me OR m.user_b = :me) AND u.is_banned = 0
        ORDER BY m.created_at DESC
        LIMIT :limit
        """,
        {"me": user_id, "limit": limit},
    )


async def audience_ids(audience: str, extra: str | None = None) -> list[int]:
    """Список получателей рассылки."""
    base = "SELECT id FROM users WHERE is_banned = 0"
    if audience == "all":
        sql, params = base, ()
    elif audience == "registered":
        sql, params = base + " AND registered = 1", ()
    elif audience == "active7":
        sql, params = base + " AND last_active >= datetime('now', '-7 days')", ()
    elif audience == "active30":
        sql, params = base + " AND last_active >= datetime('now', '-30 days')", ()
    elif audience == "sleeping":
        sql, params = base + " AND last_active < datetime('now', '-30 days')", ()
    elif audience == "male":
        sql, params = base + " AND gender = 'm'", ()
    elif audience == "female":
        sql, params = base + " AND gender = 'f'", ()
    elif audience == "city" and extra:
        sql, params = base + " AND lower(city) = lower(?)", (extra,)
    elif audience == "unfinished":
        sql, params = base + " AND registered = 0", ()
    else:
        sql, params = base, ()
    rows = await db.fetchall(sql, params)
    return [int(r["id"]) for r in rows]
