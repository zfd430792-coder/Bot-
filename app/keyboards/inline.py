"""Inline-клавиатуры и схема callback_data.

Соглашение: <раздел>:<действие>[:<аргумент>]
cap  — капча, onb — приветствие, reg — анкета, br — лента, pr — профиль,
st — настройки, rep — жалоба, ver — верификация, adm — админка.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ──────────────────────────────── Капча ─────────────────────────────────────

def captcha(buttons: list[tuple[str, int]], selected: set[int],
            can_refresh: bool = True) -> InlineKeyboardMarkup:
    """15 клеток по 5 в ряд. Токены случайны — по ним ничего не угадать."""
    builder = InlineKeyboardBuilder()
    for token, label in buttons:
        # Номер обязательно остаётся виден: иначе не проверить, что выбрал
        mark = f"✅{label}" if label in selected else str(label)
        builder.button(text=mark, callback_data=f"cap:tok:{token}")
    builder.adjust(5, 5, 5)

    tail = InlineKeyboardBuilder()
    tail.button(text="✔️ Готово", callback_data="cap:done")
    if can_refresh:
        tail.button(text="🔄 Другая картинка", callback_data="cap:new")
    tail.adjust(2)
    builder.attach(tail)
    return builder.as_markup()


# ─────────────────────── Приветствие и предупреждение ───────────────────────

WELCOME_NEXT = _kb([[_btn("▶️ Далее", "onb:next")]])
RULES_ACCEPT = _kb([[_btn("✅ Принимаю", "onb:accept")]])
CHECK_USERNAME = _kb([[_btn("🔄 Я поставил username", "onb:username")]])


# ──────────────────────────────── Анкета ────────────────────────────────────

GENDER = _kb([[_btn("👨 Парень", "reg:gender:m"), _btn("👩 Девушка", "reg:gender:f")]])
LOOKING_FOR = _kb([
    [_btn("👨 Парней", "reg:look:m"), _btn("👩 Девушек", "reg:look:f")],
    [_btn("💞 Неважно — всех", "reg:look:any")],
])
SKIP_ABOUT = _kb([[_btn("⏭ Пропустить", "reg:skip_about")]])
USE_TG_NAME = _kb([[_btn("Использовать имя из Telegram", "reg:tgname")]])


def city_choices(cities, prefix: str = "reg") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, city in enumerate(cities):
        builder.button(text=city.title, callback_data=f"{prefix}:city:{i}")
    builder.button(text="✍️ Ввести другой город", callback_data=f"{prefix}:city:retry")
    builder.adjust(1)
    return builder.as_markup()


def scope(city: str, region: str, has_coords: bool,
          prefix: str = "reg") -> InlineKeyboardMarkup:
    rows = [[_btn(f"🏙 Только {city}", f"{prefix}:scope:city")]]
    if region and region != city:
        rows.append([_btn(f"🗺 Вся {region}", f"{prefix}:scope:region")])
    if has_coords:
        rows.append([_btn("📍 По расстоянию (рядом со мной)", f"{prefix}:scope:near")])
    return _kb(rows)


CONFIRM_PROFILE = _kb([
    [_btn("✅ Всё верно, поехали", "reg:confirm")],
    [_btn("✏️ Заполнить заново", "reg:restart")],
])


# ───────────────────────────── Лента анкет ──────────────────────────────────

def browse(target_id: int, likes_left: int | None = None) -> InlineKeyboardMarkup:
    heart = "❤️" if likes_left is None else f"❤️ ({likes_left})"
    return _kb([
        [_btn(heart, f"br:like:{target_id}"),
         _btn("💌 С сообщением", f"br:note:{target_id}"),
         _btn("👎", f"br:dislike:{target_id}")],
        [_btn("🚨 Пожаловаться", f"br:report:{target_id}"), _btn("💤 В меню", "br:stop")],
    ])


def answer_like(sender_id: int) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением «вы кому-то понравились»."""
    return _kb([
        [_btn("❤️ Взаимно", f"ans:like:{sender_id}"),
         _btn("👎 Не моё", f"ans:skip:{sender_id}")],
        [_btn("🚨 Пожаловаться", f"br:report:{sender_id}")],
    ])


NOTE_CANCEL = _kb([[_btn("⬅️ Отмена", "br:note_cancel")]])


NEXT_PROFILE = _kb([[_btn("▶️ Смотреть дальше", "br:next")]])


def match_actions(username: str | None) -> InlineKeyboardMarkup:
    rows = []
    if username:
        rows.append([InlineKeyboardButton(text="✍️ Написать", url=f"https://t.me/{username}")])
    rows.append([_btn("▶️ Смотреть дальше", "br:next")])
    return _kb(rows)


# ─────────────────────────────── Профиль ────────────────────────────────────

