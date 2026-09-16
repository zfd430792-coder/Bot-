"""Моя анкета: просмотр, редактирование, скрытие и удаление."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import users as users_repo
from app.handlers import settings as settings_handlers
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services.notify import admin_log
from app.states import EditProfile
from app.handlers.registration import validate_name, LINK_RE

router = Router(name="profile")


async def show_profile(message: Message, user_id: int) -> None:
    user = await users_repo.get_user(user_id)
    if user is None or not user["registered"]:
        await message.answer("Анкеты пока нет. Заполним? — /start")
        return

    status = []
    if not user["is_active"]:
        status.append("🙈 скрыта из поиска")
    if user["verify_status"] == "verified":
        status.append("☑️ верифицирована")
    elif user["verify_status"] == "pending":
        status.append("⏳ верификация на проверке")
    tail = ("\n\n<i>" + " · ".join(status) + "</i>") if status else ""

    await message.answer(texts.MY_PROFILE + tail)
    await profile_service.send_card(
        message.bot, message.chat.id, user, show_distance=False,
        markup=kb.profile_actions(bool(user["is_active"]), user["verify_status"]),
    )


@router.message(Command("profile"))
@router.message(F.text == rkb.BTN_PROFILE)
async def my_profile(message: Message, state: FSMContext,
                     user: Mapping[str, Any]) -> None:
    await state.clear()
    await show_profile(message, user["id"])


# ───────────────────────── Видимость анкеты ─────────────────────────────────

@router.callback_query(F.data == "pr:hide")
async def hide(call: CallbackQuery, user) -> None:
    await users_repo.update_user(user["id"], is_active=0)
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=kb.profile_actions(False, user["verify_status"]))
    await call.message.answer(texts.PROFILE_HIDDEN)


@router.callback_query(F.data == "pr:show")
async def unhide(call: CallbackQuery, user) -> None:
    await users_repo.update_user(user["id"], is_active=1)
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=kb.profile_actions(True, user["verify_status"]))
    await call.message.answer(texts.PROFILE_SHOWN)


# ─────────────────────────── Удаление анкеты ────────────────────────────────

@router.callback_query(F.data == "pr:delete")
async def ask_delete(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(texts.DELETE_CONFIRM, reply_markup=kb.DELETE_CONFIRM)


@router.callback_query(F.data == "pr:delete_no")
async def cancel_delete(call: CallbackQuery) -> None:
    await call.answer(texts.CANCELLED)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "pr:delete_yes")
async def do_delete(call: CallbackQuery, state: FSMContext, bot: Bot, user) -> None:
    await users_repo.delete_profile(user["id"])
    await state.clear()
    await call.answer("Анкета удалена")
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(texts.PROFILE_DELETED, reply_markup=rkb.REMOVE)
    await admin_log(
        bot, f"🗑 Анкета удалена: <code>{user['id']}</code> @{user['username'] or '—'}"
    )


# ────────────────────────── Редактирование ──────────────────────────────────

@router.callback_query(F.data == "pr:edit")
async def edit_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditProfile.choosing)
    await call.answer()
    await call.message.answer("✏️ <b>Что меняем?</b>", reply_markup=kb.EDIT_FIELDS)


@router.callback_query(F.data == "edit:back")
async def edit_back(call: CallbackQuery, state: FSMContext, user) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await show_profile(call.message, user["id"])


@router.callback_query(F.data == "edit:name")
async def edit_name(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditProfile.name)
    await call.answer()
    await call.message.answer("Напишите новое имя:")


@router.message(EditProfile.name, F.text)
async def save_name(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    name = validate_name(message.text or "", settings)
    if not name:
        await message.answer(texts.REG_NAME_BAD.format(
            min_len=settings.name_min_len, max_len=settings.name_max_len))
        return
    await users_repo.update_user(user["id"], name=name)
    await state.clear()
    await message.answer("✅ Имя обновлено")
    await show_profile(message, user["id"])


@router.callback_query(F.data == "edit:age")
async def edit_age(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditProfile.age)
    await call.answer()
    await call.message.answer("Сколько вам лет? Напишите числом:")


@router.message(EditProfile.age, F.text)
async def save_age(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return
    age = int(raw)
    if age < settings.min_age:
        await message.answer(texts.REG_AGE_UNDERAGE)
        return
    if age > settings.max_age:
        await message.answer(texts.REG_AGE_BAD.format(
            min_age=settings.min_age, max_age=settings.max_age))
        return
    await users_repo.update_user(user["id"], age=age)
    await state.clear()
    await message.answer("✅ Возраст обновлён")
    await show_profile(message, user["id"])


@router.callback_query(F.data == "edit:about")
async def edit_about(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.set_state(EditProfile.about)
    await call.answer()
    await call.message.answer(
        f"Напишите новый текст о себе (до {settings.about_max_len} символов):"
    )


@router.message(EditProfile.about, F.text)
async def save_about(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    about = (message.text or "").strip()
    if len(about) > settings.about_max_len:
        await message.answer(texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len))
        return
    if LINK_RE.search(about):
        await message.answer(texts.REG_ABOUT_LINKS)
        return
    await users_repo.update_user(user["id"], about=about)
    await state.clear()
    await message.answer("✅ Описание обновлено")
    await show_profile(message, user["id"])


@router.callback_query(F.data == "edit:media")
async def edit_media(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.set_state(EditProfile.media)
    await call.answer()
    await call.message.answer(
        texts.REG_MEDIA.format(sec=settings.max_video_seconds)
    )


@router.message(EditProfile.media)
async def save_media(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    result = profile_service.extract_media(message, settings.max_video_seconds)
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
    await state.clear()
    await message.answer("✅ Фото/видео обновлено")
    await show_profile(message, user["id"])


@router.callback_query(F.data == "edit:city")
async def edit_city(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await settings_handlers.ask_city_change(call.message, state)
