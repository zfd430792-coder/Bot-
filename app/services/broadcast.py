"""Массовая рассылка с учётом лимитов Telegram."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)

from app.db import users as users_repo
from app.db.database import db

log = logging.getLogger(__name__)

# Telegram разрешает примерно 30 сообщений в секунду; держимся ниже с запасом
DELAY_BETWEEN_SENDS = 0.05
PROGRESS_EVERY = 25

_tasks: set[asyncio.Task] = set()


@dataclass(slots=True)
class Progress:
    total: int
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    started: float = field(default_factory=time.monotonic)

    def render(self, done: bool = False) -> str:
        elapsed = int(time.monotonic() - self.started)
        processed = self.sent + self.failed + self.blocked
        percent = int(processed / self.total * 100) if self.total else 100
        filled = percent // 10
        bar = "█" * filled + "░" * (10 - filled)
        title = "✅ <b>Рассылка завершена</b>" if done else "📢 <b>Рассылка идёт…</b>"
        return (
            f"{title}\n\n"
            f"<code>{bar}</code> {percent}%\n\n"
            f"✅ Доставлено: <b>{self.sent}</b>\n"
            f"🚫 Заблокировали бота: {self.blocked}\n"
            f"⚠️ Ошибок: {self.failed}\n"
            f"👥 Всего получателей: {self.total}\n"
            f"⏱ Прошло: {elapsed // 60} мин {elapsed % 60} сек"
        )


async def _deliver(bot: Bot, user_id: int, from_chat_id: int,
                   message_id: int, progress: Progress) -> None:
    try:
        await bot.copy_message(user_id, from_chat_id, message_id)
        progress.sent += 1
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            await bot.copy_message(user_id, from_chat_id, message_id)
            progress.sent += 1
        except Exception:
            progress.failed += 1
    except TelegramForbiddenError:
        # Бот заблокирован — прячем анкету, иначе людям пишут «в пустоту»
        progress.blocked += 1
        await users_repo.update_user(user_id, is_active=0)
    except TelegramBadRequest as exc:
        log.warning("Рассылка: %s -> %s", user_id, exc)
        progress.failed += 1
    except Exception as exc:
        log.warning("Рассылка: неожиданная ошибка для %s: %s", user_id, exc)
        progress.failed += 1


async def run(bot: Bot, broadcast_id: int, user_ids: list[int],
              from_chat_id: int, message_id: int,
              status_chat_id: int, status_message_id: int) -> Progress:
    progress = Progress(total=len(user_ids))

    for index, user_id in enumerate(user_ids, 1):
        await _deliver(bot, user_id, from_chat_id, message_id, progress)
        await asyncio.sleep(DELAY_BETWEEN_SENDS)

        if index % PROGRESS_EVERY == 0:
            await db.execute(
                "UPDATE broadcasts SET sent = ?, failed = ?, blocked = ? WHERE id = ?",
                (progress.sent, progress.failed, progress.blocked, broadcast_id),
            )
            try:
                await bot.edit_message_text(
                    progress.render(), chat_id=status_chat_id,
                    message_id=status_message_id,
                )
            except TelegramBadRequest:
                pass

    await db.execute(
        "UPDATE broadcasts SET sent = ?, failed = ?, blocked = ?, status = 'done', "
        "finished_at = datetime('now') WHERE id = ?",
        (progress.sent, progress.failed, progress.blocked, broadcast_id),
    )
    try:
        await bot.edit_message_text(
            progress.render(done=True), chat_id=status_chat_id,
            message_id=status_message_id,
        )
    except TelegramBadRequest:
        pass
    return progress


def schedule(bot: Bot, **kwargs) -> None:
    """Запускает рассылку фоном — бот продолжает отвечать людям."""
    task = asyncio.create_task(run(bot, **kwargs))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
