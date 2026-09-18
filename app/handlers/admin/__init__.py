"""Сборка служебных роутеров.

Порядок: сначала вход в панель и «⬅️ В админку» — раньше разделов с вводом
текста, иначе кнопку «назад» принял бы за ответ, например, шаг рассылки.
Затем разделы владельца (у них более узкий фильтр), затем общие для персонала.
Так модератор, нажавший кнопку владельца, не получит доступ.
"""
from __future__ import annotations

from aiogram import Router

from app.handlers.admin import ads, broadcast, moderation, panel, staff

router = Router(name="admin")
router.include_router(panel.nav_router)
router.include_router(panel.admin_router)
router.include_router(staff.router)
router.include_router(ads.router)
router.include_router(broadcast.router)
router.include_router(panel.router)
router.include_router(moderation.router)
