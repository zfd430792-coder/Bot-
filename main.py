"""Точка входа бота знакомств.

Запуск:  python3 main.py
Перед первым запуском: cp .env.example .env и заполнить BOT_TOKEN + ADMIN_IDS.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app import handlers, middlewares
from app.config import Settings, get_settings
from app.db import moderation as mod_repo
from app.db.database import db
from app.services import reengagement
from app.services.notify import safe_send

log = logging.getLogger("bot")

USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="search", description="Смотреть анкеты"),
    BotCommand(command="profile", description="Моя анкета"),
    BotCommand(command="settings", description="Настройки поиска"),
    BotCommand(command="help", description="Помощь и безопасность"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Админ-панель"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="broadcast", description="Рассылка"),
    BotCommand(command="find", description="Найти пользователя"),
    BotCommand(command="ban", description="Забанить"),
    BotCommand(command="unban", description="Разбанить"),
    BotCommand(command="verify", description="Запросить верификацию"),
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def set_commands(bot: Bot, admin_ids: list[int]) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as exc:
            log.warning("Не удалось задать команды админу %s: %s", admin_id, exc)


async def build_storage(settings: Settings) -> RedisStorage:
    """FSM-хранилище в Redis: состояние переживает перезапуск и общее
    для нескольких воркеров."""
    storage = RedisStorage.from_url(
        settings.redis_url,
        key_builder=DefaultKeyBuilder(
            prefix=settings.redis_prefix, with_bot_id=True, with_destiny=True
        ),
        # Незаконченный диалог живёт сутки, потом чистится сам
        state_ttl=86_400,
        data_ttl=86_400,
    )
    try:
        await storage.redis.ping()
    except Exception as exc:
        raise RuntimeError(
            f"Redis недоступен по адресу {settings.redis_url}: {exc}\n"
            "Запустите сервер (например: docker run -d -p 6379:6379 redis:7-alpine) "
            "или укажите другой REDIS_URL в .env."
        ) from exc
    log.info("Redis подключён: %s", settings.redis_url)
    return storage


async def housekeeping(bot: Bot) -> None:
    """Раз в 10 минут снимает истёкшие временные баны."""
    while True:
        try:
            unbanned = await mod_repo.expire_temporary_bans()
            for user_id in unbanned:
                await safe_send(
                    bot, user_id,
                    "✅ <b>Срок блокировки истёк</b>\n\nМожно снова пользоваться ботом.",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Фоновая уборка: %s", exc)
        await asyncio.sleep(600)


async def main() -> None:
    setup_logging()
    try:
        settings = get_settings()
    except RuntimeError as exc:
        print(f"\n❌ {exc}\n")
        raise SystemExit(1)

    await db.connect(settings.db_path)

    try:
        storage = await build_storage(settings)
    except RuntimeError as exc:
        print(f"\n❌ {exc}\n")
        await db.close()
        raise SystemExit(1)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)
    middlewares.setup(dp, settings)
    handlers.setup(dp)

    me = await bot.get_me()
    log.info("Бот @%s запущен. Админы: %s", me.username, settings.admin_ids)
    await set_commands(bot, settings.admin_ids)

    background = [
        asyncio.create_task(housekeeping(bot)),
        asyncio.create_task(reengagement.loop(bot, settings)),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await storage.close()
        await db.close()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено вручную")
