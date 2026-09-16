"""Лента анкет: просмотр, лайки, лимиты и взаимные симпатии."""
from __future__ import annotations

import logging
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.db.database import db, haversine
from app.handlers import menu as menu_handlers
from app.handlers.registration import LINK_RE
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import ads as ads_service
from app.services import antifraud, profile
from app.services.notify import safe_send
from app.states import Browsing

log = logging.getLogger(__name__)
router = Router(name="browse")


async def _likes_limit(settings: Settings) -> int:
    """Лимит можно менять на лету из админ-панели."""
    return await mod_repo.get_int_setting("likes_limit", settings.likes_limit_per_day)


def _with_distance(row: Mapping[str, Any], viewer: Mapping[str, Any]) -> dict:
    data = dict(row)
    data["distance"] = haversine(viewer["lat"], viewer["lon"], row["lat"], row["lon"])
    return data


async def show_next(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], settings: Settings) -> None:
    """Показывает следующую анкету, убрав предыдущую карточку."""
    data = await state.get_data()
    await profile.delete_messages(bot, chat_id, data.get("card_msgs") or [])
    await state.update_data(card_msgs=[])

    mode = data.get("feed_mode", "search")
    queue: list[int] = list(data.get("feed") or [])

    if not queue:
        rows = (await users_repo.incoming_likes(user["id"]) if mode == "likes"
                else await users_repo.search_candidates(user))
        queue = [int(r["id"]) for r in rows]

    fresh_viewer = await users_repo.get_user(user["id"])
    while queue:
        target_id = queue.pop(0)
        target = await users_repo.get_user(target_id)
        if target is None or target["is_banned"] or not target["is_active"] \
                or not target["registered"] or target["verify_forced"]:
            continue
        if await reactions_repo.has_reacted(user["id"], target_id):
            continue

        # Реклама идёт перед анкетой и убирается вместе с ней
        ads_seen = int(data.get("ads_seen", 0)) + 1
        ad_messages, ads_seen = await ads_service.maybe_send(bot, chat_id, ads_seen)

        left = await users_repo.likes_left(fresh_viewer, await _likes_limit(settings))
        card = _with_distance(target, fresh_viewer)
        note = (await reactions_repo.get_note(target_id, user["id"])
                if mode == "likes" else None)
        message_ids = await profile.send_card(
            bot, chat_id, card, markup=kb.browse(target_id, left),
            viewer=fresh_viewer, note=note,
        )
        await state.set_state(Browsing.feed if mode == "search" else Browsing.likes_inbox)
        await state.update_data(feed=queue, card_msgs=ad_messages + message_ids,
                                current=target_id, feed_mode=mode, ads_seen=ads_seen)
        return

    # Анкеты кончились — возвращаем человека в меню одним сообщением
    await state.update_data(feed=[], card_msgs=[], current=None)
    await state.set_state(None)
    text = texts.NO_INCOMING_LIKES if mode == "likes" else texts.NO_PROFILES
    await menu_handlers.send_main_menu(
        bot, chat_id, fresh_viewer, settings.is_admin(user["id"]), text=text
    )


# ─────────────────────────── Входные точки ──────────────────────────────────

async def _require_profile(message: Message, user: Mapping[str, Any]) -> bool:
    if not user["registered"]:
        await message.answer("Сначала заполните анкету — команда /start")
        return False
    if not user["is_active"]:
        await message.answer(
            "🙈 Ваша анкета скрыта из поиска, поэтому смотреть чужие нельзя.\n"
            "Включите показ в разделе «Моя анкета»."
        )
        return False
    return True


@router.message(Command("search"))
@router.message(F.text == rkb.BTN_SEARCH)
async def start_feed(message: Message, state: FSMContext, bot: Bot,
                     user: Mapping[str, Any], settings: Settings) -> None:
    if not await _require_profile(message, user):
        return
    await state.update_data(feed=[], feed_mode="search", card_msgs=[])
    await show_next(bot, message.chat.id, state, user, settings)


@router.message(F.text.startswith(rkb.BTN_LIKES))
async def start_likes_inbox(message: Message, state: FSMContext, bot: Bot,
                            user: Mapping[str, Any], settings: Settings) -> None:
    if not await _require_profile(message, user):
        return
    count = await users_repo.count_incoming_likes(user["id"])
    if not count:
        await message.answer(texts.NO_INCOMING_LIKES)
        return
    await message.answer(
        f"❤️ Вас лайкнули: <b>{count}</b>\n"
        "Показываю их анкеты — ответьте взаимностью, и бот даст контакты."
    )
    await state.update_data(feed=[], feed_mode="likes", card_msgs=[])
    await show_next(bot, message.chat.id, state, user, settings)


