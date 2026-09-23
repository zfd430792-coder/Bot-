"""Один экран вместо простыни сообщений.

Человеку нужен текущий экран, а не история нажатий: меню, анкета, лента и
шаги регистрации живут в одном месте. Бот помнит id сообщений текущего
экрана и перед показом следующего либо правит его на месте, либо удаляет и
присылает новый. Ответы пользователя удаляются там же — в личном чате боту
это разрешено.

Управление — inline-кнопками, они живут в самом сообщении, поэтому экран
почти всегда правится на месте: ни мигания, ни новых сообщений. Заменять
целиком приходится там, где меняется тип сообщения (карточка с фото, реклама).

Список сообщений экрана хранится отдельно от диалога (своя «destiny» в
хранилище FSM). Поэтому state.clear() — а его зовут на каждом /start и выходе
в меню — больше не заставляет бота забыть, что убирать.

Уведомления (совпадения, лайки, сообщения от администрации) через этот модуль
не идут: их, наоборот, нужно сохранить в переписке.
"""
from __future__ import annotations

from dataclasses import replace as _replace_key
from typing import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

DESTINY = "screen"
MESSAGES = "messages"     # id сообщений текущего экрана
EDITABLE = "editable"     # id, если экран — одно текстовое сообщение
# Нижняя клавиатура точно убрана. Имя новое: прежний флаг «kb_clean» мог
# остаться в хранилище с тех времён, когда меню было inline, — а потом
# была версия на нижних кнопках, и клавиатура у человека снова есть
KB_CLEAN = "reply_kb_removed"

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | None


def _ctx(state: FSMContext) -> FSMContext:
    return FSMContext(storage=state.storage, key=_replace_key(state.key, destiny=DESTINY))


async def _data(state: FSMContext) -> dict:
    return await _ctx(state).get_data()


async def _update(state: FSMContext, **values) -> None:
    await _ctx(state).update_data(**values)


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        # Уже удалено, старше 48 часов или недоступно — не повод падать
        pass


async def message_ids(state: FSMContext) -> list[int]:
    return list((await _data(state)).get(MESSAGES) or [])


async def remember(state: FSMContext, ids: Iterable[int], *,
                   editable: int | None = None) -> None:
    await _update(state, **{MESSAGES: list(ids), EDITABLE: editable})


async def add(state: FSMContext, ids: Sequence[int]) -> None:
    """Дописывает сообщения к текущему экрану (например, вопрос под анкетой)."""
    current = await message_ids(state)
    await _update(state, **{MESSAGES: current + list(ids), EDITABLE: None})


async def forget(state: FSMContext, ids: Sequence[int]) -> None:
    """Сообщения удалены отдельно — вычёркиваем их из экрана."""
    drop_ids = set(ids)
    current = [i for i in await message_ids(state) if i not in drop_ids]
    await _update(state, **{MESSAGES: current})


async def clear(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Убирает текущий экран."""
    ids = await message_ids(state)
    await _update(state, **{MESSAGES: [], EDITABLE: None})
    for message_id in ids:
        await _delete(bot, chat_id, message_id)


async def adopt(bot: Bot, chat_id: int, state: FSMContext, message: Message) -> None:
    """Кнопку нажали на сообщении, которое бот экраном не считает.

    Так бывает со старым сообщением выше по чату или когда хранилище забыло
    экран. Нажатое сообщение становится экраном, прежний убирается — на
    экране снова ровно одно сообщение.
    """
    ids = await message_ids(state)
    if message.message_id in ids:
        return
    for message_id in ids:
        await _delete(bot, chat_id, message_id)
    editable = message.message_id if message.text is not None else None
    await remember(state, [message.message_id], editable=editable)


async def assume_clean_keyboard(state: FSMContext) -> None:
    """Нижней клавиатуры у человека точно нет — например, он у нас впервые.
    Тогда незачем мигать служебным сообщением, снимая её."""
    await _update(state, **{KB_CLEAN: True})


async def is_fresh(state: FSMContext) -> bool:
    """Бот ещё ни разу не показывал человеку экран."""
    return not await _data(state)


async def _clean_keyboard(bot: Bot, chat_id: int, state: FSMContext,
                          markup: Markup) -> None:
    """Следит, чтобы под полем ввода не висела нижняя клавиатура.

    Она остаётся только у запроса геопозиции — и у тех, кто застал версию,
    где всё управление было нижними кнопками. Убрать её можно лишь отдельным
    сообщением, поэтому делаем это один раз и запоминаем.
    """
    if isinstance(markup, ReplyKeyboardMarkup):
        await _update(state, **{KB_CLEAN: False})
        return
    if not (await _data(state)).get(KB_CLEAN):
        await hide_reply_keyboard(bot, chat_id)
        await _update(state, **{KB_CLEAN: True})


async def prepare(bot: Bot, chat_id: int, state: FSMContext,
                  markup: Markup = None) -> None:
    """Освобождает место под экран, который вызывающий отправит сам
    (карточку с фото, рекламу) и затем передаст в remember()."""
    await clear(bot, chat_id, state)
    await _clean_keyboard(bot, chat_id, state, markup)


async def send(bot: Bot, chat_id: int, state: FSMContext, text: str,
               markup: Markup = None) -> Message:
    """Заменяет экран новым сообщением."""
    await prepare(bot, chat_id, state, markup)
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    editable = None if isinstance(markup, ReplyKeyboardMarkup) else message.message_id
    await remember(state, [message.message_id], editable=editable)
    return message


async def show(bot: Bot, chat_id: int, state: FSMContext, text: str,
               markup: Markup = None) -> int:
    """Показывает текстовый экран: правит текущий на месте, если это возможно.

    Править можно, когда экран — одно текстовое сообщение. Карточка с фото,
    реклама или нижняя клавиатура так не заменяются — тогда присылаем новое.
    Возвращает id сообщения, на котором теперь экран.
    """
    if isinstance(markup, ReplyKeyboardMarkup):
        return (await send(bot, chat_id, state, text, markup)).message_id
    data = await _data(state)
    editable = data.get(EDITABLE)
    if editable and list(data.get(MESSAGES) or []) == [editable]:
        await _clean_keyboard(bot, chat_id, state, markup)
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=editable,
                                        reply_markup=markup)
            return int(editable)
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return int(editable)
            # Сообщение удалили или его уже нельзя править — пришлём новое
    return (await send(bot, chat_id, state, text, markup)).message_id


async def drop(message: Message) -> None:
    """Удаляет сообщение пользователя, чтобы диалог не превращался в ленту."""
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
    except (TelegramBadRequest, TelegramForbiddenError):
        return
    await _delete(bot, chat_id, message.message_id)


def chat_id(call) -> int:
    """Чат, где нажали inline-кнопку. В личке он совпадает с id человека —
    подстраховка на случай, когда сообщение недоступно (слишком старое)."""
    message = getattr(call, "message", None)
    return message.chat.id if message is not None else call.from_user.id
