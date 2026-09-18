"""Главное меню, справка и список пар.

Меню — сообщение «Выберите, что нужно» с нижними кнопками разделов. Каждый
переход присылает новый экран вместо прежнего, поэтому в чате всегда одно
сообщение бота, а не история нажатий.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import get_settings
from app.db import users as users_repo
from app.keyboards import reply as rkb
from app.services import profile, screen

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

    likes = await users_repo.count_incoming_likes(fresh["id"])
    matches = await users_repo.count_matches(fresh["id"])
    is_moderator = bool(fresh["is_moderator"]) and not is_admin
    await screen.send(bot, chat_id, state, lead + texts.MAIN_MENU,
                      rkb.main_menu(likes, matches, is_admin=is_admin,
                                    is_moderator=is_moderator))


# ─────────────────────────────── Справка ────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == rkb.HELP)
async def show_help(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await screen.send(message.bot, message.chat.id, state,
                      texts.HELP.format(limit=get_settings().likes_limit_per_day),
                      rkb.HOME_ONLY)


# ──────────────────────────────── Пары ──────────────────────────────────────

async def matches_text(user_id: int) -> str | None:
    rows = await users_repo.get_matches(user_id)
    if not rows:
        return None
    lines = ["💬 <b>Взаимные симпатии</b>\n",
             "<i>Напишите первым — это работает лучше, чем ждать.</i>\n"]
    for row in rows:
        link = f"@{row['username']}" if row["username"] else "профиль скрыт"
        verified = f" {texts.VERIFY_BADGE}" if row["verify_status"] == "verified" else ""
        lines.append(
            f"{profile.GENDER_EMOJI.get(row['gender'], '•')} "
            f"<b>{profile.esc(row['name'])}</b>, {row['age']}{verified} — {link}"
        )
    return "\n".join(lines)


@router.message(F.text.startswith(rkb.MATCHES))
@router.message(F.text == rkb.MATCHES_OLD)
async def show_matches(message: Message, state: FSMContext,
                       user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    text = await matches_text(user["id"])
    if text is None:
        # Пустой раздел не стоит отдельного экрана — меню с подсказкой
        await show_menu(message.bot, message.chat.id, state, user, is_admin,
                        note=texts.NO_MATCHES)
        return
    await screen.send(message.bot, message.chat.id, state, text, rkb.HOME_ONLY)
