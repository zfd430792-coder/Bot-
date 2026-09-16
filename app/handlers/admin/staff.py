"""Назначение модераторов. Только для владельцев из ADMIN_IDS."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import get_settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.db.database import db
from app.handlers.admin.filters import IsAdmin
from app.keyboards import inline as kb
from app.services import commands as bot_commands
from app.services import profile as profile_service
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

router = Router(name="admin-staff")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

WELCOME = (
    "👮 <b>Вас назначили модератором</b>\n\n"
    "В меню появилась кнопка «👮 Модератор». Вам доступны:\n"
    "• 🚨 жалобы на анкеты\n"
    "• ☑️ заявки на верификацию\n"
    "• 🚫 бан и снятие бана\n"
    "• 🔎 поиск пользователя и сообщение от администрации\n"
    "• 📊 краткая сводка\n\n"
    "Рассылка, реклама и настройки бота остаются у владельца.\n"
    "<i>Каждое ваше действие записывается в журнал.</i>"
)

REMOVED = "👮 Права модератора сняты. Кнопка «Модератор» больше не показывается."


async def moderators() -> list:
    return await db.fetchall(
        "SELECT id, name, tg_name, username FROM users WHERE is_moderator = 1 "
        "ORDER BY id"
    )


async def staff_view() -> tuple[str, object]:
    rows = await moderators()
    admins = ", ".join(f"<code>{a}</code>" for a in get_settings().admin_ids)
    lines = [
        "👮 <b>Модераторы</b>\n",
        f"Владельцы (из .env): {admins}\n",
    ]
    if rows:
        lines.append(f"Назначено модераторов: <b>{len(rows)}</b>")
        for row in rows:
            name = profile_service.esc(row["name"] or row["tg_name"] or "—")
            username = f"@{row['username']}" if row["username"] else "—"
            lines.append(f"• {name} · <code>{row['id']}</code> · {username}")
    else:
        lines.append("Модераторов пока нет.")
    lines.append(
        "\n<i>Модератору доступны жалобы, верификация, баны и поиск "
        "пользователя. Рассылка, реклама и настройки — только владельцу.</i>"
    )
    return "\n".join(lines), kb.staff_list(rows)


@router.callback_query(F.data == "adm:staff")
async def show_staff(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.menu)
    await call.answer()
    text, markup = await staff_view()
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


@router.message(Command("mods"))
async def mods_command(message: Message) -> None:
    text, markup = await staff_view()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:staff_add")
async def ask_moderator(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.staff_add)
    await call.answer()
    await call.message.answer(
        "Кого назначить модератором? Пришлите ID или @username.\n\n"
        "<i>Человек должен хотя бы раз запустить бота — иначе его нет в базе.</i>"
    )


@router.message(AdminPanel.staff_add, F.text)
async def add_moderator(message: Message, state: FSMContext, bot: Bot) -> None:
    target = await users_repo.find_user(message.text or "")
    await state.set_state(AdminPanel.menu)

    if target is None:
        await message.answer(
            "Пользователь не найден. Попросите его запустить бота и повторите."
        )
        return
    if get_settings().is_admin(target["id"]):
        await message.answer("Это владелец бота — у него и так полный доступ.")
        return
    if target["is_moderator"]:
        await message.answer("Он уже модератор.")
        return

    await users_repo.update_user(target["id"], is_moderator=1)
    await mod_repo.log_event("moderator_added", target["id"],
                             admin_id=message.from_user.id)
    await bot_commands.apply(bot, target["id"], bot_commands.MODERATOR)
    await safe_send(bot, target["id"], WELCOME)
    await message.answer(
        f"✅ Назначен модератором: <code>{target['id']}</code> "
        f"@{target['username'] or '—'}"
    )
    await admin_log(
        bot,
        f"👮 <b>Новый модератор</b>: <code>{target['id']}</code> "
        f"@{target['username'] or '—'}\nНазначил: <code>{message.from_user.id}</code>"
    )
    text, markup = await staff_view()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:staff_del:"))
async def remove_moderator(call: CallbackQuery, bot: Bot) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await users_repo.update_user(target_id, is_moderator=0)
    await mod_repo.log_event("moderator_removed", target_id,
                             admin_id=call.from_user.id)
    await bot_commands.apply(bot, target_id, None)
    await safe_send(bot, target_id, REMOVED)
    await call.answer("Права сняты")
    await admin_log(
        bot,
        f"👮 <b>Модератор снят</b>: <code>{target_id}</code>\n"
        f"Снял: <code>{call.from_user.id}</code>"
    )
    text, markup = await staff_view()
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


@router.message(Command("addmod"))
async def addmod_command(message: Message, state: FSMContext, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/addmod 123456789</code>")
        return
    await state.set_state(AdminPanel.staff_add)
    message_copy = message.model_copy(update={"text": parts[1]})
    await add_moderator(message_copy, state, bot)


@router.message(Command("delmod"))
async def delmod_command(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/delmod 123456789</code>")
        return
    target = await users_repo.find_user(parts[1])
    if target is None or not target["is_moderator"]:
        await message.answer("Такого модератора нет.")
        return
    await users_repo.update_user(target["id"], is_moderator=0)
    await bot_commands.apply(bot, target["id"], None)
    await safe_send(bot, target["id"], REMOVED)
    await message.answer(f"✅ Права сняты: <code>{target['id']}</code>")
    await admin_log(bot, f"👮 <b>Модератор снят</b>: <code>{target['id']}</code>\n"
                         f"Снял: <code>{message.from_user.id}</code>")
