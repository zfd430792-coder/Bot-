"""Моя анкета: просмотр, редактирование, скрытие и удаление.

Анкета — экран с карточкой и кнопками под ней. Правка идёт в том же месте:
вопрос встаёт вместо карточки, ответ пользователя удаляется, после
сохранения снова видна обновлённая анкета со строкой «✅ сохранено».
"""
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
from app.handlers.registration import LINK_RE, validate_name
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log
from app.states import EditProfile

router = Router(name="profile")


async def show_profile(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                       *, notice: str | None = None) -> None:
    user = await users_repo.get_user(user_id)
    if user is None or not user["registered"]:
        await screen.show(bot, chat_id, state, "Анкеты пока нет — давайте заполним.",
                          kb.START_OVER)
        return

    status = []
    if not user["is_active"]:
        status.append("🙈 скрыта из поиска")
    if user["verify_status"] == "verified":
        status.append(f"{texts.VERIFY_BADGE} верифицирована")
    elif user["verify_status"] == "pending":
        status.append("⏳ верификация на проверке")
    header = texts.MY_PROFILE
    if status:
        header += " · <i>" + " · ".join(status) + "</i>"
    if notice:
        header = f"{notice}\n\n{header}"

    await screen.prepare(bot, chat_id, state)
    message_ids = await profile_service.send_card(
        bot, chat_id, user, show_distance=False, header=header,
        markup=kb.profile_actions(bool(user["is_active"]), user["verify_status"]),
    )
    await screen.remember(state, message_ids)


