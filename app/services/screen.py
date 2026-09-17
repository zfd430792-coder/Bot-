"""Один экран вместо простыни сообщений.

Пошаговые диалоги (анкета, настройки) не должны накапливать историю: человеку
нужен текущий вопрос, а не двадцать прошлых. Поэтому бот запоминает id своего
последнего сообщения и удаляет его перед отправкой следующего. Ответы
пользователя удаляются там же — в личном чате боту это разрешено.

Уведомления (совпадения, лайки, сообщения от администрации) через этот модуль
не идут: их, наоборот, нужно сохранить в переписке.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

KEY = "screen_messages"


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        # Уже удалено, старше 48 часов или недоступно — не повод падать
        pass


async def clear(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Убирает прошлый экран."""
    data = await state.get_data()
    message_ids: list[int] = list(data.get(KEY) or [])
    if not message_ids:
        return
    await state.update_data(**{KEY: []})
    for message_id in message_ids:
        await _delete(bot, chat_id, message_id)


async def remember(state: FSMContext, message_ids: Iterable[int]) -> None:
    await state.update_data(**{KEY: list(message_ids)})


async def add(state: FSMContext, message_ids: Sequence[int]) -> None:
    """Дописывает сообщения к текущему экрану (например, карточку анкеты)."""
    data = await state.get_data()
    await state.update_data(**{KEY: list(data.get(KEY) or []) + list(message_ids)})


async def send(bot: Bot, chat_id: int, state: FSMContext, text: str,
               markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None
               ) -> Message:
    """Заменяет экран новым сообщением."""
    await clear(bot, chat_id, state)
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    await remember(state, [message.message_id])
    return message


async def drop(message: Message) -> None:
    """Удаляет сообщение пользователя, чтобы шаги не превращались в ленту."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def hide_reply_keyboard(bot: Bot, chat_id: int) -> None:
    """Снимает нижнюю клавиатуру.

    Убрать её можно только сообщением, а совмещать в одном сообщении снятие
    нижней клавиатуры и inline-кнопки Telegram не даёт — поэтому отправляем
    пустышку и сразу удаляем её.
    """
    try:
        message = await bot.send_message(chat_id, "⌛️",
                                         reply_markup=ReplyKeyboardRemove())
    except TelegramBadRequest:
        return
    await _delete(bot, chat_id, message.message_id)
