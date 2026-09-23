"""Проверка миграции: база прежней версии должна открываться без потерь.

Новые поля появляются в проекте со временем, а CREATE TABLE IF NOT EXISTS
ничего не меняет в уже созданной таблице. Тест имитирует старую схему
(та же, но без добавленных позже колонок) и убеждается, что подключение
дописывает недостающее и не трогает данные.

Запуск: python3 tests/migration_test.py
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.database import MIGRATIONS, SCHEMA, db   # noqa: E402

failed = 0


def check(condition: bool, label: str) -> None:
    global failed
    print(f"  {'✅' if condition else '❌'} {label}")
    if not condition:
        failed += 1


def make_old_database() -> Path:
    """Схема без колонок, добавленных после первого релиза."""
    old = SCHEMA
    for columns in MIGRATIONS.values():
        for column in columns:
            old = re.sub(rf"^\s*{column}\s+[^\n]*\n", "", old, flags=re.M)

    path = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(path)
    con.executescript(old)
    con.execute(
        "INSERT INTO users (id, username, name, age, registered) "
        "VALUES (1, 'oldie', 'Старожил', 30, 1)"
    )
    # Заявка прежней версии: фото с кодом на листе, ждёт администратора
    con.execute(
        "INSERT INTO verifications (user_id, code, media_type, media_id) "
        "VALUES (1, 'K7M2', 'photo', 'old-photo')"
    )
    con.commit()
    con.close()
    return path


async def main() -> int:
    path = make_old_database()
    print("\n\033[1mМиграция базы прежней версии\033[0m")

    await db.connect(path)
    row = await db.fetchone("SELECT * FROM users WHERE id = 1")
    columns = set(row.keys())

    check(set(MIGRATIONS["users"]) <= columns, "все новые колонки добавлены")
    check(row["username"] == "oldie" and row["name"] == "Старожил",
          "старые данные не пострадали")
    check(row["notify_enabled"] == 1, "у новых колонок проставлены значения по умолчанию")
    check(row["af_strikes"] == 0, "счётчики антинакрутки обнулены")

    for table, added in MIGRATIONS.items():
        async with db.conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {r[1] for r in await cur.fetchall()}
        check(set(added) <= existing, f"в {table} добавлены новые колонки")
    old_request = await db.fetchone("SELECT * FROM verifications WHERE user_id = 1")
    check(old_request["code"] == "K7M2" and old_request["media_id"] == "old-photo"
          and old_request["action"] is None,
          "заявка на верификацию прежней версии сохранилась")

    indexes = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
    )
    names = {r["name"] for r in indexes}
    check("idx_users_notify" in names, "индекс по новой колонке создан")

    # Повторное подключение не должно ничего ломать
    await db.close()
    await db.connect(path)
    check(await db.fetchval("SELECT COUNT(*) FROM users") == 1,
          "повторное подключение проходит без ошибок")
    await db.close()

    print(f"\n\033[1m{'Готово' if not failed else f'Ошибок: {failed}'}\033[0m")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