def profile_actions(is_active: bool, verify_status: str) -> InlineKeyboardMarkup:
    rows = [[_btn("✏️ Изменить анкету", "pr:edit")]]
    rows.append([
        _btn("🙈 Скрыть из поиска", "pr:hide") if is_active
        else _btn("👀 Показывать в поиске", "pr:show")
    ])
    if verify_status not in {"verified", "pending"}:
        rows.append([_btn("☑️ Пройти верификацию", "pr:verify")])
    rows.append([_btn("🗑 Удалить анкету", "pr:delete")])
    return _kb(rows)


EDIT_FIELDS = _kb([
    [_btn("📸 Фото / видео", "edit:media"), _btn("📝 О себе", "edit:about")],
    [_btn("✏️ Имя", "edit:name"), _btn("🎂 Возраст", "edit:age")],
    [_btn("🌍 Город / геопозиция", "edit:city")],
    [_btn("⬅️ Назад", "edit:back")],
])

DELETE_CONFIRM = _kb([
    [_btn("🗑 Да, удалить", "pr:delete_yes")],
    [_btn("⬅️ Нет, оставить", "pr:delete_no")],
])


# ────────────────────────────── Настройки ───────────────────────────────────

def settings(scope_value: str, has_coords: bool,
             notify_enabled: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🎂 Возраст поиска", "st:age")],
        [_btn("🌍 Город / геопозиция", "st:city")],
        [_btn("🏙 Искать: только город", "st:scope:city")],
        [_btn("🗺 Искать: вся область", "st:scope:region")],
    ]
    if has_coords:
        rows.append([_btn("📍 Искать: по расстоянию", "st:scope:near")])
        if scope_value == "near":
            rows.append([_btn("📏 Радиус поиска", "st:radius")])
    rows.append([_btn("🔄 Вернуть пропущенные анкеты", "st:reset_skips")])
    rows.append([_btn(
        "🔔 Напоминания: включены" if notify_enabled else "🔕 Напоминания: выключены",
        "st:notify",
    )])
    rows.append([_btn("⬅️ В меню", "st:close")])
    return _kb(rows)


def radius_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for km in (5, 10, 25, 50, 100, 200, 500):
        builder.button(text=f"{km} км", callback_data=f"st:radius:{km}")
    builder.button(text="⬅️ Назад", callback_data="st:back")
    builder.adjust(4, 3, 1)
    return builder.as_markup()


# ──────────────────────────────── Жалобы ────────────────────────────────────

def report_reasons(reasons: dict[str, str], target_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, title in reasons.items():
        builder.button(text=title, callback_data=f"rep:{key}:{target_id}")
    builder.button(text="⬅️ Отмена", callback_data="rep:cancel")
    builder.adjust(1)
    return builder.as_markup()


REPORT_SKIP_COMMENT = _kb([[_btn("📨 Отправить без комментария", "rep:send")]])


# ───────────────────────────── Верификация ──────────────────────────────────

VERIFY_START = _kb([[_btn("☑️ Пройти верификацию", "ver:start")]])
VERIFY_CANCEL = _kb([[_btn("⬅️ Отмена", "ver:cancel")]])


def verify_review(verification_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [_btn("✅ Подтвердить", f"vrf:ok:{verification_id}"),
         _btn("❌ Отклонить", f"vrf:no:{verification_id}")],
        [_btn("🚫 Забанить", f"vrf:ban:{verification_id}")],
    ])


# ────────────────────────────── Админ-панель ────────────────────────────────

def admin_menu(reports_open: int = 0, verify_wait: int = 0,
               is_admin: bool = True) -> InlineKeyboardMarkup:
    """Полная панель владельцу, урезанная — модератору."""
    reports = f"🚨 Жалобы ({reports_open})" if reports_open else "🚨 Жалобы"
    verify = f"☑️ Верификация ({verify_wait})" if verify_wait else "☑️ Верификация"

    rows = [
        [_btn(reports, "adm:reports"), _btn(verify, "adm:verify")],
        [_btn("🔎 Найти пользователя", "adm:find")],
        [_btn("🚫 Забанить", "adm:ban"), _btn("✅ Разбанить", "adm:unban")],
    ]
    if is_admin:
        rows.insert(0, [_btn("📊 Статистика", "adm:stats"),
                        _btn("📢 Рассылка", "adm:bc")])
        rows.append([_btn("📣 Реклама", "adm:ads"),
                     _btn("👮 Модераторы", "adm:staff")])
        rows.append([_btn("⚙️ Настройки бота", "adm:cfg")])
    else:
        rows.insert(0, [_btn("📊 Сводка", "adm:stats")])
    rows.append([_btn("❌ Закрыть", "adm:close")])
    return _kb(rows)


def staff_list(moderators: list) -> InlineKeyboardMarkup:
    """Список модераторов: у каждого кнопка снятия."""
    builder = InlineKeyboardBuilder()
    for row in moderators:
        name = row["name"] or row["tg_name"] or str(row["id"])
        builder.button(text=f"❌ {name} ({row['id']})",
                       callback_data=f"adm:staff_del:{row['id']}")
    builder.button(text="➕ Назначить модератора", callback_data="adm:staff_add")
    builder.button(text="⬅️ В админ-панель", callback_data="adm:menu")
    builder.adjust(1)
    return builder.as_markup()


