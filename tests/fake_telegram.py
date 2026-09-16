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
from aiogram.types import Chat, Message, MessageId, Update, User

_ids = itertools.count(1000)


class FakeSession(BaseSession):
    """Сохраняет все вызовы в self.calls и отвечает заглушками."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def close(self) -> None:
        return None

    async def stream_content(self, url: str, headers=None, timeout: int = 30,
                             chunk_size: int = 65536,
                             raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod,
                           timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = type(method).__name__

        if name == "GetMe":
            return User(id=bot.id, is_bot=True, first_name="TestBot", username="test_bot")
        if name == "CopyMessage":
            return MessageId(message_id=next(_ids))
        if name.startswith("Send"):
            chat_id = getattr(method, "chat_id", 1)
            return Message(
                message_id=next(_ids),
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=int(chat_id), type="private"),
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
                    username: str | None = "tester") -> Update:
    raw = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)),
            "from": _user(user_id, username, "Тест"),
            "chat_instance": "test-instance",
            "data": data,
            "message": {
                "message_id": next(_message_ids),
                "date": int(dt.datetime.now().timestamp()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": bot.id, "is_bot": True, "first_name": "TestBot",
                         "username": "test_bot"},
                "text": "…",
            },
        },
    }
    return Update.model_validate(raw, context={"bot": bot})
