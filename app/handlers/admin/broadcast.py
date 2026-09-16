"""Рассылка: выбор аудитории -> сообщение -> предпросмотр -> отправка."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.db import users as users_repo
from app.db.database import db
from app.handlers.admin.filters import IsAdmin
from app.keyboards import inline as kb
from app.services import broadcast as broadcast_service
from app.states import AdminPanel

router = Router(name="admin-broadcast")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

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


@router.callback_query(F.data == "adm:bc")
async def choose_audience(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.broadcast_audience)
    await call.answer()
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nКому отправляем?",
        reply_markup=kb.broadcast_audience(),
    )


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminPanel.broadcast_audience)
    await message.answer("📢 <b>Рассылка</b>\n\nКому отправляем?",
                         reply_markup=kb.broadcast_audience())


@router.callback_query(F.data.startswith("adm:bc_aud:"))
async def set_audience(call: CallbackQuery, state: FSMContext) -> None:
    audience = (call.data or "").split(":")[-1]
    await state.update_data(bc_audience=audience, bc_city=None)
    await call.answer()

    if audience == "city":
        await state.set_state(AdminPanel.broadcast_city)
        await call.message.edit_text("Напишите название города (как в анкетах):")
        return

    await _ask_content(call.message, state, audience, None)


@router.message(AdminPanel.broadcast_city, F.text)
async def set_city(message: Message, state: FSMContext) -> None:
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
    await message.answer(
        f"📢 Аудитория: <b>{title}</b>\nПолучателей: <b>{len(recipients)}</b>\n\n"
        "Теперь пришлите сообщение для рассылки — текст, фото, видео, кружок "
        "или что угодно. Оно уйдёт людям ровно в том виде, в каком придёт сюда."
    )


@router.message(AdminPanel.broadcast_content)
async def preview(message: Message, state: FSMContext) -> None:
    await state.update_data(bc_from_chat=message.chat.id, bc_message=message.message_id)
    data = await state.get_data()
    await state.set_state(AdminPanel.broadcast_confirm)

    title = AUDIENCE_TITLE.get(data.get("bc_audience", "all"), "—")
    if data.get("bc_city"):
        title += f" «{data['bc_city']}»"
    await message.answer(
        "👆 <b>Так это увидят люди.</b>\n\n"
        f"Аудитория: <b>{title}</b>\n"
        f"Получателей: <b>{data.get('bc_count', 0)}</b>\n\n"
        "Отправляем?",
        reply_markup=kb.BROADCAST_CONFIRM,
    )


@router.callback_query(F.data == "adm:bc_go", AdminPanel.broadcast_confirm)
async def launch(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    audience = data.get("bc_audience", "all")
    city = data.get("bc_city")
    from_chat = data.get("bc_from_chat")
    message_id = data.get("bc_message")

    if not from_chat or not message_id:
        await call.answer("Сообщение потерялось — начните заново", show_alert=True)
        return

    recipients = await users_repo.audience_ids(audience, city)
    if not recipients:
        await call.answer("Получателей нет", show_alert=True)
        return

    broadcast_id = await db.insert(
        "INSERT INTO broadcasts (admin_id, audience, preview, total) "
        "VALUES (?, ?, ?, ?)",
        (call.from_user.id, audience + (f":{city}" if city else ""),
         (call.message.text or "")[:200], len(recipients)),
    )

    await call.answer("Запускаю")
    await state.set_state(AdminPanel.menu)
    status = await call.message.answer(
        broadcast_service.Progress(total=len(recipients)).render()
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
