"""Лента анкет: просмотр, лайки, лимиты и взаимные симпатии.

Карточка анкеты — это экран: следующая заменяет предыдущую, а вопросы по
ходу (сообщение к лайку, жалоба) появляются под ней и уходят вместе с ней.

Лента идёт от ближних к дальним, как в Дайвинчике (см. search_candidates):
когда в городе анкеты кончаются, строчка над карточкой говорит, что дальше
пойдут люди из области, а потом — из соседних городов.
"""
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
from app.services import antifraud, profile, screen
from app.services.notify import safe_send
from app.states import Browsing

log = logging.getLogger(__name__)
router = Router(name="browse")


async def _likes_limit(settings: Settings) -> int:
    """Лимит можно менять на лету из админ-панели."""
    return await mod_repo.get_int_setting("likes_limit", settings.likes_limit_per_day)


async def _like_is_free(user_id: int, target_id: int, settings: Settings) -> bool:
    """Лайк не тратит суточный лимит в двух случаях.

    1. Человека уже лайкнули — тогда его ❤️ это ответ на чужую симпатию,
       а не рассылка. Запрещать отвечать бессмысленно: он ничего не ищет,
       он решает, отвечать взаимностью или нет. Злоупотребить нельзя —
       бесплатно лайкнуть можно только того, кто лайкнул первым.
    2. Лайкает владелец бота: на админов ограничения не действуют.
    """
    if settings.is_admin(user_id):
        return True
    return await reactions_repo.liked_me(user_id, target_id)


def _with_distance(row: Mapping[str, Any], viewer: Mapping[str, Any]) -> dict:
    data = dict(row)
    data["distance"] = haversine(viewer["lat"], viewer["lon"], row["lat"], row["lon"])
    return data


def _step_notice(viewer: Mapping[str, Any], tier: int) -> str:
    """Строчка о том, что лента перешла к следующей ступени."""
    if viewer["search_scope"] == "near":
        return texts.FEED_OUT_OF_RADIUS.format(radius=viewer["search_radius"] or 50)
    if tier == users_repo.AREA_REGION and viewer["region"]:
        return texts.FEED_TO_REGION.format(region=profile.esc(viewer["region"]))
    return texts.FEED_TO_FAR


def _queue(raw: list) -> list[tuple[int, int]]:
    """Очередь хранит пары (id, ступень); старый формат — просто id."""
    return [(item, 0) if isinstance(item, int) else (int(item[0]), int(item[1]))
            for item in raw or []]


async def show_next(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], settings: Settings, *,
                    notice: str | None = None) -> None:
    """Показывает следующую анкету вместо предыдущей.

    notice — строка над карточкой: «сообщение отправлено», «жалоба принята».
    """
    data = await state.get_data()
    mode = data.get("feed_mode", "search")
    queue = _queue(data.get("feed"))

    if not queue:
        if mode == "likes":
            rows = await users_repo.incoming_likes(user["id"])
            queue = [(int(r["id"]), 0) for r in rows]
        else:
            rows = await users_repo.search_candidates(user)
            # Кто лайкнул нас — вне ступеней: он первый, где бы ни жил
            queue = [(int(r["id"]), 0 if r["liked_me"] else int(r["area_tier"]))
                     for r in rows]

    fresh_viewer = await users_repo.get_user(user["id"])
    tier_seen = int(data.get("tier_seen") or users_repo.AREA_LOCAL)
    while queue:
        target_id, tier = queue.pop(0)
        target = await users_repo.get_user(target_id)
        if target is None or target["is_banned"] or not target["is_active"] \
                or not target["registered"] or target["verify_forced"]:
            continue
        if await reactions_repo.has_reacted(user["id"], target_id):
            continue

        header = [notice] if notice else []
        if mode == "likes" and data.get("likes_intro"):
            header.append(texts.LIKES_INTRO.format(count=data["likes_intro"]))
        if mode == "search" and tier > tier_seen:
            header.append(_step_notice(fresh_viewer, tier))
            tier_seen = tier

        # Реклама идёт перед анкетой и убирается вместе с ней
        await screen.prepare(bot, chat_id, state)
        ads_seen = int(data.get("ads_seen", 0)) + 1
        ad_messages, ads_seen = await ads_service.maybe_send(bot, chat_id, ads_seen)

        free = await _like_is_free(user["id"], target_id, settings)
        left = (None if free
                else await users_repo.likes_left(fresh_viewer,
                                                 await _likes_limit(settings)))
        card = _with_distance(target, fresh_viewer)
        note = (await reactions_repo.get_note(target_id, user["id"])
                if mode == "likes" else None)
        message_ids = await profile.send_card(
            bot, chat_id, card, markup=kb.browse(target_id, left),
            viewer=fresh_viewer, note=note, header="\n".join(header),
        )
        await screen.remember(state, ad_messages + message_ids)
        await state.set_state(Browsing.feed if mode == "search" else Browsing.likes_inbox)
        await state.update_data(feed=[list(item) for item in queue], current=target_id,
                                feed_mode=mode, ads_seen=ads_seen, tier_seen=tier_seen,
                                likes_intro=None)
        return

    # Анкеты кончились совсем — экран с тем, что можно сделать дальше
    await state.update_data(feed=[], current=None)
    await state.set_state(None)
    lead = f"{notice}\n\n" if notice else ""
    if mode == "likes":
        await screen.show(bot, chat_id, state, lead + texts.LIKES_DONE, kb.LIKES_END)
        return

    skipped = await reactions_repo.count_dislikes(user["id"])
    hints = []
    if skipped:
        hints.append(texts.NO_PROFILES_SKIPPED.format(count=skipped))
    hints.append(texts.NO_PROFILES_AGE.format(age_min=fresh_viewer["age_min"] or 18,
                                              age_max=fresh_viewer["age_max"] or 99))
    await screen.show(bot, chat_id, state,
                      lead + texts.NO_PROFILES + "\n\n" + "\n".join(hints),
                      kb.feed_end(skipped))


