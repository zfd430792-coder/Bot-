"""Один экран вместо простыни сообщений.

Человеку нужен текущий экран, а не история нажатий: меню, анкета, лента
и шаги регистрации живут в одном месте. Бот помнит id сообщений
текущего экрана: следующий экран отправляется, а прежний удаляется. Нажатия
кнопок и ответы пользователя тоже удаляются — в личном чате боту это можно.

Управление — нижними кнопками, а их нельзя поменять правкой сообщения:
клавиатура приходит только с новым сообщением. Поэтому экран не правится,
а заменяется, причём сначала отправляется новый — вместе с ним приходит
нужная клавиатура, — и только потом удаляется старый.

Список сообщений экрана хранится отдельно от диалога (своя «destiny» в
хранилище FSM). Поэтому state.clear() — а его зовут на каждом /start и выходе
в меню — не заставляет бота забыть, что убирать.

Уведомления (совпадения, лайки, сообщения от администрации) через этот модуль
не идут: их, наоборот, нужно сохранить в переписке.
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

DESTINY = "screen"
MESSAGES = "messages"     # id сообщений текущего экрана

Markup = ReplyKeyboardMarkup | ReplyKeyboardRemove | None


def _ctx(state: FSMContext) -> FSMContext:
    return FSMContext(storage=state.storage, key=dc_replace(state.key, destiny=DESTINY))


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        # Уже удалено, старше 48 часов или недоступно — не повод падать
        pass


async def message_ids(state: FSMContext) -> list[int]:
    return list((await _ctx(state).get_data()).get(MESSAGES) or [])


async def remember(state: FSMContext, ids: Iterable[int]) -> None:
    await _ctx(state).update_data(**{MESSAGES: list(ids)})


async def add(state: FSMContext, ids: Sequence[int]) -> None:
    """Дописывает сообщения к текущему экрану (например, вопрос под анкетой)."""
    await remember(state, await message_ids(state) + list(ids))


async def forget(state: FSMContext, ids: Sequence[int]) -> None:
    """Сообщения удалены отдельно — вычёркиваем их из экрана."""
    drop_ids = set(ids)
    await remember(state, [i for i in await message_ids(state) if i not in drop_ids])


async def clear(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Убирает текущий экран целиком."""
    ids = await message_ids(state)
    await remember(state, [])
    for message_id in ids:
        await _delete(bot, chat_id, message_id)


async def replace(bot: Bot, chat_id: int, state: FSMContext,
                  new_ids: Sequence[int]) -> None:
    """Новый экран уже отправлен — запоминаем его и убираем прежний."""
    old = await message_ids(state)
    await remember(state, new_ids)
    keep = set(new_ids)
    for message_id in old:
        if message_id not in keep:
            await _delete(bot, chat_id, message_id)


async def send(bot: Bot, chat_id: int, state: FSMContext, text: str,
               markup: Markup = None) -> Message:
    """Показывает экран: одно сообщение со своей клавиатурой вместо прежнего."""
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    await replace(bot, chat_id, state, [message.message_id])
    return message


async def drop(message: Message) -> None:
    """Удаляет сообщение пользователя (нажатие кнопки, ответ), чтобы диалог
    не превращался в ленту."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def hide_reply_keyboard(bot: Bot, chat_id: int) -> None:
    """Снимает нижнюю клавиатуру, не оставляя следа в чате.

    Нужна перед правилами: у них inline-кнопка «Принимаю», а совмещать в одном
    сообщении снятие нижней клавиатуры и inline-кнопку Telegram не даёт.
    """
    try:
        message = await bot.send_message(chat_id, "⌛️",
                                         reply_markup=ReplyKeyboardRemove())
    except (TelegramBadRequest, TelegramForbiddenError):
        return
    await _delete(bot, chat_id, message.message_id)
