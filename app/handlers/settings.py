"""Настройки поиска: возраст, город/геопозиция, с чего начинать ленту, радиус.

Лента всегда идёт от ближних к дальним, как в Дайвинчике. Настройка «охвата»
решает только, с чего она начинается: со своего города, со всей области или
с тех, кто в радиусе. Когда там анкеты кончаются, лента идёт дальше сама.

Экран настроек — сообщение с нижними кнопками; выбранный вариант отмечен 🔘.
Вопросы (возраст, город) встают на место экрана, ответ пользователя удаляется.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import Settings
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.db.database import norm_text
from app.handlers import menu as menu_handlers
from app.keyboards import reply as rkb
from app.services import geo, profile, screen
from app.states import SearchSettings

router = Router(name="settings")

RANGE_RE = re.compile(r"^\s*(\d{1,3})\s*[-–—: ]\s*(\d{1,3})\s*$")

SCOPE_TITLE = {
    "city": "сначала мой город",
    "region": "сначала вся область",
    "near": "сначала те, кто рядом",
}
# Нажатая кнопка охвата -> его ключ (с 🔘 и без)
SCOPE_BY_BUTTON = {rkb.scope_button(key, title, current): key
                   for key, title in rkb.SCOPES for current in (key, "")}


def render(user: Mapping[str, Any]) -> str:
    scope = SCOPE_TITLE.get(user["search_scope"] or "city", SCOPE_TITLE["city"])
    place = user["city"] or "не указан"
    if user["region"] and norm_text(user["region"]) != norm_text(user["city"]):
        place += f", {user['region']}"
    geo_line = ("📍 геопозиция передана" if user["geo_source"] == "gps"
                else "📍 геопозиция не передана")
    radius = (f"\n📏 Радиус: <b>{user['search_radius']} км</b>"
              if (user["search_scope"] == "near") else "")
    notify = ("🔔 Напоминания включены" if user["notify_enabled"]
              else "🔕 Напоминания выключены")
    looking = {"m": "парней", "f": "девушек"}.get(user["looking_for"], "всех")
    return (
        f"{texts.SETTINGS_TITLE}\n\n"
        f"🎂 Возраст: <b>{user['age_min']}–{user['age_max']}</b>\n"
        f"🔍 Ищу: <b>{looking}</b>\n"
        f"🌍 Город: <b>{profile.esc(place)}</b>\n"
        f"🎯 Лента: <b>{scope}</b>{radius}\n"
        f"{geo_line}\n{notify}\n\n"
        "<i>Когда анкеты рядом закончатся, лента сама перейдёт к соседним "
        "городам — ближние первыми.</i>"
    )


async def show_settings(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                        *, notice: str | None = None) -> None:
    user = await users_repo.get_user(user_id)
    await state.clear()
    await state.set_state(SearchSettings.menu)
    text = render(user)
    if notice:
        text = f"{notice}\n\n{text}"
    await screen.send(
        bot, chat_id, state, text,
        rkb.settings(user["search_scope"] or "city",
                     has_coords=user["geo_source"] == "gps",
                     notify_enabled=bool(user["notify_enabled"])),
    )


@router.message(Command("settings"))
@router.message(F.text.in_({rkb.SETTINGS, rkb.SETTINGS_OLD}))
async def open_settings(message: Message, state: FSMContext,
                        user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    if not user["registered"]:
        await menu_handlers.show_menu(message.bot, message.chat.id, state, user, is_admin)
        return
    await show_settings(message.bot, message.chat.id, state, user["id"])


@router.message(SearchSettings.age_range, F.text == rkb.BACK)
@router.message(SearchSettings.radius, F.text == rkb.BACK)
async def back(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    await show_settings(message.bot, message.chat.id, state, user["id"])


# ───────────────────────── Возрастной диапазон ──────────────────────────────

@router.message(F.text == rkb.AGE_RANGE)
async def ask_age_range(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(SearchSettings.age_range)
    await screen.send(message.bot, message.chat.id, state,
                      texts.SETTINGS_AGE, rkb.BACK_ONLY)


@router.message(SearchSettings.age_range, F.text)
async def set_age_range(message: Message, state: FSMContext, user,
                        settings: Settings) -> None:
    await screen.drop(message)
    bad = texts.SETTINGS_AGE_BAD.format(min_age=settings.min_age,
                                        max_age=settings.max_age)
    match = RANGE_RE.match(message.text or "")
    low = high = 0
    if match:
        low, high = sorted((int(match.group(1)), int(match.group(2))))
    if not match or low < settings.min_age or high > settings.max_age:
        await screen.send(message.bot, message.chat.id, state,
                          f"⚠️ {bad}\n\n{texts.SETTINGS_AGE}", rkb.BACK_ONLY)
        return
    await users_repo.update_user(user["id"], age_min=low, age_max=high)
    await show_settings(message.bot, message.chat.id, state, user["id"],
                        notice=f"<i>{texts.SETTINGS_SAVED}</i>")


# ─────────────────────── С чего начинается лента ────────────────────────────

@router.message(F.text.in_(SCOPE_BY_BUTTON))
async def set_scope(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    scope = SCOPE_BY_BUTTON[message.text]
    fresh = await users_repo.get_user(user["id"])
    chat_id = message.chat.id

    if scope == "near" and fresh["geo_source"] != "gps":
        await ask_city_change(message.bot, chat_id, state, back="settings",
                              want_near=True)
        return
    if scope == "region" and not fresh["region"]:
        await show_settings(message.bot, chat_id, state, user["id"],
                            notice="<i>Для поиска по области сначала укажите город.</i>")
        return

    await users_repo.update_user(user["id"], search_scope=scope)
    await show_settings(message.bot, chat_id, state, user["id"],
                        notice=f"<i>{texts.SETTINGS_SAVED}</i>")


@router.message(F.text == rkb.RADIUS)
async def ask_radius(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(SearchSettings.radius)
    await screen.send(message.bot, message.chat.id, state,
                      texts.SETTINGS_RADIUS, rkb.radius_choices())


@router.message(SearchSettings.radius, F.text.regexp(rkb.RADIUS_RE))
async def set_radius(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    await screen.drop(message)
    km = int(rkb.RADIUS_RE.match(message.text or "").group(1))
    km = max(1, min(settings.max_radius_km, km))
    await users_repo.update_user(user["id"], search_radius=km, search_scope="near")
    await show_settings(message.bot, message.chat.id, state, user["id"],
                        notice=f"<i>Радиус: {km} км</i>")


# ─────────────────────── Смена города / геопозиции ──────────────────────────

async def ask_city_change(bot: Bot, chat_id: int, state: FSMContext, *,
                          back: str = "settings", want_near: bool = False,
                          error: str | None = None) -> None:
    """Вопрос о городе. Геопозицию Telegram даёт только нижней кнопкой —
    рядом с ней «⬅️ Отмена»."""
    await state.set_state(SearchSettings.city)
    await state.update_data(city_back=back, want_near=want_near)
    text = texts.SETTINGS_NEED_GEO if want_near else texts.SETTINGS_CITY
    if error:
        text = f"⚠️ {error}\n\n{text}"
    await screen.send(bot, chat_id, state, text, rkb.request_location(cancel=True))


async def _city_done(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                     notice: str | None) -> None:
    """Город сменили (или передумали) — возвращаемся туда, откуда пришли."""
    back_to = (await state.get_data()).get("city_back") or "settings"
    if back_to == "profile":
        # Анкета импортирует настройки, поэтому здесь — поздний импорт
        from app.handlers import profile as profile_handlers
        await profile_handlers.show_profile(bot, chat_id, state, user_id, notice=notice)
        return
    await show_settings(bot, chat_id, state, user_id, notice=notice)


@router.message(F.text == rkb.CITY)
async def change_city(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_city_change(message.bot, message.chat.id, state, back="settings")


@router.message(SearchSettings.city, F.location)
async def set_city_by_location(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    lat, lon = message.location.latitude, message.location.longitude
    data = await state.get_data()
    city = geo.nearest(lat, lon)
    if city is None:
        await ask_city_change(bot, chat_id, state, back=data.get("city_back") or "settings",
                              error="Не смог определить город по геопозиции — "
                                    "напишите его текстом.")
        return
    safe_lat, safe_lon = geo.jitter(lat, lon)
    fields: dict[str, Any] = dict(city=city.name, region=city.region, country=city.country,
                                  lat=safe_lat, lon=safe_lon, geo_source="gps")
    if data.get("want_near"):
        fields["search_scope"] = "near"
    await users_repo.update_user(user["id"], **fields)
    await _city_done(bot, chat_id, state, user["id"],
                     texts.REG_GEO_SAVED.format(city=profile.esc(city.title)))


@router.message(SearchSettings.city, F.text)
async def set_city_by_name(message: Message, state: FSMContext, user,
                           settings: Settings) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    query = (message.text or "").strip()
    if query in (rkb.CANCEL, rkb.MANUAL_CITY):
        await _city_done(bot, chat_id, state, user["id"], None)
        return

    found = await geo.resolve(query, settings.geocoder_enabled, settings.geocoder_email)
    place = found[0] if found else geo.find_whole_region(query)
    if place is None:
        data = await state.get_data()
        await ask_city_change(bot, chat_id, state, back=data.get("city_back") or "settings",
                              want_near=bool(data.get("want_near")),
                              error=texts.REG_CITY_NOT_FOUND)
        return

    fields: dict[str, Any] = dict(
        city=place.name, region=place.region, country=place.country,
        lat=place.lat, lon=place.lon, geo_source="city" if found else "region",
    )
    # «Сначала те, кто рядом» без настоящей геопозиции обещал бы больше, чем может
    if (await users_repo.get_user(user["id"]))["search_scope"] == "near":
        fields["search_scope"] = "city"
    await users_repo.update_user(user["id"], **fields)
    await _city_done(bot, chat_id, state, user["id"],
                     f"✅ <i>Город: {profile.esc(place.title)}</i>")


@router.message(SearchSettings.city)
async def city_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    data = await state.get_data()
    await ask_city_change(message.bot, message.chat.id, state,
                          back=data.get("city_back") or "settings",
                          want_near=bool(data.get("want_near")),
                          error="Напишите город текстом или отправьте геопозицию.")


# ──────────────────────────── Напоминания ───────────────────────────────────

@router.message(F.text.in_({rkb.NOTIFY_ON, rkb.NOTIFY_OFF}))
async def toggle_notifications(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    fresh = await users_repo.get_user(user["id"])
    enabled = not bool(fresh["notify_enabled"])
    await users_repo.update_user(user["id"], notify_enabled=int(enabled),
                                 notify_count=0)
    await show_settings(message.bot, message.chat.id, state, user["id"],
                        notice="<i>Напоминания включены</i>" if enabled
                        else "<i>Напоминания выключены</i>")


# ──────────────────── Вернуть пропущенные анкеты ────────────────────────────

@router.message(F.text == rkb.RESET_SKIPS_ALL)
async def reset_skips(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    removed = await reactions_repo.reset_dislikes(user["id"], older_than_days=0)
    await show_settings(
        message.bot, message.chat.id, state, user["id"],
        notice=(f"<i>Вернул {removed} анкет в выдачу</i>" if removed
                else "<i>Пропущенных анкет нет</i>"),
    )
