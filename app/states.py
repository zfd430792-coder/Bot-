"""Состояния диалогов (FSM)."""
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
    scope = State()
    confirm = State()


class Browsing(StatesGroup):
    feed = State()          # лента анкет
    likes_inbox = State()   # «кто меня лайкнул»


class EditProfile(StatesGroup):
    choosing = State()
    name = State()
    age = State()
    about = State()
    media = State()
    city = State()
    region_fallback = State()


class SearchSettings(StatesGroup):
    menu = State()
    age_range = State()
    radius = State()
    city = State()


class Report(StatesGroup):
    reason = State()
    comment = State()


class Verification(StatesGroup):
    waiting_media = State()


class AdminPanel(StatesGroup):
    menu = State()
    find_user = State()
    ban_user = State()
    ban_reason = State()
    unban_user = State()
    verify_request = State()
    verify_reject_reason = State()
    broadcast_audience = State()
    broadcast_city = State()
    broadcast_content = State()
    broadcast_confirm = State()
    setting_value = State()
    message_user = State()