@router.message(F.text == rkb.BTN_MATCHES)
async def show_matches(message: Message, user: Mapping[str, Any]) -> None:
    rows = await users_repo.get_matches(user["id"])
    if not rows:
        await message.answer(
            "💬 Совпадений пока нет.\n\nСтавьте ❤️ — чем больше анкет посмотрите, "
            "тем выше шанс взаимности."
        )
        return
    lines = ["💬 <b>Ваши совпадения</b>\n"]
    for row in rows:
        link = f"@{row['username']}" if row["username"] else "профиль скрыт"
        verified = " ☑️" if row["verify_status"] == "verified" else ""
        lines.append(
            f"{profile.GENDER_EMOJI.get(row['gender'], '•')} "
            f"<b>{profile.esc(row['name'])}</b>, {row['age']}{verified} — {link}"
        )
    await message.answer("\n".join(lines))


# ───────────────────────────── Реакции ──────────────────────────────────────

@router.callback_query(F.data.startswith("br:like:"))
async def like(call: CallbackQuery, state: FSMContext, bot: Bot,
               user: Mapping[str, Any], settings: Settings) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    limit = await _likes_limit(settings)

    if not await users_repo.consume_like(user["id"], limit):
        await call.answer("Лимит лайков на сегодня исчерпан", show_alert=True)
        await call.message.answer(texts.LIKE_LIMIT_REACHED.format(limit=limit, hours=24))
        return

    matched = await reactions_repo.add_reaction(user["id"], target_id, "like")
    await call.answer(texts.LIKE_SENT if not matched else "🎉 Взаимно!")

    # Накрутка лайков: слишком быстро или вообще без пропусков
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return

    if matched:
        await _announce_match(bot, user, target_id)
    else:
        await _notify_like(bot, user["id"], target_id, None)

    await show_next(bot, call.message.chat.id, state, user, settings)


# ───────────────────── Лайк с сообщением ────────────────────────────────────

@router.callback_query(F.data.startswith("br:note:"))
async def ask_note(call: CallbackQuery, state: FSMContext, user: Mapping[str, Any],
                   settings: Settings) -> None:
    """Сначала убеждаемся, что лайк вообще возможен — иначе текст писался зря."""
    target_id = int((call.data or "0").split(":")[-1])
    limit = await _likes_limit(settings)
    fresh = await users_repo.get_user(user["id"])
    if await users_repo.likes_left(fresh, limit) <= 0:
        await call.answer("Лимит лайков на сегодня исчерпан", show_alert=True)
        await call.message.answer(texts.LIKE_LIMIT_REACHED.format(limit=limit, hours=24))
        return

    await state.set_state(Browsing.note)
    await state.update_data(note_target=target_id)
    await call.answer()
    await call.message.answer(
        texts.LIKE_NOTE_ASK.format(max_len=settings.note_max_len),
        reply_markup=kb.NOTE_CANCEL,
    )


@router.callback_query(F.data == "br:note_cancel", Browsing.note)
async def cancel_note(call: CallbackQuery, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    await state.set_state(Browsing.feed)
    await call.answer(texts.CANCELLED)
    try:
        await call.message.delete()
    except Exception:
        pass
    await show_next(bot, call.message.chat.id, state, user, settings)


@router.message(Browsing.note, F.text)
async def send_note(message: Message, state: FSMContext, bot: Bot,
                    user: Mapping[str, Any], settings: Settings) -> None:
    note = (message.text or "").strip()
    if not note:
        await message.answer(texts.LIKE_NOTE_EMPTY)
        return
    if len(note) > settings.note_max_len:
        await message.answer(texts.LIKE_NOTE_LONG.format(max_len=settings.note_max_len))
        return
    if LINK_RE.search(note):
        await message.answer(texts.LIKE_NOTE_LINKS)
        return

    data = await state.get_data()
    target_id = int(data.get("note_target") or 0)
    if not target_id:
        await state.set_state(Browsing.feed)
        await show_next(bot, message.chat.id, state, user, settings)
        return

    limit = await _likes_limit(settings)
    if not await users_repo.consume_like(user["id"], limit):
        await state.set_state(Browsing.feed)
        await message.answer(texts.LIKE_LIMIT_REACHED.format(limit=limit, hours=24))
        return

    matched = await reactions_repo.add_reaction(user["id"], target_id, "like", note)
    await message.answer(texts.LIKE_NOTE_SENT)
    await state.set_state(Browsing.feed)

    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return

    if matched:
        await _announce_match(bot, user, target_id)
    else:
        await _notify_like(bot, user["id"], target_id, note)

    await show_next(bot, message.chat.id, state, user, settings)


@router.message(Browsing.note)
async def note_hint(message: Message, settings: Settings) -> None:
    await message.answer(
        texts.LIKE_NOTE_ASK.format(max_len=settings.note_max_len),
        reply_markup=kb.NOTE_CANCEL,
    )


# ───────────── Ответ на уведомление «вы кому-то понравились» ────────────────

@router.callback_query(F.data.startswith("ans:like:"))
async def answer_like(call: CallbackQuery, bot: Bot, user: Mapping[str, Any],
                      settings: Settings) -> None:
    sender_id = int((call.data or "0").split(":")[-1])
    limit = await _likes_limit(settings)
    if not await users_repo.consume_like(user["id"], limit):
        await call.answer("Лимит лайков на сегодня исчерпан", show_alert=True)
        return

    matched = await reactions_repo.add_reaction(user["id"], sender_id, "like")
    await call.answer("❤️ Взаимно!" if matched else texts.LIKE_SENT)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if matched:
        await _announce_match(bot, user, sender_id)
    else:
        await _notify_like(bot, user["id"], sender_id, None)


@router.callback_query(F.data.startswith("ans:skip:"))
async def answer_skip(call: CallbackQuery, user: Mapping[str, Any]) -> None:
    sender_id = int((call.data or "0").split(":")[-1])
    await reactions_repo.add_reaction(user["id"], sender_id, "dislike")
    await call.answer(texts.DISLIKE_SENT)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("br:dislike:"))
