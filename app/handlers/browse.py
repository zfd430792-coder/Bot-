"""Лента анкет: просмотр, лайки, лимиты и взаимные симпатии.

Карточка анкеты — это экран с inline-кнопками «❤️ 💌 👎 🚨 🏠» под ней:
следующая заменяет предыдущую. Кнопки несут id анкеты, поэтому нажатие на
карточке делает ровно то, что на ней написано. Вопросы по ходу (сообщение
к лайку, жалоба) появляются под карточкой и уходят вместе с ней, а отказ
(кончился лимит лайков) — всплывающим окном, без лишних сообщений.

Порядок — как в Дайвинчике, настраивать нечего:

1. Кто уже поставил нам ❤️ и ждёт ответа — вне очереди, где бы он ни жил.
   Отдельного раздела «кто меня лайкнул» нет: ответить можно прямо в ленте.
2. Свой город, затем своя область — строчка над карточкой говорит, что
   город кончился.
3. Когда кончилась и область, лента спрашивает, показать ли соседние
   области, и после согласия идёт к ним — ближние первыми.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.db.database import haversine
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
    if tier == users_repo.AREA_REGION and viewer["region"]:
        return texts.FEED_TO_REGION.format(region=profile.esc(viewer["region"]))
    return texts.FEED_TO_FAR


def _queue(raw: list) -> list[tuple[int, int]]:
    """Очередь хранит пары (id, ступень); старый формат — просто id."""
    return [(item, 0) if isinstance(item, int) else (int(item[0]), int(item[1]))
            for item in raw or []]


def _lead(header: list[str]) -> str:
    return "\n".join(header) + "\n\n" if header else ""


def _usable(target: Mapping[str, Any] | None) -> bool:
    return (target is not None and not target["is_banned"] and bool(target["is_active"])
            and bool(target["registered"]) and not target["verify_forced"])


async def _present(bot: Bot, chat_id: int, state: FSMContext, viewer: Mapping[str, Any],
                   target: Mapping[str, Any], settings: Settings, *,
                   header: list[str], ads_seen: int) -> int:
    """Отправляет карточку (и рекламу перед ней, если пора) вместо прежнего экрана.
    Возвращает новый счётчик анкет с прошлой рекламы."""
    liked = await reactions_repo.liked_me(viewer["id"], target["id"])
    free = liked or settings.is_admin(viewer["id"])
    left = (None if free
            else await users_repo.likes_left(viewer, await _likes_limit(settings)))
    note = None
    if liked:
        # Нас уже лайкнули: говорим об этом и показываем приложенное сообщение
        header = [*header, texts.LIKED_YOU]
        note = await reactions_repo.get_note(target["id"], viewer["id"])

    await screen.prepare(bot, chat_id, state)
    ad_messages, ads_seen = await ads_service.maybe_send(bot, chat_id, ads_seen)
    message_ids = await profile.send_card(
        bot, chat_id, _with_distance(target, viewer),
        markup=kb.feed(int(target["id"]), left),
        viewer=viewer, note=note, header="\n".join(header),
    )
    await screen.remember(state, ad_messages + message_ids)
    await state.set_state(Browsing.feed)
    return ads_seen


async def show_next(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], settings: Settings, *,
                    notice: str | None = None) -> None:
    """Показывает следующую анкету вместо предыдущей.

    notice — строка над карточкой: «сообщение отправлено», «жалоба принята».
    """
    data = await state.get_data()
    viewer = await users_repo.get_user(user["id"])
    header = [notice] if notice else []
    ads_seen = int(data.get("ads_seen", 0)) + 1

    # 1. Кто лайкнул нас и ждёт ответа — первым. Проверяем при каждой
    #    карточке: лайк, пришедший посреди ленты, не ждёт конца очереди
    likers = await users_repo.incoming_likes(viewer["id"], limit=1)
    if likers:
        target = likers[0]
        ads_seen = await _present(bot, chat_id, state, viewer, target, settings,
                                  header=header, ads_seen=ads_seen)
        await state.update_data(current=int(target["id"]), ads_seen=ads_seen)
        return

    # 2. Свой город, своя область, соседние области
    queue = _queue(data.get("feed"))
    if not queue:
        rows = await users_repo.search_candidates(viewer)
        queue = [(int(r["id"]), int(r["area_tier"])) for r in rows]

    tier_seen = int(data.get("tier_seen") or users_repo.AREA_LOCAL)
    while queue:
        target_id, tier = queue[0]
        if tier >= users_repo.AREA_FAR and not users_repo.far_allowed(viewer):
            await _offer_far(bot, chat_id, state, viewer, queue, header)
            return
        queue.pop(0)
        target = await users_repo.get_user(target_id)
        if not _usable(target) or await reactions_repo.has_reacted(viewer["id"], target_id):
            continue

        if tier > tier_seen:
            header.append(_step_notice(viewer, tier))
            tier_seen = tier
        ads_seen = await _present(bot, chat_id, state, viewer, target, settings,
                                  header=header, ads_seen=ads_seen)
        await state.update_data(feed=[list(item) for item in queue], current=target_id,
                                ads_seen=ads_seen, tier_seen=tier_seen)
        return

    # 3. Анкеты кончились совсем — экран с тем, что можно сделать дальше
    await state.update_data(feed=[], current=None)
    await state.set_state(None)
    skipped = await reactions_repo.count_dislikes(viewer["id"])
    text = texts.NO_PROFILES
    if skipped:
        text += "\n\n" + texts.NO_PROFILES_SKIPPED.format(count=skipped)
    await screen.show(bot, chat_id, state, _lead(header) + text, kb.feed_end(skipped))


async def _offer_far(bot: Bot, chat_id: int, state: FSMContext, viewer: Mapping[str, Any],
                     queue: list[tuple[int, int]], header: list[str]) -> None:
    """Город и область пройдены — спрашиваем, смотреть ли соседние области.
    Очередь сохраняем: после согласия лента продолжится с того же места."""
    await state.update_data(feed=[list(item) for item in queue], current=None)
    await state.set_state(None)
    skipped = await reactions_repo.count_dislikes(viewer["id"])
    text = texts.FEED_OFFER_FAR
    if skipped:
        text += "\n\n" + texts.NO_PROFILES_SKIPPED.format(count=skipped)
    await screen.show(bot, chat_id, state, _lead(header) + text, kb.far_offer(skipped))


async def show_current(bot: Bot, chat_id: int, state: FSMContext,
                       user: Mapping[str, Any], settings: Settings, *,
                       notice: str | None = None) -> None:
    """Показывает ту же анкету заново — например, со строкой о том, что лайк
    не прошёл. Если её уже нельзя показать — следующую."""
    data = await state.get_data()
    target = await users_repo.get_user(int(data.get("current") or 0))
    if not _usable(target):
        await show_next(bot, chat_id, state, user, settings, notice=notice)
        return
    viewer = await users_repo.get_user(user["id"])
    await _present(bot, chat_id, state, viewer, target, settings,
                   header=[notice] if notice else [],
                   ads_seen=int(data.get("ads_seen", 0)))


async def _current(state: FSMContext) -> int:
    return int((await state.get_data()).get("current") or 0)


async def _answer_late(call: CallbackQuery, refusal: str | None) -> None:
    """Ответ на нажатие после обработки — ради всплывающего окна с отказом.
    Если обработка затянулась, Telegram уже не примет ответ: это не ошибка."""
    try:
        if refusal:
            await call.answer(refusal, show_alert=True)     # анкета остаётся на экране
        else:
            await call.answer()
    except TelegramBadRequest:
        pass


def _target(call: CallbackQuery) -> int:
    """id анкеты из кнопки br:<действие>:<id>."""
    tail = (call.data or "").rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


# ─────────────────────────── Входные точки ──────────────────────────────────

async def _can_browse(bot: Bot, chat_id: int, state: FSMContext,
                      user: Mapping[str, Any]) -> bool:
    if not user["registered"]:
        await screen.show(bot, chat_id, state,
                          "Сначала заполните анкету — это пара минут.", kb.START_AGAIN)
        return False
    if not user["is_active"]:
        await screen.show(
            bot, chat_id, state,
            "🙈 Ваша анкета скрыта из поиска, поэтому смотреть чужие нельзя.\n"
            "Включите показ в разделе «👤 Моя анкета».",
            kb.TO_PROFILE,
        )
        return False
    return True


async def open_feed(bot: Bot, chat_id: int, state: FSMContext,
                    user: Mapping[str, Any], settings: Settings, *,
                    notice: str | None = None) -> None:
    if not await _can_browse(bot, chat_id, state, user):
        return
    await state.clear()
    await state.update_data(feed=[], tier_seen=users_repo.AREA_LOCAL, current=None)
    await show_next(bot, chat_id, state, user, settings, notice=notice)


@router.message(Command("search"))
async def search_command(message: Message, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    await open_feed(bot, message.chat.id, state, user, settings)


@router.callback_query(F.data.in_({"m:search", "n:search"}))
async def search_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                        user: Mapping[str, Any], settings: Settings) -> None:
    """Из меню или из уведомления: уведомление при этом остаётся в чате."""
    await call.answer()
    await open_feed(bot, screen.chat_id(call), state, user, settings)


async def _allow_far(bot: Bot, chat_id: int, state: FSMContext,
                     user: Mapping[str, Any], settings: Settings) -> None:
    """Согласие смотреть соседние области. Запоминаем его: в следующий раз
    лента перейдёт к ним сама, без вопроса."""
    if not await _can_browse(bot, chat_id, state, user):
        return
    await users_repo.update_user(user["id"], search_scope=users_repo.SCOPE_ALL)
    await show_next(bot, chat_id, state, user, settings)


@router.callback_query(F.data == "br:far")
async def far_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                     user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    await _allow_far(bot, screen.chat_id(call), state, user, settings)


async def _reset_skipped(bot: Bot, chat_id: int, state: FSMContext,
                         user: Mapping[str, Any], settings: Settings) -> None:
    """Конец ленты: вернуть в выдачу всех, кого пропустили."""
    removed = await reactions_repo.reset_dislikes(user["id"], older_than_days=0)
    await open_feed(bot, chat_id, state, user, settings,
                    notice=f"🔄 <i>Вернул пропущенные анкеты: {removed}</i>")


@router.callback_query(F.data == "br:reset")
async def reset_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    await _reset_skipped(bot, screen.chat_id(call), state, user, settings)


@router.callback_query(F.data == "n:mute")
async def stop_reminders(call: CallbackQuery, state: FSMContext, user,
                         is_admin: bool) -> None:
    """Отписка прямо из напоминания."""
    await users_repo.update_user(user["id"], notify_enabled=0)
    await call.answer("🔕 Больше не напомню.")
    await menu_handlers.show_menu(call.bot, screen.chat_id(call), state, user, is_admin)


# ───────────────────────────── Реакции ──────────────────────────────────────

async def _like(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                settings: Settings, target_id: int) -> str | None:
    """Ставит лайк и показывает следующую анкету.
    Возвращает текст отказа, если лайк не прошёл (кончился лимит)."""
    target = await users_repo.get_user(target_id)
    if not _usable(target):
        await show_next(bot, chat_id, state, user, settings)
        return None
    limit = await _likes_limit(settings)
    free = await _like_is_free(user["id"], target_id, settings)
    if not free and not await users_repo.consume_like(user["id"], limit):
        return texts.LIKE_LIMIT_POPUP.format(limit=limit)

    matched = await reactions_repo.add_reaction(user["id"], target_id, "like")

    # Накрутка лайков: слишком быстро или вообще без пропусков
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return None

    if matched:
        await _announce_match(bot, user, target_id)
    else:
        await _notify_like(bot, target_id, None)

    await show_next(bot, chat_id, state, user, settings,
                    notice="🎉 <b>Взаимно!</b> Контакты — в сообщении выше." if matched
                    else None)
    return None


async def _dislike(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                   settings: Settings, target_id: int) -> None:
    await reactions_repo.add_reaction(user["id"], target_id, "dislike")
    if await antifraud.check(bot, user["id"], settings):
        await state.clear()
        return
    await show_next(bot, chat_id, state, user, settings)


@router.callback_query(F.data.startswith("br:like:"))
async def like_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    target_id = _target(call) or await _current(state)
    if not target_id:
        await call.answer()
        await open_feed(bot, screen.chat_id(call), state, user, settings)
        return
    refusal = await _like(bot, screen.chat_id(call), state, user, settings, target_id)
    await _answer_late(call, refusal)


@router.callback_query(F.data.startswith("br:dislike:"))
async def dislike_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings) -> None:
    await call.answer()
    target_id = _target(call) or await _current(state)
    if not target_id:
        await open_feed(bot, screen.chat_id(call), state, user, settings)
        return
    await _dislike(bot, screen.chat_id(call), state, user, settings, target_id)


# ───────────────────── Лайк с сообщением ────────────────────────────────────

async def _note_prompt(bot: Bot, chat_id: int, state: FSMContext,
                       settings: Settings, error: str | None = None) -> None:
    """Вопрос «что написать» под карточкой. Ошибка — правкой того же вопроса."""
    text = texts.LIKE_NOTE_ASK.format(max_len=settings.note_max_len)
    if error:
        text = f"⚠️ {error}\n\n{text}"
    prompt = (await state.get_data()).get("note_prompt")
    if prompt:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=prompt,
                                        reply_markup=kb.NOTE_CANCEL)
            return
        except Exception:
            pass    # вопрос удалили — зададим заново
    sent = await bot.send_message(chat_id, text, reply_markup=kb.NOTE_CANCEL)
    await screen.add(state, [sent.message_id])
    await state.update_data(note_prompt=sent.message_id)


async def _remove_prompt(bot: Bot, chat_id: int, state: FSMContext, key: str) -> None:
    """Убирает вопрос под карточкой — сама карточка с кнопками остаётся."""
    prompt = (await state.get_data()).get(key)
    if prompt:
        await profile.delete_messages(bot, chat_id, [prompt])
        await screen.forget(state, [prompt])
    await state.update_data(**{key: None})


async def _ask_note(bot: Bot, chat_id: int, state: FSMContext, user: Mapping[str, Any],
                    settings: Settings, target_id: int) -> str | None:
    """Сначала убеждаемся, что лайк вообще возможен — иначе текст писался зря.
    Возвращает текст отказа."""
    limit = await _likes_limit(settings)
    fresh = await users_repo.get_user(user["id"])
    free = await _like_is_free(user["id"], target_id, settings)
    if not free and await users_repo.likes_left(fresh, limit) <= 0:
        return texts.LIKE_LIMIT_POPUP.format(limit=limit)
    await _remove_prompt(bot, chat_id, state, "report_prompt")
    await state.set_state(Browsing.note)
    await state.update_data(note_target=target_id, current=target_id)
    await _note_prompt(bot, chat_id, state, settings)
    return None


@router.callback_query(F.data.startswith("br:note:"))
async def note_button(call: CallbackQuery, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    target_id = _target(call) or await _current(state)
    if not target_id:
        await call.answer()
        await open_feed(bot, screen.chat_id(call), state, user, settings)
        return
    refusal = await _ask_note(bot, screen.chat_id(call), state, user, settings, target_id)
    await _answer_late(call, refusal)


@router.callback_query(F.data == "br:cancel")
async def cancel_note(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Передумал писать — вопрос уходит, анкета остаётся на экране."""
    await call.answer()
    await _remove_prompt(bot, screen.chat_id(call), state, "note_prompt")
    await state.update_data(note_target=None)
    await state.set_state(Browsing.feed)


