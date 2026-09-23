"""Поддельная сессия Telegram для прогона сценариев без реального бота.

Ничего не отправляет наружу: запоминает вызовы API и возвращает правдоподобные
ответы. Благодаря этому весь путь пользователя можно проверить локально.
"""
from __future__ import annotations

import datetime as dt
import itertools
from typing import Any, AsyncGenerator

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    Chat, InlineKeyboardMarkup, Message, MessageId, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, Update, User,
)

_ids = itertools.count(1000)


class FakeSession(BaseSession):
    """Сохраняет все вызовы в self.calls и отвечает заглушками."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        # Что сейчас видно в каждом чате: отправленное ботом минус удалённое.
        # clear() это не сбрасывает — так видно, убирает ли бот за собой.
        self.alive: dict[int, set[int]] = {}
        # Нижняя клавиатура каждого чата: надписи кнопок или None, если снята
        self.keyboards: dict[int, list[str] | None] = {}
        # Все inline-кнопки, какие бот когда-либо показывал: (callback_data, url)
        self.inline_buttons: list[tuple[str | None, str | None]] = []
        # Inline-кнопки последнего показанного или исправленного сообщения в чате:
        # [(надпись, callback_data)]. Сообщение без кнопок — пустой список
        self.inline: dict[int, list[tuple[str, str | None]]] = {}
        # Все надписи нижних клавиатур за прогон — clear() их не сбрасывает
        self.reply_buttons: set[str] = set()
        # Кнопки каждого сообщения: (чат, id) -> [(надпись, data)]
        self.by_message: dict[tuple[int, int], list[tuple[str, str | None]]] = {}
        # Отправленные сообщения: (метод, id) — чтобы нажать кнопку именно на нём
        self.sent: list[tuple[TelegramMethod, int]] = []

    async def close(self) -> None:
        return None

    def visible(self, chat_id: int) -> int:
        """Сколько сообщений бота сейчас видно в чате."""
        return len(self.alive.get(chat_id, set()))

    def keyboard(self, chat_id: int) -> list[str]:
        """Надписи нижней клавиатуры, которая сейчас у человека."""
        return list(self.keyboards.get(chat_id) or [])

    def buttons(self, chat_id: int) -> list[tuple[str, str | None]]:
        """Inline-кнопки последнего сообщения бота в чате: (надпись, data)."""
        return list(self.inline.get(chat_id) or [])

    def _track_markup(self, method: TelegramMethod) -> None:
        markup = getattr(method, "reply_markup", None)
        chat_id = getattr(method, "chat_id", None)
        name = type(method).__name__
        if isinstance(markup, ReplyKeyboardMarkup) and chat_id is not None:
            self.keyboards[int(chat_id)] = [b.text for row in markup.keyboard for b in row]
            self.reply_buttons.update(self.keyboards[int(chat_id)])
        elif isinstance(markup, ReplyKeyboardRemove) and chat_id is not None:
            self.keyboards[int(chat_id)] = None
        elif isinstance(markup, InlineKeyboardMarkup):
            self.inline_buttons += [(b.callback_data, b.url)
                                    for row in markup.inline_keyboard for b in row]
        # Служебное «⌛️» со снятием нижней клавиатуры — не экран
        if (chat_id is not None and name.startswith(("Send", "Edit", "CopyMessage"))
                and not isinstance(markup, ReplyKeyboardRemove)):
            self.inline[int(chat_id)] = self._inline_of(markup)
        # Правка сообщения заменяет и его кнопки (правка без кнопок — убирает их)
        message_id = getattr(method, "message_id", None)
        if name.startswith("Edit") and chat_id is not None and message_id is not None:
            self.by_message[(int(chat_id), int(message_id))] = self._inline_of(markup)

    @staticmethod
    def _inline_of(markup) -> list[tuple[str, str | None]]:
        if isinstance(markup, InlineKeyboardMarkup):
            return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        return []

    async def stream_content(self, url: str, headers=None, timeout: int = 30,
                             chunk_size: int = 65536,
                             raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod,
                           timeout: int | None = None) -> Any:
        self.calls.append(method)
        self._track_markup(method)
        name = type(method).__name__

        if name == "GetMe":
            return User(id=bot.id, is_bot=True, first_name="TestBot", username="test_bot")
        if name == "CopyMessage":
            message_id = next(_ids)
            self.alive.setdefault(int(method.chat_id), set()).add(message_id)
            self.by_message[(int(method.chat_id), message_id)] = self._inline_of(
                method.reply_markup)
            return MessageId(message_id=message_id)
        if name == "DeleteMessage":
            self.alive.get(int(method.chat_id), set()).discard(method.message_id)
            return True
        if name.startswith("Send") and name != "SendChatAction":
            chat_id = int(getattr(method, "chat_id", 1))
            message_id = next(_ids)
            self.alive.setdefault(chat_id, set()).add(message_id)
            self.sent.append((method, message_id))
            self.by_message[(chat_id, message_id)] = self._inline_of(
                getattr(method, "reply_markup", None))
            return Message(
                message_id=message_id,
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=chat_id, type="private"),
            ).as_(bot)
        # DeleteMessage, AnswerCallbackQuery, EditMessage*, SetMyCommands и прочее
        return True

    # ── помощники для тестов ────────────────────────────────────────────────

    def method_names(self) -> list[str]:
        return [type(call).__name__ for call in self.calls]

    def last(self, name: str):
        for call in reversed(self.calls):
            if type(call).__name__ == name:
                return call
        return None

    def of_type(self, name: str) -> list:
        return [c for c in self.calls if type(c).__name__ == name]

    def texts(self) -> list[str]:
        out = []
        for call in self.calls:
            text = getattr(call, "text", None) or getattr(call, "caption", None)
            if text:
                out.append(text)
        return out

    def clear(self) -> None:
        self.calls.clear()


# ───────────────────── Конструкторы входящих апдейтов ───────────────────────

_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def _user(user_id: int, username: str | None, name: str) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": name, "username": username}


def message_update(bot: Bot, user_id: int, text: str, *,
                   username: str | None = "tester", name: str = "Тест") -> Update:
    raw = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "date": int(dt.datetime.now().timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id, username, name),
            "text": text,
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def photo_update(bot: Bot, user_id: int, *, username: str | None = "tester",
                 name: str = "Тест") -> Update:
    raw = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "date": int(dt.datetime.now().timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id, username, name),
            "photo": [{"file_id": f"photo-{user_id}", "file_unique_id": f"u{user_id}",
                       "width": 800, "height": 800, "file_size": 12345}],
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def video_update(bot: Bot, user_id: int, duration: int) -> Update:
    raw = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "date": int(dt.datetime.now().timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id, "tester", "Тест"),
            "video": {"file_id": f"video-{user_id}", "file_unique_id": f"v{user_id}",
                      "width": 480, "height": 480, "duration": duration},
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def location_update(bot: Bot, user_id: int, lat: float, lon: float) -> Update:
    raw = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "date": int(dt.datetime.now().timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id, "tester", "Тест"),
            "location": {"latitude": lat, "longitude": lon},
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def callback_update(bot: Bot, user_id: int, data: str, *,
                    username: str | None = "tester",
                    message_id: int | None = None) -> Update:
    """message_id — сообщение, на котором нажата кнопка (по умолчанию новое)."""
    raw = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)),
            "from": _user(user_id, username, "Тест"),
            "chat_instance": "test-instance",
            "data": data,
            "message": {
                "message_id": message_id or next(_message_ids),
                "date": int(dt.datetime.now().timestamp()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": bot.id, "is_bot": True, "first_name": "TestBot",
                         "username": "test_bot"},
                "text": "…",
            },
        },
    }
    return Update.model_validate(raw, context={"bot": bot})
