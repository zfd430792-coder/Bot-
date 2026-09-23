"""Нижние клавиатуры.

Управление — inline-кнопками в самом сообщении (keyboards/inline.py). Нижняя
клавиатура осталась только там, где без неё никак: запросить геопозицию
Telegram позволяет лишь такой кнопкой, и появляется она ровно на шаге города.

Здесь же собраны надписи прежних версий, где управление было нижними
кнопками: у кого-то такая клавиатура ещё открыта в чате, и эти нажатия бот
понимает — они приходят обычными сообщениями.
"""
from __future__ import annotations

import re

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

REMOVE = ReplyKeyboardRemove()

LOCATION = "📍 Отправить геопозицию"
CANCEL = "⬅️ Отмена"
OTHER_CITY = "✍️ Ввести другой город"
MANUAL_CITY = "✍️ Ввести город вручную"      # прежняя версия


def request_location(*, cancel: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=LOCATION, request_location=True)]]
    if cancel:
        rows.append([KeyboardButton(text=CANCEL)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="Напишите город или отправьте геопозицию",
    )


# ─────────────────── Кнопки прежних версий (нижние) ─────────────────────────

L_HOME = "🏠 Меню"
L_SEARCH = "🔍 Смотреть анкеты"
L_PROFILE = "👤 Моя анкета"
L_SUPPORT = "💬 Поддержка"
L_ADMIN = "🛠 Админ-панель"
L_MODERATOR = "👮 Модератор"
L_START_OVER = "📝 Заполнить анкету"
L_NEXT = "▶️ Далее"
L_USERNAME_DONE = "🔄 Я поставил username"
L_NOTE = "💌 Сообщение"
L_DISLIKE = "👎"
L_REPORT = "🚨 Жалоба"
L_LIKE_RE = re.compile(r"^❤️(\s*\d+)?$")
L_FAR = "🌍 Смотреть соседние области"
L_RESET_RE = re.compile(r"^🔄 Вернуть пропущенн\w+( \(\d+\))?$")
# Разделы, которых больше нет: настройки, лайки, пары, справка
L_LIKES_RE = re.compile(r"^❤️ Кто меня лайкнул( \(\d+\))?$")
L_MENU_RE = re.compile(
    r"^(⚙️ Настройки( поиска)?|ℹ️ Помощь|💬 Мои (пары|совпадения))( \(\d+\))?$"
)
L_PROFILE_RE = re.compile(r"^(✏️ Изменить анкету|📸 Изменить фото|📝 Изменить описание"
                          r"|🔄 Заполнить анкету заново)$")
