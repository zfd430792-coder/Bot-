"""Проверка мастера настройки: рамки, диалог и итоговый .env.

Сеть и ввод подменяются заглушками, поэтому тест не трогает ни Telegram,
ни Redis, ни настоящий .env проекта.

Запуск: python3 tests/setup_test.py
"""
from __future__ import annotations

import builtins
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import setup  # noqa: E402

TOKEN = "8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
BOX_CHARS = "╭╰│┌└"

failed = 0


def check(condition: bool, label: str) -> None:
    global failed
    print(f"  {'✅' if condition else '❌'} {label}")
    if not condition:
        failed += 1


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


@contextlib.contextmanager
def scripted(answers: list[str]):
    """Подменяет ввод пользователя и собирает вывод мастера."""
    supply = iter(answers)
    original = builtins.input
    buffer = io.StringIO()
    builtins.input = lambda prompt="": next(supply)
    try:
        with contextlib.redirect_stdout(buffer):
            yield buffer
    finally:
        builtins.input = original


def sandbox() -> Path:
    """Отдельная папка с .env.example — настоящий .env не трогаем."""
    box = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / ".env.example", box / ".env.example")
    setup.ENV_FILE = box / ".env"
    setup.ENV_EXAMPLE = box / ".env.example"
    return box


def env_values(box: Path) -> dict[str, str]:
    values = {}
    for line in (box / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def fake_api(responses: dict):
    """Заглушка Telegram: отдаёт заранее заданные ответы по имени метода."""
    def call(token, method, params=None, timeout=12):
        return responses.get(method)
    return call


def main() -> int:
    # ── Рамки ───────────────────────────────────────────────────────────────
    section("Оформление")
    check(setup.dwidth("💞") == 2, "эмодзи считается за две колонки")
    check(setup.dwidth("Токен") == 5, "кириллица считается по одной колонке")
    check(setup.dwidth(setup.pad("💞 тест", 20)) == 20, "выравнивание учитывает ширину")

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        setup.banner()
        setup.step(1, "Токен бота")
        setup.framed([("✔  Настройка завершена", setup.S.bold)], setup.S.green)

    # Внимание: "" in BOX_CHARS истинно, поэтому пустые строки отсекаем явно
    widths = {
        setup.dwidth(line) for line in buffer.getvalue().splitlines()
        if line and line[0] in BOX_CHARS
    }
    check(len(widths) == 1, f"все строки рамок одной ширины (получилось {widths})")
    check(widths == {setup.WIDTH}, f"ширина равна заданной ({setup.WIDTH})")

    # ── Диалог: ввод админа вручную ─────────────────────────────────────────
    section("Мастер: ID администратора вручную")
    box = sandbox()
    setup.api_call = fake_api({
        "getMe": {"ok": True, "result": {"username": "my_dating_bot",
                                         "first_name": "Знакомства"}},
    })
    setup.check_redis = lambda url: True

    answers = [TOKEN, "n", "900001, 900002", "y", "70", "18", "-1001234567890"]
    with scripted(answers) as out:
        code = setup.main()
    text = out.getvalue()

    check(code == 0, "мастер завершился успешно")
    check("@my_dating_bot" in text, "показал найденного бота")
    values = env_values(box)
    check(values["BOT_TOKEN"] == TOKEN, "токен записан")
    check(values["ADMIN_IDS"] == "900001,900002", "несколько админов разделены запятой")
    check(values["LIKES_LIMIT_PER_DAY"] == "70", "лимит лайков записан")
    check(values["LOG_CHAT_ID"] == "-1001234567890", "чат логов записан")
    check(values["MAX_VIDEO_SECONDS"] == "15", "нетронутые настройки сохранили значения")
    check((box / ".env").read_text().count("#") > 20, "комментарии из примера на месте")
    check(oct((box / ".env").stat().st_mode)[-3:] == "600", "права на .env закрыты")
    check(TOKEN not in text.replace(TOKEN, "", 1), "в итоге токен показан в маске")

    # ── Диалог: ID определяется автоматически ───────────────────────────────
    section("Мастер: ID определяется автоматически")
    box = sandbox()
    setup.api_call = fake_api({
        "getMe": {"ok": True, "result": {"username": "my_dating_bot",
                                         "first_name": "Знакомства"}},
        "deleteWebhook": {"ok": True, "result": True},
        "getUpdates": {"ok": True, "result": [{
            "update_id": 5,
            "message": {"from": {"id": 777001, "is_bot": False,
                                 "first_name": "Иван", "username": "ivan"}},
        }]},
    })

    with scripted([TOKEN, "y", "", "n"]) as out:
        code = setup.main()
    text = out.getvalue()

    check(code == 0, "мастер завершился успешно")
    check("777001" in text, "ID пойман из сообщения боту")
    values = env_values(box)
    check(values["ADMIN_IDS"] == "777001", "определённый ID записан")
    check(values["REDIS_URL"].startswith("redis://"), "адрес Redis записан")

    # ── Повторный запуск: значения предлагаются заново ──────────────────────
    section("Повторный запуск")
    with scripted(["n", "n", "n"]) as out:
        code = setup.main()
    text = out.getvalue()
    check(code == 0, "повторный запуск проходит")
    check("уже есть" in text, "мастер заметил существующий .env")
    check(env_values(box)["ADMIN_IDS"] == "777001", "прежние значения сохранены")

    # ── Бот уже запущен: его копия забирает getUpdates ──────────────────────
    section("Мастер: бот уже запущен")
    box = sandbox()
    setup.api_call = fake_api({
        "getMe": {"ok": True, "result": {"username": "my_dating_bot",
                                         "first_name": "Знакомства"}},
        "getUpdates": {"ok": False, "error_code": 409},
    })

    with scripted([TOKEN, "y", "900001", "n"]) as out:
        code = setup.main()
    text = out.getvalue()

    check(code == 0, "мастер завершился успешно")
    check("уже запущен" in text, "объяснил, почему ID не ловится")
    check("systemctl stop" in text, "подсказал, как остановить бота")
    check(env_values(box)["ADMIN_IDS"] == "900001", "ID введён вручную и записан")

    # ── Отбраковка мусора ───────────────────────────────────────────────────
    section("Проверка ввода")
    check(not setup.TOKEN_RE.match("просто текст"), "текст вместо токена отбраковывается")
    check(not setup.TOKEN_RE.match("123:короткий"), "короткий токен отбраковывается")
    check(bool(setup.TOKEN_RE.match(TOKEN)), "настоящий токен проходит")

    box = sandbox()
    with scripted([TOKEN, "n", "не число", "900001", "n"]) as out:
        code = setup.main()
    check(code == 0 and env_values(box)["ADMIN_IDS"] == "900001",
          "нечисловой ID переспрашивается")

    print(f"\n\033[1m{'Готово' if not failed else f'Ошибок: {failed}'}\033[0m")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
