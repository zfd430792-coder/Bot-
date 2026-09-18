"""Inline-кнопки. Управление ботом — нижними кнопками (keyboards/reply.py);
здесь только то, что нижней кнопкой не сделать.

«✅ Принимаю» появляется под правилами в том же сообщении, когда закончится
отсчёт: нижняя клавиатура не умеет появляться через паузу у конкретного
сообщения. Кнопка-ссылка под рекламным постом собирается в services/ads.py.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RULES_ACCEPT = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Принимаю", callback_data="onb:accept")
]])
