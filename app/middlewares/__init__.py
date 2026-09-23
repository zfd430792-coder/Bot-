"""Подключение middleware к диспетчеру."""
from __future__ import annotations

from aiogram import Dispatcher

from app.config import Settings
from app.middlewares.gates import AccessGateMiddleware
from app.middlewares.screen_ctx import ScreenMiddleware
from app.middlewares.throttling import ThrottlingMiddleware
from app.middlewares.user_ctx import UserContextMiddleware


def setup(dp: Dispatcher, settings: Settings) -> None:
    """Порядок: антифлуд -> загрузка пользователя -> проверки доступа
    (-> для кнопок: чей это экран)."""
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(ThrottlingMiddleware(
            rate=settings.throttle_seconds,
            # Клетки капчи и кнопки ленты жмут часто — для них порог мягче
            fast_rate=min(0.12, settings.throttle_seconds / 3),
        ))
        observer.outer_middleware(UserContextMiddleware())
        observer.outer_middleware(AccessGateMiddleware())
    dp.callback_query.outer_middleware(ScreenMiddleware())
