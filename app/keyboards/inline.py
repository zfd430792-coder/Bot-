"""Inline-клавиатуры и схема callback_data.

Кнопки живут в самом сообщении: экран правится на месте, под полем ввода
пусто. Нижняя клавиатура осталась ровно в одном месте — запрос геопозиции
(Telegram отдаёт её только так), см. keyboards/reply.py.

Соглашение: <раздел>:<действие>[:<аргумент>]

m — меню и переходы, cap — капча, onb — вход, reg — анкета, br — лента,
rep — жалоба, pr — моя анкета, ver — верификация, adm — админка,
n — кнопки уведомлений: такие сообщения остаются в переписке и экраном
не становятся (см. middlewares/screen_ctx.py).
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _kb(rows: Iterable[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def counted(title: str, count: int) -> str:
    return f"{title} ({count})" if count else title


# ──────────────────────────── Главное меню ──────────────────────────────────

HOME = _btn("🏠 Меню", "m:home")

HOME_ONLY = _kb([[HOME]])
START_AGAIN = _kb([[_btn("📝 Заполнить анкету", "m:start")]])
RETRY = _kb([[_btn("🔄 Попробовать снова", "m:start")]])
SUPPORT_SETUP = _kb([[_btn("🛠 Админ-панель", "m:admin")], [HOME]])
TO_PROFILE = _kb([[_btn("👤 Моя анкета", "m:profile")], [HOME]])


def main_menu(*, support: bool = False, is_admin: bool = False,
              is_moderator: bool = False) -> InlineKeyboardMarkup:
    """support — показывать «Поддержку»: пока контакт не указан, кнопка
    видна только владельцу, чтобы он знал, где её включить."""
    rows = [[_btn("🔍 Смотреть анкеты", "m:search")]]
    profile_row = [_btn("👤 Моя анкета", "m:profile")]
    if support:
        profile_row.append(_btn("💬 Поддержка", "m:support"))
    rows.append(profile_row)
    if is_admin:
        rows.append([_btn("🛠 Админ-панель", "m:admin")])
    elif is_moderator:
        rows.append([_btn("👮 Модератор", "m:admin")])
    return _kb(rows)


# ──────────────────────────────── Капча ─────────────────────────────────────

def captcha(buttons: Sequence[tuple[str, int]], selected: set[int],
            can_refresh: bool = True) -> InlineKeyboardMarkup:
    """15 клеток по 5 в ряд. Токены случайны — по ним ничего не угадать,
    а выбранное видно прямо на кнопке."""
    cells = [_btn(f"✅{label}" if label in selected else str(label), f"cap:tok:{token}")
             for token, label in buttons]
    tail = [_btn("✔️ Готово", "cap:done")]
    if can_refresh:
        tail.append(_btn("🔄 Другая картинка", "cap:new"))
    return _kb([cells[0:5], cells[5:10], cells[10:15], tail])


# ─────────────────────── Приветствие и предупреждение ───────────────────────

WELCOME = _kb([[_btn("▶️ Далее", "onb:next")]])
RULES_ACCEPT = _kb([[_btn("✅ Принимаю", "onb:accept")]])
USERNAME_CHECK = _kb([[_btn("🔄 Я поставил username", "onb:username")]])


# ──────────────────────────────── Анкета ────────────────────────────────────

GENDER = _kb([[_btn("👨 Я парень", "reg:gender:m"), _btn("👩 Я девушка", "reg:gender:f")]])
LOOKING = _kb([
    [_btn("👨 Парней", "reg:look:m"), _btn("👩 Девушек", "reg:look:f")],
    [_btn("💞 Всех", "reg:look:any")],
])
ABOUT = _kb([[_btn("⏭ Пропустить", "reg:skip_about")]])
CONFIRM_PROFILE = _kb([
    [_btn("✅ Всё верно, поехали", "reg:confirm")],
    [_btn("✏️ Заполнить заново", "reg:restart")],
])


def name_suggestion(name: str | None) -> InlineKeyboardMarkup | None:
    """Имя из Telegram одной кнопкой — если им вообще можно пользоваться."""
    return _kb([[_btn(f"Использовать «{name}»", "reg:tgname")]]) if name else None


def city_choices(titles: Sequence[str]) -> InlineKeyboardMarkup:
    rows = [[_btn(title, f"reg:city:{index}")] for index, title in enumerate(titles)]
    return _kb(rows + [[_btn("✍️ Ввести другой город", "reg:city:retry")]])


# ───────────────────────────── Лента анкет ──────────────────────────────────

def feed(target_id: int, likes_left: int | None = None) -> InlineKeyboardMarkup:
    """Кнопки несут id анкеты: нажатие на карточку выше по чату сделает
    ровно то, что на ней написано."""
    heart = "❤️" if likes_left is None else f"❤️ {likes_left}"
    return _kb([
        [_btn(heart, f"br:like:{target_id}"),
         _btn("💌 Сообщение", f"br:note:{target_id}"),
         _btn("👎", f"br:dislike:{target_id}")],
        [_btn("🚨 Жалоба", f"br:report:{target_id}"), HOME],
    ])


def far_offer(skipped: int) -> InlineKeyboardMarkup:
    """Город и область пройдены: соседние области — или ещё раз своих."""
    rows = [[_btn("🌍 Смотреть соседние области", "br:far")]]
    if skipped:
        rows.append([_btn(counted("🔄 Вернуть пропущенных", skipped), "br:reset")])
    return _kb(rows + [[HOME]])


def feed_end(skipped: int) -> InlineKeyboardMarkup:
    rows = [[_btn(counted("🔄 Вернуть пропущенных", skipped), "br:reset")]] if skipped else []
    return _kb(rows + [[HOME]])


NOTE_CANCEL = _kb([[_btn("⬅️ Отмена", "br:cancel")]])


def report_reasons(reasons: Mapping[str, str]) -> InlineKeyboardMarkup:
    rows = [[_btn(title, f"rep:{key}")] for key, title in reasons.items()]
    return _kb(rows + [[_btn("⬅️ Отмена", "rep:cancel")]])


REPORT_COMMENT = _kb([
    [_btn("📨 Отправить без комментария", "rep:send")],
    [_btn("⬅️ Отмена", "rep:cancel")],
])


# ─────────────────────────── Уведомления ────────────────────────────────────

# Эти сообщения остаются в переписке, экраном не становятся
NOTIFY_LIKE = _kb([[_btn("❤️ Посмотреть", "n:search")]])
REMINDER = _kb([
    [_btn("🔍 Смотреть анкеты", "n:search")],
    [_btn("🔕 Больше не напоминать", "n:mute")],
])
NOTIFY_REPORTS = _kb([[_btn("🚨 Разобрать жалобы", "n:reports")]])
NOTIFY_VERIFY = _kb([[_btn("✅ Проверить заявку", "n:verify")]])


# ──────────────────────────── Моя анкета ────────────────────────────────────

def profile_actions(is_active: bool, verify_status: str) -> InlineKeyboardMarkup:
    rows = [
        [_btn("📸 Изменить фото", "pr:media"), _btn("📝 Изменить описание", "pr:about")],
        [_btn("🔄 Заполнить анкету заново", "pr:refill")],
        [_btn("🙈 Скрыть из поиска", "pr:hide") if is_active
         else _btn("👀 Показывать в поиске", "pr:show")],
    ]
    if verify_status not in {"verified", "pending"}:
        rows.append([_btn("✅ Пройти верификацию", "ver:self")])
    rows.append([_btn("🗑 Удалить анкету", "pr:del"), HOME])
    return _kb(rows)


DELETE_CONFIRM = _kb([
    [_btn("🗑 Да, удалить", "pr:del:yes")],
    [_btn("⬅️ Нет, оставить", "pr:back")],
])
EDIT_CANCEL = _kb([[_btn("⬅️ Отмена", "pr:back")]])


# ─────────────────────────── Верификация ────────────────────────────────────

VERIFY_SELF = _kb([
    [_btn("📸 Отправить фото с кодом", "ver:send")],
    [_btn("⬅️ К анкете", "pr:back")],
])
VERIFY_REQUIRED = _kb([[_btn("📸 Отправить фото с кодом", "ver:send")]])
VERIFY_CANCEL = _kb([[_btn("⬅️ Отмена", "ver:cancel")]])


# ───────────────────────────── Админка ──────────────────────────────────────

ADMIN_BACK = _kb([[_btn("⬅️ В админку", "adm:home")]])

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


def admin_menu(reports: int = 0, verify: int = 0, *,
               is_admin: bool = True) -> InlineKeyboardMarkup:
    """Полная панель владельцу, урезанная — модератору."""
    rows = [
        [_btn(counted("🚨 Жалобы", reports), "adm:reports"),
         _btn(counted("✅ Верификация", verify), "adm:verify")],
        [_btn("🔎 Найти пользователя", "adm:find")],
        [_btn("🚫 Забанить", "adm:ban"), _btn("✅ Разбанить", "adm:unban")],
    ]
    if is_admin:
        rows.insert(0, [_btn("📊 Статистика", "adm:stats"),
                        _btn("📢 Рассылка", "adm:broadcast")])
        rows += [[_btn("📣 Реклама", "adm:ads"), _btn("👮 Модераторы", "adm:staff")],
                 [_btn("⚙️ Настройки бота", "adm:config")]]
    else:
        rows.insert(0, [_btn("📊 Сводка", "adm:stats")])
    rows.append([HOME])
    return _kb(rows)


def admin_user_card(target_id: int, is_banned: bool, verified: bool,
                    forced: bool) -> InlineKeyboardMarkup:
    """Кнопки несут id: карточка выше по чату действует на своего человека."""
    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, f"adm:card:{action}:{target_id}")

    rows = [[act("✅ Разбанить", "unban") if is_banned else act("🚫 Забанить", "ban")]]
    if not verified:
        rows.append([act("✅ Запросить верификацию", "req")])
    if forced:
        rows.append([act("🔓 Снять требование верификации", "drop")])
    if verified:
        rows.append([act("❎ Снять галочку", "unverify")])
    rows += [[act("✉️ Написать пользователю", "msg")],
             [_btn("⬅️ В админку", "adm:home")]]
    return _kb(rows)


def card_back(target_id: int) -> InlineKeyboardMarkup:
    return _kb([[_btn("⬅️ К карточке", f"adm:card:show:{target_id}")]])


def report_view(report_id: int) -> InlineKeyboardMarkup:
    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, f"adm:rep:{action}:{report_id}")

    return _kb([
        [act("🚫 Забанить", "ban"), act("✅ Запросить верификацию", "req")],
        [act("👌 Отклонить жалобу", "decline"), act("⏭ Дальше", "next")],
        [_btn("⬅️ В админку", "adm:home")],
    ])


def verify_view(verification_id: int) -> InlineKeyboardMarkup:
    def act(text: str, action: str) -> InlineKeyboardButton:
        return _btn(text, f"adm:ver:{action}:{verification_id}")

    return _kb([
        [act("✅ Подтвердить", "ok"), act("❌ Отклонить", "no")],
        [act("🚫 Забанить", "ban"), act("⏭ Дальше", "next")],
        [_btn("⬅️ В админку", "adm:home")],
    ])


BROADCAST_AUDIENCE = _kb(
    [[_btn(title, f"adm:bc:{key}") for title, key in AUDIENCES[i:i + 2]]
     for i in range(0, len(AUDIENCES), 2)] + [[_btn("⬅️ В админку", "adm:home")]]
)
BROADCAST_CONFIRM = _kb([
    [_btn("🚀 Отправить", "adm:bc:send")],
    [_btn("⬅️ В админку", "adm:home")],
])
AD_BUTTON = _kb([
    [_btn("⏭ Без кнопки", "adm:ad:nobutton")],
    [_btn("⬅️ В админку", "adm:home")],
])


def ads_list(ads: Sequence[Mapping]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"📣 #{ad['id']} {str(ad['title'])[:28]}", f"adm:ad:open:{ad['id']}")]
            for ad in ads]
    return _kb(rows + [[_btn("➕ Новый пост", "adm:ad:new")],
                       [_btn("⬅️ В админку", "adm:home")]])


def ad_view(ad_id: int, is_active: bool) -> InlineKeyboardMarkup:
    return _kb([
        [_btn("⏸ Выключить пост", f"adm:ad:off:{ad_id}") if is_active
         else _btn("▶️ Включить пост", f"adm:ad:on:{ad_id}")],
        [_btn("🗑 Удалить пост", f"adm:ad:del:{ad_id}")],
        [_btn("⬅️ К списку", "adm:ads")],
    ])


ADS_BACK = _kb([[_btn("⬅️ К списку", "adm:ads")]])
STAFF_BACK = _kb([[_btn("⬅️ К модераторам", "adm:staff")]])
REJECT_BACK = _kb([[_btn("⬅️ Отмена", "adm:verify")]])


def staff_list(moderators: Sequence[Mapping]) -> InlineKeyboardMarkup:
    rows = []
    for row in moderators:
        name = row["name"] or row["tg_name"] or "без имени"
        rows.append([_btn(f"❌ Снять {str(name)[:20]} ({row['id']})",
                          f"adm:staff:del:{row['id']}")])
    return _kb(rows + [[_btn("➕ Назначить модератора", "adm:staff:add")],
                       [_btn("⬅️ В админку", "adm:home")]])


CONFIG_BACK = _kb([[_btn("⬅️ К настройкам", "adm:config")]])


def bot_settings(likes_limit: int, registration_open: bool,
                 support: str) -> InlineKeyboardMarkup:
    return _kb([
        [_btn(f"❤️ Лимит лайков: {likes_limit}", "adm:cfg:likes")],
        [_btn(f"💬 Контакт поддержки: @{support}" if support
              else "💬 Контакт поддержки: не указан", "adm:cfg:support")],
        [_btn("🟢 Регистрация открыта" if registration_open
              else "🔴 Регистрация закрыта", "adm:cfg:reg")],
        [_btn("⬅️ В админку", "adm:home")],
    ])
