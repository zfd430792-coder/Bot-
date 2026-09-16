"""Порядок подключения роутеров важен: fallback всегда последний."""
from __future__ import annotations

from aiogram import Dispatcher


def setup(dp: Dispatcher) -> None:
    from app.handlers import (
        admin, browse, fallback, menu, onboarding, profile, registration,
        reports, settings, verification,
    )

    dp.include_router(admin.router)
    dp.include_router(onboarding.router)
    dp.include_router(verification.router)
    dp.include_router(registration.router)
    dp.include_router(reports.router)
    dp.include_router(profile.router)
    dp.include_router(settings.router)
    dp.include_router(browse.router)
    dp.include_router(menu.router)
    dp.include_router(fallback.router)
