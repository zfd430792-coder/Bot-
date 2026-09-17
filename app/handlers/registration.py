"""Пошаговое заполнение анкеты.

Каждый шаг сразу пишется в базу, а флаг registered выставляется только в конце.
Поэтому перезапуск бота или потеря FSM не заставляют начинать сначала —
команда /start продолжает с первого незаполненного поля.

Диалог живёт одним экраном: перед новым вопросом бот удаляет и предыдущий
вопрос, и ответ пользователя. В чате всегда видно ровно текущий шаг, а сверху
короткой строкой — то, что уже заполнено.
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
from app.services import geo, profile, screen
from app.services.notify import admin_log
from app.states import Registration

router = Router(name="registration")

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[a-zA-Z0-9_]{4,}|telegram\.me)", re.I)
NAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9 \-'’.]+$")

GENDER_TITLE = {"m": "парень", "f": "девушка"}


def progress(user: Mapping[str, Any] | None) -> str:
    """Короткая сводка заполненного — вместо отдельных сообщений «✅ принято»."""
    if user is None:
        return ""
    parts: list[str] = []
    if user["gender"]:
        parts.append(GENDER_TITLE.get(user["gender"], ""))
    if user["looking_for"]:
        parts.append(f"ищу {profile.LOOKING_WORD.get(user['looking_for'], '')}")
    if user["age"]:
        parts.append(profile.years(user["age"]))
    if user["name"]:
        parts.append(profile.esc(user["name"]))
    if user["media_id"]:
        parts.append("фото")
    if user["about"]:
        parts.append("о себе")
    if user["city"]:
        parts.append(profile.esc(user["city"]))
    if not parts:
        return ""
    return "✅ <i>" + " · ".join(p for p in parts if p) + "</i>\n\n"


async def _step(bot: Bot, chat_id: int, state: FSMContext, text: str,
                markup=None, error: str | None = None) -> None:
    """Показывает шаг единственным сообщением, заменяя предыдущее."""
    user = await users_repo.get_user(chat_id)
    body = (f"⚠️ {error}\n\n" if error else "") + progress(user) + text
    await screen.send(bot, chat_id, state, body, markup)


# ──────────────────────────── Экраны шагов ──────────────────────────────────

async def ask_gender(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.gender)
    await _step(bot, chat_id, state, texts.REG_GENDER, kb.GENDER, error)


async def ask_looking(bot: Bot, chat_id: int, state: FSMContext,
                      error: str | None = None) -> None:
    await state.set_state(Registration.looking_for)
    await _step(bot, chat_id, state, texts.REG_LOOKING, kb.LOOKING_FOR, error)


async def ask_age(bot: Bot, chat_id: int, state: FSMContext,
                  error: str | None = None) -> None:
    await state.set_state(Registration.age)
    await _step(bot, chat_id, state, texts.REG_AGE, None, error)


async def ask_name(bot: Bot, chat_id: int, state: FSMContext,
                   error: str | None = None) -> None:
    await state.set_state(Registration.name)
    await _step(bot, chat_id, state, texts.REG_NAME, kb.USE_TG_NAME, error)


async def ask_media(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.media)
    await _step(bot, chat_id, state,
                texts.REG_MEDIA.format(sec=settings.max_video_seconds), None, error)


async def ask_about(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.about)
    await _step(bot, chat_id, state,
                texts.REG_ABOUT.format(max_len=settings.about_max_len),
                kb.SKIP_ABOUT, error)


async def ask_city(bot: Bot, chat_id: int, state: FSMContext,
                   error: str | None = None) -> None:
    await state.set_state(Registration.city)
    await _step(bot, chat_id, state, texts.REG_CITY,
                rkb.request_location(), error)


async def ask_region(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.region_fallback)
    await _step(
        bot, chat_id, state,
        texts.REG_CITY_NOT_FOUND + "\n\n🗺 Или напишите вашу <b>область / "
        "регион</b> — например, <code>Волгоградская область</code>. "
        "Тогда я буду искать по области.",
        rkb.request_location(), error,
    )


async def ask_scope(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Город определён — спрашиваем охват поиска и убираем нижнюю клавиатуру."""
    user = await users_repo.get_user(chat_id)
    settings = get_settings()
    await screen.hide_reply_keyboard(bot, chat_id)
    await state.set_state(Registration.scope)

    # Приём геопозиции подтверждаем прямо здесь: отдельное сообщение ради
    # одной строки — как раз то, от чего мы уходим
    note = ""
    if user["geo_source"] == "gps":
        place = user["city"]
        if user["region"] and user["region"] != user["city"]:
            place += f", {user['region']}"
        note = texts.REG_GEO_SAVED.format(city=profile.esc(place)) + "\n\n"

    await _step(
        bot, chat_id, state,
        note + texts.REG_SCOPE.format(
            city=user["city"], region=user["region"] or "—",
            radius=user["search_radius"] or settings.default_radius_km,
        ),
        kb.scope(user["city"], user["region"] or "", user["geo_source"] == "gps"),
    )


