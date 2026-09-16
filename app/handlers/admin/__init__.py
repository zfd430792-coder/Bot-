"""Сборка админских роутеров."""
from __future__ import annotations

from aiogram import Router

from app.handlers.admin import broadcast, moderation, panel

router = Router(name="admin")
router.include_router(panel.router)
router.include_router(moderation.router)
router.include_router(broadcast.router)