ADMIN_BACK = _kb([[_btn("⬅️ В админ-панель", "adm:menu")]])


def admin_user_card(user_id: int, is_banned: bool, verified: bool,
                    forced: bool) -> InlineKeyboardMarkup:
    rows = []
    rows.append([
        _btn("✅ Разбанить", f"adm:unban_id:{user_id}") if is_banned
        else _btn("🚫 Забанить", f"adm:ban_id:{user_id}")
    ])
    if not verified:
        rows.append([_btn("☑️ Запросить верификацию", f"adm:req_verify:{user_id}")])
    if forced:
        rows.append([_btn("🔓 Снять требование верификации", f"adm:drop_verify:{user_id}")])
    if verified:
        rows.append([_btn("❎ Снять галочку", f"adm:unverify:{user_id}")])
    rows.append([_btn("✉️ Написать пользователю", f"adm:msg:{user_id}")])
    rows.append([_btn("⬅️ В админ-панель", "adm:menu")])
    return _kb(rows)


def reminder_actions() -> InlineKeyboardMarkup:
    """Кнопки под напоминанием: зайти или отписаться."""
    return _kb([
        [_btn("🔍 Смотреть анкеты", "remind:search")],
        [_btn("🔕 Больше не напоминать", "remind:off")],
    ])


def autoban_actions(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением об автобане — последнее слово за админом."""
    return _kb([
        [_btn("✅ Разбанить", f"adm:unban_id:{user_id}"),
         _btn("👤 Карточка", f"adm:card:{user_id}")],
    ])


def broadcast_audience() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = [
        ("👥 Всем", "all"),
        ("📋 С анкетой", "registered"),
        ("🔥 Активным за 7 дней", "active7"),
        ("📆 Активным за 30 дней", "active30"),
        ("😴 Спящим (30+ дней)", "sleeping"),
        ("🧩 Не дозаполнившим анкету", "unfinished"),
        ("👨 Парням", "male"),
        ("👩 Девушкам", "female"),
        ("🏙 По городу", "city"),
    ]
    for title, key in options:
        builder.button(text=title, callback_data=f"adm:bc_aud:{key}")
    builder.button(text="⬅️ Назад", callback_data="adm:menu")
    builder.adjust(2, 2, 2, 2, 1, 1)
    return builder.as_markup()


BROADCAST_CONFIRM = _kb([
    [_btn("🚀 Отправить", "adm:bc_go")],
    [_btn("⬅️ Отмена", "adm:menu")],
])


def report_actions(report_id: int, target_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [_btn("🚫 Забанить", f"rp:ban:{report_id}"),
         _btn("☑️ Запросить верификацию", f"rp:verify:{report_id}")],
        [_btn("👤 Карточка", f"adm:card:{target_id}"),
         _btn("✅ Отклонить жалобу", f"rp:skip:{report_id}")],
    ])


def ads_list(ads: list) -> InlineKeyboardMarkup:
    """Список рекламных постов: у каждого переключатель и удаление."""
    builder = InlineKeyboardBuilder()
    for ad in ads:
        mark = "🟢" if ad["is_active"] else "⚪️"
        builder.button(text=f"{mark} #{ad['id']} {ad['title'][:24]}",
                       callback_data=f"adm:ad_view:{ad['id']}")
        builder.button(text="⏸" if ad["is_active"] else "▶️",
                       callback_data=f"adm:ad_toggle:{ad['id']}")
        builder.button(text="🗑", callback_data=f"adm:ad_del:{ad['id']}")
    builder.button(text="➕ Новый пост", callback_data="adm:ad_new")
    builder.button(text="⬅️ В админ-панель", callback_data="adm:menu")
    builder.adjust(*([3] * len(ads)), 1, 1)
    return builder.as_markup()


AD_NO_BUTTON = _kb([[_btn("⏭ Без кнопки", "adm:ad_nobutton")]])
AD_CANCEL = _kb([[_btn("⬅️ Отмена", "adm:ads")]])


def ad_confirm(ad_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [_btn("🗑 Удалить", f"adm:ad_del:{ad_id}")],
        [_btn("⬅️ К списку", "adm:ads")],
    ])


def bot_settings(likes_limit: int, registration_open: bool) -> InlineKeyboardMarkup:
    reg = "🟢 Регистрация открыта" if registration_open else "🔴 Регистрация закрыта"
    return _kb([
        [_btn(f"❤️ Лимит лайков: {likes_limit}", "adm:set:likes_limit")],
        [_btn(reg, "adm:set:registration")],
        [_btn("⬅️ В админ-панель", "adm:menu")],
    ])
