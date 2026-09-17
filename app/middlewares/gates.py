"""Проверки доступа перед любым действием: бан, username, верификация.

Порядок важен: сначала бан (жёстче всего), затем требование username, затем
принудительная верификация. Админов проверки не касаются — иначе владелец
бота может сам себя запереть.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import texts
from app.keyboards import inline as kb
from app.states import Verification

# Что пропускаем мимо проверок (иначе из блокировки не выбраться)
ALLOWED_CALLBACKS = ("onb:username", "ver:", "adm:")


async def _reply(event: TelegramObject, text: str, markup=None) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message is not None:
            await event.message.answer(text, reply_markup=markup)
    elif isinstance(event, Message):
        await event.answer(text, reply_markup=markup)


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

        callback_data = event.data or "" if isinstance(event, CallbackQuery) else ""

        # 1. Бан — разговор окончен
        if user["is_banned"]:
            until = ""
            if user["banned_until"]:
                until = f"\n<b>Действует до:</b> {user['banned_until']} (UTC)"
            await _reply(event, texts.BANNED.format(
                reason=user["ban_reason"] or "нарушение правил", until=until))
            return None

        # 2. Без username знакомство не состоится — писать друг другу нечем
        if not user["username"]:
            if callback_data == "onb:username":
                return await handler(event, data)
            await _reply(event, texts.NEED_USERNAME, kb.CHECK_USERNAME)
            return None

        # 3. Капчу сбросила антинакрутка — пока не пройдена, дальше не пускаем
        if user["registered"] and not user["captcha_passed"]:
            if callback_data.startswith("cap:"):
                return await handler(event, data)
            if isinstance(event, Message) and (event.text or "").startswith("/start"):
                return await handler(event, data)
            await _reply(event, texts.CAPTCHA_RECHECK)
            return None

        # 4. Принудительная верификация: бот закрыт, пока админ не подтвердит
        if user["verify_forced"] and user["verify_status"] != "verified":
            if callback_data.startswith(ALLOWED_CALLBACKS):
                return await handler(event, data)

            state: FSMContext | None = data.get("state")
            current = await state.get_state() if state else None
            if current == Verification.waiting_media.state and isinstance(event, Message):
                return await handler(event, data)

            if user["verify_status"] == "pending":
                await _reply(event, texts.VERIFY_PENDING)
            else:
                await _reply(
                    event,
                    texts.VERIFY_REQUIRED.format(code=user["verify_code"] or "—"),
                    kb.VERIFY_START,
                )
            return None

        return await handler(event, data)
