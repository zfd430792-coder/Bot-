"""Модерация: баны, жалобы, верификация.

Жалобы и заявки на верификацию разбираются по одной: на экране одна жалоба
с карточкой нарушителя (или одна заявка с фото) и нижние кнопки решения.
«⏭ Дальше» откладывает текущую до следующего захода в раздел.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import get_settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import verification as verification_handlers
from app.handlers.admin import panel
from app.handlers.admin.filters import IsStaff
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

router = Router(name="staff-moderation")
router.message.filter(IsStaff())

DURATION_RE = re.compile(r"^\s*(\d{1,3})\s*([dhдч])\s+(.*)$", re.I)


def parse_reason(raw: str) -> tuple[str, str | None]:
    """«7d спам» -> (причина, срок для SQL). Без префикса бан бессрочный."""
    match = DURATION_RE.match(raw or "")
    if not match:
        return (raw or "").strip() or "нарушение правил", None
    amount, unit, reason = match.groups()
    hours = int(amount) * (24 if unit.lower() in "dд" else 1)
    return reason.strip() or "нарушение правил", f"+{hours} hours"


async def do_ban(bot: Bot, admin_id: int, target: Mapping[str, Any],
                 raw_reason: str, by_admin: bool = True) -> str:
    """Банит пользователя. Персонал защищён: модератор не трогает своих."""
    if get_settings().is_admin(target["id"]):
        return "🛡 Это владелец бота — забанить его нельзя."
    if target["is_moderator"] and not by_admin:
        return ("🛡 Это модератор. Снять его может только владелец "
                "через «👮 Модераторы».")

    reason, offset = parse_reason(raw_reason)
    until = await users_repo.ban_until(offset) if offset else None

    await mod_repo.ban_user(target["id"], admin_id, reason, until)
    await safe_send(
        bot, target["id"],
        texts.BANNED.format(
            reason=reason,
            until=f"\n<b>Действует до:</b> {until} (UTC)" if until else "",
        ),
        rkb.REMOVE,
    )
    await admin_log(
        bot,
        f"🚫 <b>Бан</b>: <code>{target['id']}</code> @{target['username'] or '—'}\n"
        f"Причина: {profile_service.esc(reason)}\n"
        f"Срок: {until or 'бессрочно'}\nАдмин: <code>{admin_id}</code>"
    )
    return f"🚫 Забанен: <code>{target['id']}</code>\nПричина: {profile_service.esc(reason)}" \
           + (f"\nДо: {until} (UTC)" if until else "\nСрок: бессрочно")


# ──────────────────────────────── Жалобы ────────────────────────────────────

async def show_report(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                      notice: str | None = None) -> None:
    """Следующая открытая жалоба (кроме отложенных «Дальше») — или панель."""
    data = await state.get_data()
    skipped = set(data.get("report_skip") or [])
    rows = [r for r in await mod_repo.open_reports(limit=50) if r["id"] not in skipped]
    if not rows:
        await panel.open_panel(bot, chat_id, state, is_admin,
                               notice=(f"{notice}\n" if notice else "")
                               + "🚨 <i>Открытых жалоб больше нет</i>")
        return

    row = rows[0]
    await state.set_state(AdminPanel.report_view)
    await state.update_data(report_id=row["id"], report_target=row["target_id"],
                            report_skip=sorted(skipped))
    header = (
        (f"{notice}\n\n" if notice else "")
        + f"🚨 <b>Жалоба #{row['id']}</b> · осталось: {len(rows)}\n"
        f"Причина: {texts.REPORT_REASONS.get(row['reason'], row['reason'])}\n"
        f"Комментарий: {profile_service.esc(row['comment']) if row['comment'] else '—'}\n"
        f"От: {row['created_at']}"
    )
    target = await users_repo.get_user(row["target_id"])
    if target is None:
        await screen.send(bot, chat_id, state, header + "\n\n<i>Анкета недоступна.</i>",
                          rkb.REPORT_VIEW)
        return
    message_ids = await profile_service.send_card(
        bot, chat_id, target, admin_view=True, show_distance=False,
        header=header, markup=rkb.REPORT_VIEW,
    )
    await screen.replace(bot, chat_id, state, message_ids)


@router.message(F.text.startswith(rkb.A_REPORTS))
async def list_reports(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    await state.update_data(report_skip=[])
    await show_report(message.bot, message.chat.id, state, is_admin)


@router.message(AdminPanel.report_view, F.text == rkb.A_BAN)
async def report_ban(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target_id = int((await state.get_data()).get("report_target") or 0)
    await panel.ask_ban_reason(message.bot, message.chat.id, state, target_id, "report")


@router.message(AdminPanel.report_view, F.text == rkb.A_REQ_VERIFY)
async def report_request_verify(message: Message, state: FSMContext, bot: Bot,
                                is_admin: bool) -> None:
    await screen.drop(message)
    data = await state.get_data()
    report_id, target_id = int(data.get("report_id") or 0), int(data.get("report_target") or 0)
    code = await verification_handlers.request_verification(
        bot, target_id, forced=True, admin_id=message.from_user.id)
    if code is None:
        notice = "🛡 <i>Это владелец бота — проверки на него не действуют</i>"
    else:
        await mod_repo.close_report(report_id, message.from_user.id, "done")
        notice = (f"✅ <i>По жалобе #{report_id} запрошена верификация "
                  f"<code>{target_id}</code>, код: <code>{code}</code></i>")
    await show_report(bot, message.chat.id, state, is_admin, notice)


@router.message(AdminPanel.report_view, F.text == rkb.A_DECLINE)
async def report_decline(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    report_id = int((await state.get_data()).get("report_id") or 0)
    await mod_repo.close_report(report_id, message.from_user.id, "declined")
    await show_report(message.bot, message.chat.id, state, is_admin,
                      f"👌 <i>Жалоба #{report_id} отклонена</i>")


@router.message(AdminPanel.report_view, F.text == rkb.A_NEXT)
async def report_next(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    data = await state.get_data()
    skipped = set(data.get("report_skip") or []) | {int(data.get("report_id") or 0)}
    await state.update_data(report_skip=sorted(skipped))
    await show_report(message.bot, message.chat.id, state, is_admin)


# ───────────────────────────── Верификация ──────────────────────────────────

async def show_verification(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                            notice: str | None = None) -> None:
    """Следующая заявка с фото (кроме отложенных) — или панель."""
    data = await state.get_data()
    skipped = set(data.get("verify_skip") or [])
    rows = [r for r in await mod_repo.pending_verifications(limit=50)
            if r["id"] not in skipped]
    if not rows:
        await panel.open_panel(bot, chat_id, state, is_admin,
                               notice=(f"{notice}\n" if notice else "")
                               + "✅ <i>Заявок на верификацию больше нет</i>")
        return

    row = rows[0]
    await state.set_state(AdminPanel.verify_view)
    await state.update_data(verify_id=row["id"], verify_user=row["user_id"],
                            verify_skip=sorted(skipped))
    caption = (
        (f"{notice}\n\n" if notice else "")
        + f"✅ <b>Заявка #{row['id']}</b> · осталось: {len(rows)}\n"
        f"<b>{profile_service.esc(row['name'] or '—')}</b> "
        f"<code>{row['user_id']}</code> @{row['username'] or '—'}\n"
        f"Код на фото должен быть: <code>{row['code']}</code>\n"
        f"Тип: {'запрошена админом' if row['forced'] else 'по своей инициативе'}"
    )
    sent_ids: list[int] = []
    try:
        if row["media_type"] == "photo":
            sent = await bot.send_photo(chat_id, row["media_id"], caption=caption,
                                        reply_markup=rkb.VERIFY_VIEW)
        elif row["media_type"] == "video":
            sent = await bot.send_video(chat_id, row["media_id"], caption=caption,
                                        reply_markup=rkb.VERIFY_VIEW)
        else:
            circle = await bot.send_video_note(chat_id, row["media_id"])
            sent_ids.append(circle.message_id)
            sent = await bot.send_message(chat_id, caption, reply_markup=rkb.VERIFY_VIEW)
    except Exception:
        sent = await bot.send_message(chat_id, caption + "\n\n<i>Медиа недоступно.</i>",
                                      reply_markup=rkb.VERIFY_VIEW)
    await screen.replace(bot, chat_id, state, sent_ids + [sent.message_id])


@router.message(F.text.startswith(rkb.A_VERIFY))
async def list_verifications(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    await state.update_data(verify_skip=[])
    await show_verification(message.bot, message.chat.id, state, is_admin)


@router.message(AdminPanel.verify_view, F.text == rkb.A_APPROVE)
async def approve_verification(message: Message, state: FSMContext, bot: Bot,
                               is_admin: bool) -> None:
    await screen.drop(message)
    verification_id = int((await state.get_data()).get("verify_id") or 0)
    record = await mod_repo.get_verification(verification_id)
    if record is None or record["status"] != "pending":
        await show_verification(bot, message.chat.id, state, is_admin,
                                "<i>Заявка уже обработана</i>")
        return

    await mod_repo.review_verification(verification_id, message.from_user.id, True)
    await users_repo.mark_verified(record["user_id"])
    await safe_send(bot, record["user_id"], texts.VERIFY_APPROVED)
    await admin_log(bot, f"✅ Верификация подтверждена: <code>{record['user_id']}</code> "
                         f"(админ <code>{message.from_user.id}</code>)")
    await show_verification(bot, message.chat.id, state, is_admin,
                            f"✅ <i>Заявка #{verification_id} подтверждена</i>")


@router.message(AdminPanel.verify_view, F.text == rkb.A_REJECT)
async def reject_verification(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(AdminPanel.verify_reject_reason)
    await screen.send(message.bot, message.chat.id, state,
                      "Почему отклоняем? Напишите причину — пользователь её увидит.\n"
                      "Например: <i>кода не видно на фото</i>",
                      rkb.CANCEL_ONLY)


@router.message(AdminPanel.verify_reject_reason, F.text)
async def reject_reason(message: Message, state: FSMContext, bot: Bot,
                        is_admin: bool) -> None:
    await screen.drop(message)
    chat_id = message.chat.id
    if message.text == rkb.CANCEL:
        await show_verification(bot, chat_id, state, is_admin)
        return
    verification_id = int((await state.get_data()).get("verify_id") or 0)
    record = await mod_repo.get_verification(verification_id)
    if record is None:
        await show_verification(bot, chat_id, state, is_admin, "<i>Заявка не найдена</i>")
        return

    reason = (message.text or "").strip()
    await mod_repo.review_verification(verification_id, message.from_user.id, False, reason)
    await users_repo.update_user(record["user_id"], verify_status="rejected")
    if record["forced"]:
        await safe_send(bot, record["user_id"], texts.VERIFY_REJECTED_FORCED.format(
            reason=profile_service.esc(reason)), rkb.VERIFY_REQUIRED)
    else:
        await safe_send(bot, record["user_id"], texts.VERIFY_REJECTED.format(
            reason=profile_service.esc(reason)))
    await show_verification(bot, chat_id, state, is_admin,
                            f"❌ <i>Заявка #{verification_id} отклонена</i>")


@router.message(AdminPanel.verify_view, F.text == rkb.A_BAN)
async def verification_ban(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    user_id = int((await state.get_data()).get("verify_user") or 0)
    await panel.ask_ban_reason(message.bot, message.chat.id, state, user_id, "verify")


@router.message(AdminPanel.verify_view, F.text == rkb.A_NEXT)
async def verification_next(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    data = await state.get_data()
    skipped = set(data.get("verify_skip") or []) | {int(data.get("verify_id") or 0)}
    await state.update_data(verify_skip=sorted(skipped))
    await show_verification(message.bot, message.chat.id, state, is_admin)


# ──────────────────────────────── Бан ───────────────────────────────────────

@router.message(AdminPanel.ban_reason, F.text)
async def ban_apply(message: Message, state: FSMContext, bot: Bot,
                    is_admin: bool) -> None:
    """Причина бана — откуда бы ни пришли: из панели, карточки, жалобы или заявки."""
    await screen.drop(message)
    chat_id = message.chat.id
    data = await state.get_data()
    target = await users_repo.get_user(int(data.get("ban_target") or 0))
    back = data.get("ban_back") or "panel"
    if target is None:
        await panel.open_panel(bot, chat_id, state, is_admin, "<i>Пользователь не найден</i>")
        return

    result = await do_ban(bot, message.from_user.id, target, message.text or "",
                          by_admin=is_admin)
    await mod_repo.close_reports_for(target["id"], message.from_user.id)
    if back == "card":
        await panel.show_user_card(bot, chat_id, state,
                                   await users_repo.get_user(target["id"]), notice=result)
    elif back == "report":
        await show_report(bot, chat_id, state, is_admin, result)
    elif back == "verify":
        await show_verification(bot, chat_id, state, is_admin, result)
    else:
        await panel.open_panel(bot, chat_id, state, is_admin, result)


async def _ask_target(message: Message, state: FSMContext, new_state, question: str,
                      error: str | None = None) -> None:
    await state.set_state(new_state)
    await screen.send(message.bot, message.chat.id, state,
                      f"⚠️ {error}\n\n{question}" if error else question, rkb.ADMIN_BACK)


@router.message(F.text == rkb.A_BAN)
async def ask_ban_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask_target(message, state, AdminPanel.ban_user,
                      "🚫 Кого банить? Пришлите ID или @username:")


@router.message(AdminPanel.ban_user, F.text)
async def ban_pick_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_target(message, state, AdminPanel.ban_user,
                          "🚫 Кого банить? Пришлите ID или @username:",
                          "Пользователь не найден.")
        return
    await panel.ask_ban_reason(message.bot, message.chat.id, state, target["id"], "panel")


@router.message(F.text == rkb.A_UNBAN)
async def ask_unban(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask_target(message, state, AdminPanel.unban_user,
                      "✅ Кого разбанить? Пришлите ID или @username:")


@router.message(AdminPanel.unban_user, F.text)
async def unban_apply(message: Message, state: FSMContext, bot: Bot,
                      is_admin: bool) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_target(message, state, AdminPanel.unban_user,
                          "✅ Кого разбанить? Пришлите ID или @username:",
                          "Пользователь не найден.")
        return
    await panel.unban(bot, message.from_user.id, target["id"])
    await panel.open_panel(bot, message.chat.id, state, is_admin,
                           f"✅ <i>Разбанен: <code>{target['id']}</code></i>")


# ─────────────────────────── Быстрые команды ────────────────────────────────

@router.message(Command("ban"))
async def ban_command(message: Message, bot: Bot, is_admin: bool) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: <code>/ban 123456789 причина</code>")
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    reason = parts[2] if len(parts) > 2 else "нарушение правил"
    result = await do_ban(bot, message.from_user.id, target, reason, by_admin=is_admin)
    await mod_repo.close_reports_for(target["id"], message.from_user.id)
    await message.answer(result)


@router.message(Command("unban"))
async def unban_command(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/unban 123456789</code>")
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    await panel.unban(bot, message.from_user.id, target["id"])
    await message.answer(f"✅ Разбанен: <code>{target['id']}</code>")


@router.message(Command("verify"))
async def verify_command(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/verify 123456789</code>")
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    code = await verification_handlers.request_verification(
        bot, target["id"], forced=True, admin_id=message.from_user.id
    )
    if code is None:
        await message.answer("Это владелец бота — проверки на него не действуют.")
        return
    await message.answer(
        f"✅ Требование отправлено. Код: <code>{code}</code>\n"
        "Пока пользователь не пройдёт проверку, бот для него закрыт."
    )
