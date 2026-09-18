"""Нижние клавиатуры — основной способ управлять ботом, как в Дайвинчике.

Кнопка — это просто текст, который человек отправляет нажатием. Поэтому все
надписи собраны здесь константами: по ним же хендлеры узнают нажатия.
Надписи не должны совпадать между экранами, если значат разное; общие
«⬅️ Назад» и «⬅️ Отмена» разбираются по состоянию диалога.

Inline-кнопок в боте две: «✅ Принимаю» под правилами (появляется после
отсчёта в том же сообщении) и кнопка-ссылка под рекламным постом — ссылку
Telegram умеет открывать только так.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

REMOVE = ReplyKeyboardRemove()

# ─────────────────────────────── Общие ──────────────────────────────────────

HOME = "🏠 Меню"
BACK = "⬅️ Назад"
CANCEL = "⬅️ Отмена"

# ──────────────────────────── Главное меню ──────────────────────────────────

SEARCH = "🔍 Смотреть анкеты"
LIKES = "❤️ Кто меня лайкнул"           # + « (N)», если есть
MATCHES = "💬 Мои пары"                  # + « (N)»
PROFILE = "👤 Моя анкета"
SETTINGS = "⚙️ Настройки"
HELP = "ℹ️ Помощь"
ADMIN = "🛠 Админ-панель"
MODERATOR = "👮 Модератор"
# Надписи прежних версий — у кого-то клавиатура ещё старая
MATCHES_OLD = "💬 Мои совпадения"
SETTINGS_OLD = "⚙️ Настройки поиска"

# ─────────────────────────── Вход в бота ────────────────────────────────────

NEXT = "▶️ Далее"
USERNAME_DONE = "🔄 Я поставил username"
CAPTCHA_DONE = "✅ Готово"
CAPTCHA_NEW = "🔄 Другая картинка"
START = "/start"

# ─────────────────────────────── Анкета ─────────────────────────────────────

GENDER_M = "👨 Я парень"
GENDER_F = "👩 Я девушка"
LOOK_M = "👨 Парней"
LOOK_F = "👩 Девушек"
LOOK_ANY = "💞 Всех"
SKIP = "⏭ Пропустить"
LOCATION = "📍 Отправить геопозицию"
OTHER_CITY = "✍️ Ввести другой город"
MANUAL_CITY = "✍️ Ввести город вручную"   # прежняя версия
CONFIRM = "✅ Всё верно, поехали"
REFILL = "✏️ Заполнить заново"
START_OVER = "📝 Заполнить анкету"

# ─────────────────────────────── Лента ──────────────────────────────────────

LIKE = "❤️"                               # + « 49» — сколько лайков осталось
LIKE_RE = re.compile(r"^❤️(\s*\d+)?$")
NOTE = "💌 Сообщение"
DISLIKE = "👎"
REPORT = "🚨 Жалоба"
RESET_SKIPS = "🔄 Вернуть пропущенных"    # + « (N)»
NO_COMMENT = "📨 Отправить без комментария"
STOP_REMINDERS = "🔕 Больше не напоминать"

# ────────────────────────────── Моя анкета ──────────────────────────────────

EDIT = "✏️ Изменить анкету"
HIDE = "🙈 Скрыть из поиска"
SHOW = "👀 Показывать в поиске"
VERIFY = "✅ Пройти верификацию"
DELETE = "🗑 Удалить анкету"
DELETE_YES = "🗑 Да, удалить"
DELETE_NO = "⬅️ Нет, оставить"
EDIT_MEDIA = "📸 Фото / видео"
EDIT_ABOUT = "📝 О себе"
EDIT_NAME = "✏️ Имя"
EDIT_AGE = "🎂 Возраст"
CITY = "🌍 Город"
VERIFY_SEND = "📸 Отправить фото с кодом"
TO_PROFILE = "⬅️ К анкете"

# ────────────────────────────── Настройки ───────────────────────────────────

AGE_RANGE = "🎂 Возраст поиска"
RADIUS = "📏 Радиус"
RESET_SKIPS_ALL = "🔄 Вернуть пропущенные анкеты"
NOTIFY_ON = "🔔 Напоминания: включены"
NOTIFY_OFF = "🔕 Напоминания: выключены"
# С чего начинается лента; 🔘 — выбранный вариант
SCOPES = (("city", "Сначала мой город"), ("region", "Сначала вся область"),
          ("near", "Сначала те, кто рядом"))
RADII = (5, 10, 25, 50, 100, 200, 500)
RADIUS_RE = re.compile(r"^(\d{1,3}) км$")

# ─────────────────────────────── Админка ────────────────────────────────────

A_STATS = "📊 Статистика"
A_SUMMARY = "📊 Сводка"
A_BROADCAST = "📢 Рассылка"
A_REPORTS = "🚨 Жалобы"                   # + « (N)»
A_VERIFY = "✅ Верификация"               # + « (N)»
A_FIND = "🔎 Найти пользователя"
A_BAN = "🚫 Забанить"
A_UNBAN = "✅ Разбанить"
A_ADS = "📣 Реклама"
A_STAFF = "👮 Модераторы"
A_CONFIG = "⚙️ Настройки бота"
A_BACK = "⬅️ В админку"
A_REQ_VERIFY = "✅ Запросить верификацию"
A_DROP_VERIFY = "🔓 Снять требование верификации"
A_UNVERIFY = "❎ Снять галочку"
A_MESSAGE = "✉️ Написать пользователю"
A_DECLINE = "👌 Отклонить жалобу"
A_NEXT = "⏭ Дальше"
A_APPROVE = "✅ Подтвердить"
A_REJECT = "❌ Отклонить"
A_SEND = "🚀 Отправить"
A_AD_NEW = "➕ Новый пост"
A_AD_NO_BUTTON = "⏭ Без кнопки"
A_AD_OFF = "⏸ Выключить пост"
A_AD_ON = "▶️ Включить пост"
A_AD_DELETE = "🗑 Удалить пост"
A_AD_LIST = "⬅️ К списку"
A_STAFF_ADD = "➕ Назначить модератора"
A_LIKES_LIMIT = "❤️ Лимит лайков"         # + «: N»
A_REG_OPEN = "🟢 Регистрация открыта"
A_REG_CLOSED = "🔴 Регистрация закрыта"
AD_RE = re.compile(r"^📣 #(\d+)\b")
STAFF_RE = re.compile(r"^❌ Снять .*\((\d+)\)$")

AUDIENCES = (
    ("👥 Всем", "all"),
    ("📋 С анкетой", "registered"),
    ("🔥 Активным за 7 дней", "active7"),
    ("📆 Активным за 30 дней", "active30"),
    ("😴 Спящим (30+ дней)", "sleeping"),
    ("🧩 Не дозаполнившим анкету", "unfinished"),
    ("👨 Парням", "male"),
    ("👩 Девушкам", "female"),
    ("🏙 По городу", "city"),
)


# ───────────────────────────── Сборка ───────────────────────────────────────

def keyboard(rows: Iterable[Sequence[str]],
             placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows if row],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def counted(title: str, count: int) -> str:
    return f"{title} ({count})" if count else title


HOME_ONLY = keyboard([[HOME]])
CANCEL_ONLY = keyboard([[CANCEL]])
BACK_ONLY = keyboard([[BACK]])
ADMIN_BACK = keyboard([[A_BACK]])


def main_menu(likes: int = 0, matches: int = 0, *, is_admin: bool = False,
              is_moderator: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [SEARCH],
        [counted(LIKES, likes), counted(MATCHES, matches)],
        [PROFILE, SETTINGS],
        [HELP],
    ]
    if is_admin:
        rows.append([ADMIN])
    elif is_moderator:
        rows.append([MODERATOR])
    return keyboard(rows, "Выберите, что нужно")


# ── Вход и анкета ───────────────────────────────────────────────────────────

WELCOME = keyboard([[NEXT]])
USERNAME_CHECK = keyboard([[USERNAME_DONE]])
RECHECK = keyboard([[START]])
GENDER = keyboard([[GENDER_M, GENDER_F]])
LOOKING = keyboard([[LOOK_M, LOOK_F], [LOOK_ANY]])
ABOUT = keyboard([[SKIP]], "Пара слов о себе")
CONFIRM_PROFILE = keyboard([[CONFIRM], [REFILL]])
START_AGAIN = keyboard([[START_OVER]])


def captcha(can_refresh: bool = True) -> ReplyKeyboardMarkup:
    """Номера клеток 1–15 и действия. Номер на кнопке — это номер,
    нарисованный на клетке, а не её позиция: позиция ничего не подсказывает."""
    numbers = [str(n) for n in range(1, 16)]
    rows = [numbers[0:5], numbers[5:10], numbers[10:15],
            [CAPTCHA_DONE, CAPTCHA_NEW] if can_refresh else [CAPTCHA_DONE]]
    return keyboard(rows, "Номера клеток, затем «Готово»")


def name_suggestion(name: str | None) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Имя из Telegram одной кнопкой — если им вообще можно пользоваться."""
    return keyboard([[name]], "Как вас зовут") if name else REMOVE