# ─────────────────────────── Входные точки ──────────────────────────────────

async def _can_browse(bot: Bot, chat_id: int, state: FSMContext,
                      user: Mapping[str, Any]) -> bool:
    if not user["registered"]:
        await screen.show(bot, chat_id, state,
                          "Сначала заполните анкету — это пара минут.", kb.START_OVER)
        return False
    if not user["is_active"]:
        await screen.show(
            bot, chat_id, state,
            "🙈 Ваша анкета скрыта из поиска, поэтому смотреть чужие нельзя.\n"
            "Включите показ в разделе «Моя анкета».",
            kb.BACK_HOME,
        )
        return False
    return True


async def open_feed(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], settings: Settings, *,
                    notice: str | None = None) -> None:
    if not await _can_browse(bot, chat_id, state, user):
        return
    await state.update_data(feed=[], feed_mode="search", tier_seen=users_repo.AREA_LOCAL,
                            current=None)
    await show_next(bot, chat_id, state, user, settings, notice=notice)


@router.message(Command("search"))
@router.message(F.text == rkb.BTN_SEARCH)
async def search_command(message: Message, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    await open_feed(bot, message.chat.id, state, user, settings)


@router.callback_query(F.data == "m:search")
async def search_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    await open_feed(bot, call.message.chat.id, state, user, settings)


async def open_likes(bot: Bot, chat_id: int, state: FSMContext,
                     user: Mapping[str, Any], settings: Settings, count: int) -> None:
    await state.update_data(feed=[], feed_mode="likes", likes_intro=count, current=None)
    await show_next(bot, chat_id, state, user, settings)


@router.message(F.text.startswith(rkb.BTN_LIKES))
async def likes_command(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings,
                        is_admin: bool) -> None:
    await screen.drop(message)
    if not await _can_browse(bot, message.chat.id, state, user):
        return
    count = await users_repo.count_incoming_likes(user["id"])
    if not count:
        await menu_handlers.show_menu(bot, message.chat.id, state, user, is_admin,
                                      note=texts.NO_INCOMING_LIKES)
        return
    await open_likes(bot, message.chat.id, state, user, settings, count)


@router.callback_query(F.data == "m:likes")
async def likes_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    count = await users_repo.count_incoming_likes(user["id"])
    if not count:
        # Пустой раздел не стоит отдельного экрана — хватит всплывашки
        await call.answer(texts.NO_INCOMING_LIKES, show_alert=True)
        return
    await call.answer()
    if await _can_browse(bot, call.message.chat.id, state, user):
        await open_likes(bot, call.message.chat.id, state, user, settings, count)


@router.callback_query(F.data == "br:reset")
async def reset_skipped(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    """Конец ленты: вернуть в выдачу всех, кого пропустили."""
    removed = await reactions_repo.reset_dislikes(user["id"], older_than_days=0)
    await call.answer()
    await open_feed(bot, call.message.chat.id, state, user, settings,
                    notice=f"🔄 <i>Вернул пропущенные анкеты: {removed}</i>")


# ───────────────────────────── Реакции ──────────────────────────────────────

@router.callback_query(F.data.startswith("br:like:"))
async def like(call: CallbackQuery, state: FSMContext, bot: Bot,
               user: Mapping[str, Any], settings: Settings) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    limit = await _likes_limit(settings)

    free = await _like_is_free(user["id"], target_id, settings)
    if not free and not await users_repo.consume_like(user["id"], limit):
        await call.answer(texts.LIKE_LIMIT_ALERT.format(limit=limit), show_alert=True)
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

async def _note_prompt(bot: Bot, chat_id: int, state: FSMContext,
                       settings: Settings, error: str | None = None) -> None:
    """Вопрос «что написать» под карточкой. Ошибка — новым вопросом на его месте."""
    data = await state.get_data()
    old = data.get("note_prompt")
    if old:
        await profile.delete_messages(bot, chat_id, [old])
        await screen.forget(state, [old])
    text = texts.LIKE_NOTE_ASK.format(max_len=settings.note_max_len)
    if error:
        text = f"⚠️ {error}\n\n{text}"
    sent = await bot.send_message(chat_id, text, reply_markup=kb.NOTE_CANCEL)
    await screen.add(state, [sent.message_id])
    await state.update_data(note_prompt=sent.message_id)


@router.callback_query(F.data.startswith("br:note:"))
async def ask_note(call: CallbackQuery, state: FSMContext, bot: Bot,
                   user: Mapping[str, Any], settings: Settings) -> None:
    """Сначала убеждаемся, что лайк вообще возможен — иначе текст писался зря."""
    target_id = int((call.data or "0").split(":")[-1])
    limit = await _likes_limit(settings)
    fresh = await users_repo.get_user(user["id"])
    free = await _like_is_free(user["id"], target_id, settings)
    if not free and await users_repo.likes_left(fresh, limit) <= 0:
        await call.answer(texts.LIKE_LIMIT_ALERT.format(limit=limit), show_alert=True)
        return

    await state.set_state(Browsing.note)
    await state.update_data(note_target=target_id)
    await call.answer()
    await _note_prompt(bot, call.message.chat.id, state, settings)


@router.callback_query(F.data == "br:note_cancel", Browsing.note)
async def cancel_note(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Передумал писать — убираем вопрос, анкета остаётся на экране."""
    data = await state.get_data()
    prompt = data.get("note_prompt")
    if prompt:
        await profile.delete_messages(bot, call.message.chat.id, [prompt])
        await screen.forget(state, [prompt])
    await state.update_data(note_prompt=None, note_target=None)
    await state.set_state(Browsing.feed)
    await call.answer(texts.CANCELLED)


@router.message(Browsing.note, F.text)
async def send_note(message: Message, state: FSMContext, bot: Bot,
                    user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    note = (message.text or "").strip()
    chat_id = message.chat.id
    if not note:
        await _note_prompt(bot, chat_id, state, settings, texts.LIKE_NOTE_EMPTY)
        return
    if len(note) > settings.note_max_len:
        await _note_prompt(bot, chat_id, state, settings,
                           texts.LIKE_NOTE_LONG.format(max_len=settings.note_max_len))
        return
    if LINK_RE.search(note):
        await _note_prompt(bot, chat_id, state, settings, texts.LIKE_NOTE_LINKS)
        return

    data = await state.get_data()
    target_id = int(data.get("note_target") or 0)
    await state.update_data(note_prompt=None, note_target=None)
    await state.set_state(Browsing.feed)
    if not target_id:
        await show_next(bot, chat_id, state, user, settings)
        return

    limit = await _likes_limit(settings)
    free = await _like_is_free(user["id"], target_id, settings)
    if not free and not await users_repo.consume_like(user["id"], limit):
        await show_next(bot, chat_id, state, user, settings,
                        notice=texts.LIKE_LIMIT_ALERT.format(limit=limit))
        return

    matched = await reactions_repo.add_reaction(user["id"], target_id, "like", note)

    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return

    if matched:
        await _announce_match(bot, user, target_id)
    else:
        await _notify_like(bot, user["id"], target_id, note)

    await show_next(bot, chat_id, state, user, settings, notice=texts.LIKE_NOTE_SENT)


@router.message(Browsing.note)
async def note_hint(message: Message, state: FSMContext, bot: Bot,
                    settings: Settings) -> None:
    await screen.drop(message)
    await _note_prompt(bot, message.chat.id, state, settings,
                       "Напишите сообщение текстом.")


# ───────────── Ответ на уведомление «вы кому-то понравились» ────────────────

@router.callback_query(F.data.startswith("ans:like:"))
async def answer_like(call: CallbackQuery, bot: Bot, user: Mapping[str, Any],
                      settings: Settings) -> None:
    sender_id = int((call.data or "0").split(":")[-1])
    # Это ответ на чужой лайк — лимит здесь не при чём
    free = await _like_is_free(user["id"], sender_id, settings)
    limit = await _likes_limit(settings)
    if not free and not await users_repo.consume_like(user["id"], limit):
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
    if user["registered"] and not user["is_active"]:
        await users_repo.update_user(user["id"], is_active=1)
        user = await users_repo.get_user(user["id"])
    await open_feed(bot, call.message.chat.id, state, user, settings)


@router.callback_query(F.data == "br:next")
async def next_profile(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    await show_next(bot, call.message.chat.id, state, user, settings)


@router.callback_query(F.data == "br:stop")
async def stop_feed(call: CallbackQuery, state: FSMContext, bot: Bot,
                    user: Mapping[str, Any], is_admin: bool) -> None:
    await call.answer()
    await menu_handlers.show_menu(bot, call.message.chat.id, state, user, is_admin)


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