async def show_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    fresh = await users_repo.get_user(chat_id)
    await state.set_state(Registration.confirm)
    await screen.clear(bot, chat_id, state)
    await screen.hide_reply_keyboard(bot, chat_id)

    header = await bot.send_message(chat_id, texts.REG_DONE)
    card = await profile.send_card(bot, chat_id, fresh,
                                   markup=kb.CONFIRM_PROFILE, show_distance=False)
    await screen.remember(state, [header.message_id, *card])


# ────────────────────────────── Точки входа ─────────────────────────────────

async def start(message: Message, state: FSMContext, settings: Settings) -> None:
    await ask_gender(message.bot, message.chat.id, state)


async def resume(message: Message, state: FSMContext, user: Mapping[str, Any],
                 settings: Settings) -> None:
    """Продолжает анкету с первого незаполненного поля."""
    bot, chat_id = message.bot, message.chat.id
    if not user["gender"]:
        await ask_gender(bot, chat_id, state)
    elif not user["looking_for"]:
        await ask_looking(bot, chat_id, state)
    elif not user["age"]:
        await ask_age(bot, chat_id, state)
    elif not user["name"]:
        await ask_name(bot, chat_id, state)
    elif not user["media_id"]:
        await ask_media(bot, chat_id, state, settings)
    elif user["about"] is None:
        await ask_about(bot, chat_id, state, settings)
    elif not user["city"]:
        await ask_city(bot, chat_id, state)
    else:
        await show_preview(bot, chat_id, state)


# ──────────────────────────── Шаг 1: пол ────────────────────────────────────

@router.callback_query(F.data.startswith("reg:gender:"), Registration.gender)
async def set_gender(call: CallbackQuery, state: FSMContext, user) -> None:
    gender = (call.data or "").split(":")[-1]
    if gender not in {"m", "f"}:
        await call.answer()
        return
    await users_repo.update_user(user["id"], gender=gender)
    await call.answer()
    await ask_looking(call.bot, call.message.chat.id, state)


# ─────────────────────── Шаг 2: кого ищем ───────────────────────────────────

@router.callback_query(F.data.startswith("reg:look:"), Registration.looking_for)
async def set_looking(call: CallbackQuery, state: FSMContext, user) -> None:
    value = (call.data or "").split(":")[-1]
    if value not in {"m", "f", "any"}:
        await call.answer()
        return
    await users_repo.update_user(user["id"], looking_for=value)
    await call.answer()
    await ask_age(call.bot, call.message.chat.id, state)


# ───────────────────────── Шаг 3: возраст ───────────────────────────────────

