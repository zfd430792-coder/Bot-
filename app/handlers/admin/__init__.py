"""Сборка служебных роутеров.

Разделы владельца идут раньше общих для персонала: у них фильтр уже, и
модератор, нажавший кнопку владельца, до обработчика просто не доберётся.
Кнопки — inline, поэтому нажатие «назад» не спутать с ответом на вопрос:
оно приходит не текстом, а отдельным событием.
"""
from __future__ import annotations

from aiogram import Router

from app.handlers.admin import ads, broadcast, moderation, panel, staff

router = Router(name="admin")
router.include_router(panel.admin_router)
router.include_router(staff.router)
router.include_router(ads.router)
router.include_router(broadcast.router)
router.include_router(panel.router)
router.include_router(moderation.router)