async def dislike(call: CallbackQuery, state: FSMContext, bot: Bot,
                  user: Mapping[str, Any], settings: Settings) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await reactions_repo.add_reaction(user["id"], target_id, "dislike")
    await call.answer()
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return
    await show_next(bot, call.message.chat.id, state, user, settings)


@router.callback_query(F.data == "remind:search")
async def from_reminder(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    """Переход в ленту прямо из напоминания."""
    await call.answer()
    if not user["registered"]:
        await call.message.answer("Сначала заполните анкету — /start")
        return
    if not user["is_active"]:
        await users_repo.update_user(user["id"], is_active=1)
        user = await users_repo.get_user(user["id"])
    await state.update_data(feed=[], feed_mode="search", card_msgs=[])
    await show_next(bot, call.message.chat.id, state, user, settings)


@router.callback_query(F.data == "br:next")
async def next_profile(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    await show_next(bot, call.message.chat.id, state, user, settings)


@router.callback_query(F.data == "br:stop")
async def stop_feed(call: CallbackQuery, state: FSMContext, bot: Bot,
                    user: Mapping[str, Any], is_admin: bool) -> None:
    data = await state.get_data()
    await profile.delete_messages(bot, call.message.chat.id, data.get("card_msgs") or [])
    await state.clear()
    await call.answer()
    await menu_handlers.show_main_menu(call.message, user, is_admin)


# ───────────────────── Уведомления о симпатиях ──────────────────────────────

async def _announce_match(bot: Bot, user: Mapping[str, Any], target_id: int) -> None:
    """Обоим участникам — контакт друг друга."""
    me = await users_repo.get_user(user["id"])
    target = await users_repo.get_user(target_id)
    if target is None or me is None:
        return

    if target["username"]:
        await safe_send(bot, me["id"], texts.MATCH_TEXT.format(username=target["username"]))
    if me["username"]:
        await safe_send(bot, target_id, texts.MATCH_TEXT.format(username=me["username"]))
        try:
            await profile.send_card(bot, target_id, dict(me), show_distance=False)
        except Exception as exc:
            log.warning("Не удалось показать анкету при совпадении: %s", exc)


async def _notify_like(bot: Bot, sender_id: int, target_id: int,
                       note: str | None) -> None:
    """Сообщаем о симпатии.

    Лайк с сообщением показываем сразу и целиком — анкета плюс текст, чтобы
    человек мог ответить не уходя из чата. Обычный лайк — короткий сигнал и
    только один раз, пока предыдущие не разобраны, иначе это превратится
    в поток уведомлений.
    """
    if note:
        sender = await users_repo.get_user(sender_id)
        if sender is None:
            return
        await safe_send(bot, target_id, texts.NEW_LIKE_WITH_NOTE)
        try:
            await profile.send_card(
                bot, target_id, dict(sender), note=note, show_distance=False,
                markup=kb.answer_like(sender_id),
            )
        except Exception as exc:
            log.warning("Не удалось показать анкету с сообщением: %s", exc)
        return

    pending = await db.fetchval(
        "SELECT COUNT(*) FROM reactions WHERE to_id = ? AND kind = 'like' AND is_seen = 0",
        (target_id,), default=0,
    )
    if int(pending or 0) != 1:
        return
    await safe_send(bot, target_id, texts.NEW_LIKE_NOTIFY)
