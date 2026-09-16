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
    for column in MIGRATIONS["users"]:
        old = re.sub(rf"^\s*{column}\s+[^\n]*\n", "", old, flags=re.M)

    path = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(path)
    con.executescript(old)
    con.execute(
        "INSERT INTO users (id, username, name, age, registered) "
        "VALUES (1, 'oldie', 'Старожил', 30, 1)"
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
