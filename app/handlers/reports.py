"""Жалобы на анкеты. Видит их только администратор, автор жалобы не раскрывается.

Вопрос «на что жалуемся» появляется под карточкой с причинами на нижних
кнопках; после отправки лента продолжается, а подтверждение видно строкой
над следующей анкетой. «⬅️ Отмена» возвращает ту же анкету.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import Settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import browse
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import safe_send
from app.states import Report

router = Router(name="reports")

REASON_BY_TITLE = {title: key for key, title in texts.REPORT_REASONS.items()}


async def _prompt(bot: Bot, chat_id: int, state: FSMContext, text: str, markup) -> None:
    """Вопрос жалобы под карточкой; прежний вопрос уходит."""
    data = await state.get_data()
    old = data.get("report_prompt")
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await screen.add(state, [sent.message_id])
    if old:
        await profile_service.delete_messages(bot, chat_id, [old])
        await screen.forget(state, [old])
    await state.update_data(report_prompt=sent.message_id)


@router.message(F.text == rkb.REPORT)
async def ask_reason(message: Message, state: FSMContext, bot: Bot, user,
                     settings: Settings) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    target_id = int((await state.get_data()).get("current") or 0)
    if not target_id:
        await browse.open_feed(bot, chat_id, state, user, settings)
        return
    if await mod_repo.already_reported(user["id"], target_id):
        await browse.show_current(bot, chat_id, state, user, settings,
                                  notice=f"<i>{texts.REPORT_DUPLICATE}</i>")
        return
    await state.set_state(Report.reason)
    await state.update_data(report_target=target_id, report_prompt=None)
    await _prompt(bot, chat_id, state, texts.REPORT_ASK,
                  rkb.report_reasons(texts.REPORT_REASONS))


@router.message(Report.reason, F.text == rkb.CANCEL)
@router.message(Report.comment, F.text == rkb.CANCEL)
async def cancel(message: Message, state: FSMContext, bot: Bot, user,
                 settings: Settings) -> None:
    """Передумали жаловаться — анкета снова на экране с кнопками ленты."""
    await screen.drop(message)
    await state.update_data(report_prompt=None)
    await browse.show_current(bot, message.chat.id, state, user, settings)


@router.message(Report.reason)
async def pick_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    reason = REASON_BY_TITLE.get(message.text or "")
    if reason is None:
        return      # причина выбирается кнопкой; остальное просто убираем
    await state.update_data(report_reason=reason)
    await state.set_state(Report.comment)
    await _prompt(bot, message.chat.id, state,
                  texts.REPORT_COMMENT.format(reason=texts.REPORT_REASONS[reason]),
                  rkb.REPORT_COMMENT)


@router.message(Report.comment, F.text == rkb.NO_COMMENT)
async def send_without_comment(message: Message, state: FSMContext, bot: Bot,
                               user, settings: Settings) -> None:
    await screen.drop(message)
    await _submit(bot, message.chat.id, state, user, settings, comment=None)


@router.message(Report.comment, F.text)
async def send_with_comment(message: Message, state: FSMContext, bot: Bot,
                            user, settings: Settings) -> None:
    await screen.drop(message)
    await _submit(bot, message.chat.id, state, user, settings,
                  comment=(message.text or "")[:500])


@router.message(Report.comment)
async def comment_hint(message: Message) -> None:
    await screen.drop(message)


async def _submit(bot: Bot, chat_id: int, state: FSMContext,
                  user: Mapping[str, Any], settings: Settings,
                  comment: str | None) -> None:
    data = await state.get_data()
    target_id = int(data.get("report_target") or 0)
    reason = data.get("report_reason") or "other"
    await state.update_data(report_prompt=None)
    if not target_id:
        await browse.show_current(bot, chat_id, state, user, settings)
        return

    report_id = await mod_repo.add_report(user["id"], target_id, reason, comment)

    target = await users_repo.get_user(target_id)
    total = target["reports_count"] if target else 0
    header = (
        f"🚨 <b>Жалоба #{report_id}</b>\n\n"
        f"Причина: <b>{texts.REPORT_REASONS.get(reason, reason)}</b>\n"
        f"Комментарий: {profile_service.esc(comment) if comment else '—'}\n"
        f"От: <code>{user['id']}</code> @{user['username'] or '—'}\n"
        f"Всего жалоб на анкету: <b>{total}</b>\n\n"
        "<i>Разобрать: «🛠 Админ-панель» → «🚨 Жалобы».</i>"
    )
    for admin_id in settings.admin_ids:
        await safe_send(bot, admin_id, header)
        if target is not None:
            try:
                await profile_service.send_card(bot, admin_id, target, admin_view=True,
                                                show_distance=False)
            except Exception:
                await safe_send(bot, admin_id, "Анкета недоступна.")

    # Лента продолжается, подтверждение — строкой над следующей анкетой
    await state.set_state(None)
    await browse.show_next(bot, chat_id, state, user, settings,
                           notice=f"<i>{texts.REPORT_SENT}</i>")
