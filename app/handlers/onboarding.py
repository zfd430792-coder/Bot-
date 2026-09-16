"""Вход в бота: капча -> приветствие -> предупреждение о мошенниках."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import captcha as captcha_repo
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import menu as menu_handlers
from app.handlers import registration
from app.keyboards import inline as kb
from app.services import captcha as captcha_service
from app.services.notify import admin_log
from app.states import Onboarding

log = logging.getLogger(__name__)
router = Router(name="onboarding")

# Держим ссылки на фоновые задачи отсчёта, иначе их может собрать сборщик мусора
_countdown_tasks: set[asyncio.Task] = set()


# ──────────────────────────────── /start ────────────────────────────────────

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot, user: Mapping[str, Any],
                settings: Settings, is_admin: bool) -> None:
    await state.clear()

    if user["registered"]:
        await menu_handlers.show_main_menu(message, user, is_admin)
        return

    # Приём новых анкет можно приостановить из админ-панели
    if not is_admin and await mod_repo.get_setting("registration_open", "1") != "1":
        await message.answer(texts.REGISTRATION_CLOSED)
        return

    if not user["captcha_passed"]:
        blocked = await captcha_repo.blocked_seconds(user["id"])
        if blocked:
            await message.answer(
                texts.CAPTCHA_BLOCKED.format(minutes=max(1, blocked // 60))
            )
            return
        await issue_captcha(bot, message.chat.id, state, settings, intro=True)
        return

    if not user["rules_accepted"]:
        await send_welcome(message, state, user)
        return

    # Регистрация не была доведена до конца — продолжаем с того же места
    await registration.resume(message, state, user, settings)


# ──────────────────────────────── Капча ─────────────────────────────────────

async def issue_captcha(bot: Bot, chat_id: int, state: FSMContext,
                        settings: Settings, *, intro: bool = False,
                        note: str | None = None) -> None:
    """Генерирует и присылает новое задание капчи."""
    # Рисование картинки — работа для CPU, уводим её из основного потока
    challenge = await asyncio.to_thread(captcha_service.generate)
    used = await captcha_repo.attempts_used(chat_id)

    caption_parts = []
    if intro:
        caption_parts.append(texts.CAPTCHA_INTRO)
    if note:
        caption_parts.append(note)
    caption_parts.append(texts.CAPTCHA_TASK.format(
        task=challenge.task,
        attempt=min(used + 1, settings.captcha_max_attempts),
        total=settings.captcha_max_attempts,
    ))

    data = await state.get_data()
    old_message = data.get("cap_msg")
    if old_message:
        try:
            await bot.delete_message(chat_id, old_message)
        except TelegramBadRequest:
            pass

    sent = await bot.send_photo(
        chat_id,
        BufferedInputFile(challenge.image, filename="captcha.png"),
        caption="\n\n".join(caption_parts),
        reply_markup=kb.captcha(challenge.buttons, set()),
    )

    await state.set_state(Onboarding.captcha)
    await state.update_data(
        cap_tokens=challenge.tokens,
        cap_correct=challenge.correct,
        cap_selected=[],
        cap_started=time.monotonic(),
        cap_msg=sent.message_id,
        cap_refresh=data.get("cap_refresh", 0),
    )


@router.callback_query(F.data.startswith("cap:tok:"), Onboarding.captcha)
async def captcha_toggle(call: CallbackQuery, state: FSMContext) -> None:
    """Переключает клетку. Токен ничего не выдаёт — маппинг живёт на сервере."""
    token = (call.data or "").removeprefix("cap:tok:")
    data = await state.get_data()
    tokens: dict[str, int] = data.get("cap_tokens") or {}
    label = tokens.get(token)
    if label is None:
        await call.answer()
        return

    selected = set(data.get("cap_selected") or [])
    selected.symmetric_difference_update({label})
    await state.update_data(cap_selected=sorted(selected))

    buttons = sorted(tokens.items(), key=lambda kv: kv[1])
    can_refresh = data.get("cap_refresh", 0) < 3
    try:
        await call.message.edit_reply_markup(
            reply_markup=kb.captcha(buttons, selected, can_refresh)
        )
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data == "cap:new", Onboarding.captcha)
async def captcha_refresh(call: CallbackQuery, state: FSMContext, bot: Bot,
                          settings: Settings) -> None:
    data = await state.get_data()
    used = int(data.get("cap_refresh", 0))
    if used >= settings.captcha_max_refresh:
        await call.answer(texts.CAPTCHA_REFRESH_LIMIT, show_alert=True)
        return
    await state.update_data(cap_refresh=used + 1)
    await call.answer()
    await issue_captcha(bot, call.message.chat.id, state, settings)


@router.callback_query(F.data == "cap:done", Onboarding.captcha)
async def captcha_submit(call: CallbackQuery, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings) -> None:
    data = await state.get_data()
    selected = list(data.get("cap_selected") or [])
    correct = list(data.get("cap_correct") or [])
    started = float(data.get("cap_started") or 0)
    elapsed_ms = (time.monotonic() - started) * 1000 if started else 0

    if not selected:
        await call.answer(texts.CAPTCHA_EMPTY, show_alert=True)
        return

    # Слишком быстро — человек физически не успевает рассмотреть 15 клеток
    if elapsed_ms < settings.captcha_min_solve_ms:
        await call.answer(texts.CAPTCHA_TOO_FAST, show_alert=True)
        await _fail_captcha(call, state, bot, user, settings, reason="too_fast")
        return

    # Слишком долго — задание протухло, даём новое без списания попытки
    if elapsed_ms > settings.captcha_timeout_seconds * 1000:
        await call.answer()
        await issue_captcha(bot, call.message.chat.id, state, settings,
                            note=texts.CAPTCHA_EXPIRED)
        return

    if not captcha_service.check(correct, selected):
        await call.answer()
        await _fail_captcha(call, state, bot, user, settings, reason="wrong")
        return

    # Успех
    await captcha_repo.register_pass(user["id"])
    await users_repo.update_user(user["id"], captcha_passed=1)
    await mod_repo.log_event("captcha_pass", user["id"], ms=int(elapsed_ms))
    await call.answer(texts.CAPTCHA_PASSED)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await state.update_data(cap_msg=None)
    await send_welcome(call.message, state, user)


async def _fail_captcha(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings,
                        reason: str) -> None:
    left = await captcha_repo.register_fail(
        user["id"], settings.captcha_max_attempts, settings.captcha_block_minutes
    )
    await mod_repo.log_event("captcha_fail", user["id"], reason=reason)

    if left <= 0:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
        await state.clear()
        await call.message.answer(
            texts.CAPTCHA_BLOCKED.format(minutes=settings.captcha_block_minutes)
        )
        await admin_log(
            bot,
            f"🤖 Капча: пользователь <code>{user['id']}</code> "
            f"(@{user['username'] or '—'}) заблокирован после "
            f"{settings.captcha_max_attempts} неудачных попыток ({reason})."
        )
        return

    await issue_captcha(bot, call.message.chat.id, state, settings,
                        note=texts.CAPTCHA_WRONG.format(left=left))


# ───────────────────── Приветствие и предупреждение ─────────────────────────

async def send_welcome(message: Message, state: FSMContext,
                       user: Mapping[str, Any]) -> None:
    name = user["tg_name"] or "друг"
    await state.set_state(Onboarding.welcome)
    await message.answer(
        texts.WELCOME.format(name=name),
        reply_markup=kb.WELCOME_NEXT,
    )


@router.callback_query(F.data == "onb:next")
async def show_warning(call: CallbackQuery, state: FSMContext, bot: Bot,
                       settings: Settings) -> None:
    """Приветствие удаляется, на его месте появляется предупреждение."""
    await call.answer()
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass

    seconds = max(1, settings.rules_delay_seconds)
    sent = await call.message.answer(
        texts.WARNING + texts.WARNING_COUNTDOWN.format(sec=seconds)
    )
    await state.set_state(Onboarding.rules)

    task = asyncio.create_task(
        _countdown(bot, sent.chat.id, sent.message_id, seconds)
    )
    _countdown_tasks.add(task)
    task.add_done_callback(_countdown_tasks.discard)


async def _countdown(bot: Bot, chat_id: int, message_id: int, seconds: int) -> None:
    """Тикает до нуля и только потом показывает кнопку «Принимаю»."""
    try:
        for left in range(seconds - 1, 0, -1):
            await asyncio.sleep(1)
            try:
                await bot.edit_message_text(
                    texts.WARNING + texts.WARNING_COUNTDOWN.format(sec=left),
                    chat_id=chat_id, message_id=message_id,
                )
            except TelegramBadRequest:
                return  # сообщение удалено или пользователь ушёл дальше
        await asyncio.sleep(1)
        await bot.edit_message_text(
            texts.WARNING, chat_id=chat_id, message_id=message_id,
            reply_markup=kb.RULES_ACCEPT,
        )
    except TelegramBadRequest:
        pass
    except Exception as exc:  # фоновая задача не должна ронять бота
        log.warning("Отсчёт правил прерван: %s", exc)


@router.callback_query(F.data == "onb:accept")
async def accept_rules(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    await users_repo.update_user(user["id"], rules_accepted=1)
    await call.answer(texts.RULES_ACCEPTED)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await admin_log(
        bot,
        f"🆕 Новый пользователь: <code>{user['id']}</code> "
        f"(@{user['username'] or '—'}), {user['tg_name'] or ''}"
    )
    await registration.start(call.message, state, settings)


# ─────────────────────── Повторная проверка username ────────────────────────

@router.callback_query(F.data == "onb:username")
async def recheck_username(call: CallbackQuery, state: FSMContext,
                           settings: Settings) -> None:
    username = call.from_user.username
    if not username:
        await call.answer(texts.USERNAME_STILL_MISSING, show_alert=True)
        return
    await users_repo.update_user(call.from_user.id, username=username)
    await call.answer(texts.USERNAME_OK)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.message.answer("Продолжаем — нажмите /start")
