"""Нижние клавиатуры.

Главное меню живёт в сообщении (inline-кнопки). Нижняя клавиатура осталась
только там, где без неё никак: запросить геопозицию Telegram позволяет лишь
такой кнопкой.
"""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Пункты прежнего нижнего меню. У кого-то оно ещё открыто со старой версии —
# эти нажатия ловим так же, как новые кнопки, и заодно убираем клавиатуру.
BTN_SEARCH = "🔍 Смотреть анкеты"
BTN_LIKES = "❤️ Кто меня лайкнул"
BTN_MATCHES = "💬 Мои совпадения"
BTN_PROFILE = "👤 Моя анкета"
BTN_SETTINGS = "⚙️ Настройки поиска"
BTN_HELP = "ℹ️ Помощь"
BTN_ADMIN = "🛠 Админ-панель"
BTN_MODERATOR = "👮 Модератор"

BTN_LOCATION = "📍 Отправить геопозицию"
BTN_MANUAL_CITY = "✍️ Ввести город вручную"
BTN_CANCEL = "⬅️ Отмена"

REMOVE = ReplyKeyboardRemove()


def request_location(*, cancel: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_LOCATION, request_location=True)]]
    if cancel:
        rows.append([KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True,
        input_field_placeholder="Напишите город или отправьте геопозицию",
    )
