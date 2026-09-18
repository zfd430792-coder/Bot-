"""Жалобы на анкеты. Видит их только администратор, автор жалобы не раскрывается.

Вопрос «на что жалуемся» появляется под карточкой и уходит вместе с ней;
после отправки лента продолжается, а подтверждение видно строкой над
следующей анкетой.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import browse
from app.handlers import menu as menu_handlers
from app.keyboards import inline as kb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import safe_send
from app.states import Browsing, Report

router = Router(name="reports")


async def _back_to_browsing(state: FSMContext) -> None:
    """Жалобу отменили — возвращаем тот режим ленты, что был до неё."""
    data = await state.get_data()
    if data.get("current"):
        await state.set_state(Browsing.likes_inbox if data.get("feed_mode") == "likes"
                              else Browsing.feed)
    else:
        await state.set_state(None)


async def _drop_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    prompt = data.get("report_prompt")
    if prompt:
        await profile_service.delete_messages(bot, chat_id, [prompt])
        await screen.forget(state, [prompt])
    await state.update_data(report_prompt=None)


@router.callback_query(F.data.startswith("br:report:"))
async def ask_reason(call: CallbackQuery, state: FSMContext, bot: Bot, user) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    if await mod_repo.already_reported(user["id"], target_id):
        await call.answer(texts.REPORT_DUPLICATE, show_alert=True)
        return
    await call.answer()
    await _drop_prompt(bot, call.message.chat.id, state)
    await state.set_state(Report.reason)
    await state.update_data(report_target=target_id)
    sent = await call.message.answer(
        texts.REPORT_ASK,
        reply_markup=kb.report_reasons(texts.REPORT_REASONS, target_id),
    )
    await screen.add(state, [sent.message_id])
    await state.update_data(report_prompt=sent.message_id)


@router.callback_query(F.data == "rep:cancel")
async def cancel(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Передумали жаловаться — вопрос уходит, анкета остаётся на экране."""
    await call.answer(texts.CANCELLED)
    await _drop_prompt(bot, call.message.chat.id, state)
    await _back_to_browsing(state)


@router.callback_query(F.data.startswith("rep:"), Report.reason)
async def pick_reason(call: CallbackQuery, state: FSMContext) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        await call.answer()
        return
    _, reason, target_id = parts
    if reason not in texts.REPORT_REASONS:
        await call.answer()
        return

    await state.update_data(report_reason=reason, report_target=int(target_id))
    await state.set_state(Report.comment)
    await call.answer()
    await call.message.edit_text(
        f"{texts.REPORT_REASONS[reason]}\n\n{texts.REPORT_COMMENT}",
        reply_markup=kb.REPORT_SKIP_COMMENT,
    )


@router.callback_query(F.data == "rep:send", Report.comment)
async def send_without_comment(call: CallbackQuery, state: FSMContext, bot: Bot,
                               user, settings: Settings, is_admin: bool) -> None:
    await call.answer()
    await _submit(bot, call.message.chat.id, state, user, settings, is_admin,
                  comment=None)


@router.message(Report.comment, F.text)
async def send_with_comment(message: Message, state: FSMContext, bot: Bot,
                            user, settings: Settings, is_admin: bool) -> None:
    await screen.drop(message)
    await _submit(bot, message.chat.id, state, user, settings, is_admin,
                  comment=(message.text or "")[:500])


async def _submit(bot: Bot, chat_id: int, state: FSMContext,
                  user: Mapping[str, Any], settings: Settings, is_admin: bool,
                  comment: str | None) -> None:
    data = await state.get_data()
    target_id = int(data.get("report_target") or 0)
    reason = data.get("report_reason") or "other"
    await _drop_prompt(bot, chat_id, state)
    if not target_id:
        await _back_to_browsing(state)
        return

    report_id = await mod_repo.add_report(user["id"], target_id, reason, comment)

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
        await safe_send(bot, admin_id, header)
        if target is not None:
            try:
                await profile_service.send_card(
                    bot, admin_id, target, admin_view=True, show_distance=False,
                    markup=kb.report_actions(report_id, target_id),
                )
            except Exception:
                await safe_send(bot, admin_id, "Анкета недоступна.",
                                kb.report_actions(report_id, target_id))

    # Жаловались из ленты — она продолжается; из уведомления — возвращаем в меню
    notice = f"<i>{texts.REPORT_SENT}</i>"
    if data.get("current") == target_id:
        await state.set_state(None)
        await browse.show_next(bot, chat_id, state, user, settings, notice=notice)
    else:
        await menu_handlers.show_menu(bot, chat_id, state, user, is_admin, note=notice)


@router.message(Report.comment)
async def comment_hint(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
