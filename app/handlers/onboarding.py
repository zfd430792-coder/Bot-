"""Вход в бота: капча -> приветствие -> предупреждение о мошенниках.

Всё это — один экран: следующий шаг заменяет предыдущий, а повторный /start
убирает то, что было на экране, вместо того чтобы прислать ещё одну копию.

Капча — на нижних кнопках: номера клеток 1–15, «Готово» и «Другая картинка».
Выбранные клетки видны строкой в подписи к картинке — её бот правит на месте
при каждом нажатии, новых сообщений не появляется.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
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
from app.keyboards import reply as rkb
from app.services import captcha as captcha_service
from app.services import profile, screen
from app.services.notify import admin_log
from app.states import Onboarding

log = logging.getLogger(__name__)
router = Router(name="onboarding")

# Держим ссылки на фоновые задачи отсчёта, иначе их может собрать сборщик мусора
_countdown_tasks: set[asyncio.Task] = set()


async def begin(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                settings: Settings, is_admin: bool, *, first_name: str = "") -> None:
    """Вход с любого места: капча, правила, анкета или меню — что нужно сейчас."""
    await state.clear()

    # Приём новых анкет можно приостановить из админ-панели.
    # Проверяем до капчи: незачем гонять новичка через задание, если вход закрыт.
    if (not user["registered"] and not is_admin
            and await mod_repo.get_setting("registration_open", "1") != "1"):
        await screen.send(bot, chat_id, state, texts.REGISTRATION_CLOSED, rkb.REMOVE)
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
                minutes=max(1, blocked // 60)), rkb.RECHECK)
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

def _caption(data: Mapping[str, Any], settings: Settings, status: str = "") -> str:
    selected = sorted(data.get("cap_selected") or [])
    if selected:
        status += texts.CAPTCHA_SELECTED.format(labels=", ".join(map(str, selected)))
    return texts.CAPTCHA_TASK.format(
        title=data.get("cap_title") or texts.CAPTCHA_TITLE,
        task=data.get("cap_task") or "",
        status=status,
        attempt=data.get("cap_attempt") or 1,
        total=settings.captcha_max_attempts,
    )


async def issue_captcha(bot: Bot, chat_id: int, state: FSMContext,
                        settings: Settings, *, title: str = texts.CAPTCHA_TITLE) -> None:
    """Генерирует и присылает новое задание капчи вместо прежнего экрана."""
    # Рисование картинки — работа для CPU, уводим её из основного потока
    challenge = await asyncio.to_thread(captcha_service.generate)
    used = await captcha_repo.attempts_used(chat_id)
    refreshes = int((await state.get_data()).get("cap_refresh", 0))

    await state.set_state(Onboarding.captcha)
    await state.update_data(
        cap_correct=challenge.correct,
        cap_selected=[],
        cap_started=time.monotonic(),
        cap_refresh=refreshes,
        cap_title=title,
        cap_task=challenge.task,
        cap_attempt=min(used + 1, settings.captcha_max_attempts),
    )
    sent = await bot.send_photo(
        chat_id,
        BufferedInputFile(challenge.image, filename="captcha.png"),
        caption=_caption(await state.get_data(), settings),
        reply_markup=rkb.captcha(refreshes < settings.captcha_max_refresh),
    )
    await screen.replace(bot, chat_id, state, [sent.message_id])


async def _update_caption(bot: Bot, chat_id: int, state: FSMContext,
                          settings: Settings, status: str = "") -> None:
    """Обновляет подпись капчи: какие клетки выбраны и что пошло не так."""
    ids = await screen.message_ids(state)
    if not ids:
        return
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=ids[-1],
            caption=_caption(await state.get_data(), settings, status),
        )
    except TelegramBadRequest:
        pass    # подпись не изменилась или сообщение уже убрано


@router.message(Onboarding.captcha, F.text.regexp(r"^\d{1,2}$"))
async def captcha_toggle(message: Message, state: FSMContext,
                         settings: Settings) -> None:
    """Номер клетки: выбрать или снять выбор. Что выбрано — видно в подписи."""
    await screen.drop(message)
    label = int(message.text or "0")
    if not 1 <= label <= captcha_service.CELLS:
        return
    data = await state.get_data()
    selected = set(data.get("cap_selected") or [])
    selected.symmetric_difference_update({label})
    await state.update_data(cap_selected=sorted(selected))
    await _update_caption(message.bot, message.chat.id, state, settings)


@router.message(Onboarding.captcha, F.text == rkb.CAPTCHA_NEW)
async def captcha_refresh(message: Message, state: FSMContext, bot: Bot,
                          settings: Settings) -> None:
    await screen.drop(message)
    used = int((await state.get_data()).get("cap_refresh", 0))
    if used >= settings.captcha_max_refresh:
        await _update_caption(bot, message.chat.id, state, settings,
                              texts.CAPTCHA_REFRESH_LIMIT)
        return
    await state.update_data(cap_refresh=used + 1)
    await issue_captcha(bot, message.chat.id, state, settings)


@router.message(Onboarding.captcha, F.text == rkb.CAPTCHA_DONE)
async def captcha_submit(message: Message, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings,
                         is_admin: bool) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    data = await state.get_data()
    selected = list(data.get("cap_selected") or [])
    correct = list(data.get("cap_correct") or [])
    started = float(data.get("cap_started") or 0)
    elapsed_ms = (time.monotonic() - started) * 1000 if started else 0

    if not selected:
        await _update_caption(bot, chat_id, state, settings, texts.CAPTCHA_EMPTY)
        return

    # Слишком быстро — человек физически не успевает рассмотреть 15 клеток
    if elapsed_ms < settings.captcha_min_solve_ms:
        await _fail_captcha(bot, chat_id, state, user, settings,
                            reason="too_fast", title=texts.CAPTCHA_TOO_FAST)
        return

    # Слишком долго — задание протухло, даём новое без списания попытки
    if elapsed_ms > settings.captcha_timeout_seconds * 1000:
        await issue_captcha(bot, chat_id, state, settings, title=texts.CAPTCHA_EXPIRED)
        return

    if not captcha_service.check(correct, selected):
        await _fail_captcha(bot, chat_id, state, user, settings,
                            reason="wrong", title=texts.CAPTCHA_WRONG)
        return

    # Успех
    await captcha_repo.register_pass(user["id"])
    await users_repo.update_user(user["id"], captcha_passed=1)
    await mod_repo.log_event("captcha_pass", user["id"], ms=int(elapsed_ms))

    if user["registered"]:
        # Проверку сбросила антинакрутка — возвращаем человека в меню
        await menu_handlers.show_menu(bot, chat_id, state, user, is_admin,
                                      note="✅ Проверка пройдена. Продолжаем!")
        return
    await send_welcome(bot, chat_id, state, user, note=texts.CAPTCHA_PASSED)


@router.message(Onboarding.captcha)
async def captcha_other(message: Message) -> None:
    """Посторонний текст во время капчи просто убираем: всё нужное — на кнопках."""
    await screen.drop(message)


async def _fail_captcha(bot: Bot, chat_id: int, state: FSMContext,
                        user: Mapping[str, Any], settings: Settings,
                        reason: str, title: str) -> None:
    left, block_minutes = await captcha_repo.register_fail(
        user["id"], settings.captcha_max_attempts, settings.captcha_block_minutes
    )
    await mod_repo.log_event("captcha_fail", user["id"], reason=reason)

    if left <= 0:
        await state.clear()
        await screen.send(bot, chat_id, state,
                          texts.CAPTCHA_BLOCKED.format(minutes=block_minutes), rkb.RECHECK)
        await admin_log(
            bot,
            f"🤖 Капча: пользователь <code>{user['id']}</code> "
            f"(@{user['username'] or '—'}) заблокирован на {block_minutes} мин "
            f"после {settings.captcha_max_attempts} неудачных попыток ({reason})."
        )
        return

    await issue_captcha(bot, chat_id, state, settings, title=title)


# ───────────────────── Приветствие и предупреждение ─────────────────────────

async def send_welcome(bot: Bot, chat_id: int, state: FSMContext,
                       user: Mapping[str, Any], note: str | None = None) -> None:
    await state.set_state(Onboarding.welcome)
    text = texts.WELCOME.format(name=profile.esc(user["tg_name"] or "друг"))
    await screen.send(bot, chat_id, state, f"{note}\n\n{text}" if note else text,
                      rkb.WELCOME)


@router.message(F.text == rkb.NEXT)
async def show_warning(message: Message, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings,
                       is_admin: bool) -> None:
    """Приветствие уходит, на его месте появляется предупреждение."""
    await screen.drop(message)
    chat_id = message.chat.id
    if user["rules_accepted"]:
        # Правила уже приняты — кнопка из старой клавиатуры, просто идём дальше
        await begin(bot, chat_id, state, user, settings, is_admin,
                    first_name=message.from_user.first_name or "")
        return

    seconds = max(1, settings.rules_delay_seconds)
    warning = texts.WARNING.format(min_age=settings.min_age)
    # У правил inline-кнопка «Принимаю», а она не уживается в одном сообщении
    # со снятием нижней клавиатуры — снимаем её отдельно
    await screen.hide_reply_keyboard(bot, chat_id)
    sent = await screen.send(bot, chat_id, state,
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

@router.message(F.text == rkb.USERNAME_DONE)
async def recheck_username(message: Message, state: FSMContext, bot: Bot,
                           user: Mapping[str, Any], settings: Settings,
                           is_admin: bool) -> None:
    await screen.drop(message)
    username = message.from_user.username
    if not username:
        await screen.send(bot, message.chat.id, state,
                          f"{texts.USERNAME_STILL_MISSING}\n\n{texts.NEED_USERNAME}",
                          rkb.USERNAME_CHECK)
        return
    await users_repo.update_user(message.from_user.id, username=username)
    fresh = await users_repo.get_user(message.from_user.id)
    await begin(bot, message.chat.id, state, fresh, settings, is_admin,
                first_name=message.from_user.first_name or "")
