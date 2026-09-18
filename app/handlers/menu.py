"""Главное меню, справка и список пар.

Меню — одно сообщение с кнопками: сводка по анкете сверху, разделы под ней.
Переходы правят это же сообщение или заменяют его (если нужна карточка с
фото), поэтому чат не зарастает ни меню, ни ответами «ничего нет».
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import get_settings
from app.db import users as users_repo
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile, screen

router = Router(name="menu")


def _summary(user: Mapping[str, Any], likes: int, matches: int) -> str:
    verified = f" {texts.VERIFY_BADGE}" if user["verify_status"] == "verified" else ""
    lines = [
        f"👤 {profile.esc(user['name'])}, {profile.years(user['age'])} · "
        f"{profile.esc(user['city'] or 'город не указан')}{verified}",
        f"❤️ Вас лайкнули: <b>{likes}</b>",
        f"💬 Взаимных симпатий: <b>{matches}</b>",
    ]
    if not user["is_active"]:
        lines.append("🙈 <i>Анкета скрыта из поиска</i>")
    elif user["verify_status"] == "pending":
        lines.append("⏳ <i>Верификация на проверке</i>")
    return "\n".join(lines)


async def show_menu(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], is_admin: bool,
                    note: str | None = None) -> None:
    """Главное меню одним экраном. Любой незаконченный диалог при этом бросаем."""
    await state.clear()
    fresh = await users_repo.get_user(user["id"]) or user
    if not fresh["registered"]:
        await screen.show(bot, chat_id, state,
                          (f"{note}\n\n" if note else "")
                          + "Анкеты пока нет — давайте заполним.",
                          kb.START_OVER)
        return

    likes = await users_repo.count_incoming_likes(fresh["id"])
    matches = await users_repo.count_matches(fresh["id"])
    text = texts.MAIN_MENU.format(summary=_summary(fresh, likes, matches))
    if note:
        text = f"{note}\n\n{text}"
    is_moderator = bool(fresh["is_moderator"]) and not is_admin
    await screen.show(bot, chat_id, state, text,
                      kb.main_menu(likes, matches, is_admin=is_admin,
                                   is_moderator=is_moderator))


@router.message(Command("menu"))
async def menu_command(message: Message, state: FSMContext, user,
                       is_admin: bool) -> None:
    await screen.drop(message)
    await show_menu(message.bot, message.chat.id, state, user, is_admin)


@router.callback_query(F.data == "m:home")
async def menu_button(call: CallbackQuery, state: FSMContext, user,
                      is_admin: bool) -> None:
    await call.answer()
    await show_menu(call.bot, call.message.chat.id, state, user, is_admin)


# ─────────────────────────────── Справка ────────────────────────────────────

async def show_help(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await screen.show(bot, chat_id, state,
                      texts.HELP.format(limit=get_settings().likes_limit_per_day),
                      kb.BACK_HOME)


@router.message(Command("help"))
@router.message(F.text == rkb.BTN_HELP)
async def help_command(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await show_help(message.bot, message.chat.id, state)


@router.callback_query(F.data == "m:help")
async def help_button(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await show_help(call.bot, call.message.chat.id, state)


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


@router.message(F.text == rkb.BTN_MATCHES)
async def matches_command(message: Message, state: FSMContext,
                          user: Mapping[str, Any], is_admin: bool) -> None:
    await screen.drop(message)
    text = await matches_text(user["id"])
    if text is None:
        await show_menu(message.bot, message.chat.id, state, user, is_admin,
                        note=texts.NO_MATCHES)
        return
    await screen.show(message.bot, message.chat.id, state, text, kb.BACK_HOME)


@router.callback_query(F.data == "m:matches")
async def matches_button(call: CallbackQuery, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    text = await matches_text(user["id"])
    if text is None:
        # Пустой раздел не стоит отдельного экрана — хватит всплывашки
        await call.answer(texts.NO_MATCHES, show_alert=True)
        return
    await call.answer()
    await screen.show(call.bot, call.message.chat.id, state, text, kb.BACK_HOME)
