"""Главное меню и поддержка.

Меню — сообщение «Выберите, что нужно» с inline-кнопками: анкеты, своя
анкета и поддержка. Настроек нет — лента сама идёт от ближних к дальним.
Лайки и пары отдельными разделами не живут: кто лайкнул, тот первым в
ленте, а контакт пары бот присылает сразу при совпадении.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.keyboards import inline as kb
from app.services import screen

router = Router(name="menu")


async def show_menu(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], is_admin: bool,
                    note: str | None = None) -> None:
    """Главное меню одним экраном. Любой незаконченный диалог при этом бросаем."""
    await state.clear()
    fresh = await users_repo.get_user(user["id"]) or user
    lead = f"{note}\n\n" if note else ""
    if not fresh["registered"]:
        await screen.show(bot, chat_id, state,
                          lead + "Анкеты пока нет — давайте заполним.",
                          kb.START_AGAIN)
        return

    # Пока контакт поддержки не указан, кнопку видит только владелец
    support = is_admin or bool(await mod_repo.support_username())
    is_moderator = bool(fresh["is_moderator"]) and not is_admin
    await screen.show(bot, chat_id, state, lead + texts.MAIN_MENU,
                      kb.main_menu(support=support, is_admin=is_admin,
                                   is_moderator=is_moderator))


async def show_support(bot: Bot, chat_id: int, state: FSMContext,
                       user: Mapping[str, Any], is_admin: bool) -> None:
    await state.clear()     # иначе, скажем, ждущий текст к лайку принял бы за него ответ
    username = await mod_repo.support_username()
    if username:
        await screen.show(bot, chat_id, state,
                          texts.SUPPORT.format(username=username), kb.HOME_ONLY)
    elif is_admin:
        await screen.show(bot, chat_id, state, texts.SUPPORT_NOT_SET, kb.SUPPORT_SETUP)
    else:
        await show_menu(bot, chat_id, state, user, is_admin)


@router.message(Command("support"))
@router.message(Command("help"))
async def support_command(message: Message, state: FSMContext,
                          user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    await show_support(message.bot, message.chat.id, state, user, is_admin)


@router.callback_query(F.data == "m:support")
async def support_button(call: CallbackQuery, state: FSMContext,
                         user: Mapping[str, Any], is_admin: bool) -> None:
    await call.answer()
    await show_support(call.bot, screen.chat_id(call), state, user, is_admin)
