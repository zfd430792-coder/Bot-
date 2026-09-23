"""Переходы, которые работают откуда угодно: /start, /menu и кнопка «🏠 Меню».

Роутер подключается первым: иначе кнопку меню, нажатую посреди ввода
(например, пока бот ждёт текст сообщения к лайку), перехватил бы хендлер
этого ввода и принял бы за ответ.

Здесь же — нажатия нижних кнопок прежних версий: у кого-то такая клавиатура
ещё открыта в чате. Бот делает то же, что сделала бы нынешняя кнопка, и
заодно убирает клавиатуру, показывая обычный экран.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.handlers import browse
from app.handlers import menu as menu_handlers
from app.handlers import onboarding
from app.handlers import profile as profile_handlers
from app.keyboards import reply as rkb
from app.services import screen

router = Router(name="nav")


# ──────────────────────────────── /start ────────────────────────────────────

@router.message(CommandStart())
@router.message(F.text == rkb.L_START_OVER)
async def start_command(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings,
                        is_admin: bool) -> None:
    await screen.drop(message)          # сама команда в чате не нужна
    await onboarding.begin(bot, message.chat.id, state, user, settings, is_admin,
                           first_name=message.from_user.first_name or "")


@router.callback_query(F.data == "m:start")
async def start_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings,
                       is_admin: bool) -> None:
    await call.answer()
    await onboarding.begin(bot, screen.chat_id(call), state, user, settings, is_admin,
                           first_name=call.from_user.first_name or "")


# ───────────────────────────── Главное меню ─────────────────────────────────

@router.message(Command("menu"))
@router.message(F.text == rkb.L_HOME)
@router.message(F.text.regexp(rkb.L_MENU_RE))
async def menu_command(message: Message, state: FSMContext,
                       user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    await menu_handlers.show_menu(message.bot, message.chat.id, state, user, is_admin)


@router.callback_query(F.data == "m:home")
async def menu_button(call: CallbackQuery, state: FSMContext,
                      user: Mapping[str, Any], is_admin: bool) -> None:
    await call.answer()
    await menu_handlers.show_menu(call.bot, screen.chat_id(call), state, user, is_admin)


# ──────────────── Прежние нижние кнопки: делаем то же самое ─────────────────

@router.message(F.text == rkb.L_SEARCH)
@router.message(F.text.regexp(rkb.L_LIKES_RE))
async def legacy_search(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    """«Смотреть анкеты» и «Кто меня лайкнул»: лайкнувшие теперь первые в ленте."""
    await screen.drop(message)
    await browse.open_feed(bot, message.chat.id, state, user, settings)


@router.message(F.text == rkb.L_PROFILE)
@router.message(F.text.regexp(rkb.L_PROFILE_RE))
async def legacy_profile(message: Message, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await profile_handlers.show_profile(message.bot, message.chat.id, state, user["id"])


@router.message(F.text == rkb.L_SUPPORT)
async def legacy_support(message: Message, state: FSMContext,
                         user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    await menu_handlers.show_support(message.bot, message.chat.id, state, user, is_admin)
