"""Пошаговое заполнение анкеты.

Каждый шаг сразу пишется в базу, а флаг registered выставляется только в конце.
Поэтому перезапуск бота или потеря FSM не заставляют начинать сначала —
команда /start продолжает с первого незаполненного поля.

Диалог живёт одним экраном: каждый вопрос приходит со своими нижними
кнопками вместо прежнего, ответ пользователя удаляется. В чате всегда видно
ровно текущий шаг, а сверху короткой строкой — то, что уже заполнено.

Отсюда же «🔄 Заполнить анкету заново» из «Моей анкеты»: те же шаги, только
анкета остаётся опубликованной, а каждый ответ заменяет прежний.

Вопроса «где искать» нет: лента сама идёт от ближних к дальним, как в
Дайвинчике, — город, область, а соседние области с согласия человека.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import Settings
from app.db import users as users_repo
from app.db.database import norm_text
from app.handlers import menu as menu_handlers
from app.keyboards import reply as rkb
from app.services import geo, profile, screen
from app.services.notify import admin_log
from app.states import Registration

router = Router(name="registration")

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[a-zA-Z0-9_]{4,}|telegram\.me)", re.I)

# В имени оставляем буквы любого алфавита: среди пользователей есть Айгүл,
# Олексій и Ա — отвергать их имена целиком было бы дико. Эмодзи и прочие
# украшения просто убираем, а не заставляем человека переписывать имя.
NAME_EXTRA_CHARS = " -'’."

GENDER_TITLE = {"m": "парень", "f": "девушка"}
GENDER_BY_BUTTON = {rkb.GENDER_M: "m", rkb.GENDER_F: "f"}
LOOKING_BY_BUTTON = {rkb.LOOK_M: "m", rkb.LOOK_F: "f", rkb.LOOK_ANY: "any"}


def progress(user: Mapping[str, Any] | None, step: int) -> str:
    """Короткая сводка заполненного — вместо отдельных сообщений «✅ принято».

    Берём только шаги до текущего: когда анкету заполняют заново, старые
    ответы на следующие шаги ещё в базе, но они уже не в счёт.
    """
    if user is None:
        return ""
    answers = [
        GENDER_TITLE.get(user["gender"] or "", ""),
        f"ищу {profile.LOOKING_WORD.get(user['looking_for'], '')}" if user["looking_for"] else "",
        profile.years(user["age"]) if user["age"] else "",
        profile.esc(user["name"]) if user["name"] else "",
        "фото" if user["media_id"] else "",
        "о себе" if user["about"] else "",
    ]
    parts = [answer for answer in answers[:step - 1] if answer]
    if not parts:
        return ""
    return "✅ <i>" + " · ".join(parts) + "</i>\n\n"


async def _step(bot: Bot, chat_id: int, state: FSMContext, step: int, text: str,
                markup=None, error: str | None = None) -> None:
    """Показывает шаг единственным сообщением вместо предыдущего."""
    user = await users_repo.get_user(chat_id)
    body = (f"⚠️ {error}\n\n" if error else "") + progress(user, step) + text
    await screen.send(bot, chat_id, state, body, markup)


# ──────────────────────────── Экраны шагов ──────────────────────────────────

async def ask_gender(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.gender)
    await _step(bot, chat_id, state, 1, texts.REG_GENDER, rkb.GENDER, error)


async def ask_looking(bot: Bot, chat_id: int, state: FSMContext,
                      error: str | None = None) -> None:
    await state.set_state(Registration.looking_for)
    await _step(bot, chat_id, state, 2, texts.REG_LOOKING, rkb.LOOKING, error)


async def ask_age(bot: Bot, chat_id: int, state: FSMContext,
                  error: str | None = None) -> None:
    await state.set_state(Registration.age)
    await _step(bot, chat_id, state, 3, texts.REG_AGE, rkb.REMOVE, error)


async def ask_name(bot: Bot, chat_id: int, state: FSMContext,
                   settings: Settings, tg_name: str = "",
                   error: str | None = None) -> None:
    await state.set_state(Registration.name)
    # Имя из Telegram — одной кнопкой, но только если им реально можно
    # пользоваться: кнопка, которая всегда отвечает «не подходит», хуже её отсутствия
    suggestion = validate_name(tg_name, settings)
    await _step(bot, chat_id, state, 4, texts.REG_NAME,
                rkb.name_suggestion(suggestion), error)


async def ask_media(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.media)
    await _step(bot, chat_id, state, 5,
                texts.REG_MEDIA.format(sec=settings.max_video_seconds), rkb.REMOVE, error)


async def ask_about(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.about)
    await _step(bot, chat_id, state, 6,
                texts.REG_ABOUT.format(max_len=settings.about_max_len), rkb.ABOUT, error)


async def ask_city(bot: Bot, chat_id: int, state: FSMContext,
                   error: str | None = None) -> None:
    await state.set_state(Registration.city)
    await state.update_data(city_options=None)
    await _step(bot, chat_id, state, 7, texts.REG_CITY, rkb.request_location(), error)


async def ask_region(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.region_fallback)
    await _step(
        bot, chat_id, state, 7,
        texts.REG_CITY_NOT_FOUND + "\n\n🗺 Или напишите вашу <b>область / "
        "регион</b> — например, <code>Волгоградская область</code>. "
        "Тогда я буду искать по области.",
        rkb.request_location(), error,
    )


async def geo_done(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Место известно — дальше предпросмотр. Согласие смотреть соседние
    области относилось к прежнему месту, поэтому сбрасываем его: когда
    здесь анкеты кончатся, лента спросит снова."""
    await users_repo.update_user(chat_id, search_scope=users_repo.SCOPE_HOME)
    await show_preview(bot, chat_id, state)


