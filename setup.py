#!/usr/bin/env python3
"""Мастер настройки бота знакомств.

Спрашивает токен и админа по шагам, проверяет их на месте и собирает готовый
.env — все комментарии из .env.example при этом сохраняются.

Запускается сам из install.sh, но можно вызвать и отдельно, чтобы
перенастроить бота:  python3 setup.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"
ENV_EXAMPLE = BASE / ".env.example"

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")

WIDTH = max(54, min(66, shutil.get_terminal_size((70, 24)).columns - 4))
TOTAL_STEPS = 4


# ─────────────────────────────── Оформление ─────────────────────────────────

def dwidth(text: str) -> int:
    """Ширина строки в колонках терминала.

    len() здесь не годится: эмодзи вроде 💞 занимают две колонки, и рамки
    из-за этого разъезжаются.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - dwidth(text))
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


class Style:
    """ANSI-цвета с честной проверкой: в файл или в «тупой» терминал не пишем."""

    enabled = (
        sys.stdout.isatty()
        and os.environ.get("TERM", "") != "dumb"
        and "NO_COLOR" not in os.environ
    )

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def bold(cls, t: str) -> str:    return cls._wrap("1", t)
    @classmethod
    def dim(cls, t: str) -> str:     return cls._wrap("2", t)
    @classmethod
    def red(cls, t: str) -> str:     return cls._wrap("31", t)
    @classmethod
    def green(cls, t: str) -> str:   return cls._wrap("32", t)
    @classmethod
    def yellow(cls, t: str) -> str:  return cls._wrap("33", t)
    @classmethod
    def blue(cls, t: str) -> str:    return cls._wrap("36", t)
    @classmethod
    def pink(cls, t: str) -> str:    return cls._wrap("35", t)


S = Style


def framed(lines: list[tuple[str, str]], color) -> None:
    """Рамка вокруг набора строк: (текст, стиль)."""
    rule = "─" * (WIDTH - 2)
    print(color("╭" + rule + "╮"))
    for text, style in lines:
        body = pad(text, WIDTH - 2, "center")
        print(color("│") + (style(body) if style else body) + color("│"))
    print(color("╰" + rule + "╯"))


def banner() -> None:
    print()
    framed([
        ("", None),
        ("💞  БОТ ЗНАКОМСТВ", S.bold),
        ("мастер настройки", S.dim),
        ("", None),
    ], S.pink)
    print()


def step(number: int, title: str) -> None:
    label = f" Шаг {number} из {TOTAL_STEPS} "
    print()
    print(S.blue("┌" + label + "─" * (WIDTH - 2 - dwidth(label)) + "┐"))
    print(S.blue("│ ") + S.bold(pad(title, WIDTH - 4)) + S.blue(" │"))
    print(S.blue("└" + "─" * (WIDTH - 2) + "┘"))
    print()


def ok(text: str) -> None:      print(f"  {S.green('✔')}  {text}")
def fail(text: str) -> None:    print(f"  {S.red('✖')}  {text}")
def warn(text: str) -> None:    print(f"  {S.yellow('!')}  {text}")
def info(text: str) -> None:    print(f"  {S.blue('•')}  {text}")
def hint(text: str) -> None:    print(f"     {S.dim(text)}")


