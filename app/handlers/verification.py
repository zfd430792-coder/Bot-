"""Верификация анкет кружком.

Два сценария:
* пользователь сам просит галочку — бот работает как обычно;
* администратор требует проверку — бот закрыт до подтверждения (см. gates.py).

Проверка — кружок: его записывают прямо с камеры, поэтому чужое фото или
старое видео не подсунуть. В кружке человек показывает листок, где крупно
написан код, а под ним @ник бота, и читает код вслух. Код бот выбирает
случайно в момент, когда человек садится записывать, и задание живёт
TASK_MINUTES минут. Ник бота на листке не даёт выдать за проверку готовый
кружок из чужого канала. Принимается только кружок — не фото, не обычное
видео и не пересланный.

Если администратор загрузил кружок-пример («⚙️ Настройки бота»), он стоит
над заданием. Заявки администратор разбирает в админке («✅ Верификация»),
сюда ему приходит только короткое уведомление с кружком и кнопкой
«Проверить заявку».
"""
from __future__ import annotations

import secrets
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings, get_settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import profile as profile_handlers
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import safe_send
from app.states import Verification

router = Router(name="verification")

TASK_MINUTES = 10       # столько живёт задание: записанный заранее кружок не подойдёт
MIN_SECONDS = 3         # короче не успеть показать листок и назвать код
MAX_SECONDS = 20
# Код из кружка-примера. Настоящим он не выдаётся — иначе проверку прошёл бы
# сам пример
EXAMPLE_CODE = "1234"
SEND_TEXT = "📸 Отправить фото с кодом"      # нижняя кнопка прежней версии


def new_code() -> str:
    """Четыре цифры — их легко написать разборчиво и назвать вслух."""
    while True:
        code = "".join(secrets.choice("0123456789") for _ in range(4))
        if code != EXAMPLE_CODE:
            return code


async def bot_name(bot: Bot) -> str:
    """@ник бота — его пишут на листке под кодом."""
    return f"@{(await bot.me()).username}"


def task_summary(record: Mapping[str, Any], bot_nick: str) -> str:
    """Что должно быть в кружке — чек-лист для администратора."""
    if record["issued_at"] is None:
        # Заявка прежней версии: фото с кодом на листе бумаги
        return f"Код на фото должен быть: <code>{record['code']}</code>"
    return (
        "В кружке должно быть:\n"
        f"• листок: <b>{record['code']}</b>, под ним <b>{bot_nick}</b> — чётко видно\n"
        f"• код вслух: <b>{record['code']}</b>"
    )


async def request_verification(bot: Bot, user_id: int, *, forced: bool,
                               admin_id: int | None = None,
                               notify: bool = True) -> bool:
    """Открывает заявку и уведомляет пользователя (notify=False — экран
    покажет вызывающий). Код бот выдаст позже, когда человек
    нажмёт «🎥 Записать кружок».

    Возвращает False, если это владелец бота — на него ограничения не
    действуют, и запереть его требованием проверки нельзя.
    """
    if get_settings().is_admin(user_id):
        return False

    # Сам попросивший остаётся в прежнем статусе, пока не пришлёт кружок:
    # передумает — кнопка «Пройти верификацию» в анкете никуда не денется
    if forced:
        await users_repo.update_user(user_id, verify_status="required", verify_forced=1)
    await mod_repo.create_verification(user_id, forced, admin_id)
    await mod_repo.log_event("verify_requested", user_id, forced=forced,
                             admin_id=admin_id)

    if notify:
        if forced:
            await safe_send(bot, user_id, texts.VERIFY_REQUIRED, kb.VERIFY_REQUIRED)
        else:
            await safe_send(bot, user_id, texts.VERIFY_SELF, kb.VERIFY_SELF)
    return True


# ────────────────────── Пользователь начинает проверку ──────────────────────

@router.callback_query(F.data == "ver:self")
async def self_request(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any]) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    if not user["registered"]:
        # Проверять нечего, а заявка ушла бы админу — сначала анкета
        await profile_handlers.show_profile(bot, chat_id, state, user["id"])
        return
    if user["verify_status"] == "verified":
        await screen.show(bot, chat_id, state, texts.VERIFY_ALREADY, kb.HOME_ONLY)
        return
    if await mod_repo.awaiting_review(user["id"]):
        # Кнопка со старого экрана: кружок уже у администратора
        await screen.show(bot, chat_id, state, texts.VERIFY_PENDING, kb.TO_PROFILE)
        return
    if not await request_verification(bot, user["id"], forced=False, notify=False):
        await screen.show(bot, chat_id, state,
                          "Вы владелец бота — верификация вам не нужна.", kb.HOME_ONLY)
        return
    await screen.show(bot, chat_id, state, texts.VERIFY_SELF, kb.VERIFY_SELF)


def _fresh(record: Mapping[str, Any]) -> bool:
    """Задание выдано, и его срок не вышел."""
    age = record["task_age"]
    return age is not None and age < TASK_MINUTES * 60


async def _task(user: Mapping[str, Any]) -> Mapping[str, Any]:
    """Действующее задание. Заявки нет (её отклонили, бот перезапускался) —
    открывает новую; задание не выдано или устарело — выдаёт новое."""
    record = await mod_repo.current_verification(user["id"])
    if record is None:
        await mod_repo.create_verification(user["id"], bool(user["verify_forced"]), None)
        record = await mod_repo.current_verification(user["id"])
    if not _fresh(record):
        await mod_repo.issue_verification_task(record["id"], new_code())
        record = await mod_repo.current_verification(user["id"])
    return record


