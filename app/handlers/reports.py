"""Жалобы на анкеты. Видит их только администратор, автор жалобы не раскрывается."""
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
from app.keyboards import inline as kb
from app.services import profile as profile_service
from app.services.notify import safe_send
from app.states import Report

router = Router(name="reports")


@router.callback_query(F.data.startswith("br:report:"))
async def ask_reason(call: CallbackQuery, state: FSMContext, user) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    if await mod_repo.already_reported(user["id"], target_id):
        await call.answer(texts.REPORT_DUPLICATE, show_alert=True)
        return
    await state.set_state(Report.reason)
    await call.answer()
    await call.message.answer(
        texts.REPORT_ASK,
        reply_markup=kb.report_reasons(texts.REPORT_REASONS, target_id),
    )


@router.callback_query(F.data == "rep:cancel")
async def cancel(call: CallbackQuery, state: FSMContext, bot: Bot, user,
                 settings: Settings) -> None:
    await call.answer(texts.CANCELLED)
    try:
        await call.message.delete()
    except Exception:
        pass
    await browse.show_next(bot, call.message.chat.id, state, user, settings)


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
                               user, settings: Settings) -> None:
    await call.answer()
    await _submit(call.message, state, bot, user, settings, comment=None)


@router.message(Report.comment, F.text)
async def send_with_comment(message: Message, state: FSMContext, bot: Bot,
                            user, settings: Settings) -> None:
    await _submit(message, state, bot, user, settings,
                  comment=(message.text or "")[:500])


async def _submit(message: Message, state: FSMContext, bot: Bot,
                  user: Mapping[str, Any], settings: Settings,
                  comment: str | None) -> None:
    data = await state.get_data()
    target_id = int(data.get("report_target") or 0)
    reason = data.get("report_reason") or "other"
    if not target_id:
        await state.clear()
        await message.answer(texts.CANCELLED)
        return

    report_id = await mod_repo.add_report(user["id"], target_id, reason, comment)
    await message.answer(texts.REPORT_SENT)

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

    # Возвращаем человека в ленту — жалоба не должна прерывать просмотр
    await state.set_state(None)
    await browse.show_next(bot, message.chat.id, state, user, settings)


@router.message(Report.comment)
async def comment_hint(message: Message) -> None:
    await message.answer(texts.REPORT_COMMENT, reply_markup=kb.REPORT_SKIP_COMMENT)
