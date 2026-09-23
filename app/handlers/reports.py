"""Жалобы на анкеты. Видит их только администратор, автор жалобы не раскрывается.

Вопрос «на что жалуемся» появляется под карточкой с причинами на кнопках и
правится на месте: причина → комментарий. После отправки лента продолжается,
а подтверждение видно строкой над следующей анкетой. «⬅️ Отмена» просто
убирает вопрос — анкета остаётся на экране.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import reactions as reactions_repo
from app.db import users as users_repo
from app.handlers import browse
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import safe_send
from app.states import Browsing, Report

router = Router(name="reports")

REASON_BY_TITLE = {title: key for key, title in texts.REPORT_REASONS.items()}
NO_COMMENT_TEXT = "📨 Отправить без комментария"     # нижняя кнопка прежней версии


async def _prompt(bot: Bot, chat_id: int, state: FSMContext, text: str, markup) -> None:
    """Вопрос жалобы под карточкой: правим прежний на месте или задаём новый."""
    prompt = (await state.get_data()).get("report_prompt")
    if prompt:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=prompt,
                                        reply_markup=markup)
            return
        except Exception:
            pass    # вопрос удалили — зададим заново
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await screen.add(state, [sent.message_id])
    await state.update_data(report_prompt=sent.message_id)


async def _remove_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    prompt = (await state.get_data()).get("report_prompt")
    if prompt:
        await profile_service.delete_messages(bot, chat_id, [prompt])
        await screen.forget(state, [prompt])
    await state.update_data(report_prompt=None)


@router.callback_query(F.data.startswith("br:report:"))
async def ask_reason(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                     settings: Settings) -> None:
    chat_id = screen.chat_id(call)
    tail = (call.data or "").rsplit(":", 1)[-1]
    target_id = int(tail) if tail.isdigit() else 0
    if not target_id:
        await call.answer()
        await browse.open_feed(bot, chat_id, state, user, settings)
        return
    if await mod_repo.already_reported(user["id"], target_id):
        await call.answer(texts.REPORT_DUPLICATE, show_alert=True)
        return
    await call.answer()
    # Если под анкетой уже висел вопрос о сообщении к лайку — он больше не нужен
    note_prompt = (await state.get_data()).get("note_prompt")
    if note_prompt:
        await profile_service.delete_messages(bot, chat_id, [note_prompt])
        await screen.forget(state, [note_prompt])
    await state.set_state(Report.reason)
    await state.update_data(report_target=target_id, current=target_id,
                            note_prompt=None, note_target=None)
    await _prompt(bot, chat_id, state, texts.REPORT_ASK,
                  kb.report_reasons(texts.REPORT_REASONS))


async def _cancel(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Передумали жаловаться — вопрос уходит, анкета остаётся на экране."""
    await _remove_prompt(bot, chat_id, state)
    await state.update_data(report_target=None, report_reason=None)
    await state.set_state(Browsing.feed)


@router.callback_query(F.data == "rep:cancel")
async def cancel(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    await _cancel(bot, screen.chat_id(call), state)


async def _pick_reason(bot: Bot, chat_id: int, state: FSMContext, reason: str) -> None:
    await state.update_data(report_reason=reason)
    await state.set_state(Report.comment)
    await _prompt(bot, chat_id, state,
                  texts.REPORT_COMMENT.format(reason=texts.REPORT_REASONS[reason]),
                  kb.REPORT_COMMENT)


@router.callback_query(Report.reason, F.data.startswith("rep:"))
async def reason_button(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    reason = (call.data or "").removeprefix("rep:")
    if reason in texts.REPORT_REASONS:
        await _pick_reason(bot, screen.chat_id(call), state, reason)


@router.message(Report.reason)
async def reason_text(message: Message, state: FSMContext, bot: Bot) -> None:
    """Причина выбирается кнопкой; надписи прежних нижних кнопок понимаем."""
    await screen.drop(message)
    if message.text == rkb.CANCEL:
        await _cancel(bot, message.chat.id, state)
        return
    reason = REASON_BY_TITLE.get(message.text or "")
    if reason is not None:
        await _pick_reason(bot, message.chat.id, state, reason)


@router.callback_query(Report.comment, F.data == "rep:send")
async def send_without_comment(call: CallbackQuery, state: FSMContext, bot: Bot,
                               user, settings: Settings) -> None:
    await call.answer()
    await _submit(bot, screen.chat_id(call), state, user, settings, comment=None)


@router.message(Report.comment, F.text)
async def send_with_comment(message: Message, state: FSMContext, bot: Bot,
                            user, settings: Settings) -> None:
    await screen.drop(message)
    if message.text == rkb.CANCEL:
        await _cancel(bot, message.chat.id, state)
        return
    comment = None if message.text == NO_COMMENT_TEXT else (message.text or "")[:500]
    await _submit(bot, message.chat.id, state, user, settings, comment=comment)


@router.message(Report.comment)
async def comment_hint(message: Message) -> None:
    await screen.drop(message)


@router.callback_query(F.data.startswith("rep:"))
async def stale_report_button(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Кнопка жалобы, которая уже неактуальна (сообщение выше по чату)."""
    await call.answer()
    message = call.message
    if message is not None and message.message_id not in await screen.message_ids(state):
        await profile_service.delete_messages(bot, message.chat.id, [message.message_id])


async def _submit(bot: Bot, chat_id: int, state: FSMContext,
                  user: Mapping[str, Any], settings: Settings,
                  comment: str | None) -> None:
    data = await state.get_data()
    target_id = int(data.get("report_target") or 0)
    reason = data.get("report_reason") or "other"
    await state.update_data(report_prompt=None, report_target=None)
    if not target_id:
        await browse.show_next(bot, chat_id, state, user, settings)
        return

    report_id = await mod_repo.add_report(user["id"], target_id, reason, comment)
    # На кого пожаловались, того больше не показываем — как после 👎.
    # Иначе лайкнувший нарушитель, которого лента ставит первым, вернулся бы сразу
    await reactions_repo.add_reaction(user["id"], target_id, "dislike")

    target = await users_repo.get_user(target_id)
    total = target["reports_count"] if target else 0
    header = (
        f"🚨 <b>Жалоба #{report_id}</b>\n\n"
        f"Причина: <b>{texts.REPORT_REASONS.get(reason, reason)}</b>\n"
        f"Комментарий: {profile_service.esc(comment) if comment else '—'}\n"
        f"От: <code>{user['id']}</code> @{user['username'] or '—'}\n"
        f"Всего жалоб на анкету: <b>{total}</b>"
    )
    for admin_id in settings.admin_ids:
        if target is not None:
            try:
                await profile_service.send_card(bot, admin_id, target, admin_view=True,
                                                show_distance=False)
            except Exception:
                await safe_send(bot, admin_id, "Анкета недоступна.")
        await safe_send(bot, admin_id, header, kb.NOTIFY_REPORTS)

    # Лента продолжается, подтверждение — строкой над следующей анкетой
    await state.set_state(None)
    await browse.show_next(bot, chat_id, state, user, settings,
                           notice=f"<i>{texts.REPORT_SENT}</i>")
