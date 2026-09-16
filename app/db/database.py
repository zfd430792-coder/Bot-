"""Подключение к SQLite, схема и общие помощники."""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT,
    tg_name         TEXT,

    -- онбординг
    captcha_passed  INTEGER NOT NULL DEFAULT 0,
    rules_accepted  INTEGER NOT NULL DEFAULT 0,
    registered      INTEGER NOT NULL DEFAULT 0,

    -- анкета
    name            TEXT,
    gender          TEXT,                       -- 'm' | 'f'
    looking_for     TEXT,                       -- 'm' | 'f' | 'any'
    age             INTEGER,
    about           TEXT,
    media_type      TEXT,                       -- 'photo' | 'video' | 'video_note'
    media_id        TEXT,

    -- география
    city            TEXT,
    region          TEXT,
    country         TEXT,
    lat             REAL,
    lon             REAL,
    geo_source      TEXT,                       -- 'city' | 'gps'
    search_scope    TEXT NOT NULL DEFAULT 'city',   -- 'city' | 'region' | 'near'
    search_radius   INTEGER NOT NULL DEFAULT 50,

    -- предпочтения поиска
    age_min         INTEGER,
    age_max         INTEGER,

    -- статусы
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    ban_reason      TEXT,
    banned_until    TEXT,
    verify_status   TEXT NOT NULL DEFAULT 'none', -- none|required|pending|verified|rejected
    verify_forced   INTEGER NOT NULL DEFAULT 0,   -- проверка запрошена админом (блокирует бота)
    verify_code     TEXT,
    verified_at     TEXT,

    -- счётчики
    likes_today     INTEGER NOT NULL DEFAULT 0,
    likes_date      TEXT,
    likes_received  INTEGER NOT NULL DEFAULT 0,
    matches_count   INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    reports_count   INTEGER NOT NULL DEFAULT 0,

    -- антинакрутка
    af_strikes      INTEGER NOT NULL DEFAULT 0,
    af_fast_streak  INTEGER NOT NULL DEFAULT 0,
    af_last_reaction TEXT,
    af_ratio_after  INTEGER NOT NULL DEFAULT 0,

    -- напоминания уснувшим
    notify_enabled  INTEGER NOT NULL DEFAULT 1,
    notify_count    INTEGER NOT NULL DEFAULT 0,
    last_notify_at  TEXT,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_active     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reactions (
    from_id     INTEGER NOT NULL,
    to_id       INTEGER NOT NULL,
    kind        TEXT NOT NULL,                  -- 'like' | 'dislike'
    is_seen     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS matches (
    user_a      INTEGER NOT NULL,               -- всегда меньший id
    user_b      INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_a, user_b)
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    comment     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | done | declined
    handled_by  INTEGER,
    handled_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    code        TEXT NOT NULL,
    media_type  TEXT,
    media_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    forced      INTEGER NOT NULL DEFAULT 0,      -- запрошена админом
    requested_by INTEGER,
    reviewed_by INTEGER,
    review_note TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS bans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    admin_id    INTEGER,
    reason      TEXT,
    until       TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    user_id     INTEGER,
    payload     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS captcha_state (
    user_id       INTEGER PRIMARY KEY,
    fails         INTEGER NOT NULL DEFAULT 0,
    total_fails   INTEGER NOT NULL DEFAULT 0,
    passes        INTEGER NOT NULL DEFAULT 0,
    blocked_until TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    audience    TEXT NOT NULL,
    preview     TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS geo_cache (
    query      TEXT PRIMARY KEY,
    name       TEXT,
    region     TEXT,
    country    TEXT,
    lat        REAL,
    lon        REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_users_search
    ON users (registered, is_active, is_banned, gender, age);
CREATE INDEX IF NOT EXISTS idx_users_city   ON users (country, city);
CREATE INDEX IF NOT EXISTS idx_users_region ON users (country, region);
CREATE INDEX IF NOT EXISTS idx_users_coords ON users (lat, lon);
CREATE INDEX IF NOT EXISTS idx_users_active ON users (last_active);
CREATE INDEX IF NOT EXISTS idx_users_notify
    ON users (registered, is_banned, notify_enabled, last_active);
CREATE INDEX IF NOT EXISTS idx_reactions_inbox ON reactions (to_id, kind, is_seen);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports (status, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_target ON reports (target_id);
CREATE INDEX IF NOT EXISTS idx_verifications_status ON verifications (status, created_at);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, created_at);
"""

EARTH_RADIUS_KM = 6371.0088


def haversine(lat1: float | None, lon1: float | None,
              lat2: float | None, lon2: float | None) -> float | None:
    """Расстояние между двумя точками в километрах."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# Поля, добавленные после первого релиза. Для уже созданных баз
# CREATE TABLE IF NOT EXISTS ничего не меняет, поэтому дописываем их вручную.
MIGRATIONS: dict[str, dict[str, str]] = {
    "users": {
        "af_strikes": "INTEGER NOT NULL DEFAULT 0",
        "af_fast_streak": "INTEGER NOT NULL DEFAULT 0",
        "af_last_reaction": "TEXT",
        "af_ratio_after": "INTEGER NOT NULL DEFAULT 0",
        "notify_enabled": "INTEGER NOT NULL DEFAULT 1",
        "notify_count": "INTEGER NOT NULL DEFAULT 0",
        "last_notify_at": "TEXT",
        "verify_forced": "INTEGER NOT NULL DEFAULT 0",
    },
}


class Database:
    """Тонкая обёртка над aiosqlite с ленивым подключением."""

    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None
        self.path: Path | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База не инициализирована — вызовите db.connect(path)")
        return self._conn

    async def connect(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Считать расстояние прямо в SQL — так поиск «рядом» делается одним запросом
        await self._conn.create_function("dist_km", 4, haversine, deterministic=True)
        # Порядок важен: сначала таблицы, потом недостающие колонки и только
        # затем индексы — иначе индекс по новой колонке упадёт на старой базе.
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate()
        await self._conn.executescript(INDEXES)
        await self._conn.commit()
        log.info("База данных готова: %s", path)

    async def _migrate(self) -> None:
        """Дописывает недостающие колонки в уже существующую базу."""
        for table, columns in MIGRATIONS.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for name, definition in columns.items():
                if name in existing:
                    continue
                await self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
                log.info("Миграция: в %s добавлена колонка %s", table, name)
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── базовые операции ────────────────────────────────────────────────────

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, params)
        await self.conn.commit()

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value


db = Database()
