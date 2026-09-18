"""Рассылка: выбор аудитории -> сообщение -> предпросмотр -> отправка.

Сообщение для рассылки бот не удаляет: люди получат его копию, а копируется
оно именно из этого чата.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.db import users as users_repo
from app.db.database import db
from app.handlers.admin import panel
from app.handlers.admin.filters import IsAdmin
from app.keyboards import reply as rkb
from app.services import broadcast as broadcast_service
from app.services import screen
from app.states import AdminPanel

router = Router(name="admin-broadcast")
router.message.filter(IsAdmin())

AUDIENCE_TITLE = {
    "all": "всем пользователям",
    "registered": "всем с заполненной анкетой",
    "active7": "активным за 7 дней",
    "active30": "активным за 30 дней",
    "sleeping": "спящим (30+ дней без входа)",
    "unfinished": "не дозаполнившим анкету",
    "male": "парням",
    "female": "девушкам",
    "city": "по городу",
}
AUDIENCE_BY_BUTTON = dict(rkb.AUDIENCES)


@router.message(Command("broadcast"))
@router.message(F.text == rkb.A_BROADCAST)
async def choose_audience(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(AdminPanel.broadcast_audience)
    await screen.send(message.bot, message.chat.id, state,
                      "📢 <b>Рассылка</b>\n\nКому отправляем?", rkb.BROADCAST_AUDIENCE)


@router.message(AdminPanel.broadcast_audience, F.text.in_(AUDIENCE_BY_BUTTON))
async def set_audience(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    audience = AUDIENCE_BY_BUTTON[message.text]
    await state.update_data(bc_audience=audience, bc_city=None)
    if audience == "city":
        await state.set_state(AdminPanel.broadcast_city)
        await screen.send(message.bot, message.chat.id, state,
                          "Напишите название города (как в анкетах):", rkb.ADMIN_BACK)
        return
    await _ask_content(message, state, audience, None)


@router.message(AdminPanel.broadcast_audience)
async def audience_hint(message: Message) -> None:
    await screen.drop(message)


@router.message(AdminPanel.broadcast_city, F.text)
async def set_city(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    city = (message.text or "").strip()
    await state.update_data(bc_city=city)
    await _ask_content(message, state, "city", city)


async def _ask_content(message: Message, state: FSMContext, audience: str,
                       city: str | None) -> None:
    recipients = await users_repo.audience_ids(audience, city)
    await state.update_data(bc_count=len(recipients))
    await state.set_state(AdminPanel.broadcast_content)

    title = AUDIENCE_TITLE.get(audience, audience)
    if city:
        title += f" «{city}»"
    await screen.send(
        message.bot, message.chat.id, state,
        f"📢 Аудитория: <b>{title}</b>\nПолучателей: <b>{len(recipients)}</b>\n\n"
        "Теперь пришлите сообщение для рассылки — текст, фото, видео, кружок "
        "или что угодно. Оно уйдёт людям ровно в том виде, в каком придёт сюда.",
        rkb.ADMIN_BACK,
    )


@router.message(AdminPanel.broadcast_content)
async def preview(message: Message, state: FSMContext) -> None:
    # Это сообщение не удаляем: рассылка копирует именно его
    await state.update_data(bc_from_chat=message.chat.id, bc_message=message.message_id,
                            bc_preview=(message.text or message.caption or "")[:200])
    data = await state.get_data()
    await state.set_state(AdminPanel.broadcast_confirm)

    title = AUDIENCE_TITLE.get(data.get("bc_audience", "all"), "—")
    if data.get("bc_city"):
        title += f" «{data['bc_city']}»"
    await screen.send(
        message.bot, message.chat.id, state,
        "👆 <b>Так это увидят люди.</b>\n\n"
        f"Аудитория: <b>{title}</b>\n"
        f"Получателей: <b>{data.get('bc_count', 0)}</b>\n\n"
        "Отправляем?",
        rkb.BROADCAST_CONFIRM,
    )


@router.message(AdminPanel.broadcast_confirm, F.text == rkb.A_SEND)
async def launch(message: Message, state: FSMContext, bot: Bot, is_admin: bool) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    data = await state.get_data()
    audience = data.get("bc_audience", "all")
    city = data.get("bc_city")
    from_chat = data.get("bc_from_chat")
    message_id = data.get("bc_message")

    if not from_chat or not message_id:
        await panel.open_panel(bot, chat_id, state, is_admin,
                               "⚠️ <i>Сообщение потерялось — начните рассылку заново</i>")
        return

    recipients = await users_repo.audience_ids(audience, city)
    if not recipients:
        await panel.open_panel(bot, chat_id, state, is_admin, "<i>Получателей нет</i>")
        return

    broadcast_id = await db.insert(
        "INSERT INTO broadcasts (admin_id, audience, preview, total) "
        "VALUES (?, ?, ?, ?)",
        (message.from_user.id, audience + (f":{city}" if city else ""),
         data.get("bc_preview") or "", len(recipients)),
    )

    # Прогресс — отдельным сообщением: его правит рассылка, пока идёт
    status = await bot.send_message(
        chat_id, broadcast_service.Progress(total=len(recipients)).render()
    )
    broadcast_service.schedule(
        bot,
        broadcast_id=broadcast_id,
        user_ids=recipients,
        from_chat_id=from_chat,
        message_id=message_id,
        status_chat_id=status.chat.id,
        status_message_id=status.message_id,
    )
    await panel.open_panel(bot, chat_id, state, is_admin,
                           "🚀 <i>Рассылка запущена — прогресс в сообщении выше</i>")


@router.message(AdminPanel.broadcast_confirm)
async def confirm_hint(message: Message) -> None:
    await screen.drop(message)
