"""Главное меню и поддержка.

Меню — сообщение «Выберите, что нужно» с нижними кнопками: анкеты, своя
анкета и поддержка. Настроек нет — лента сама идёт от ближних к дальним.
Лайки и пары отдельными разделами не живут: кто лайкнул, тот первым в
ленте, а контакт пары бот присылает сразу при совпадении.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.keyboards import reply as rkb
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
        await screen.send(bot, chat_id, state,
                          lead + "Анкеты пока нет — давайте заполним.",
                          rkb.START_AGAIN)
        return

    # Пока контакт поддержки не указан, кнопку видит только владелец
    support = is_admin or bool(await mod_repo.support_username())
    is_moderator = bool(fresh["is_moderator"]) and not is_admin
    await screen.send(bot, chat_id, state, lead + texts.MAIN_MENU,
                      rkb.main_menu(support=support, is_admin=is_admin,
                                    is_moderator=is_moderator))


@router.message(Command("support"))
@router.message(Command("help"))
@router.message(F.text == rkb.SUPPORT)
async def show_support(message: Message, state: FSMContext,
                       user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    await state.clear()     # иначе, скажем, ждущий текст к лайку принял бы за него ответ
    bot, chat_id = message.bot, message.chat.id
    username = await mod_repo.support_username()
    if username:
        await screen.send(bot, chat_id, state,
                          texts.SUPPORT.format(username=username), rkb.HOME_ONLY)
    elif is_admin:
        await screen.send(bot, chat_id, state, texts.SUPPORT_NOT_SET, rkb.SUPPORT_SETUP)
    else:
        await show_menu(bot, chat_id, state, user, is_admin)
