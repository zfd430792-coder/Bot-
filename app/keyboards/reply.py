"""Обычные (нижние) клавиатуры."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Пункты главного меню — их же ловим фильтрами в хендлерах
BTN_SEARCH = "🔍 Смотреть анкеты"
BTN_LIKES = "❤️ Кто меня лайкнул"
BTN_MATCHES = "💬 Мои совпадения"
BTN_PROFILE = "👤 Моя анкета"
BTN_SETTINGS = "⚙️ Настройки поиска"
BTN_HELP = "ℹ️ Помощь"
BTN_ADMIN = "🛠 Админ-панель"

REMOVE = ReplyKeyboardRemove()


def main_menu(is_admin: bool = False, likes_count: int = 0) -> ReplyKeyboardMarkup:
    likes = f"{BTN_LIKES} ({likes_count})" if likes_count else BTN_LIKES
    rows = [
        [KeyboardButton(text=BTN_SEARCH)],
        [KeyboardButton(text=likes), KeyboardButton(text=BTN_MATCHES)],
        [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def request_location(with_skip: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]]
    if with_skip:
        rows.append([KeyboardButton(text="✍️ Ввести город вручную")])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True,
        input_field_placeholder="Напишите город или отправьте геопозицию",
    )
