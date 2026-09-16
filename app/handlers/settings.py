"""Настройки поиска: возраст, город/геопозиция, охват, радиус."""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.handlers import menu as menu_handlers
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import geo
from app.states import SearchSettings

router = Router(name="settings")

RANGE_RE = re.compile(r"^\s*(\d{1,3})\s*[-–—: ]\s*(\d{1,3})\s*$")

SCOPE_TITLE = {
    "city": "только мой город",
    "region": "вся область / регион",
    "near": "по расстоянию",
}


def render(user: Mapping[str, Any]) -> str:
    scope = SCOPE_TITLE.get(user["search_scope"] or "city", "город")
    place = user["city"] or "не указан"
    if user["region"] and user["region"] != user["city"]:
        place += f", {user['region']}"
    geo_line = ("📍 геопозиция передана" if user["geo_source"] == "gps"
                else "📍 геопозиция не передана")
    radius = (f"\n📏 Радиус: <b>{user['search_radius']} км</b>"
              if (user["search_scope"] == "near") else "")
    notify = ("🔔 Напоминания включены" if user["notify_enabled"]
              else "🔕 Напоминания выключены")
    return (
        f"{texts.SETTINGS_TITLE}\n\n"
        f"🎂 Возраст: <b>{user['age_min']}–{user['age_max']}</b>\n"
        f"🔍 Ищу: <b>{ {'m': 'парней', 'f': 'девушек'}.get(user['looking_for'], 'всех') }</b>\n"
        f"🌍 Город: <b>{place}</b>\n"
        f"🎯 Охват: <b>{scope}</b>{radius}\n"
        f"{geo_line}\n{notify}"
    )


async def show(message: Message, state: FSMContext, user_id: int) -> None:
    user = await users_repo.get_user(user_id)
    await state.set_state(SearchSettings.menu)
    await message.answer(
        render(user),
        reply_markup=kb.settings(user["search_scope"] or "city",
                                 has_coords=user["geo_source"] == "gps",
                                 notify_enabled=bool(user["notify_enabled"])),
    )


@router.message(Command("settings"))
@router.message(F.text == rkb.BTN_SETTINGS)
async def open_settings(message: Message, state: FSMContext,
                        user: Mapping[str, Any]) -> None:
    if not user["registered"]:
        await message.answer("Сначала заполните анкету — /start")
        return
    await show(message, state, user["id"])


@router.callback_query(F.data == "st:close")
async def close(call: CallbackQuery, state: FSMContext, user, is_admin: bool) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await menu_handlers.show_main_menu(call.message, user, is_admin)


