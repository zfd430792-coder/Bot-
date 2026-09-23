"""Моя анкета: просмотр, новое фото или описание, скрытие и удаление.

Анкета — экран с карточкой и inline-кнопками действий. Поменять на месте
можно только фото и описание: вопрос встаёт вместо карточки, ответ
пользователя удаляется, после сохранения снова видна обновлённая анкета.
Имя, возраст, город и кого искать меняются через «Заполнить анкету
заново» — те же шаги, что при регистрации; лайки и пары при этом остаются.
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
from app.handlers import registration
from app.handlers.registration import LINK_RE
from app.keyboards import inline as kb
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
        await screen.show(bot, chat_id, state, "Анкеты пока нет — давайте заполним.",
                          kb.START_AGAIN)
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
async def profile_command(message: Message, state: FSMContext,
                          user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await show_profile(message.bot, message.chat.id, state, user["id"])


@router.callback_query(F.data.in_({"m:profile", "pr:back"}))
async def profile_button(call: CallbackQuery, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    await call.answer()
    await show_profile(call.bot, screen.chat_id(call), state, user["id"])


# ───────────────────────── Видимость анкеты ─────────────────────────────────

@router.callback_query(F.data.in_({"pr:hide", "pr:show"}))
async def visibility(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    active = call.data == "pr:show"
    if user["registered"]:
        await users_repo.update_user(user["id"], is_active=int(active))
    await show_profile(call.bot, screen.chat_id(call), state, user["id"],
                       notice=texts.PROFILE_SHOWN if active else texts.PROFILE_HIDDEN)


# ─────────────────────────── Удаление анкеты ────────────────────────────────

@router.callback_query(F.data == "pr:del")
async def ask_delete(call: CallbackQuery, state: FSMContext, user) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        await show_profile(call.bot, chat_id, state, user["id"])
        return
    await state.set_state(EditProfile.delete_confirm)
    await screen.show(call.bot, chat_id, state, texts.DELETE_CONFIRM, kb.DELETE_CONFIRM)


@router.callback_query(F.data == "pr:del:yes")
async def do_delete(call: CallbackQuery, state: FSMContext, bot: Bot, user) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    # Удаляем только после экрана подтверждения — нажатие со старого
    # сообщения выше по чату просто вернёт к анкете
    if await state.get_state() != EditProfile.delete_confirm.state:
        await show_profile(bot, chat_id, state, user["id"])
        return
    await users_repo.delete_profile(user["id"])
    await state.clear()
    await screen.show(bot, chat_id, state, texts.PROFILE_DELETED, kb.START_AGAIN)
    await admin_log(
        bot, f"🗑 Анкета удалена: <code>{user['id']}</code> @{user['username'] or '—'}"
    )


# ─────────────────────── Новое фото или описание ────────────────────────────

async def _ask(bot: Bot, chat_id: int, state: FSMContext, new_state, text: str,
               error: str | None = None) -> None:
    await state.set_state(new_state)
    await screen.show(bot, chat_id, state,
                      f"⚠️ {error}\n\n{text}" if error else text, kb.EDIT_CANCEL)


@router.callback_query(F.data == "pr:about")
async def edit_about(call: CallbackQuery, state: FSMContext, user,
                     settings: Settings) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        await show_profile(call.bot, chat_id, state, user["id"])
        return
    await _ask(call.bot, chat_id, state, EditProfile.about,
               texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len))


@router.callback_query(F.data == "pr:media")
async def edit_media(call: CallbackQuery, state: FSMContext, user,
                     settings: Settings) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        await show_profile(call.bot, chat_id, state, user["id"])
        return
    await _ask(call.bot, chat_id, state, EditProfile.media,
               texts.REG_MEDIA.format(sec=settings.max_video_seconds))


@router.message(EditProfile.about, F.text == rkb.CANCEL)
@router.message(EditProfile.media, F.text == rkb.CANCEL)
async def edit_back_legacy(message: Message, state: FSMContext, user) -> None:
    await screen.drop(message)
    await show_profile(message.bot, message.chat.id, state, user["id"])


@router.message(EditProfile.about, F.text)
async def save_about(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    about = (message.text or "").strip()
    ask = texts.EDIT_ASK_ABOUT.format(max_len=settings.about_max_len)
    if len(about) > settings.about_max_len:
        await _ask(bot, chat_id, state, EditProfile.about, ask,
                   texts.REG_ABOUT_LONG.format(max_len=settings.about_max_len))
        return
    if LINK_RE.search(about):
        await _ask(bot, chat_id, state, EditProfile.about, ask, texts.REG_ABOUT_LINKS)
        return
    await users_repo.update_user(user["id"], about=about)
    await show_profile(bot, chat_id, state, user["id"], notice="✅ <i>Описание обновлено</i>")


@router.message(EditProfile.media)
async def save_media(message: Message, state: FSMContext, user,
                     settings: Settings) -> None:
    result = profile_service.extract_media(message, settings.max_video_seconds)
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    ask = texts.REG_MEDIA.format(sec=settings.max_video_seconds)
    errors = {
        "long": texts.REG_MEDIA_TOO_LONG.format(sec=settings.max_video_seconds),
        "file": texts.REG_MEDIA_AS_FILE,
        "bad": texts.REG_MEDIA_BAD,
    }
    if isinstance(result, str):
        await _ask(bot, chat_id, state, EditProfile.media, ask, errors[result])
        return

    media_type, media_id = result
    await users_repo.update_user(user["id"], media_type=media_type, media_id=media_id)
    await show_profile(bot, chat_id, state, user["id"], notice="✅ <i>Фото/видео обновлено</i>")


# ───────────────────────── Заполнить заново ─────────────────────────────────

@router.callback_query(F.data == "pr:refill")
async def refill(call: CallbackQuery, state: FSMContext, bot: Bot, user) -> None:
    """Все шаги анкеты по новой. Анкета остаётся опубликованной, каждый ответ
    сразу заменяет прежний — поэтому лайки и пары никуда не деваются."""
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        # Новичок идёт через /start: капча и правила — раньше анкеты
        await show_profile(bot, chat_id, state, user["id"])
        return
    await state.clear()
    await state.update_data(refill=True)
    await registration.start(bot, chat_id, state)