@router.message(Browsing.note, F.text == rkb.CANCEL)
async def cancel_note_legacy(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    await _remove_prompt(bot, message.chat.id, state, "note_prompt")
    await state.update_data(note_target=None)
    await state.set_state(Browsing.feed)


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
        await _notify_like(bot, target_id, note)

    await show_next(bot, chat_id, state, user, settings, notice=texts.LIKE_NOTE_SENT)


@router.message(Browsing.note)
async def note_hint(message: Message, state: FSMContext, bot: Bot,
                    settings: Settings) -> None:
    await screen.drop(message)
    await _note_prompt(bot, message.chat.id, state, settings,
                       "Напишите сообщение текстом.")


# ─────────── Нижние кнопки ленты прежней версии: к текущей анкете ───────────

@router.message(F.text.regexp(rkb.L_LIKE_RE))
async def like_legacy(message: Message, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    target_id = await _current(state)
    if not target_id:
        await open_feed(bot, chat_id, state, user, settings)
        return
    refusal = await _like(bot, chat_id, state, user, settings, target_id)
    if refusal:
        await show_current(bot, chat_id, state, user, settings,
                           notice=texts.LIKE_LIMIT_ALERT.format(
                               limit=await _likes_limit(settings)))


@router.message(F.text == rkb.L_DISLIKE)
async def dislike_legacy(message: Message, state: FSMContext, bot: Bot,
                         user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    target_id = await _current(state)
    if not target_id:
        await open_feed(bot, message.chat.id, state, user, settings)
        return
    await _dislike(bot, message.chat.id, state, user, settings, target_id)


@router.message(F.text == rkb.L_NOTE)
@router.message(F.text == rkb.L_REPORT)
async def card_legacy(message: Message, state: FSMContext, bot: Bot,
                      user: Mapping[str, Any], settings: Settings) -> None:
    """«Сообщение» и «Жалоба» со старой клавиатуры: та же анкета — уже с
    кнопками под ней, там всё и продолжится."""
    await screen.drop(message)
    await show_current(bot, message.chat.id, state, user, settings)


@router.message(F.text == rkb.L_FAR)
async def far_legacy(message: Message, state: FSMContext, bot: Bot,
                     user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    await _allow_far(bot, message.chat.id, state, user, settings)


@router.message(F.text.regexp(rkb.L_RESET_RE))
async def reset_legacy(message: Message, state: FSMContext, bot: Bot,
                       user: Mapping[str, Any], settings: Settings) -> None:
    await screen.drop(message)
    await _reset_skipped(bot, message.chat.id, state, user, settings)


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


async def _notify_like(bot: Bot, target_id: int, note: str | None) -> None:
    """Сообщаем о симпатии — с кнопкой «Посмотреть», которая открывает ленту,
    где этот человек первый. Само уведомление остаётся в чате.

    Об обычных лайках говорим один раз, пока предыдущие не разобраны, иначе
    это превратится в поток уведомлений; о лайке с сообщением — всегда.
    """
    if note:
        await safe_send(bot, target_id, texts.NEW_LIKE_WITH_NOTE, kb.NOTIFY_LIKE)
        return
    if await users_repo.count_incoming_likes(target_id) != 1:
        return
    await safe_send(bot, target_id, texts.NEW_LIKE_NOTIFY, kb.NOTIFY_LIKE)
