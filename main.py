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
from app import handlers, middlewares
from app.config import Settings, get_settings
from app.db import moderation as mod_repo
from app.db.database import db
from app.services import commands as bot_commands
from app.services import reengagement
from app.services.notify import safe_send

log = logging.getLogger("bot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def build_storage(settings: Settings) -> RedisStorage:
    """FSM-хранилище в Redis: состояние переживает перезапуск и общее
    для нескольких воркеров."""
    storage = RedisStorage.from_url(
        settings.redis_url,
        # with_destiny обязателен: экран (services/screen.py) хранится в своей
        # «destiny», отдельно от диалога, и не должен с ним смешиваться
        key_builder=DefaultKeyBuilder(
            prefix=settings.redis_prefix, with_bot_id=True, with_destiny=True
        ),
        # Незаконченный диалог живёт сутки, потом чистится сам. Данные — двое
        # суток: столько Telegram разрешает удалять сообщения, так что экран,
        # который бот помнит, всё ещё можно убрать
        state_ttl=86_400,
        data_ttl=172_800,
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

    # Закрываем всё в finally при любой ошибке старта: поток aiosqlite не
    # фоновый, и незакрытая база держит процесс живым после трейсбека —
    # бот висит «запущенным», а systemd не может его перезапустить
    storage: RedisStorage | None = None
    bot: Bot | None = None
    background: list[asyncio.Task] = []
    try:
        try:
            storage = await build_storage(settings)
        except RuntimeError as exc:
            print(f"\n❌ {exc}\n")
            raise SystemExit(1)

        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher(storage=storage)
        middlewares.setup(dp, settings)
        handlers.setup(dp)

        me = await bot.get_me()
        moderators = [
            int(row["id"]) for row in
            await db.fetchall("SELECT id FROM users WHERE is_moderator = 1")
        ]
        log.info("Бот @%s запущен. Админы: %s, модераторов: %s",
                 me.username, settings.admin_ids, len(moderators))
        await bot_commands.setup(bot, settings.admin_ids, moderators)

        background = [
            asyncio.create_task(housekeeping(bot)),
            asyncio.create_task(reengagement.loop(bot, settings)),
        ]
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        if storage is not None:
            await storage.close()
        await db.close()
        if bot is not None:
            await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено вручную")
