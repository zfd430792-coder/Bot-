"""Вход в бота: капча -> приветствие -> предупреждение о мошенниках.

Всё это — один экран: следующий шаг заменяет предыдущий, а повторный /start
убирает то, что было на экране, вместо того чтобы прислать ещё одну копию.
"""
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
from app.services import profile, screen
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
    await screen.drop(message)          # сама команда в чате не нужна
    await begin(bot, message.chat.id, state, user, settings, is_admin,
                first_name=message.from_user.first_name or "")


@router.callback_query(F.data == "m:start")
async def start_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings,
                       is_admin: bool) -> None:
    await call.answer()
    await begin(bot, call.message.chat.id, state, user, settings, is_admin,
                first_name=call.from_user.first_name or "")


async def begin(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                settings: Settings, is_admin: bool, *, first_name: str = "") -> None:
    """Вход с любого места: капча, правила, анкета или меню — что нужно сейчас."""
    await state.clear()
    # До правил нижних клавиатур бот не показывал никогда — снимать нечего
    if not user["rules_accepted"]:
        await screen.assume_clean_keyboard(state)

    # Приём новых анкет можно приостановить из админ-панели.
    # Проверяем до капчи: незачем гонять новичка через задание, если вход закрыт.
    if (not user["registered"] and not is_admin
            and await mod_repo.get_setting("registration_open", "1") != "1"):
        await screen.send(bot, chat_id, state, texts.REGISTRATION_CLOSED)
        return

    # Владельцу капча не показывается — проверка нужна против ботов, не против него
    if is_admin and not user["captcha_passed"]:
        await users_repo.update_user(user["id"], captcha_passed=1)
        user = await users_repo.get_user(user["id"])

    # Капчу может сбросить антинакрутка, поэтому проверяем её и у давних анкет
    if not user["captcha_passed"]:
        blocked = await captcha_repo.blocked_seconds(user["id"])
        if blocked:
            await screen.send(bot, chat_id, state, texts.CAPTCHA_BLOCKED.format(
                minutes=max(1, blocked // 60)))
            return
        await issue_captcha(bot, chat_id, state, settings)
        return

    if user["registered"]:
        await menu_handlers.show_menu(bot, chat_id, state, user, is_admin)
        return

    if not user["rules_accepted"]:
        await send_welcome(bot, chat_id, state, user)
        return

    # Регистрация не была доведена до конца — продолжаем с того же места
    await registration.resume(bot, chat_id, state, user, settings, first_name)


# ──────────────────────────────── Капча ─────────────────────────────────────

async def issue_captcha(bot: Bot, chat_id: int, state: FSMContext,
                        settings: Settings, *, title: str = texts.CAPTCHA_TITLE) -> None:
    """Генерирует и присылает новое задание капчи вместо прежнего экрана."""
    # Рисование картинки — работа для CPU, уводим её из основного потока
    challenge = await asyncio.to_thread(captcha_service.generate)
    used = await captcha_repo.attempts_used(chat_id)
    caption = texts.CAPTCHA_TASK.format(
        title=title,
        task=challenge.task,
        attempt=min(used + 1, settings.captcha_max_attempts),
        total=settings.captcha_max_attempts,
    )

    data = await state.get_data()
    refreshes = int(data.get("cap_refresh", 0))
    await screen.prepare(bot, chat_id, state)
    sent = await bot.send_photo(
        chat_id,
        BufferedInputFile(challenge.image, filename="captcha.png"),
        caption=caption,
        reply_markup=kb.captcha(challenge.buttons, set(),
                                refreshes < settings.captcha_max_refresh),
    )
    await screen.remember(state, [sent.message_id])

    await state.set_state(Onboarding.captcha)
    await state.update_data(
        cap_tokens=challenge.tokens,
        cap_correct=challenge.correct,
        cap_selected=[],
        cap_started=time.monotonic(),
        cap_refresh=refreshes,
    )


@router.callback_query(F.data.startswith("cap:tok:"), Onboarding.captcha)
async def captcha_toggle(call: CallbackQuery, state: FSMContext,
                         settings: Settings) -> None:
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
    can_refresh = int(data.get("cap_refresh", 0)) < settings.captcha_max_refresh
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
                         user: Mapping[str, Any], settings: Settings,
                         is_admin: bool) -> None:
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
                            title=texts.CAPTCHA_EXPIRED)
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

    if user["registered"]:
        # Проверку сбросила антинакрутка — возвращаем человека в меню
        await menu_handlers.show_menu(bot, call.message.chat.id, state, user, is_admin,
                                      note="✅ Проверка пройдена. Продолжаем!")
        return
    await send_welcome(bot, call.message.chat.id, state, user)


async def _fail_captcha(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings,
                        reason: str) -> None:
    left, block_minutes = await captcha_repo.register_fail(
        user["id"], settings.captcha_max_attempts, settings.captcha_block_minutes
    )
    await mod_repo.log_event("captcha_fail", user["id"], reason=reason)

    if left <= 0:
        await state.clear()
        await screen.send(bot, call.message.chat.id, state,
                          texts.CAPTCHA_BLOCKED.format(minutes=block_minutes))
        await admin_log(
            bot,
            f"🤖 Капча: пользователь <code>{user['id']}</code> "
            f"(@{user['username'] or '—'}) заблокирован на {block_minutes} мин "
            f"после {settings.captcha_max_attempts} неудачных попыток ({reason})."
        )
        return

    await issue_captcha(bot, call.message.chat.id, state, settings,
                        title=texts.CAPTCHA_WRONG)


# ───────────────────── Приветствие и предупреждение ─────────────────────────

async def send_welcome(bot: Bot, chat_id: int, state: FSMContext,
                       user: Mapping[str, Any]) -> None:
    await state.set_state(Onboarding.welcome)
    await screen.send(bot, chat_id, state,
                      texts.WELCOME.format(name=profile.esc(user["tg_name"] or "друг")),
                      kb.WELCOME_NEXT)


@router.callback_query(F.data == "onb:next")
async def show_warning(call: CallbackQuery, state: FSMContext, bot: Bot,
                       settings: Settings) -> None:
    """Приветствие уходит, на его месте появляется предупреждение."""
    await call.answer()
    seconds = max(1, settings.rules_delay_seconds)
    warning = texts.WARNING.format(min_age=settings.min_age)
    sent = await screen.send(bot, call.message.chat.id, state,
                             warning + texts.WARNING_COUNTDOWN.format(sec=seconds))
    await state.set_state(Onboarding.rules)

    task = asyncio.create_task(
        _countdown(bot, sent.chat.id, sent.message_id, seconds, warning)
    )
    _countdown_tasks.add(task)
    task.add_done_callback(_countdown_tasks.discard)


async def _countdown(bot: Bot, chat_id: int, message_id: int, seconds: int,
                     warning: str) -> None:
    """Тикает до нуля и только потом показывает кнопку «Принимаю»."""
    try:
        for left in range(seconds - 1, 0, -1):
            await asyncio.sleep(1)
            try:
                await bot.edit_message_text(
                    warning + texts.WARNING_COUNTDOWN.format(sec=left),
                    chat_id=chat_id, message_id=message_id,
                )
            except TelegramBadRequest:
                return  # сообщение удалено или пользователь ушёл дальше
        await asyncio.sleep(1)
        await bot.edit_message_text(
            warning, chat_id=chat_id, message_id=message_id,
            reply_markup=kb.RULES_ACCEPT,
        )
    except TelegramBadRequest:
        pass
    except Exception as exc:  # фоновая задача не должна ронять бота
        log.warning("Отсчёт правил прерван: %s", exc)


@router.callback_query(F.data == "onb:accept")
async def accept_rules(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any]) -> None:
    await users_repo.update_user(user["id"], rules_accepted=1)
    await call.answer(texts.RULES_ACCEPTED)
    await admin_log(
        bot,
        f"🆕 Новый пользователь: <code>{user['id']}</code> "
        f"(@{user['username'] or '—'}), {profile.esc(user['tg_name'] or '')}"
    )
    # Предупреждение прочитано — первый шаг анкеты встаёт на его место
    await registration.start(bot, call.message.chat.id, state)


# ─────────────────────── Повторная проверка username ────────────────────────

@router.callback_query(F.data == "onb:username")
async def recheck_username(call: CallbackQuery, state: FSMContext, bot: Bot,
                           user: Mapping[str, Any], settings: Settings,
                           is_admin: bool) -> None:
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
    fresh = await users_repo.get_user(call.from_user.id)
    await begin(bot, call.message.chat.id, state, fresh, settings, is_admin,
                first_name=call.from_user.first_name or "")