async def show_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    fresh = await users_repo.get_user(chat_id)
    await state.set_state(Registration.confirm)

    # Приём геопозиции подтверждаем строкой над анкетой, а не отдельным сообщением
    header = texts.REG_DONE
    if fresh["geo_source"] == "gps":
        place = fresh["city"]
        if fresh["region"] and norm_text(fresh["region"]) != norm_text(fresh["city"]):
            place += f", {fresh['region']}"
        header = texts.REG_GEO_SAVED.format(city=profile.esc(place)) + "\n" + header

    card = await profile.send_card(bot, chat_id, fresh, markup=rkb.CONFIRM_PROFILE,
                                   show_distance=False, header=header)
    await screen.replace(bot, chat_id, state, card)


# ────────────────────────────── Точки входа ─────────────────────────────────

async def start(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await ask_gender(bot, chat_id, state)


async def resume(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                 settings: Settings, first_name: str = "") -> None:
    """Продолжает анкету с первого незаполненного поля."""
    if not user["gender"]:
        await ask_gender(bot, chat_id, state)
    elif not user["looking_for"]:
        await ask_looking(bot, chat_id, state)
    elif not user["age"]:
        await ask_age(bot, chat_id, state)
    elif not user["name"]:
        await ask_name(bot, chat_id, state, settings, first_name)
    elif not user["media_id"]:
        await ask_media(bot, chat_id, state, settings)
    elif user["about"] is None:
        await ask_about(bot, chat_id, state, settings)
    elif not user["city"]:
        await ask_city(bot, chat_id, state)
    else:
        await show_preview(bot, chat_id, state)


# ──────────────────────────── Шаги 1–2: кнопки ──────────────────────────────

@router.message(Registration.gender)
async def set_gender(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    gender = GENDER_BY_BUTTON.get(message.text or "")
    if gender is None:
        await ask_gender(message.bot, message.chat.id, state,
                         "Выберите вариант кнопкой внизу.")
        return
    await users_repo.update_user(user["id"], gender=gender)
    await ask_looking(message.bot, message.chat.id, state)


@router.message(Registration.looking_for)
async def set_looking(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    value = LOOKING_BY_BUTTON.get(message.text or "")
    if value is None:
        await ask_looking(message.bot, message.chat.id, state,
                          "Выберите вариант кнопкой внизу.")
        return
    await users_repo.update_user(user["id"], looking_for=value)
    await ask_age(message.bot, message.chat.id, state)


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

    # Кого показывать по возрасту, лента решает сама — см. users.age_window
    await users_repo.update_user(user["id"], age=age)
    await ask_name(bot, chat_id, state, settings,
                   message.from_user.first_name or "")


# ─────────────────────────── Шаг 4: имя ─────────────────────────────────────

def clean_name(raw: str) -> str:
    """Убирает из имени эмодзи и украшения, сохраняя буквы любого языка."""
    kept = [
        char for char in (raw or "")
        if char.isalpha() or char.isdigit() or char in NAME_EXTRA_CHARS
    ]
    return " ".join("".join(kept).split())


def validate_name(raw: str, settings: Settings) -> str | None:
    """Приводит имя к пригодному виду. None — использовать нельзя вообще."""
    if LINK_RE.search(raw or ""):
        return None
    name = clean_name(raw)[:settings.name_max_len].strip(NAME_EXTRA_CHARS)
    if len(name) < settings.name_min_len:
        return None
    return name


@router.message(Registration.name, F.text)
async def set_name(message: Message, state: FSMContext, user, settings: Settings) -> None:
    """И набранное имя, и нажатая кнопка с именем из Telegram приходят сюда текстом."""
    await screen.drop(message)
    name = validate_name(message.text or "", settings)
    if not name:
        await ask_name(message.bot, message.chat.id, state, settings,
                       message.from_user.first_name or "",
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

@router.message(Registration.about, F.text)
async def set_about(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    await screen.drop(message)
    about = (message.text or "").strip()
    bot, chat_id = message.bot, message.chat.id

    if about == rkb.SKIP:
        await users_repo.update_user(user["id"], about="")
        await ask_city(bot, chat_id, state)
        return
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
    await geo_done(bot, chat_id, state)


@router.message(Registration.city, F.text)
async def set_city(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    query = (message.text or "").strip()
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id

    if query in (rkb.OTHER_CITY, rkb.MANUAL_CITY):
        await ask_city(bot, chat_id, state)
        return

    # Нажата кнопка одного из найденных вариантов
    options = (await state.get_data()).get("city_options") or []
    chosen = next((o for o in options if o["title"] == query), None)
    if chosen is not None:
        city = geo.City(chosen["name"], chosen["region"], chosen["country"],
                        chosen["lat"], chosen["lon"])
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await geo_done(bot, chat_id, state)
        return

    found = await geo.resolve(query, settings.geocoder_enabled, settings.geocoder_email)

    if len(found) == 1:
        city = found[0]
        await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
        await geo_done(bot, chat_id, state)
        return

    if len(found) > 1:
        await state.update_data(city_options=[
            {"title": c.title, "name": c.name, "region": c.region,
             "country": c.country, "lat": c.lat, "lon": c.lon} for c in found
        ])
        await _step(bot, chat_id, state, 7, texts.REG_CITY_CHOICE,
                    rkb.city_choices([c.title for c in found]))
        return

    # Назвали не город, а область целиком — так и запоминаем: такой человек
    # свой для любого города этой области
    region = geo.find_whole_region(query)
    if region is not None:
        await save_city(user["id"], region, lat=region.lat, lon=region.lon,
                        source="region")
        await geo_done(bot, chat_id, state)
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
        await geo_done(bot, chat_id, state)
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

    # Посёлка нет в справочнике: храним его название и центр области —
    # искать будем по области, а земляки из того же посёлка найдут друг друга
    anchor = anchors[0]
    place = pending if pending[:1].isupper() else pending.title()
    if norm_text(place) == norm_text(anchor.region):
        place = anchor.region
    await users_repo.update_user(
        user["id"], city=place, region=anchor.region,
        country=anchor.country, lat=anchor.lat, lon=anchor.lon, geo_source="region",
    )
    await geo_done(bot, chat_id, state)


# ───────────────────────── Подтверждение ────────────────────────────────────

@router.message(Registration.confirm, F.text == rkb.CONFIRM)
async def confirm(message: Message, state: FSMContext, bot: Bot, user,
                  settings: Settings, is_admin: bool) -> None:
    await screen.drop(message)
    refill = bool((await state.get_data()).get("refill"))
    await users_repo.update_user(user["id"], registered=1, is_active=1)
    fresh = await users_repo.get_user(user["id"])
    await menu_handlers.show_menu(
        bot, message.chat.id, state, fresh, is_admin,
        note=texts.PROFILE_UPDATED if refill else texts.PROFILE_PUBLISHED,
    )
    await admin_log(
        bot,
        f"{'✏️ Анкета обновлена' if refill else '✅ Анкета заполнена'}: "
        f"<b>{profile.esc(fresh['name'])}</b>, "
        f"{fresh['age']} · {profile.GENDER_WORD.get(fresh['gender'], '')} · "
        f"{profile.esc(fresh['city'] or '—')}\n"
        f"<code>{fresh['id']}</code> @{fresh['username'] or '—'}"
    )


@router.message(Registration.confirm, F.text == rkb.REFILL)
async def restart(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await ask_gender(message.bot, message.chat.id, state)


# ────────────── Подсказки, если на шаге прислали не то ──────────────────────

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


@router.message(Registration.confirm)
async def confirm_hint(message: Message, state: FSMContext) -> None:
    """Предпросмотр ждёт одну из двух кнопок — остальное просто убираем."""
    await screen.drop(message)


@router.message(Registration.scope)
async def old_scope_step(message: Message, state: FSMContext) -> None:
    """Шаг «где искать» из прежней версии: его больше нет — сразу к предпросмотру."""
    await screen.drop(message)
    await show_preview(message.bot, message.chat.id, state)
