"""Проверки доступа перед любым действием: бан, username, верификация.

Порядок важен: сначала бан (жёстче всего), затем требование username, затем
принудительная верификация. Админов проверки не касаются — иначе владелец
бота может сам себя запереть.

Ответ проверки — это тоже экран: повторные нажатия не плодят в чате копии
«нужен username» или «доступ заблокирован».
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import texts
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import screen
from app.services.notify import appeal_contact
from app.states import Onboarding, Verification

# Нажатия, которые пропускаем мимо проверок — иначе из блокировки не выбраться
USERNAME_PASS = {"onb:username", rkb.L_USERNAME_DONE}
CAPTCHA_PASS = {"m:start"}
VERIFY_PASS = {"ver:send", "ver:cancel", "📸 Отправить фото с кодом", rkb.CANCEL}


async def _reply(event: TelegramObject, data: dict[str, Any], text: str,
                 markup=None) -> None:
    state: FSMContext | None = data.get("state")
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
    else:
        message = event
        await screen.drop(event)
    if not isinstance(message, Message):
        return
    if state is not None:
        await screen.show(message.bot, message.chat.id, state, text, markup)
    else:
        await message.bot.send_message(message.chat.id, text, reply_markup=markup)


class AccessGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Владелец бота не подпадает ни под одну проверку — иначе он рискует
        # запереть сам себя. Модератор в этом смысле обычный пользователь:
        # ему тоже нужен username, и забаненным он модерировать не может.
        user = data.get("user")
        if user is None or data.get("is_admin"):
            return await handler(event, data)

        # Что нажато: текст сообщения или данные inline-кнопки
        if isinstance(event, Message):
            action = event.text or ""
        elif isinstance(event, CallbackQuery):
            action = event.data or ""
        else:
            action = ""
        state: FSMContext | None = data.get("state")
        current = await state.get_state() if state else None

        # 1. Бан — разговор окончен
        if user["is_banned"]:
            until = ""
            if user["banned_until"]:
                until = f"\n<b>Действует до:</b> {user['banned_until']} (UTC)"
            await _reply(event, data, texts.BANNED.format(
                reason=user["ban_reason"] or "нарушение правил", until=until,
                contact=await appeal_contact()))
            return None

        # 2. Без username знакомство не состоится — писать друг другу нечем
        if not user["username"]:
            if action in USERNAME_PASS:
                return await handler(event, data)
            await _reply(event, data, texts.NEED_USERNAME, kb.USERNAME_CHECK)
            return None

        # 3. Капчу сбросила антинакрутка — пока не пройдена, дальше не пускаем
        if user["registered"] and not user["captcha_passed"]:
            if (action.startswith("/start") or action in CAPTCHA_PASS
                    or current == Onboarding.captcha.state):
                return await handler(event, data)
            await _reply(event, data, texts.CAPTCHA_RECHECK, kb.RETRY)
            return None

        # 4. Принудительная верификация: бот закрыт, пока админ не подтвердит
        if user["verify_forced"] and user["verify_status"] != "verified":
            if action in VERIFY_PASS or current == Verification.waiting_media.state:
                return await handler(event, data)
            if user["verify_status"] == "pending":
                await _reply(event, data, texts.VERIFY_PENDING)
            else:
                await _reply(event, data, texts.VERIFY_REQUIRED, kb.VERIFY_REQUIRED)
            return None

        return await handler(event, data)