def request_location(*, cancel: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=LOCATION, request_location=True)]]
    if cancel:
        rows.append([KeyboardButton(text=CANCEL)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True,
        input_field_placeholder="Напишите город или отправьте геопозицию",
    )


def city_choices(titles: Sequence[str]) -> ReplyKeyboardMarkup:
    return keyboard([[title] for title in titles] + [[OTHER_CITY]])


# ── Лента ───────────────────────────────────────────────────────────────────

def feed(likes_left: int | None = None) -> ReplyKeyboardMarkup:
    heart = LIKE if likes_left is None else f"{LIKE} {likes_left}"
    return keyboard([[heart, NOTE, DISLIKE], [REPORT, HOME]])


def feed_end(skipped: int) -> ReplyKeyboardMarkup:
    rows = [[counted(RESET_SKIPS, skipped)]] if skipped else []
    return keyboard(rows + [[SETTINGS], [HOME]])


LIKES_END = keyboard([[SEARCH], [HOME]])


def report_reasons(reasons: Mapping[str, str]) -> ReplyKeyboardMarkup:
    return keyboard([[title] for title in reasons.values()] + [[CANCEL]])


REPORT_COMMENT = keyboard([[NO_COMMENT], [CANCEL]], "Что случилось")
REMINDER = keyboard([[SEARCH], [STOP_REMINDERS]])


# ── Моя анкета ──────────────────────────────────────────────────────────────

def profile_actions(is_active: bool, verify_status: str) -> ReplyKeyboardMarkup:
    rows = [[EDIT], [HIDE if is_active else SHOW]]
    if verify_status not in {"verified", "pending"}:
        rows.append([VERIFY])
    rows += [[DELETE], [HOME]]
    return keyboard(rows)


EDIT_FIELDS = keyboard([[EDIT_MEDIA, EDIT_ABOUT], [EDIT_NAME, EDIT_AGE], [CITY], [BACK]])
DELETE_CONFIRM = keyboard([[DELETE_YES], [DELETE_NO]])
VERIFY_SELF = keyboard([[VERIFY_SEND], [TO_PROFILE]])
VERIFY_REQUIRED = keyboard([[VERIFY_SEND]])


# ── Настройки ───────────────────────────────────────────────────────────────

def scope_button(key: str, title: str, current: str) -> str:
    return f"{'🔘' if key == current else '⚪️'} {title}"


def settings(scope_value: str, has_coords: bool,
             notify_enabled: bool = True) -> ReplyKeyboardMarkup:
    rows = [[AGE_RANGE, CITY]]
    rows += [[scope_button(key, title, scope_value)] for key, title in SCOPES]
    if has_coords and scope_value == "near":
        rows.append([RADIUS])
    rows += [[RESET_SKIPS_ALL], [NOTIFY_ON if notify_enabled else NOTIFY_OFF], [HOME]]
    return keyboard(rows)


def radius_choices() -> ReplyKeyboardMarkup:
    labels = [f"{km} км" for km in RADII]
    return keyboard([labels[:4], labels[4:], [BACK]])


# ── Админка ─────────────────────────────────────────────────────────────────

def admin_menu(reports: int = 0, verify: int = 0, *,
               is_admin: bool = True) -> ReplyKeyboardMarkup:
    """Полная панель владельцу, урезанная — модератору."""
    rows = [
        [counted(A_REPORTS, reports), counted(A_VERIFY, verify)],
        [A_FIND],
        [A_BAN, A_UNBAN],
    ]
    if is_admin:
        rows.insert(0, [A_STATS, A_BROADCAST])
        rows += [[A_ADS, A_STAFF], [A_CONFIG]]
    else:
        rows.insert(0, [A_SUMMARY])
    rows.append([HOME])
    return keyboard(rows, "Раздел админки")


def admin_user_card(is_banned: bool, verified: bool, forced: bool) -> ReplyKeyboardMarkup:
    rows = [[A_UNBAN if is_banned else A_BAN]]
    if not verified:
        rows.append([A_REQ_VERIFY])
    if forced:
        rows.append([A_DROP_VERIFY])
    if verified:
        rows.append([A_UNVERIFY])
    rows += [[A_MESSAGE], [A_BACK]]
    return keyboard(rows)


REPORT_VIEW = keyboard([[A_BAN, A_REQ_VERIFY], [A_DECLINE, A_NEXT], [A_BACK]])
VERIFY_VIEW = keyboard([[A_APPROVE, A_REJECT], [A_BAN, A_NEXT], [A_BACK]])
BROADCAST_AUDIENCE = keyboard(
    [[t for t, _ in AUDIENCES[i:i + 2]] for i in range(0, len(AUDIENCES), 2)] + [[A_BACK]]
)
BROADCAST_CONFIRM = keyboard([[A_SEND], [A_BACK]])
AD_BUTTON = keyboard([[A_AD_NO_BUTTON], [A_BACK]])


def ad_title(ad: Mapping) -> str:
    return f"📣 #{ad['id']} {str(ad['title'])[:28]}"


def ads_list(ads: Sequence[Mapping]) -> ReplyKeyboardMarkup:
    return keyboard([[ad_title(ad)] for ad in ads] + [[A_AD_NEW], [A_BACK]])


def ad_view(is_active: bool) -> ReplyKeyboardMarkup:
    return keyboard([[A_AD_OFF if is_active else A_AD_ON], [A_AD_DELETE], [A_AD_LIST]])


def staff_button(row: Mapping) -> str:
    name = row["name"] or row["tg_name"] or "без имени"
    return f"❌ Снять {str(name)[:20]} ({row['id']})"


def staff_list(moderators: Sequence[Mapping]) -> ReplyKeyboardMarkup:
    return keyboard([[staff_button(row)] for row in moderators]
                    + [[A_STAFF_ADD], [A_BACK]])


def bot_settings(likes_limit: int, registration_open: bool) -> ReplyKeyboardMarkup:
    return keyboard([[f"{A_LIKES_LIMIT}: {likes_limit}"],
                     [A_REG_OPEN if registration_open else A_REG_CLOSED],
                     [A_BACK]])
