"""Моя анкета: просмотр, редактирование, скрытие и удаление.

Анкета — экран с карточкой и нижними кнопками действий. «Изменить анкету»
выводит под карточкой, что именно менять; вопрос встаёт вместо карточки,
ответ пользователя удаляется, после сохранения снова видна обновлённая
анкета со строкой «✅ сохранено».
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import Settings
from app.db import users as users_repo
from app.handlers import settings as settings_handlers
from app.handlers.registration import LINK_RE, validate_name
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log
from app.states import EditProfile

router = Router(name="profile")


async def show_profile(bot: Bot, chat_id: int, state: FSMContext, user_id: int,
                       *, notice: str | None = None) -> None:
    await state.clear()
    user = await users_repo.get_user(user_id)
    if user is None or not user["registered"]:
        await screen.send(bot, chat_id, state, "Анкеты пока нет — давайте заполним.",
                          rkb.START_AGAIN)
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

    message_ids = await profile_service.send_card(
        bot, chat_id, user, show_distance=False, header=header,
        markup=rkb.profile_actions(bool(user["is_active"]), user["verify_status"]),
    )
    await screen.replace(bot, chat_id, state, message_ids)


@router.message(Command("profile"))
@router.message(F.text == rkb.PROFILE)
@router.message(F.text == rkb.TO_PROFILE)
async def my_profile(message: Message, state: FSMContext,
                     user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await show_profile(message.bot, message.chat.id, state, user["id"])


# ───────────────────────── Видимость анкеты ─────────────────────────────────

@router.message(F.text == rkb.HIDE)
async def hide(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    await users_repo.update_user(user["id"], is_active=0)
    await show_profile(message.bot, message.chat.id, state, user["id"],
                       notice=texts.PROFILE_HIDDEN)


@router.message(F.text == rkb.SHOW)
async def unhide(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    await users_repo.update_user(user["id"], is_active=1)
    await show_profile(message.bot, message.chat.id, state, user["id"],
                       notice=texts.PROFILE_SHOWN)


# ─────────────────────────── Удаление анкеты ────────────────────────────────

@router.message(F.text == rkb.DELETE)
async def ask_delete(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(EditProfile.delete_confirm)
    await screen.send(message.bot, message.chat.id, state,
                      texts.DELETE_CONFIRM, rkb.DELETE_CONFIRM)


@router.message(EditProfile.delete_confirm, F.text == rkb.DELETE_YES)
async def do_delete(message: Message, state: FSMContext, bot: Bot, user) -> None:
    await screen.drop(message)
    await users_repo.delete_profile(user["id"])
    await state.clear()
    await screen.send(bot, message.chat.id, state, texts.PROFILE_DELETED, rkb.START_AGAIN)
    await admin_log(
        bot, f"🗑 Анкета удалена: <code>{user['id']}</code> @{user['username'] or '—'}"
    )


@router.message(EditProfile.delete_confirm)
async def cancel_delete(message: Message, state: FSMContext, user) -> None:
    """«Нет, оставить» — и любое другое нажатие: удаляем только по явному «Да»."""
    await screen.drop(message)
    await show_profile(message.bot, message.chat.id, state, user["id"],
                       notice=f"<i>{texts.CANCELLED}</i>")


# ────────────────────────── Редактирование ──────────────────────────────────

@router.message(F.text == rkb.EDIT)
async def edit_menu(message: Message, state: FSMContext) -> None:
    """Что меняем — вопрос под карточкой, анкета остаётся перед глазами."""
    await screen.drop(message)
    await state.set_state(EditProfile.choosing)
    sent = await message.bot.send_message(message.chat.id, "✏️ <b>Что меняем?</b>",
                                          reply_markup=rkb.EDIT_FIELDS)
    await screen.add(state, [sent.message_id])


@router.message(EditProfile.choosing, F.text == rkb.BACK)
@router.message(EditProfile.name, F.text == rkb.CANCEL)
@router.message(EditProfile.age, F.text == rkb.CANCEL)
@router.message(EditProfile.about, F.text == rkb.CANCEL)
@router.message(EditProfile.media, F.text == rkb.CANCEL)
async def edit_back(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    await show_profile(message.bot, message.chat.id, state, user["id"])


async def _ask(message: Message, state: FSMContext, new_state, text: str,
               error: str | None = None) -> None:
    await state.set_state(new_state)
    await screen.send(message.bot, message.chat.id, state,
                      f"⚠️ {error}\n\n{text}" if error else text, rkb.CANCEL_ONLY)


async def _saved(message: Message, state: FSMContext, user_id: int, notice: str) -> None:
    await show_profile(message.bot, message.chat.id, state, user_id, notice=notice)


@router.message(EditProfile.choosing, F.text == rkb.EDIT_NAME)
async def edit_name(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask(message, state, EditProfile.name, texts.EDIT_ASK_NAME)


@router.message(EditProfile.name, F.text)
async def save_name(message: Message, state: FSMContext, user,
                    settings: Settings) -> None:
    await screen.drop(message)
    name = validate_name(message.text or "", settings)
    if not name:
        await _ask(message, state, EditProfile.name, texts.EDIT_ASK_NAME,
                   texts.REG_NAME_BAD.format(min_len=settings.name_min_len,
                                             max_len=settings.name_max_len))
        return
    await users_repo.update_user(user["id"], name=name)
    await _saved(message, state, user["id"], "✅ <i>Имя обновлено</i>")


@router.message(EditProfile.choosing, F.text == rkb.EDIT_AGE)
async def edit_age(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask(message, state, EditProfile.age, texts.EDIT_ASK_AGE)


@router.message(EditProfile.age, F.text)
async def save_age(message: Message, state: FSMContext, user,
                   settings: Settings) -> None:
    await screen.drop(message)
    raw = (message.text or "").strip()
    bad = texts.REG_AGE_BAD.format(min_age=settings.min_age, max_age=settings.max_age)
    if not raw.isdigit() or int(raw) > settings.max_age:
        await _ask(message, state, EditProfile.age, texts.EDIT_ASK_AGE, bad)
        return
    if int(raw) < settings.min_age:
        await _ask(message, state, EditProfile.age, texts.EDIT_ASK_AGE,
                   texts.REG_AGE_TOO_YOUNG.format(min_age=settings.min_age))
        return
    await users_repo.update_user(user["id"], age=int(raw))
    await _saved(message, state, user["id"], "✅ <i>Возраст обновлён</i>")


@router.message(EditProfile.choosing, F.text == rkb.EDIT_ABOUT)
async def edit_about(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    await _ask(message, state, EditProfile.about,
               texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len))


@router.message(EditProfile.about, F.text)
async def save_about(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    await screen.drop(message)
    about = (message.text or "").strip()
    ask = texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len)
    if len(about) > settings.about_max_len:
        await _ask(message, state, EditProfile.about, ask,
                   texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len))
        return
    if LINK_RE.search(about):
        await _ask(message, state, EditProfile.about, ask, texts.REG_ABOUT_LINKS)
        return
    await users_repo.update_user(user["id"], about=about)
    await _saved(message, state, user["id"], "✅ <i>Описание обновлено</i>")


@router.message(EditProfile.choosing, F.text == rkb.EDIT_MEDIA)
async def edit_media(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    await _ask(message, state, EditProfile.media,
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
        await _ask(message, state, EditProfile.media, ask, errors[result])
        return

    media_type, media_id = result
    await users_repo.update_user(user["id"], media_type=media_type, media_id=media_id)
    await _saved(message, state, user["id"], "✅ <i>Фото/видео обновлено</i>")


@router.message(EditProfile.choosing, F.text == rkb.CITY)
async def edit_city(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await settings_handlers.ask_city_change(message.bot, message.chat.id, state,
                                            back="profile")


@router.message(EditProfile.choosing)
async def choosing_hint(message: Message) -> None:
    await screen.drop(message)
