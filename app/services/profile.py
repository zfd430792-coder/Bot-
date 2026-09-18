"""Отрисовка и отправка карточек анкет."""
from __future__ import annotations

import html
import logging
from typing import Any, Mapping, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.db.database import norm_text
from app.services import geo
from app.texts import VERIFY_BADGE

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024      # подпись к фото и видео в Telegram
GENDER_EMOJI = {"m": "👨", "f": "👩"}
GENDER_WORD = {"m": "парень", "f": "девушка"}
LOOKING_WORD = {"m": "парней", "f": "девушек", "any": "всех"}


def years(age: int | None) -> str:
    """1 год / 22 года / 15 лет — без этого текст выглядит машинным."""
    if age is None:
        return ""
    if 11 <= age % 100 <= 14:
        return f"{age} лет"
    last = age % 10
    if last == 1:
        return f"{age} год"
    if last in (2, 3, 4):
        return f"{age} года"
    return f"{age} лет"


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _distance_line(user: Mapping[str, Any], viewer: Mapping[str, Any]) -> str:
    """Расстояние показываем, когда оно что-то говорит.

    Ищет «рядом» — всегда. Иначе — только если человек из другого места:
    лента доходит и до соседних городов, и «~70 км от вас» объясняет, почему
    анкета здесь. Для своего города расстояние между центрами — ноль, его
    не пишем.
    """
    km = user["distance"] if "distance" in user.keys() else None
    if km is None:
        return ""
    same_place = norm_text(user["city"]) == norm_text(viewer["city"])
    if viewer["search_scope"] == "near" or (not same_place and km >= 5):
        return f"\n🚶 {geo.distance_text(km)}"
    return ""


def render_card(user: Mapping[str, Any], *, viewer: Mapping[str, Any] | None = None,
                show_distance: bool = True, admin_view: bool = False,
                note: str | None = None, header: str = "",
                about_limit: int | None = None) -> str:
    """Текст карточки. Координаты не раскрываются — только расстояние."""
    verified = f" {VERIFY_BADGE}" if user["verify_status"] == "verified" else ""
    gender = GENDER_EMOJI.get(user["gender"], "")
    head = f"{gender} <b>{esc(user['name'])}</b>, {years(user['age'])}{verified}"

    place = ""
    if user["city"]:
        place = f"\n📍 {esc(user['city'])}"
        # Без учёта регистра: прежняя версия могла записать «Самарская Область»
        if user["region"] and norm_text(user["region"]) != norm_text(user["city"]):
            place += f", {esc(user['region'])}"

    distance = ""
    if show_distance and viewer is not None:
        distance = _distance_line(user, viewer)

    about_text = user["about"] or ""
    if about_limit is not None and len(about_text) > about_limit:
        about_text = about_text[:max(0, about_limit - 1)].rstrip() + "…"
    about = f"\n\n{esc(about_text)}" if about_text else ""

    card = head + place + distance + about
    if header:
        card = f"{header}\n\n{card}"

    # Сообщение, приложенное к лайку, — главное в карточке, выделяем его
    if note is None and "like_note" in user.keys():
        note = user["like_note"]
    if note:
        card += f"\n\n💌 <b>Сообщение для вас:</b>\n<i>{esc(note)}</i>"

    if admin_view:
        username = f"@{user['username']}" if user["username"] else "—"
        card += (
            f"\n\n<code>ID: {user['id']}</code>\n"
            f"Username: {username}\n"
            f"Статус: {'🚫 бан' if user['is_banned'] else '🟢 активен'}"
            f" · {'👀 в поиске' if user['is_active'] else '🙈 скрыт'}\n"
            f"Верификация: {user['verify_status']}\n"
            f"Жалоб: {user['reports_count']} · Лайков получено: {user['likes_received']}\n"
            f"Регистрация: {user['created_at']}\nБыл(а): {user['last_active']}"
        )
    return card


async def send_card(bot: Bot, chat_id: int, user: Mapping[str, Any], *,
                    markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
                    viewer: Mapping[str, Any] | None = None,
                    show_distance: bool = True,
                    admin_view: bool = False,
                    note: str | None = None,
                    header: str = "") -> list[int]:
    """Отправляет анкету. Возвращает id сообщений (их потом нужно удалить)."""
    options = dict(viewer=viewer, show_distance=show_distance,
                   admin_view=admin_view, note=note, header=header)
    caption = render_card(user, **options)
    media_type, media_id = user["media_type"], user["media_id"]
    # Подпись к фото короче обычного сообщения: лишнее срезаем с «о себе»,
    # иначе Telegram отверг бы анкету целиком
    overflow = len(caption) - CAPTION_LIMIT
    if media_type in ("photo", "video") and overflow > 0:
        about_len = len(user["about"] or "")
        caption = render_card(user, about_limit=max(0, about_len - overflow - 1),
                              **options)
    sent: list[Message] = []

    try:
        if media_type == "photo" and media_id:
            sent.append(await bot.send_photo(chat_id, media_id, caption=caption,
                                             reply_markup=markup))
        elif media_type == "video" and media_id:
            sent.append(await bot.send_video(chat_id, media_id, caption=caption,
                                             reply_markup=markup))
        elif media_type == "video_note" and media_id:
            # У кружка не бывает подписи — шлём двумя сообщениями
            sent.append(await bot.send_video_note(chat_id, media_id))
            sent.append(await bot.send_message(chat_id, caption, reply_markup=markup))
        else:
            sent.append(await bot.send_message(chat_id, caption, reply_markup=markup))
    except TelegramBadRequest as exc:
        # Файл мог «протухнуть» — не теряем пользователя, показываем текст
        log.warning("Не удалось отправить медиа анкеты %s: %s", user["id"], exc)
        sent.append(await bot.send_message(chat_id, caption, reply_markup=markup))

    return [m.message_id for m in sent]


async def delete_messages(bot: Bot, chat_id: int, message_ids: Sequence[int]) -> None:
    for message_id in message_ids or ():
        try:
            await bot.delete_message(chat_id, message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # сообщение уже удалено или старше 48 часов — не страшно


def extract_media(message: Message, max_seconds: int) -> tuple[str, str] | str:
    """Достаёт фото/видео из сообщения.

    Возвращает (тип, file_id) либо строку с кодом ошибки: 'long' | 'file' | 'bad'.
    """
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        if (message.video.duration or 0) > max_seconds:
            return "long"
        return "video", message.video.file_id
    if message.video_note:
        if (message.video_note.duration or 0) > max_seconds:
            return "long"
        return "video_note", message.video_note.file_id
    if message.document and (message.document.mime_type or "").startswith(("image/", "video/")):
        return "file"
    return "bad"
