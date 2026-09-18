"""Лента анкет: просмотр, лайки, лимиты и взаимные симпатии.

Карточка анкеты — это экран с нижними кнопками «❤️ 💌 👎 🚨 🏠»: следующая
заменяет предыдущую, а вопросы по ходу (сообщение к лайку, жалоба)
появляются под ней и уходят вместе с ней.

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
from aiogram.types import Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.db.database import db, haversine
from app.handlers import menu as menu_handlers
from app.handlers.registration import LINK_RE
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


async def _present(bot: Bot, chat_id: int, state: FSMContext, viewer: Mapping[str, Any],
                   target: Mapping[str, Any], settings: Settings, *, mode: str,
                   header: list[str], ads_seen: int) -> int:
    """Отправляет карточку (и рекламу перед ней, если пора) вместо прежнего экрана.
    Возвращает новый счётчик анкет с прошлой рекламы."""
    ad_messages, ads_seen = await ads_service.maybe_send(bot, chat_id, ads_seen)

    free = await _like_is_free(viewer["id"], target["id"], settings)
    left = (None if free
            else await users_repo.likes_left(viewer, await _likes_limit(settings)))
    note = (await reactions_repo.get_note(target["id"], viewer["id"])
            if mode == "likes" else None)
    message_ids = await profile.send_card(
        bot, chat_id, _with_distance(target, viewer), markup=rkb.feed(left),
        viewer=viewer, note=note, header="\n".join(header),
    )
    await screen.replace(bot, chat_id, state, ad_messages + message_ids)
    await state.set_state(Browsing.feed if mode == "search" else Browsing.likes_inbox)
    return ads_seen


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

        ads_seen = await _present(bot, chat_id, state, fresh_viewer, target, settings,
                                  mode=mode, header=header,
                                  ads_seen=int(data.get("ads_seen", 0)) + 1)
        await state.update_data(feed=[list(item) for item in queue], current=target_id,
                                feed_mode=mode, ads_seen=ads_seen, tier_seen=tier_seen,
                                likes_intro=None)
        return

    # Анкеты кончились совсем — экран с тем, что можно сделать дальше
    await state.update_data(feed=[], current=None)
    await state.set_state(None)
    lead = f"{notice}\n\n" if notice else ""
    if mode == "likes":
        await screen.send(bot, chat_id, state, lead + texts.LIKES_DONE, rkb.LIKES_END)
        return

    skipped = await reactions_repo.count_dislikes(user["id"])
    hints = []
    if skipped:
        hints.append(texts.NO_PROFILES_SKIPPED.format(count=skipped))
    hints.append(texts.NO_PROFILES_AGE.format(age_min=fresh_viewer["age_min"] or 18,
                                              age_max=fresh_viewer["age_max"] or 99))
    await screen.send(bot, chat_id, state,
                      lead + texts.NO_PROFILES + "\n\n" + "\n".join(hints),
                      rkb.feed_end(skipped))


async def show_current(bot: Bot, chat_id: int, state: FSMContext,
                       user: Mapping[str, Any], settings: Settings, *,
                       notice: str | None = None) -> None:
    """Показывает ту же анкету заново — после отменённой жалобы или сообщения.

    Карточку приходится прислать ещё раз: вместе с ней вернётся клавиатура
    ленты, а убрать чужую нижнюю клавиатуру иначе нельзя."""
    data = await state.get_data()
    target = await users_repo.get_user(int(data.get("current") or 0))
    if target is None or not target["registered"] or target["is_banned"]:
        await show_next(bot, chat_id, state, user, settings, notice=notice)
        return
    viewer = await users_repo.get_user(user["id"])
    await _present(bot, chat_id, state, viewer, target, settings,
                   mode=data.get("feed_mode", "search"),
                   header=[notice] if notice else [],
                   ads_seen=int(data.get("ads_seen", 0)))


async def _current(state: FSMContext) -> int:
    return int((await state.get_data()).get("current") or 0)


# ─────────────────────────── Входные точки ──────────────────────────────────

async def _can_browse(bot: Bot, chat_id: int, state: FSMContext,
                      user: Mapping[str, Any]) -> bool:
    if not user["registered"]:
        await screen.send(bot, chat_id, state,
                          "Сначала заполните анкету — это пара минут.", rkb.START_AGAIN)
        return False
    if not user["is_active"]:
        await screen.send(
            bot, chat_id, state,
            "🙈 Ваша анкета скрыта из поиска, поэтому смотреть чужие нельзя.\n"
            "Включите показ в разделе «👤 Моя анкета».",
            rkb.keyboard([[rkb.PROFILE], [rkb.HOME]]),
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
@router.message(F.text == rkb.SEARCH)
async def search(message: Message, state: FSMContext, bot: Bot,
                 user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    await open_feed(bot, message.chat.id, state, user, settings)


@router.message(F.text.startswith(rkb.LIKES))
async def likes_inbox(message: Message, state: FSMContext, bot: Bot,
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
    await state.update_data(feed=[], feed_mode="likes", likes_intro=count, current=None)
    await show_next(bot, message.chat.id, state, user, settings)


@router.message(F.text.startswith(rkb.RESET_SKIPS))
async def reset_skipped(message: Message, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    """Конец ленты: вернуть в выдачу всех, кого пропустили."""
    await screen.drop(message)
    removed = await reactions_repo.reset_dislikes(user["id"], older_than_days=0)
    await open_feed(bot, message.chat.id, state, user, settings,
                    notice=f"🔄 <i>Вернул пропущенные анкеты: {removed}</i>")


@router.message(F.text == rkb.STOP_REMINDERS)
async def stop_reminders(message: Message, state: FSMContext, user,
                         is_admin: bool) -> None:
    """Отписка прямо из напоминания — без захода в настройки."""
    await screen.drop(message)
    await users_repo.update_user(user["id"], notify_enabled=0)
    await menu_handlers.show_menu(
        message.bot, message.chat.id, state, user, is_admin,
        note="🔕 <i>Больше не напомню. Включить обратно можно в ⚙️ Настройках.</i>",
    )


# ───────────────────────────── Реакции ──────────────────────────────────────

@router.message(F.text.regexp(rkb.LIKE_RE))
async def like(message: Message, state: FSMContext, bot: Bot,
               user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    target_id = await _current(state)
    if not target_id:
        await open_feed(bot, chat_id, state, user, settings)
        return
    limit = await _likes_limit(settings)

    free = await _like_is_free(user["id"], target_id, settings)
    if not free and not await users_repo.consume_like(user["id"], limit):
        await _notice_under_card(bot, chat_id, state,
                                 texts.LIKE_LIMIT_ALERT.format(limit=limit))
        return

    matched = await reactions_repo.add_reaction(user["id"], target_id, "like")

    # Накрутка лайков: слишком быстро или вообще без пропусков
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return

    if matched:
        await _announce_match(bot, user, target_id)
    else:
        await _notify_like(bot, user["id"], target_id, None)

    await show_next(bot, chat_id, state, user, settings,
                    notice="🎉 <b>Взаимно!</b> Контакты — в сообщении выше." if matched
                    else None)


@router.message(F.text == rkb.DISLIKE)
async def dislike(message: Message, state: FSMContext, bot: Bot,
                  user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    target_id = await _current(state)
    if not target_id:
        await open_feed(bot, message.chat.id, state, user, settings)
        return
    await reactions_repo.add_reaction(user["id"], target_id, "dislike")
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return
    await show_next(bot, message.chat.id, state, user, settings)


async def _notice_under_card(bot: Bot, chat_id: int, state: FSMContext,
                             text: str) -> None:
    """Короткое пояснение под анкетой. Клавиатура остаётся от карточки, а
    с переходом к следующей анкете пояснение уйдёт вместе с экраном."""
    sent = await bot.send_message(chat_id, text)
    await screen.add(state, [sent.message_id])


# ───────────────────── Лайк с сообщением ────────────────────────────────────

async def _note_prompt(bot: Bot, chat_id: int, state: FSMContext,
                       settings: Settings, error: str | None = None) -> None:
    """Вопрос «что написать» под карточкой. Ошибка — новым вопросом на его месте."""
    data = await state.get_data()
    old = data.get("note_prompt")
    text = texts.LIKE_NOTE_ASK.format(max_len=settings.note_max_len)
    if error:
        text = f"⚠️ {error}\n\n{text}"
    sent = await bot.send_message(chat_id, text, reply_markup=rkb.CANCEL_ONLY)
    await screen.add(state, [sent.message_id])
    if old:
        await profile.delete_messages(bot, chat_id, [old])
        await screen.forget(state, [old])
    await state.update_data(note_prompt=sent.message_id)


@router.message(F.text == rkb.NOTE)
async def ask_note(message: Message, state: FSMContext, bot: Bot,
                   user: Mapping[str, Any], settings: Settings) -> None:
    """Сначала убеждаемся, что лайк вообще возможен — иначе текст писался зря."""
    await screen.drop(message)
    chat_id = message.chat.id
    target_id = await _current(state)
    if not target_id:
        await open_feed(bot, chat_id, state, user, settings)
        return
    limit = await _likes_limit(settings)
    fresh = await users_repo.get_user(user["id"])
    free = await _like_is_free(user["id"], target_id, settings)
    if not free and await users_repo.likes_left(fresh, limit) <= 0:
        await _notice_under_card(bot, chat_id, state,
                                 texts.LIKE_LIMIT_ALERT.format(limit=limit))
        return

    await state.set_state(Browsing.note)
    await state.update_data(note_target=target_id, note_prompt=None)
    await _note_prompt(bot, chat_id, state, settings)


@router.message(Browsing.note, F.text == rkb.CANCEL)
async def cancel_note(message: Message, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    """Передумал писать — анкета снова на экране с кнопками ленты."""
    await screen.drop(message)
    await state.update_data(note_prompt=None, note_target=None)
    await show_current(bot, message.chat.id, state, user, settings)


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
        await show_current(bot, chat_id, state, user, settings,
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

    Уведомление — без кнопок: у человека сейчас клавиатура того экрана, где он
    находится, и сбивать её нельзя. Анкета и сообщение ждут в «Кто меня
    лайкнул». Об обычных лайках говорим один раз, пока предыдущие не
    разобраны, иначе это превратится в поток уведомлений; о лайке с
    сообщением — всегда.
    """
    if note:
        await safe_send(bot, target_id, texts.NEW_LIKE_WITH_NOTE)
        return

    pending = await db.fetchval(
        "SELECT COUNT(*) FROM reactions WHERE to_id = ? AND kind = 'like' AND is_seen = 0",
        (target_id,), default=0,
    )
    if int(pending or 0) != 1:
        return
    await safe_send(bot, target_id, texts.NEW_LIKE_NOTIFY)