def prompt(label: str, default: str = "") -> str:
    suffix = f" {S.dim('[' + default + ']')}" if default else ""
    try:
        value = input(f"  {S.bold(label)}{suffix} {S.pink('›')} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        fail("Настройка прервана.")
        sys.exit(1)
    if not sys.stdin.isatty():
        print()          # при вводе из файла терминал не переводит строку сам
    return value or default


def confirm(label: str, default: bool = False) -> bool:
    options = "Y/n" if default else "y/N"
    while True:
        answer = prompt(f"{label} {S.dim('(' + options + ')')}").lower()
        if not answer:
            return default
        if answer in {"y", "yes", "д", "да"}:
            return True
        if answer in {"n", "no", "н", "нет"}:
            return False
        hint("Ответьте y (да) или n (нет).")


def choose(options: list[tuple[str, str]]) -> str:
    """Нумерованное меню. Возвращает ключ выбранного пункта."""
    print()
    for index, (title, _) in enumerate(options, 1):
        print(f"  {S.pink(str(index) + ')')} {title}")
    print()
    while True:
        raw = prompt("Ваш выбор", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        fail(f"Введите число от 1 до {len(options)}.")


class Spinner:
    """Крутилка на время сетевых запросов, чтобы не выглядело зависшим."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0

    def tick(self) -> None:
        if not S.enabled:
            return
        frame = self.FRAMES[self.index % len(self.FRAMES)]
        self.index += 1
        sys.stdout.write(f"\r  {S.pink(frame)}  {self.text}")
        sys.stdout.flush()

    def clear(self) -> None:
        if S.enabled:
            sys.stdout.write("\r" + " " * (len(self.text) + 8) + "\r")
            sys.stdout.flush()


# ──────────────────────────── Работа с Telegram ─────────────────────────────

def api_call(token: str, method: str, params: dict | None = None,
             timeout: int = 12) -> dict | None:
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        return payload if payload.get("ok") else None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error_code": 401}
        return None
    except Exception:
        return None


def check_token(token: str) -> dict | None:
    """Проверяет токен через getMe и возвращает данные бота."""
    spinner = Spinner("Проверяю токен…")
    spinner.tick()
    result = api_call(token, "getMe")
    spinner.clear()

    if result is None:
        return None
    if not result.get("ok"):
        return {"invalid": True}
    return result["result"]


def detect_admin(token: str, bot_username: str, seconds: int = 120) -> dict | None:
    """Ловит первое сообщение боту и забирает из него ID отправителя."""
    api_call(token, "deleteWebhook", {"drop_pending_updates": "true"})

    print()
    info(f"Откройте {S.bold('@' + bot_username)} и отправьте ему любое сообщение.")
    hint("Это самый надёжный способ — ID определится сам.")
    hint("Отмена — Ctrl+C.")
    print()

    spinner = Spinner("Жду сообщение…")
    deadline = time.monotonic() + seconds
    offset = 0

    try:
        while time.monotonic() < deadline:
            left = int(deadline - time.monotonic())
            spinner.text = f"Жду сообщение…  {S.dim(f'осталось {left} сек')}"
            spinner.tick()

            result = api_call(token, "getUpdates",
                              {"offset": offset, "timeout": 0, "limit": 10})
            for update in (result or {}).get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                sender = (message or {}).get("from")
                if sender and not sender.get("is_bot"):
                    spinner.clear()
                    return sender
            time.sleep(2)
    except KeyboardInterrupt:
        pass

    spinner.clear()
    return None


# ────────────────────────────────── Redis ───────────────────────────────────

def parse_redis_url(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or "localhost", parsed.port or 6379


def check_redis(url: str) -> bool:
    """PING напрямую через сокет — библиотека redis тут ещё может быть не нужна."""
    host, port = parse_redis_url(url)
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(b"PING\r\n")
            return sock.recv(64).startswith(b"+PONG")
    except Exception:
        return False


def try_start_redis() -> bool:
    """Поднимает Redis в докере — самый быстрый путь на чистой машине."""
    import subprocess

    spinner = Spinner("Поднимаю контейнер redis…")
    spinner.tick()
    command = [
        "docker", "run", "-d", "--name", "dating-redis",
        "--restart", "unless-stopped", "-p", "6379:6379", "redis:7-alpine",
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except Exception:
        spinner.clear()
        return False
    spinner.clear()

    if done.returncode != 0:
        # Возможно, контейнер уже есть — попробуем просто запустить его
        restart = subprocess.run(["docker", "start", "dating-redis"],
                                 capture_output=True, text=True)
        if restart.returncode != 0:
            fail("Docker не смог запустить Redis.")
            hint(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "")
            return False

    for _ in range(15):
        if check_redis("redis://localhost:6379/0"):
            return True
        time.sleep(1)
    return False


# ────────────────────────────── Файл .env ───────────────────────────────────

def read_existing() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    """Пишет .env на основе .env.example, сохраняя все пояснения."""
    if ENV_EXAMPLE.is_file():
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        lines = [f"{key}=" for key in values]

    used: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                used.add(key)
                continue
        out.append(line)

    leftover = [k for k in values if k not in used]
    if leftover:
        out += ["", "# Добавлено мастером настройки"]
        out += [f"{key}={values[key]}" for key in leftover]

    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)     # в файле лежит токен — чужим читать незачем
    except OSError:
        pass


def mask(token: str) -> str:
    if len(token) < 12:
        return "…"
    return f"{token[:8]}…{token[-4:]}"


# ──────────────────────────────── Шаги ──────────────────────────────────────

def ask_token(existing: str) -> tuple[str, str]:
    step(1, "Токен бота")
    info("Откройте @BotFather → /newbot → скопируйте выданный токен.")
    hint("Выглядит так: 8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    print()

    if existing:
        info(f"Сейчас записан токен {S.bold(mask(existing))}")
        if not confirm("Заменить его?", default=False):
            bot = check_token(existing)
            if bot and not bot.get("invalid"):
                ok(f"Бот: {S.bold('@' + bot['username'])}")
                return existing, bot["username"]
            warn("Прежний токен не отвечает — введите новый.")
        print()

    while True:
        token = prompt("Токен")
        if not token:
            fail("Без токена бот не запустится.")
            continue
        if not TOKEN_RE.match(token):
            fail("Не похоже на токен: ожидается вид 123456789:AA…")
            hint("Скопируйте строку целиком, без пробелов и кавычек.")
            continue

        bot = check_token(token)
        if bot is None:
            warn("Telegram не ответил — проверить токен не вышло.")
            if confirm("Использовать его всё равно?", default=True):
                return token, ""
            continue
        if bot.get("invalid"):
            fail("Telegram не принял этот токен.")
            hint("Проверьте, что он не отозван: @BotFather → /mybots → API Token")
            continue

        ok(f"Бот найден: {S.bold('@' + bot['username'])} "
           f"{S.dim('(' + bot.get('first_name', '') + ')')}")
        return token, bot["username"]


def ask_admins(token: str, bot_username: str, existing: str) -> str:
    step(2, "Администратор")
    info("Админ видит статистику, жалобы, рассылку и модерацию.")
    print()

    if existing:
        info(f"Сейчас записано: {S.bold(existing)}")
        if not confirm("Изменить?", default=False):
            return existing
        print()

    if bot_username and confirm("Определить ваш ID автоматически?", default=True):
        sender = detect_admin(token, bot_username)
        if sender:
            name = sender.get("first_name", "")
            username = sender.get("username")
            ok(f"Это вы: {S.bold(str(sender['id']))} "
               f"{S.dim('(' + name + (' @' + username if username else '') + ')')}")
            extra = prompt("Ещё админы через запятую (не обязательно)")
            ids = [str(sender["id"])] + [
                p.strip() for p in extra.replace(";", ",").split(",") if p.strip()
            ]
            return ",".join(dict.fromkeys(ids))
        warn("Сообщение так и не пришло — введите ID вручную.")
        print()

    info("Узнать свой ID: напишите @userinfobot, он пришлёт число.")
    print()
    while True:
        raw = prompt("ID администратора")
        ids = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if ids and all(p.lstrip("-").isdigit() for p in ids):
            ok("Админов: " + ", ".join(ids))
            return ",".join(dict.fromkeys(ids))
        fail("Нужно число, например 123456789. Несколько — через запятую.")


def ask_redis(existing: str) -> str:
    step(3, "Redis")
    info("В Redis хранятся незаконченные диалоги — без него бот не стартует.")
    print()

    url = existing or "redis://localhost:6379/0"
    docker_tried = False

    while True:
        spinner = Spinner(f"Проверяю {url}…")
        spinner.tick()
        available = check_redis(url)
        spinner.clear()

        if available:
            ok(f"Redis отвечает: {S.bold(url)}")
            return url

        fail(f"Redis не отвечает по адресу {url}")

        options: list[tuple[str, str]] = []
        if shutil.which("docker") and not docker_tried:
            options.append(("Запустить Redis в Docker  "
                            + S.dim("(проще всего)"), "docker"))
        options.append(("Указать другой адрес", "url"))
        options.append(("Записать как есть и поднять Redis позже", "skip"))
        action = choose(options)

        if action == "docker":
            docker_tried = True
            if try_start_redis():
                ok("Redis запущен в Docker (контейнер dating-redis)")
                return "redis://localhost:6379/0"
            fail("Не получилось — выберите другой вариант.")
            continue

        if action == "url":
            print()
            hint("Например: redis://localhost:6379/0")
            hint("С паролем: redis://:пароль@хост:6379/0")
            new_url = prompt("Адрес Redis", url)
            if new_url == url:
                warn("Адрес тот же — проверю его ещё раз.")
            url = new_url
            continue

        warn("Записал как есть. Поднимите Redis до запуска бота:")
        hint("docker run -d -p 6379:6379 --name dating-redis redis:7-alpine")
        hint("или: sudo apt install redis-server")
        return url


def ask_extras(existing: dict[str, str]) -> dict[str, str]:
    step(4, "Дополнительно")
    info("Всё остальное уже настроено разумно — этот шаг можно пропустить.")
    print()

    values: dict[str, str] = {}
    if not confirm("Настроить лимит лайков, возраст и чат для логов?", default=False):
        ok("Оставляю значения по умолчанию")
        return values

    print()
    while True:
        raw = prompt("Лимит лайков в сутки", existing.get("LIKES_LIMIT_PER_DAY", "50"))
        if raw.isdigit() and 1 <= int(raw) <= 1000:
            values["LIKES_LIMIT_PER_DAY"] = raw
            break
        fail("Нужно число от 1 до 1000.")

    while True:
        raw = prompt("Минимальный возраст анкеты", existing.get("MIN_AGE", "18"))
        if raw.isdigit() and 1 <= int(raw) <= 99:
            values["MIN_AGE"] = raw
            if int(raw) < 18:
                warn("Порог ниже 18 на сервисе знакомств — ваша ответственность.")
            break
        fail("Нужно число от 1 до 99.")

    print()
    info("Чат для журнала событий: новые анкеты, жалобы, баны, автоблокировки.")
    hint("Можно оставить пустым — тогда всё придёт первому админу в личку.")
    hint("Для канала: добавьте бота админом и укажите ID вида -1001234567890")
    raw = prompt("ID чата для логов", existing.get("LOG_CHAT_ID", ""))
    if raw:
        if raw.lstrip("-").isdigit():
            values["LOG_CHAT_ID"] = raw
        else:
            warn("Не похоже на ID чата — пропускаю.")
    return values


def summary(values: dict[str, str], bot_username: str) -> None:
    print()
    framed([("✔  Настройка завершена", S.bold)], S.green)
    print()

    rows = [
        ("Бот", f"@{bot_username}" if bot_username else "—"),
        ("Токен", mask(values["BOT_TOKEN"])),
        ("Админы", values["ADMIN_IDS"]),
        ("Redis", values["REDIS_URL"]),
        ("Лимит лайков", values.get("LIKES_LIMIT_PER_DAY", "50 (по умолчанию)")),
        ("Мин. возраст", values.get("MIN_AGE", "18 (по умолчанию)")),
        ("Чат логов", values.get("LOG_CHAT_ID") or "личка первого админа"),
        ("Файл настроек", str(ENV_FILE)),
    ]
    for label, value in rows:
        print(f"  {S.dim(label.ljust(16))} {value}")
    print()


def main() -> int:
    banner()

    if not ENV_EXAMPLE.is_file():
        fail("Рядом нет .env.example — запускайте мастер из папки проекта.")
        return 1

    existing = read_existing()
    if ENV_FILE.is_file():
        warn("Файл .env уже есть — значения можно оставить или изменить.")

    token, bot_username = ask_token(existing.get("BOT_TOKEN", ""))
    admins = ask_admins(token, bot_username, existing.get("ADMIN_IDS", ""))
    redis_url = ask_redis(existing.get("REDIS_URL", ""))

    values = {"BOT_TOKEN": token, "ADMIN_IDS": admins, "REDIS_URL": redis_url}
    values.update(ask_extras(existing))

    write_env(values)
    summary(values, bot_username)

    # install.sh печатает свои подсказки сам — не повторяемся
    if not os.environ.get("DATING_BOT_FROM_INSTALLER"):
        launcher = (".venv/bin/python main.py" if (BASE / ".venv").is_dir()
                    else "python3 main.py")
        print(f"  {S.bold('Запустить бота:')}")
        print(f"     {S.blue(launcher)}")
        print()
    if bot_username:
        print(f"  Потом откройте {S.bold('@' + bot_username)} и нажмите "
              f"{S.bold('/start')} 🎉")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