@router.message(Registration.age, F.text)
async def set_age(message: Message, state: FSMContext, user, settings: Settings) -> None:
    await screen.drop(message)
    raw = (message.text or "").strip()
    bot, chat_id = message.bot, message.chat.id

    if not raw.isdigit():
        await ask_age(bot, chat_id, state, texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return

    age = int(raw)
    if age < settings.min_age:
        await ask_age(bot, chat_id, state,
                      texts.REG_AGE_TOO_YOUNG.format(min_age=settings.min_age))
        return
    if age > settings.max_age:
        await ask_age(bot, chat_id, state, texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return

    # Разумные рамки поиска по умолчанию — пользователь поменяет их в настройках
    await users_repo.update_user(
        user["id"], age=age,
        age_min=max(settings.min_age, age - 5),
        age_max=min(settings.max_age, age + 5),
    )
    await ask_name(bot, chat_id, state)


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
    await ask_media(call.bot, call.message.chat.id, state, settings)


@router.message(Registration.name, F.text)
async def set_name(message: Message, state: FSMContext, user, settings: Settings) -> None:
    await screen.drop(message)
    name = validate_name(message.text or "", settings)
    if not name:
        await ask_name(message.bot, message.chat.id, state,
                       texts.REG_NAME_BAD.format(min_len=settings.name_min_len,
                                                 max_len=settings.name_max_len))
        return
    await users_repo.update_user(user["id"], name=name)
    await ask_media(message.bot, message.chat.id, state, settings)


# ─────────────────────── Шаг 5: фото или видео ──────────────────────────────

@router.message(Registration.media)
async def set_media(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    result = profile.extract_media(message, settings.max_video_seconds)
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id

    errors = {
        "long": texts.REG_MEDIA_TOO_LONG.format(sec=settings.max_video_seconds),
        "file": texts.REG_MEDIA_AS_FILE,
        "bad": texts.REG_MEDIA_BAD,
    }
    if isinstance(result, str):
        await ask_media(bot, chat_id, state, settings, errors[result])
        return

    media_type, media_id = result
    await users_repo.update_user(user["id"], media_type=media_type, media_id=media_id)
    await ask_about(bot, chat_id, state, settings)


# ───────────────────────── Шаг 6: о себе ────────────────────────────────────

@router.callback_query(F.data == "reg:skip_about", Registration.about)
async def skip_about(call: CallbackQuery, state: FSMContext, user) -> None:
    await users_repo.update_user(user["id"], about="")
    await call.answer()
    await ask_city(call.bot, call.message.chat.id, state)


@router.message(Registration.about, F.text)
async def set_about(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    await screen.drop(message)
    about = (message.text or "").strip()
    bot, chat_id = message.bot, message.chat.id

    if len(about) > settings.about_max_len:
        await ask_about(bot, chat_id, state, settings,
                        texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len))
        return
    if LINK_RE.search(about):
        await ask_about(bot, chat_id, state, settings, texts.REG_ABOUT_LINKS)
        return

    await users_repo.update_user(user["id"], about=about)
    await ask_city(bot, chat_id, state)


# ─────────────────────── Шаг 7: город и геопозиция ──────────────────────────

async def save_city(user_id: int, city: geo.City, *, lat: float, lon: float,
                    source: str) -> None:
    await users_repo.update_user(
        user_id, city=city.name, region=city.region, country=city.country,
        lat=lat, lon=lon, geo_source=source,
    )


@router.message(Registration.city, F.location)
@router.message(Registration.region_fallback, F.location)
async def set_location(message: Message, state: FSMContext, user) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id

    city = geo.nearest(lat, lon)
    if city is None:
        await ask_city(bot, chat_id, state,
                       "Не удалось определить город по геопозиции. "
                       "Напишите его название текстом.")
        return

    # Координаты храним со сдвигом ~350 м: расстояние не страдает,
    # а восстановить адрес по лайкам нельзя
    safe_lat, safe_lon = geo.jitter(lat, lon)
    await save_city(user["id"], city, lat=safe_lat, lon=safe_lon, source="gps")
    await ask_scope(bot, chat_id, state)


@router.message(Registration.city, F.text)
async def set_city(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    query = (message.text or "").strip()
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id

    if query == "✍️ Ввести город вручную":
        await ask_city(bot, chat_id, state)
        return

    found = await geo.resolve(query, settings.geocoder_enabled, settings.geocoder_email)

    if len(found) == 1:
        city = found[0]
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await ask_scope(bot, chat_id, state)
        return

    if len(found) > 1:
        await state.update_data(city_options=[
            {"name": c.name, "region": c.region, "country": c.country,
             "lat": c.lat, "lon": c.lon} for c in found
        ])
        await _step(bot, chat_id, state, texts.REG_CITY_CHOICE,
                    kb.city_choices(found))
        return

    # Города нет в справочнике — спрашиваем область, чтобы поиск всё же работал
    await state.update_data(pending_city=query[:60])
    await ask_region(bot, chat_id, state)


@router.message(Registration.region_fallback, F.text)
async def set_region_fallback(message: Message, state: FSMContext, user) -> None:
    query = (message.text or "").strip()
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id

    data = await state.get_data()
    pending = data.get("pending_city") or query

    # Вдруг со второй попытки написали существующий город
    found = geo.find(query)
    if found:
        city = found[0]
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await ask_scope(bot, chat_id, state)
        return

    anchors = geo.find_region(query)
    if not anchors:
        await ask_region(
            bot, chat_id, state,
            "Не нашёл такой регион. Напишите область целиком "
            "(<code>Тверская область</code>, <code>Пермский край</code>) "
            "или отправьте геопозицию.",
        )
        return

    anchor = anchors[0]
    await users_repo.update_user(
        user["id"], city=pending.title(), region=anchor.region,
        country=anchor.country, lat=anchor.lat, lon=anchor.lon, geo_source="region",
    )
    await ask_scope(bot, chat_id, state)


@router.callback_query(F.data.startswith("reg:city:"), Registration.city)
async def pick_city(call: CallbackQuery, state: FSMContext, user) -> None:
    suffix = (call.data or "").split(":")[-1]
    await call.answer()

    if suffix == "retry":
        await ask_city(call.bot, call.message.chat.id, state)
        return

    data = await state.get_data()
    options = data.get("city_options") or []
    try:
        chosen = options[int(suffix)]
    except (ValueError, IndexError):
        return

    city = geo.City(chosen["name"], chosen["region"], chosen["country"],
                    chosen["lat"], chosen["lon"])
    await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
    await ask_scope(call.bot, call.message.chat.id, state)


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
    await show_preview(call.bot, call.message.chat.id, state)


# ───────────────────────── Подтверждение ────────────────────────────────────

@router.callback_query(F.data == "reg:confirm", Registration.confirm)
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                  settings: Settings, is_admin: bool) -> None:
    await users_repo.update_user(user["id"], registered=1, is_active=1)
    await call.answer("Готово!")
    await screen.clear(bot, call.message.chat.id, state)
    await state.clear()

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
async def restart(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask_gender(call.bot, call.message.chat.id, state)


# ────────────── Подсказки, если на шаге прислали не то ──────────────────────

@router.message(Registration.gender)
async def gender_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_gender(message.bot, message.chat.id, state,
                     "Выберите вариант кнопкой ниже.")


@router.message(Registration.looking_for)
async def looking_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_looking(message.bot, message.chat.id, state,
                      "Выберите вариант кнопкой ниже.")


@router.message(Registration.about)
async def about_hint(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    await ask_about(message.bot, message.chat.id, state, settings,
                    "Напишите пару слов текстом или нажмите «Пропустить».")


@router.message(Registration.city)
async def city_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_city(message.bot, message.chat.id, state,
                   "Напишите название города текстом или отправьте геопозицию.")


@router.message(Registration.region_fallback)
async def region_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_region(message.bot, message.chat.id, state,
                     "Напишите название области текстом или отправьте геопозицию.")