@router.callback_query(F.data == "st:back")
async def back(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    fresh = await users_repo.get_user(user["id"])
    await call.message.edit_text(
        render(fresh),
        reply_markup=kb.settings(fresh["search_scope"] or "city",
                                 has_coords=fresh["geo_source"] == "gps",
                                 notify_enabled=bool(fresh["notify_enabled"])),
    )


# ───────────────────────── Возрастной диапазон ──────────────────────────────

@router.callback_query(F.data == "st:age")
async def ask_age_range(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchSettings.age_range)
    await call.answer()
    await call.message.answer(texts.SETTINGS_AGE)


@router.message(SearchSettings.age_range, F.text)
async def set_age_range(message: Message, state: FSMContext, user,
                        settings: Settings) -> None:
    match = RANGE_RE.match(message.text or "")
    if not match:
        await message.answer(texts.SETTINGS_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return
    low, high = int(match.group(1)), int(match.group(2))
    low, high = min(low, high), max(low, high)
    if low < settings.min_age or high > settings.max_age:
        await message.answer(texts.SETTINGS_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return
    await users_repo.update_user(user["id"], age_min=low, age_max=high)
    await message.answer(texts.SETTINGS_SAVED)
    await show(message, state, user["id"])


# ──────────────────────────── Охват поиска ──────────────────────────────────

@router.callback_query(F.data.startswith("st:scope:"))
async def set_scope(call: CallbackQuery, state: FSMContext, user) -> None:
    scope = (call.data or "").split(":")[-1]
    fresh = await users_repo.get_user(user["id"])

    if scope == "near" and fresh["geo_source"] != "gps":
        await call.answer()
        await state.set_state(SearchSettings.city)
        await call.message.answer(texts.SETTINGS_NEED_GEO,
                                  reply_markup=rkb.request_location(with_skip=True))
        return
    if scope == "region" and not fresh["region"]:
        await call.answer("Для поиска по области сначала укажите город", show_alert=True)
        return

    await users_repo.update_user(user["id"], search_scope=scope)
    await call.answer(texts.SETTINGS_SAVED)
    await back(call, state, user)


@router.callback_query(F.data == "st:radius")
async def ask_radius(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(texts.SETTINGS_RADIUS, reply_markup=kb.radius_choices())


@router.callback_query(F.data.startswith("st:radius:"))
async def set_radius(call: CallbackQuery, state: FSMContext, user,
                     settings: Settings) -> None:
    km = int((call.data or "0").split(":")[-1])
    km = max(1, min(settings.max_radius_km, km))
    await users_repo.update_user(user["id"], search_radius=km, search_scope="near")
    await call.answer(f"Радиус: {km} км")
    await back(call, state, user)


# ─────────────────────── Смена города / геопозиции ──────────────────────────

async def ask_city_change(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchSettings.city)
    await message.answer(
        "🌍 Напишите новый город или отправьте геопозицию.\n\n"
        "<i>Геопозиция открывает поиск «по расстоянию» и показывает, "
        "кто рядом.</i>",
        reply_markup=rkb.request_location(),
    )


@router.callback_query(F.data == "st:city")
async def change_city(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask_city_change(call.message, state)


@router.message(SearchSettings.city, F.location)
async def set_city_by_location(message: Message, state: FSMContext, user) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    city = geo.nearest(lat, lon)
    if city is None:
        await message.answer("Не смог определить город по геопозиции — напишите его текстом.")
        return
    safe_lat, safe_lon = geo.jitter(lat, lon)
    await users_repo.update_user(
        user["id"], city=city.name, region=city.region, country=city.country,
        lat=safe_lat, lon=safe_lon, geo_source="gps",
    )
    await message.answer(texts.REG_GEO_SAVED.format(city=city.title),
                         reply_markup=rkb.REMOVE)
    await show(message, state, user["id"])


@router.message(SearchSettings.city, F.text)
async def set_city_by_name(message: Message, state: FSMContext, user,
                           settings: Settings) -> None:
    query = (message.text or "").strip()
    if query == "✍️ Ввести город вручную":
        await message.answer("Напишите название города:", reply_markup=rkb.REMOVE)
        return

    found = await geo.resolve(query, settings.geocoder_enabled, settings.geocoder_email)
    if not found:
        anchors = geo.find_region(query)
        if anchors:
            anchor = anchors[0]
            await users_repo.update_user(
                user["id"], city=anchor.name, region=anchor.region,
                country=anchor.country, lat=anchor.lat, lon=anchor.lon,
                geo_source="region",
            )
            await message.answer(f"✅ {anchor.title}", reply_markup=rkb.REMOVE)
            await show(message, state, user["id"])
            return
        await message.answer(texts.REG_CITY_NOT_FOUND)
        return

    city = found[0]
    await users_repo.update_user(
        user["id"], city=city.name, region=city.region, country=city.country,
        lat=city.lat, lon=city.lon, geo_source="city",
    )
    # Поиск «по расстоянию» без реальной геопозиции обещал бы больше, чем может
    fresh = await users_repo.get_user(user["id"])
    if fresh["search_scope"] == "near":
        await users_repo.update_user(user["id"], search_scope="city")
    await message.answer(f"✅ {city.title}", reply_markup=rkb.REMOVE)
    await show(message, state, user["id"])


# ──────────────────────────── Напоминания ───────────────────────────────────

@router.callback_query(F.data == "st:notify")
async def toggle_notifications(call: CallbackQuery, state: FSMContext, user) -> None:
    fresh = await users_repo.get_user(user["id"])
    enabled = not bool(fresh["notify_enabled"])
    await users_repo.update_user(user["id"], notify_enabled=int(enabled),
                                 notify_count=0)
    await call.answer("Напоминания включены" if enabled else "Напоминания выключены")
    await back(call, state, user)


@router.callback_query(F.data == "remind:off")
async def unsubscribe(call: CallbackQuery, user) -> None:
    """Отписка прямо из напоминания — без захода в настройки."""
    await users_repo.update_user(user["id"], notify_enabled=0)
    await call.answer("Больше не напомню")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        "🔕 Напоминания выключены. Включить обратно можно в ⚙️ Настройках поиска."
    )


# ──────────────────── Вернуть пропущенные анкеты ────────────────────────────

@router.callback_query(F.data == "st:reset_skips")
async def reset_skips(call: CallbackQuery, state: FSMContext, user) -> None:
    removed = await reactions_repo.reset_dislikes(user["id"], older_than_days=0)
    await call.answer(
        f"Вернул {removed} анкет в выдачу" if removed else "Пропущенных анкет нет",
        show_alert=True,
    )
