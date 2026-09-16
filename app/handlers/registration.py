"""Пошаговое заполнение анкеты.

Каждый шаг сразу пишется в базу, а флаг registered выставляется только в конце.
Поэтому перезапуск бота или потеря FSM не заставляют начинать сначала —
команда /start продолжает с первого незаполненного поля.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings, get_settings
from app.db import users as users_repo
from app.handlers import menu as menu_handlers
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import geo, profile
from app.services.notify import admin_log
from app.states import Registration

router = Router(name="registration")

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[a-zA-Z0-9_]{4,}|telegram\.me)", re.I)
NAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9 \-'’.]+$")


# ────────────────────────────── Точки входа ─────────────────────────────────

async def start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(Registration.gender)
    await message.answer(texts.REG_GENDER, reply_markup=kb.GENDER)


async def resume(message: Message, state: FSMContext, user: Mapping[str, Any],
                 settings: Settings) -> None:
    """Продолжает анкету с первого незаполненного поля."""
    if not user["gender"]:
        await state.set_state(Registration.gender)
        await message.answer(texts.REG_GENDER, reply_markup=kb.GENDER)
    elif not user["looking_for"]:
        await state.set_state(Registration.looking_for)
        await message.answer(texts.REG_LOOKING, reply_markup=kb.LOOKING_FOR)
    elif not user["age"]:
        await state.set_state(Registration.age)
        await message.answer(texts.REG_AGE)
    elif not user["name"]:
        await state.set_state(Registration.name)
        await message.answer(texts.REG_NAME, reply_markup=kb.USE_TG_NAME)
    elif not user["media_id"]:
        await state.set_state(Registration.media)
        await message.answer(texts.REG_MEDIA.format(sec=settings.max_video_seconds))
    elif user["about"] is None:
        await state.set_state(Registration.about)
        await message.answer(texts.REG_ABOUT.format(max_len=settings.about_max_len),
                             reply_markup=kb.SKIP_ABOUT)
    elif not user["city"]:
        await ask_city(message, state)
    else:
        await show_preview(message, state, user)


async def ask_city(message: Message, state: FSMContext) -> None:
    await state.set_state(Registration.city)
    await message.answer(texts.REG_CITY, reply_markup=rkb.request_location())


async def show_preview(message: Message, state: FSMContext,
                       user: Mapping[str, Any]) -> None:
    fresh = await users_repo.get_user(user["id"])
    await state.set_state(Registration.confirm)
    await message.answer(texts.REG_DONE, reply_markup=rkb.REMOVE)
    await profile.send_card(message.bot, message.chat.id, fresh,
                            markup=kb.CONFIRM_PROFILE, show_distance=False)


# ──────────────────────────── Шаг 1: пол ────────────────────────────────────

@router.callback_query(F.data.startswith("reg:gender:"), Registration.gender)
async def set_gender(call: CallbackQuery, state: FSMContext, user) -> None:
    gender = (call.data or "").split(":")[-1]
    if gender not in {"m", "f"}:
        await call.answer()
        return
    await users_repo.update_user(user["id"], gender=gender)
    await call.answer()
    await call.message.edit_text(
        f"{texts.REG_GENDER}\n\n✅ {'Парень' if gender == 'm' else 'Девушка'}"
    )
    await state.set_state(Registration.looking_for)
    await call.message.answer(texts.REG_LOOKING, reply_markup=kb.LOOKING_FOR)


# ─────────────────────── Шаг 2: кого ищем ───────────────────────────────────

@router.callback_query(F.data.startswith("reg:look:"), Registration.looking_for)
async def set_looking(call: CallbackQuery, state: FSMContext, user) -> None:
    value = (call.data or "").split(":")[-1]
    if value not in {"m", "f", "any"}:
        await call.answer()
        return
    await users_repo.update_user(user["id"], looking_for=value)
    await call.answer()
    await call.message.edit_text(
        f"{texts.REG_LOOKING}\n\n✅ {profile.LOOKING_WORD[value].capitalize()}"
    )
    await state.set_state(Registration.age)
    await call.message.answer(texts.REG_AGE)


# ───────────────────────── Шаг 3: возраст ───────────────────────────────────

@router.message(Registration.age, F.text)
async def set_age(message: Message, state: FSMContext, user, settings: Settings) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return

    age = int(raw)
    if age < settings.min_age:
        await message.answer(texts.REG_AGE_TOO_YOUNG.format(
            min_age=settings.min_age))
        return
    if age > settings.max_age:
        await message.answer(texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return

    # Разумные рамки поиска по умолчанию — пользователь поменяет их в настройках
    await users_repo.update_user(
        user["id"], age=age,
        age_min=max(settings.min_age, age - 5),
        age_max=min(settings.max_age, age + 5),
    )
    await state.set_state(Registration.name)
    await message.answer(texts.REG_NAME, reply_markup=kb.USE_TG_NAME)


# ─────────────────────────── Шаг 4: имя ─────────────────────────────────────

def validate_name(raw: str, settings: Settings) -> str | None:
    name = " ".join((raw or "").split())
    if not (settings.name_min_len <= len(name) <= settings.name_max_len):
        return None
    if LINK_RE.search(name) or not NAME_RE.match(name):
        return None
    return name


@router.callback_query(F.data == "reg:tgname", Registration.name)
async def use_tg_name(call: CallbackQuery, state: FSMContext, user,
                      settings: Settings) -> None:
    name = validate_name((call.from_user.first_name or "").strip(), settings)
    if not name:
        await call.answer("Имя из Telegram не подходит — напишите вручную",
                          show_alert=True)
        return
    await users_repo.update_user(user["id"], name=name)
    await call.answer()
    await call.message.edit_text(f"{texts.REG_NAME}\n\n✅ {name}")
    await state.set_state(Registration.media)
    await call.message.answer(texts.REG_MEDIA.format(sec=settings.max_video_seconds))


@router.message(Registration.name, F.text)
async def set_name(message: Message, state: FSMContext, user, settings: Settings) -> None:
    name = validate_name(message.text or "", settings)
    if not name:
        await message.answer(texts.REG_NAME_BAD.format(
            min_len=settings.name_min_len, max_len=settings.name_max_len))
        return
    await users_repo.update_user(user["id"], name=name)
    await state.set_state(Registration.media)
    await message.answer(texts.REG_MEDIA.format(sec=settings.max_video_seconds))


# ─────────────────────── Шаг 5: фото или видео ──────────────────────────────

@router.message(Registration.media)
async def set_media(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    result = profile.extract_media(message, settings.max_video_seconds)
    if result == "long":
        await message.answer(texts.REG_MEDIA_TOO_LONG.format(
            sec=settings.max_video_seconds))
        return
    if result == "file":
        await message.answer(texts.REG_MEDIA_AS_FILE)
        return
    if result == "bad":
        await message.answer(texts.REG_MEDIA_BAD)
        return

    media_type, media_id = result
    await users_repo.update_user(user["id"], media_type=media_type, media_id=media_id)
    await state.set_state(Registration.about)
    await message.answer(texts.REG_ABOUT.format(max_len=settings.about_max_len),
                         reply_markup=kb.SKIP_ABOUT)


# ───────────────────────── Шаг 6: о себе ────────────────────────────────────

@router.callback_query(F.data == "reg:skip_about", Registration.about)
async def skip_about(call: CallbackQuery, state: FSMContext, user) -> None:
    await users_repo.update_user(user["id"], about="")
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_city(call.message, state)


@router.message(Registration.about, F.text)
async def set_about(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    about = (message.text or "").strip()
    if len(about) > settings.about_max_len:
        await message.answer(texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len))
        return
    if LINK_RE.search(about):
        await message.answer(texts.REG_ABOUT_LINKS)
        return
    await users_repo.update_user(user["id"], about=about)
    await ask_city(message, state)


# ─────────────────────── Шаг 7: город и геопозиция ──────────────────────────

async def save_city(user_id: int, city: geo.City, *, lat: float, lon: float,
                    source: str) -> None:
    await users_repo.update_user(
        user_id, city=city.name, region=city.region, country=city.country,
        lat=lat, lon=lon, geo_source=source,
    )


async def ask_scope(message: Message, state: FSMContext, user_id: int,
                    next_state, prefix: str = "reg") -> None:
    user = await users_repo.get_user(user_id)
    settings = get_settings()
    has_coords = user["geo_source"] == "gps"
    await state.set_state(next_state)
    await message.answer(
        texts.REG_SCOPE.format(
            city=user["city"], region=user["region"] or "—",
            radius=user["search_radius"] or settings.default_radius_km,
        ),
        reply_markup=kb.scope(user["city"], user["region"] or "", has_coords, prefix),
    )


@router.message(Registration.city, F.location)
@router.message(Registration.region_fallback, F.location)
async def set_location(message: Message, state: FSMContext, user) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    city = geo.nearest(lat, lon)
    if city is None:
        await message.answer(
            "Не удалось определить город по геопозиции. Напишите его название текстом."
        )
        return

    # Координаты храним со сдвигом ~350 м: расстояние не страдает,
    # а восстановить адрес по лайкам нельзя
    safe_lat, safe_lon = geo.jitter(lat, lon)
    await save_city(user["id"], city, lat=safe_lat, lon=safe_lon, source="gps")
    await message.answer(texts.REG_GEO_SAVED.format(city=city.title),
                         reply_markup=rkb.REMOVE)
    await ask_scope(message, state, user["id"], Registration.scope)


@router.message(Registration.city, F.text)
async def set_city(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    query = (message.text or "").strip()
    if query == "✍️ Ввести город вручную":
        await message.answer("Напишите название города:", reply_markup=rkb.REMOVE)
        return

    found = await geo.resolve(query, settings.geocoder_enabled, settings.geocoder_email)

    if len(found) == 1:
        city = found[0]
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await message.answer(f"✅ {city.title}", reply_markup=rkb.REMOVE)
        await ask_scope(message, state, user["id"], Registration.scope)
        return

    if len(found) > 1:
        await state.update_data(city_options=[
            {"name": c.name, "region": c.region, "country": c.country,
             "lat": c.lat, "lon": c.lon} for c in found
        ])
        await message.answer(texts.REG_CITY_CHOICE, reply_markup=rkb.REMOVE)
        await message.answer("Выберите:", reply_markup=kb.city_choices(found))
        return

    # Города нет в справочнике — спрашиваем область, чтобы поиск всё же работал
    await state.update_data(pending_city=query[:60])
    await state.set_state(Registration.region_fallback)
    await message.answer(
        texts.REG_CITY_NOT_FOUND + "\n\n🗺 Или напишите вашу <b>область / регион</b> — "
        "например, <code>Волгоградская область</code>. Тогда я буду искать по области.",
        reply_markup=rkb.request_location(),
    )


@router.message(Registration.region_fallback, F.text)
async def set_region_fallback(message: Message, state: FSMContext, user) -> None:
    data = await state.get_data()
    pending = data.get("pending_city") or (message.text or "").strip()

    # Вдруг со второй попытки написали существующий город
    found = geo.find(message.text or "")
    if found:
        city = found[0]
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await message.answer(f"✅ {city.title}", reply_markup=rkb.REMOVE)
        await ask_scope(message, state, user["id"], Registration.scope)
        return

    anchors = geo.find_region(message.text or "")
    if not anchors:
        await message.answer(
            "Не нашёл такой регион. Напишите область целиком "
            "(<code>Тверская область</code>, <code>Пермский край</code>) "
            "или отправьте геопозицию."
        )
        return

    anchor = anchors[0]
    await users_repo.update_user(
        user["id"], city=pending.title(), region=anchor.region,
        country=anchor.country, lat=anchor.lat, lon=anchor.lon, geo_source="region",
    )
    await message.answer(
        f"✅ {pending.title()}, {anchor.region}", reply_markup=rkb.REMOVE
    )
    await ask_scope(message, state, user["id"], Registration.scope)


@router.callback_query(F.data.startswith("reg:city:"), Registration.city)
async def pick_city(call: CallbackQuery, state: FSMContext, user) -> None:
    suffix = (call.data or "").split(":")[-1]
    if suffix == "retry":
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("Напишите название города:")
        return

    data = await state.get_data()
    options = data.get("city_options") or []
    try:
        chosen = options[int(suffix)]
    except (ValueError, IndexError):
        await call.answer()
        return

    city = geo.City(chosen["name"], chosen["region"], chosen["country"],
                    chosen["lat"], chosen["lon"])
    await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
    await call.answer()
    await call.message.edit_text(f"✅ {city.title}")
    await ask_scope(call.message, state, user["id"], Registration.scope)


@router.callback_query(F.data.startswith("reg:scope:"), Registration.scope)
async def set_scope(call: CallbackQuery, state: FSMContext, user,
                    settings: Settings) -> None:
    scope = (call.data or "").split(":")[-1]
    if scope not in {"city", "region", "near"}:
        await call.answer()
        return
    await users_repo.update_user(
        user["id"], search_scope=scope,
        search_radius=user["search_radius"] or settings.default_radius_km,
    )
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    fresh = await users_repo.get_user(user["id"])
    await show_preview(call.message, state, fresh)


# ───────────────────────── Подтверждение ────────────────────────────────────

@router.callback_query(F.data == "reg:confirm", Registration.confirm)
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                  settings: Settings, is_admin: bool) -> None:
    await users_repo.update_user(user["id"], registered=1, is_active=1)
    await state.clear()
    await call.answer("Готово!")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    fresh = await users_repo.get_user(user["id"])
    await menu_handlers.show_main_menu(
        call.message, fresh, is_admin,
        text="🎉 Анкета опубликована! Начинайте смотреть анкеты 👇",
    )
    await admin_log(
        bot,
        f"✅ Анкета заполнена: <b>{profile.esc(fresh['name'])}</b>, "
        f"{fresh['age']} · {profile.GENDER_WORD.get(fresh['gender'], '')} · "
        f"{profile.esc(fresh['city'] or '—')}\n"
        f"<code>{fresh['id']}</code> @{fresh['username'] or '—'}"
    )


@router.callback_query(F.data == "reg:restart", Registration.confirm)
async def restart(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await start(call.message, state, settings)


# ────────────── Подсказки, если на шаге прислали не то ──────────────────────

@router.message(Registration.gender)
async def gender_hint(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой 👆", reply_markup=kb.GENDER)


@router.message(Registration.looking_for)
async def looking_hint(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой 👆", reply_markup=kb.LOOKING_FOR)


@router.message(Registration.about)
async def about_hint(message: Message, settings: Settings) -> None:
    await message.answer(
        "Напишите пару слов текстом или нажмите «Пропустить».",
        reply_markup=kb.SKIP_ABOUT,
    )


@router.message(Registration.city)
@router.message(Registration.region_fallback)
async def city_hint(message: Message) -> None:
    await message.answer(
        "Напишите название города текстом или отправьте геопозицию кнопкой ниже.",
        reply_markup=rkb.request_location(),
    )