@router.message(Command("profile"))
@router.message(F.text == rkb.BTN_PROFILE)
async def profile_command(message: Message, state: FSMContext,
                          user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await state.clear()
    await show_profile(message.bot, message.chat.id, state, user["id"])


@router.callback_query(F.data == "m:profile")
async def profile_button(call: CallbackQuery, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    await call.answer()
    await state.clear()
    await show_profile(call.bot, call.message.chat.id, state, user["id"])


# ───────────────────────── Видимость анкеты ─────────────────────────────────

@router.callback_query(F.data == "pr:hide")
async def hide(call: CallbackQuery, state: FSMContext, user) -> None:
    await users_repo.update_user(user["id"], is_active=0)
    await call.answer()
    await show_profile(call.bot, call.message.chat.id, state, user["id"],
                       notice=texts.PROFILE_HIDDEN)


@router.callback_query(F.data == "pr:show")
async def unhide(call: CallbackQuery, state: FSMContext, user) -> None:
    await users_repo.update_user(user["id"], is_active=1)
    await call.answer()
    await show_profile(call.bot, call.message.chat.id, state, user["id"],
                       notice=texts.PROFILE_SHOWN)


# ─────────────────────────── Удаление анкеты ────────────────────────────────

@router.callback_query(F.data == "pr:delete")
async def ask_delete(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await screen.show(call.bot, call.message.chat.id, state,
                      texts.DELETE_CONFIRM, kb.DELETE_CONFIRM)


@router.callback_query(F.data == "pr:delete_no")
async def cancel_delete(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer(texts.CANCELLED)
    await show_profile(call.bot, call.message.chat.id, state, user["id"])


@router.callback_query(F.data == "pr:delete_yes")
async def do_delete(call: CallbackQuery, state: FSMContext, bot: Bot, user) -> None:
    await users_repo.delete_profile(user["id"])
    await state.clear()
    await call.answer("Анкета удалена")
    await screen.show(bot, call.message.chat.id, state,
                      texts.PROFILE_DELETED, kb.START_OVER)
    await admin_log(
        bot, f"🗑 Анкета удалена: <code>{user['id']}</code> @{user['username'] or '—'}"
    )


# ────────────────────────── Редактирование ──────────────────────────────────

@router.callback_query(F.data == "pr:edit")
async def edit_menu(call: CallbackQuery, state: FSMContext) -> None:
    """Что меняем — кнопки прямо под карточкой, анкета остаётся перед глазами."""
    await state.set_state(EditProfile.choosing)
    await call.answer("Что меняем?")
    try:
        await call.message.edit_reply_markup(reply_markup=kb.EDIT_FIELDS)
    except Exception:
        await screen.show(call.bot, call.message.chat.id, state,
                          "✏️ <b>Что меняем?</b>", kb.EDIT_FIELDS)


@router.callback_query(F.data == "edit:back")
async def edit_back(call: CallbackQuery, state: FSMContext, user) -> None:
    await state.clear()
    await call.answer()
    await show_profile(call.bot, call.message.chat.id, state, user["id"])


async def _ask(call: CallbackQuery, state: FSMContext, new_state, text: str) -> None:
    await state.set_state(new_state)
    await call.answer()
    await screen.show(call.bot, call.message.chat.id, state, text, kb.EDIT_CANCEL)


async def _saved(message: Message, state: FSMContext, user_id: int, notice: str) -> None:
    await state.clear()
    await show_profile(message.bot, message.chat.id, state, user_id, notice=notice)


async def _retry(message: Message, state: FSMContext, error: str, text: str) -> None:
    await screen.show(message.bot, message.chat.id, state,
                      f"⚠️ {error}\n\n{text}", kb.EDIT_CANCEL)


@router.callback_query(F.data == "edit:name")
async def edit_name(call: CallbackQuery, state: FSMContext) -> None:
    await _ask(call, state, EditProfile.name, texts.EDIT_ASK_NAME)


@router.message(EditProfile.name, F.text)
async def save_name(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    await screen.drop(message)
    name = validate_name(message.text or "", settings)
    if not name:
        await _retry(message, state, texts.REG_NAME_BAD.format(
            min_len=settings.name_min_len, max_len=settings.name_max_len),
            texts.EDIT_ASK_NAME)
        return
    await users_repo.update_user(user["id"], name=name)
    await _saved(message, state, user["id"], "✅ <i>Имя обновлено</i>")


@router.callback_query(F.data == "edit:age")
async def edit_age(call: CallbackQuery, state: FSMContext) -> None:
    await _ask(call, state, EditProfile.age, texts.EDIT_ASK_AGE)


@router.message(EditProfile.age, F.text)
async def save_age(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    await screen.drop(message)
    raw = (message.text or "").strip()
    bad = texts.REG_AGE_BAD.format(min_age=settings.min_age, max_age=settings.max_age)
    if not raw.isdigit():
        await _retry(message, state, bad, texts.EDIT_ASK_AGE)
        return
    age = int(raw)
    if age < settings.min_age:
        await _retry(message, state,
                     texts.REG_AGE_TOO_YOUNG.format(min_age=settings.min_age),
                     texts.EDIT_ASK_AGE)
        return
    if age > settings.max_age:
        await _retry(message, state, bad, texts.EDIT_ASK_AGE)
        return
    await users_repo.update_user(user["id"], age=age)
    await _saved(message, state, user["id"], "✅ <i>Возраст обновлён</i>")


@router.callback_query(F.data == "edit:about")
async def edit_about(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await _ask(call, state, EditProfile.about,
               texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len))


@router.message(EditProfile.about, F.text)
async def save_about(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    await screen.drop(message)
    about = (message.text or "").strip()
    ask = texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len)
    if len(about) > settings.about_max_len:
        await _retry(message, state,
                     texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len), ask)
        return
    if LINK_RE.search(about):
        await _retry(message, state, texts.REG_ABOUT_LINKS, ask)
        return
    await users_repo.update_user(user["id"], about=about)
    await _saved(message, state, user["id"], "✅ <i>Описание обновлено</i>")


@router.callback_query(F.data == "edit:media")
async def edit_media(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await _ask(call, state, EditProfile.media,
               texts.REG_MEDIA.format(sec=settings.max_video_seconds))


@router.message(EditProfile.media)
async def save_media(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    result = profile_service.extract_media(message, settings.max_video_seconds)
    await screen.drop(message)
    ask = texts.REG_MEDIA.format(sec=settings.max_video_seconds)
    errors = {
        "long": texts.REG_MEDIA_TOO_LONG.format(sec=settings.max_video_seconds),
        "file": texts.REG_MEDIA_AS_FILE,
        "bad": texts.REG_MEDIA_BAD,
    }
    if isinstance(result, str):
        await _retry(message, state, errors[result], ask)
        return

    media_type, media_id = result
    await users_repo.update_user(user["id"], media_type=media_type, media_id=media_id)
    await _saved(message, state, user["id"], "✅ <i>Фото/видео обновлено</i>")


@router.callback_query(F.data == "edit:city")
async def edit_city(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await settings_handlers.ask_city_change(call.bot, call.message.chat.id, state,
                                            back="profile")
