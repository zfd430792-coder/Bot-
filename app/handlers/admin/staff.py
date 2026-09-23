"""Назначение модераторов. Только для владельцев из ADMIN_IDS.

Список — inline-кнопки «❌ Снять Имя (ID)»: номер человека в самой кнопке,
поэтому состояние для выбора не нужно.
"""
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
from app.services import screen
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

router = Router(name="admin-staff")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

WELCOME = (
    "👮 <b>Вас назначили модератором</b>\n\n"
    "В меню появилась кнопка «👮 Модератор». Вам доступны:\n"
    "• 🚨 жалобы на анкеты\n"
    "• ✅ заявки на верификацию\n"
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


async def show_staff(bot: Bot, chat_id: int, state: FSMContext,
                     notice: str | None = None) -> None:
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
    text = "\n".join(lines)
    await state.set_state(AdminPanel.staff_list)
    await screen.show(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      kb.staff_list(rows))


@router.message(Command("mods"))
async def mods_command(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await show_staff(message.bot, message.chat.id, state)


@router.callback_query(F.data == "adm:staff")
async def staff_button(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await show_staff(call.bot, screen.chat_id(call), state)


@router.callback_query(F.data == "adm:staff:add")
async def ask_moderator(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminPanel.staff_add)
    await screen.show(call.bot, screen.chat_id(call), state,
                      "Кого назначить модератором? Пришлите ID или @username.\n\n"
                      "<i>Человек должен хотя бы раз запустить бота — иначе его нет "
                      "в базе.</i>", kb.STAFF_BACK)


async def add_moderator(bot: Bot, admin_id: int, query: str) -> str:
    """Назначает модератора. Возвращает строку о результате."""
    target = await users_repo.find_user(query)
    if target is None:
        return "Пользователь не найден. Попросите его запустить бота и повторите."
    if get_settings().is_admin(target["id"]):
        return "Это владелец бота — у него и так полный доступ."
    if target["is_moderator"]:
        return "Он уже модератор."

    await users_repo.update_user(target["id"], is_moderator=1)
    await mod_repo.log_event("moderator_added", target["id"], admin_id=admin_id)
    await bot_commands.apply(bot, target["id"], bot_commands.MODERATOR)
    await safe_send(bot, target["id"], WELCOME)
    await admin_log(
        bot,
        f"👮 <b>Новый модератор</b>: <code>{target['id']}</code> "
        f"@{target['username'] or '—'}\nНазначил: <code>{admin_id}</code>"
    )
    return (f"✅ Назначен модератором: <code>{target['id']}</code> "
            f"@{target['username'] or '—'}")


async def remove_moderator(bot: Bot, admin_id: int, target_id: int) -> None:
    await users_repo.update_user(target_id, is_moderator=0)
    await mod_repo.log_event("moderator_removed", target_id, admin_id=admin_id)
    await bot_commands.apply(bot, target_id, None)
    await safe_send(bot, target_id, REMOVED)
    await admin_log(
        bot,
        f"👮 <b>Модератор снят</b>: <code>{target_id}</code>\n"
        f"Снял: <code>{admin_id}</code>"
    )


@router.message(AdminPanel.staff_add, F.text)
async def add_from_panel(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    result = await add_moderator(bot, message.from_user.id, message.text or "")
    await show_staff(bot, message.chat.id, state, f"<i>{result}</i>")


@router.callback_query(F.data.startswith("adm:staff:del:"))
async def remove_from_panel(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    tail = (call.data or "").rsplit(":", 1)[-1]
    target = await users_repo.get_user(int(tail)) if tail.isdigit() else None
    if target is None or not target["is_moderator"]:
        await show_staff(bot, screen.chat_id(call), state, "<i>Такого модератора нет</i>")
        return
    await remove_moderator(bot, call.from_user.id, int(target["id"]))
    await show_staff(bot, screen.chat_id(call), state,
                     f"<i>Права сняты: <code>{target['id']}</code></i>")


@router.message(Command("addmod"))
async def addmod_command(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/addmod 123456789</code>")
        return
    await message.answer(await add_moderator(bot, message.from_user.id, parts[1]))


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
    await remove_moderator(bot, message.from_user.id, target["id"])
    await message.answer(f"✅ Права сняты: <code>{target['id']}</code>")
