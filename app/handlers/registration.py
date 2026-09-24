"""Пошаговое заполнение анкеты.

Каждый шаг сразу пишется в базу, а флаг registered выставляется только в конце.
Поэтому перезапуск бота или потеря FSM не заставляют начинать сначала —
команда /start продолжает с первого незаполненного поля.

Диалог живёт одним экраном: вопрос правится на месте, ответ пользователя
удаляется. В чате всегда видно ровно текущий шаг, а сверху короткой строкой —
то, что уже заполнено. Варианты — inline-кнопками под вопросом; нижняя
кнопка появляется один раз, на шаге города: геопозицию Telegram отдаёт
только через неё.

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
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import users as users_repo
from app.db.database import norm_text
from app.handlers import menu as menu_handlers
from app.keyboards import inline as kb
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

# Надписи нижних кнопок прежней версии: у кого-то они ещё открыты в чате
GENDER_BY_TEXT = {"👨 Я парень": "m", "👩 Я девушка": "f"}
LOOKING_BY_TEXT = {"👨 Парней": "m", "👩 Девушек": "f", "💞 Всех": "any"}
SKIP_TEXT = "⏭ Пропустить"
CONFIRM_TEXT = "✅ Всё верно, поехали"
REFILL_TEXT = "✏️ Заполнить заново"
PICK_BUTTON = "Выберите вариант кнопкой под сообщением."


async def _step(bot: Bot, chat_id: int, state: FSMContext, text: str,
                markup=None, error: str | None = None) -> None:
    """Показывает вопрос единственным сообщением вместо предыдущего.
    Без номера шага и пересказа прошлых ответов — только сам вопрос."""
    body = (f"⚠️ {error}\n\n" if error else "") + text
    await screen.show(bot, chat_id, state, body, markup)


# ──────────────────────────── Экраны шагов ──────────────────────────────────

async def ask_gender(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.gender)
    await _step(bot, chat_id, state, texts.REG_GENDER, kb.GENDER, error)


async def ask_looking(bot: Bot, chat_id: int, state: FSMContext,
                      error: str | None = None) -> None:
    await state.set_state(Registration.looking_for)
    await _step(bot, chat_id, state, texts.REG_LOOKING, kb.LOOKING, error)


async def ask_age(bot: Bot, chat_id: int, state: FSMContext,
                  error: str | None = None) -> None:
    await state.set_state(Registration.age)
    await _step(bot, chat_id, state, texts.REG_AGE, None, error)


async def ask_name(bot: Bot, chat_id: int, state: FSMContext,
                   settings: Settings, tg_name: str = "",
                   error: str | None = None) -> None:
    await state.set_state(Registration.name)
    # Имя из Telegram — одной кнопкой, но только если им реально можно
    # пользоваться: кнопка, которая всегда отвечает «не подходит», хуже её отсутствия
    suggestion = validate_name(tg_name, settings)
    await _step(bot, chat_id, state, texts.REG_NAME,
                kb.name_suggestion(suggestion), error)


async def ask_media(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.media)
    await _step(bot, chat_id, state,
                texts.REG_MEDIA.format(sec=settings.max_video_seconds), None, error)


async def ask_about(bot: Bot, chat_id: int, state: FSMContext,
                    settings: Settings, error: str | None = None) -> None:
    await state.set_state(Registration.about)
    await _step(bot, chat_id, state,
                texts.REG_ABOUT.format(max_len=settings.about_max_len), kb.ABOUT, error)


async def ask_city(bot: Bot, chat_id: int, state: FSMContext,
                   error: str | None = None) -> None:
    await state.set_state(Registration.city)
    await state.update_data(city_options=None)
    await _step(bot, chat_id, state, texts.REG_CITY, rkb.request_location(), error)


async def ask_region(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    await state.set_state(Registration.region_fallback)
    await _step(
        bot, chat_id, state,
        texts.REG_CITY_NOT_FOUND + "\n\nМожно написать область — например, "
        "<code>Волгоградская область</code>.",
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

    # Заодно снимается нижняя кнопка геопозиции с прошлого шага
    await screen.prepare(bot, chat_id, state)
    card = await profile.send_card(bot, chat_id, fresh, markup=kb.CONFIRM_PROFILE,
                                   show_distance=False, header=header)
    await screen.remember(state, card)


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

async def _gender_chosen(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                         gender: str) -> None:
    await users_repo.update_user(user_id, gender=gender)
    await ask_looking(bot, chat_id, state)


async def _looking_chosen(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                          value: str) -> None:
    await users_repo.update_user(user_id, looking_for=value)
    await ask_age(bot, chat_id, state)


@router.callback_query(Registration.gender, F.data.in_({"reg:gender:m", "reg:gender:f"}))
async def gender_button(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    await _gender_chosen(call.bot, screen.chat_id(call), state, user["id"],
                         (call.data or "").rsplit(":", 1)[-1])


@router.message(Registration.gender)
async def gender_text(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    gender = GENDER_BY_TEXT.get(message.text or "")
    if gender is None:
        await ask_gender(message.bot, message.chat.id, state, PICK_BUTTON)
        return
    await _gender_chosen(message.bot, message.chat.id, state, user["id"], gender)


@router.callback_query(Registration.looking_for,
                       F.data.in_({"reg:look:m", "reg:look:f", "reg:look:any"}))
async def looking_button(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    await _looking_chosen(call.bot, screen.chat_id(call), state, user["id"],
                          (call.data or "").rsplit(":", 1)[-1])


@router.message(Registration.looking_for)
async def looking_text(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    value = LOOKING_BY_TEXT.get(message.text or "")
    if value is None:
        await ask_looking(message.bot, message.chat.id, state, PICK_BUTTON)
        return
    await _looking_chosen(message.bot, message.chat.id, state, user["id"], value)


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


async def _save_name(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                     raw: str, tg_name: str, settings: Settings) -> None:
    name = validate_name(raw, settings)
    if not name:
        await ask_name(bot, chat_id, state, settings, tg_name,
                       texts.REG_NAME_BAD.format(min_len=settings.name_min_len,
                                                 max_len=settings.name_max_len))
        return
    await users_repo.update_user(user_id, name=name)
    await ask_media(bot, chat_id, state, settings)


@router.callback_query(Registration.name, F.data == "reg:tgname")
async def use_tg_name(call: CallbackQuery, state: FSMContext, user,
                      settings: Settings) -> None:
    """Кнопка «Использовать имя из Telegram»."""
    await call.answer()
    tg_name = call.from_user.first_name or ""
    await _save_name(call.bot, screen.chat_id(call), state, user["id"], tg_name, tg_name,
                     settings)


@router.message(Registration.name, F.text)
async def set_name(message: Message, state: FSMContext, user, settings: Settings) -> None:
    await screen.drop(message)
    tg_name = message.from_user.first_name or ""
    await _save_name(message.bot, message.chat.id, state, user["id"],
                     message.text or "", tg_name, settings)


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

@router.callback_query(Registration.about, F.data == "reg:skip_about")
async def skip_about(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    await users_repo.update_user(user["id"], about="")
    await ask_city(call.bot, screen.chat_id(call), state)


@router.message(Registration.about, F.text)
async def set_about(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    await screen.drop(message)
    about = (message.text or "").strip()
    bot, chat_id = message.bot, message.chat.id

    if about == SKIP_TEXT:
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


@router.callback_query(Registration.city, F.data.startswith("reg:city:"))
async def city_button(call: CallbackQuery, state: FSMContext, user) -> None:
    """Выбран один из найденных городов — или «ввести другой»."""
    await call.answer()
    bot, chat_id = call.bot, screen.chat_id(call)
    choice = (call.data or "").removeprefix("reg:city:")
    options = (await state.get_data()).get("city_options") or []
    if not choice.isdigit() or int(choice) >= len(options):
        await ask_city(bot, chat_id, state)
        return
    chosen = options[int(choice)]
    city = geo.City(chosen["name"], chosen["region"], chosen["country"],
                    chosen["lat"], chosen["lon"])
    await save_city(user["id"], city, lat=city.lat, lon=city.lon, source="city")
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
        await _step(bot, chat_id, state, texts.REG_CITY_CHOICE,
                    kb.city_choices([c.title for c in found]))
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

async def _confirm(bot: Bot, chat_id: int, state: FSMContext, user,
                   is_admin: bool) -> None:
    refill = bool((await state.get_data()).get("refill"))
    await users_repo.update_user(user["id"], registered=1, is_active=1)
    fresh = await users_repo.get_user(user["id"])
    await menu_handlers.show_menu(
        bot, chat_id, state, fresh, is_admin,
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


@router.callback_query(Registration.confirm, F.data == "reg:confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                  is_admin: bool) -> None:
    await call.answer()
    await _confirm(bot, screen.chat_id(call), state, user, is_admin)


@router.callback_query(Registration.confirm, F.data == "reg:restart")
async def restart(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask_gender(call.bot, screen.chat_id(call), state)


@router.message(Registration.confirm)
async def confirm_text(message: Message, state: FSMContext, bot: Bot, user,
                       is_admin: bool) -> None:
    """Предпросмотр ждёт одну из двух кнопок. Надписи прежних нижних кнопок
    понимаем, остальное просто убираем."""
    await screen.drop(message)
    if message.text == CONFIRM_TEXT:
        await _confirm(bot, message.chat.id, state, user, is_admin)
    elif message.text == REFILL_TEXT:
        await ask_gender(bot, message.chat.id, state)


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


@router.message(Registration.scope)
async def old_scope_step(message: Message, state: FSMContext) -> None:
    """Шаг «где искать» из прежней версии: его больше нет — сразу к предпросмотру."""
    await screen.drop(message)
    await show_preview(message.bot, message.chat.id, state)


@router.callback_query(F.data.startswith("reg:"))
async def stale_step(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                     settings: Settings, is_admin: bool) -> None:
    """Кнопка шага, на котором диалог сейчас не стоит (сообщение выше по чату):
    показываем актуальный шаг, а если анкета не заполняется — меню или
    первое незаполненное поле."""
    await call.answer()
    chat_id = screen.chat_id(call)
    first_name = call.from_user.first_name or ""
    steps = {
        Registration.gender.state: lambda: ask_gender(bot, chat_id, state),
        Registration.looking_for.state: lambda: ask_looking(bot, chat_id, state),
        Registration.age.state: lambda: ask_age(bot, chat_id, state),
        Registration.name.state: lambda: ask_name(bot, chat_id, state, settings, first_name),
        Registration.media.state: lambda: ask_media(bot, chat_id, state, settings),
        Registration.about.state: lambda: ask_about(bot, chat_id, state, settings),
        Registration.city.state: lambda: ask_city(bot, chat_id, state),
        Registration.region_fallback.state: lambda: ask_region(bot, chat_id, state),
        Registration.confirm.state: lambda: show_preview(bot, chat_id, state),
    }
    step = steps.get(await state.get_state())
    if step is not None:
        await step()
        return
    # Вне анкеты: begin() сам решит — меню, капча или правила. Шаги анкеты
    # напрямую не открываем: нажатие можно подделать в обход капчи.
    # Поздний импорт: onboarding сам импортирует этот модуль.
    from app.handlers import onboarding
    await onboarding.begin(bot, chat_id, state, user, settings, is_admin,
                           first_name=first_name)
