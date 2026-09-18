"""Состояния диалогов (FSM).

Кнопки — это тексты, поэтому состояние решает, что значит нажатие, когда
надпись общая («⬅️ Назад», «⬅️ Отмена», номер клетки капчи, «🚫 Забанить»).
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    captcha = State()
    welcome = State()
    rules = State()


class Registration(StatesGroup):
    gender = State()
    looking_for = State()
    age = State()
    name = State()
    media = State()
    about = State()
    city = State()
    region_fallback = State()   # города нет в справочнике — уточняем область
    scope = State()             # шаг прежних версий, остался для совместимости
    confirm = State()


class Browsing(StatesGroup):
    feed = State()          # лента анкет
    note = State()          # пишем сообщение к лайку


class EditProfile(StatesGroup):
    about = State()
    media = State()
    delete_confirm = State()


class Report(StatesGroup):
    reason = State()
    comment = State()


class Verification(StatesGroup):
    waiting_media = State()


class AdminPanel(StatesGroup):
    menu = State()
    find_user = State()
    user_card = State()          # открыта карточка пользователя
    ban_user = State()
    ban_reason = State()
    unban_user = State()
    verify_request = State()
    report_view = State()        # разбор жалоб по одной
    verify_view = State()        # разбор заявок на верификацию по одной
    verify_reject_reason = State()
    broadcast_audience = State()
    broadcast_city = State()
    broadcast_content = State()
    broadcast_confirm = State()
    bot_settings = State()
    setting_value = State()
    message_user = State()
    staff_list = State()
    staff_add = State()
    ads_list = State()
    ad_view = State()
    ad_title = State()
    ad_content = State()
    ad_button_text = State()
    ad_button_url = State()
    ad_every = State()


# Диалоги, которые бот сейчас ведёт. В Redis могут остаться состояния прежних
# версий (например, настроек поиска) — их хендлеров больше нет, и fallback
# по этому списку понимает, что человека нужно вернуть в меню.
KNOWN = frozenset(
    name
    for group in (Onboarding, Registration, Browsing, EditProfile, Report,
                  Verification, AdminPanel)
    for name in group.__all_states_names__
)


def is_known(value: str | None) -> bool:
    return value in KNOWN