async def _show_task(bot: Bot, chat_id: int, state: FSMContext,
                     user: Mapping[str, Any], notice: str | None = None) -> None:
    """Экран записи: задание, а над ним — кружок-пример, если он загружен."""
    record = await _task(user)
    await state.set_state(Verification.waiting_media)
    text = texts.VERIFY_TASK.format(
        code=record["code"],
        bot=await bot_name(bot),
        minutes=TASK_MINUTES,
    )
    example = await mod_repo.verify_example()
    if not example:
        await screen.show(bot, chat_id, state,
                          f"{notice}\n\n{text}" if notice else text, kb.VERIFY_CANCEL)
        return

    # У кружка не бывает подписи — пример и задание идут двумя сообщениями
    await screen.prepare(bot, chat_id, state)
    ids: list[int] = []
    try:
        ids.append((await bot.send_video_note(chat_id, example)).message_id)
        text = f"{texts.VERIFY_EXAMPLE_NOTE.format(code=EXAMPLE_CODE)}\n\n{text}"
    except TelegramBadRequest:
        pass    # файл примера недоступен — задание важнее, покажем его без примера
    if notice:
        text = f"{notice}\n\n{text}"
    sent = await bot.send_message(chat_id, text, reply_markup=kb.VERIFY_CANCEL)
    await screen.remember(state, ids + [sent.message_id])


async def _start_upload(bot: Bot, chat_id: int, state: FSMContext,
                        user: Mapping[str, Any]) -> None:
    fresh = await users_repo.get_user(user["id"])
    if not fresh["registered"] and not fresh["verify_forced"]:
        await profile_handlers.show_profile(bot, chat_id, state, user["id"])
        return
    if fresh["verify_status"] == "verified":
        await screen.show(bot, chat_id, state, texts.VERIFY_ALREADY, kb.HOME_ONLY)
        return
    if await mod_repo.awaiting_review(fresh["id"]):
        # Кружок уже у администратора — новое задание разошлось бы с ним
        await state.clear()
        await screen.show(bot, chat_id, state, texts.VERIFY_PENDING,
                          None if fresh["verify_forced"] else kb.TO_PROFILE)
        return
    await _show_task(bot, chat_id, state, fresh)


@router.callback_query(F.data == "ver:send")
async def start_upload(call: CallbackQuery, state: FSMContext,
                       user: Mapping[str, Any]) -> None:
    await call.answer()
    await _start_upload(call.bot, screen.chat_id(call), state, user)


@router.message(F.text == SEND_TEXT)
async def start_upload_legacy(message: Message, state: FSMContext,
                              user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await _start_upload(message.bot, message.chat.id, state, user)


async def _cancel_upload(bot: Bot, chat_id: int, state: FSMContext,
                         user: Mapping[str, Any]) -> None:
    await state.clear()
    fresh = await users_repo.get_user(user["id"])
    if fresh["verify_forced"]:
        await screen.show(bot, chat_id, state, texts.VERIFY_REQUIRED, kb.VERIFY_REQUIRED)
        return
    # Проверку просили сами, из анкеты — туда и возвращаем
    await profile_handlers.show_profile(bot, chat_id, state, user["id"])


@router.callback_query(F.data == "ver:cancel")
async def cancel_upload(call: CallbackQuery, state: FSMContext,
                        user: Mapping[str, Any]) -> None:
    await call.answer()
    await _cancel_upload(call.bot, screen.chat_id(call), state, user)


@router.message(Verification.waiting_media, F.text == rkb.CANCEL)
async def cancel_upload_legacy(message: Message, state: FSMContext,
                               user: Mapping[str, Any]) -> None:
    await screen.drop(message)
    await _cancel_upload(message.bot, message.chat.id, state, user)


@router.message(Verification.waiting_media)
async def receive_media(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    circle = message.video_note
    error = None
    if circle is None:
        error = texts.VERIFY_NEED_CIRCLE
    elif message.forward_origin is not None:
        error = texts.VERIFY_FORWARDED
    elif (circle.duration or 0) < MIN_SECONDS:
        error = texts.VERIFY_TOO_SHORT
    elif (circle.duration or 0) > MAX_SECONDS:
        error = texts.VERIFY_TOO_LONG.format(seconds=MAX_SECONDS)
    if error:
        await _show_task(bot, chat_id, state, user, error)
        return

    record = await mod_repo.current_verification(user["id"])
    if record is None or not _fresh(record):
        # Кружок записан не под действующее задание — нужен новый
        await _show_task(bot, chat_id, state, user, texts.VERIFY_EXPIRED)
        return

    await mod_repo.attach_verification_media(record["id"], "video_note", circle.file_id)
    await users_repo.update_user(user["id"], verify_status="pending")
    await state.clear()
    await screen.show(bot, chat_id, state, texts.VERIFY_SENT,
                      None if user["verify_forced"] else kb.HOME_ONLY)

    fresh = await users_repo.get_user(user["id"])
    header = (
        "✅ <b>Заявка на верификацию</b>\n\n"
        f"Пользователь: <b>{profile_service.esc(fresh['name'] or fresh['tg_name'])}</b>\n"
        f"<code>{fresh['id']}</code> @{fresh['username'] or '—'}\n"
        f"Тип заявки: {'запрошена админом' if record['forced'] else 'по своей инициативе'}\n\n"
        f"{task_summary(record, await bot_name(bot))}"
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_video_note(admin_id, circle.file_id)
        except Exception:
            await safe_send(bot, admin_id, "Не удалось показать кружок заявки.")
        await safe_send(bot, admin_id, header, kb.NOTIFY_VERIFY)
