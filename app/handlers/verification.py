"""Верификация анкет.

Два сценария:
* пользователь сам просит галочку — бот работает как обычно;
* администратор требует проверку — бот закрыт до подтверждения (см. gates.py).

Заявки администратор разбирает в админке («✅ Верификация»), сюда ему
приходит только короткое уведомление с фото и кнопкой «Проверить заявку».
"""
from __future__ import annotations

import secrets
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings, get_settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import profile as profile_handlers
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import safe_send
from app.states import Verification

router = Router(name="verification")

# Без похожих символов: 0/O, 1/I — иначе код на фото не прочитать
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SEND_TEXT = "📸 Отправить фото с кодом"      # нижняя кнопка прежней версии


def new_code(length: int = 4) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


async def request_verification(bot: Bot, user_id: int, *, forced: bool,
                               admin_id: int | None = None,
                               notify: bool = True) -> str | None:
    """Создаёт заявку и уведомляет пользователя (notify=False — экран
    покажет вызывающий).

    Возвращает код либо None, если это владелец бота — на него ограничения
    не действуют, и запереть его требованием проверки нельзя.
    """
    if get_settings().is_admin(user_id):
        return None

    code = new_code()
    await users_repo.update_user(
        user_id, verify_code=code, verify_status="required" if forced else "pending",
        verify_forced=1 if forced else 0,
    )
    await mod_repo.create_verification(user_id, code, forced, admin_id)
    await mod_repo.log_event("verify_requested", user_id, forced=forced,
                             admin_id=admin_id)

    if notify:
        if forced:
            await safe_send(bot, user_id, texts.VERIFY_REQUIRED.format(code=code),
                            kb.VERIFY_REQUIRED)
        else:
            await safe_send(bot, user_id, texts.VERIFY_SELF.format(code=code),
                            kb.VERIFY_SELF)
    return code


# ────────────────────── Пользователь начинает проверку ──────────────────────

@router.callback_query(F.data == "ver:self")
async def self_request(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any]) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        # Проверять нечего, а заявка ушла бы админу — сначала анкета
        await profile_handlers.show_profile(bot, chat_id, state, user["id"])
        return
    if user["verify_status"] == "verified":
        await screen.show(bot, chat_id, state, texts.VERIFY_ALREADY, kb.HOME_ONLY)
        return
    code = await request_verification(bot, user["id"], forced=False, notify=False)
    if code is None:
        await screen.show(bot, chat_id, state,
                          "Вы владелец бота — верификация вам не нужна.", kb.HOME_ONLY)
        return
    await screen.show(bot, chat_id, state, texts.VERIFY_SELF.format(code=code),
                      kb.VERIFY_SELF)


def _upload_prompt(code: str, error: str | None = None) -> str:
    text = texts.VERIFY_UPLOAD.format(code=code)
    return f"⚠️ {error}\n\n{text}" if error else text


async def _start_upload(bot: Bot, chat_id: int, state: FSMContext,
                        user: Mapping[str, Any]) -> None:
    fresh = await users_repo.get_user(user["id"])
    if not fresh["registered"] and not fresh["verify_forced"]:
        await profile_handlers.show_profile(bot, chat_id, state, user["id"])
        return
    code = fresh["verify_code"] or new_code()
    if not fresh["verify_code"]:
        await users_repo.update_user(user["id"], verify_code=code)

    await state.set_state(Verification.waiting_media)
    await screen.show(bot, chat_id, state, _upload_prompt(code), kb.VERIFY_CANCEL)


@router.callback_query(F.data == "ver:send")
async def start_upload(call: CallbackQuery, state: FSMContext,
                       user: Mapping[str, Any]) -> None:
    await call.answer()
    await _start_upload(call.bot, screen.chat_id(call), state, user)


@router.message(F.text == SEND_TEXT)
async def start_upload_legacy(message: Message, state: FSMContext,
                              user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await _start_upload(message.bot, message.chat.id, state, user)


async def _cancel_upload(bot: Bot, chat_id: int, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    await state.clear()
    fresh = await users_repo.get_user(user["id"])
    if fresh["verify_forced"]:
        await screen.show(bot, chat_id, state,
                          texts.VERIFY_REQUIRED.format(code=fresh["verify_code"] or "—"),
                          kb.VERIFY_REQUIRED)
        return
    # Проверку просили сами, из анкеты — туда и возвращаем
    await profile_handlers.show_profile(bot, chat_id, state, user["id"])


@router.callback_query(F.data == "ver:cancel")
async def cancel_upload(call: CallbackQuery, state: FSMContext,
                        user: Mapping[str, Any]) -> None:
    await call.answer()
    await _cancel_upload(call.bot, screen.chat_id(call), state, user)


@router.message(Verification.waiting_media, F.text == rkb.CANCEL)
async def cancel_upload_legacy(message: Message, state: FSMContext,
                               user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await _cancel_upload(message.bot, message.chat.id, state, user)


@router.message(Verification.waiting_media)
async def receive_media(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    result = profile_service.extract_media(message, max_seconds=60)
    await screen.drop(message)
    if isinstance(result, str):
        await screen.show(bot, message.chat.id, state,
                          _upload_prompt(user["verify_code"] or "—",
                                         texts.VERIFY_NEED_MEDIA),
                          kb.VERIFY_CANCEL)
        return

    media_type, media_id = result
    record = await mod_repo.attach_verification_media(user["id"], media_type, media_id)
    if record is None:
        # Заявки нет (например, бот перезапускался) — создаём новую
        code = user["verify_code"] or new_code()
        await mod_repo.create_verification(user["id"], code,
                                           bool(user["verify_forced"]), None)
        record = await mod_repo.attach_verification_media(user["id"], media_type, media_id)

    await users_repo.update_user(user["id"], verify_status="pending")
    await state.clear()
    await screen.show(bot, message.chat.id, state, texts.VERIFY_SENT,
                      None if user["verify_forced"] else kb.HOME_ONLY)

    fresh = await users_repo.get_user(user["id"])
    header = (
        "✅ <b>Заявка на верификацию</b>\n\n"
        f"Пользователь: <b>{profile_service.esc(fresh['name'] or fresh['tg_name'])}</b>\n"
        f"<code>{fresh['id']}</code> @{fresh['username'] or '—'}\n"
        f"Код на фото должен быть: <code>{record['code']}</code>\n"
        f"Тип заявки: {'запрошена админом' if record['forced'] else 'по своей инициативе'}"
    )
    for admin_id in settings.admin_ids:
        try:
            if media_type == "photo":
                await bot.send_photo(admin_id, media_id)
            elif media_type == "video":
                await bot.send_video(admin_id, media_id)
            else:
                await bot.send_video_note(admin_id, media_id)
        except Exception:
            await safe_send(bot, admin_id, "Не удалось показать медиа заявки.")
        await safe_send(bot, admin_id, header, kb.NOTIFY_VERIFY)
