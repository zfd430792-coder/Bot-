"""Модерация: баны, жалобы, верификация."""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import get_settings
from app.db import moderation as mod_repo
from app.db import users as users_repo
from app.handlers import verification as verification_handlers
from app.handlers.admin.filters import IsStaff
from app.handlers.admin.panel import send_user_card
from app.keyboards import inline as kb
from app.services import profile as profile_service
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

router = Router(name="staff-moderation")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

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
    )
    await admin_log(
        bot,
        f"🚫 <b>Бан</b>: <code>{target['id']}</code> @{target['username'] or '—'}\n"
        f"Причина: {profile_service.esc(reason)}\n"
        f"Срок: {until or 'бессрочно'}\nАдмин: <code>{admin_id}</code>"
    )
    return f"🚫 Забанен: <code>{target['id']}</code>\nПричина: {profile_service.esc(reason)}" \
           + (f"\nДо: {until} (UTC)" if until else "\nСрок: бессрочно")


# ──────────────────────────────── Бан ───────────────────────────────────────

@router.callback_query(F.data == "adm:ban")
async def ask_ban_user(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.ban_user)
    await call.answer()
    await call.message.answer("Кого банить? Пришлите ID или @username:")


@router.message(AdminPanel.ban_user, F.text)
async def ban_pick_user(message: Message, state: FSMContext) -> None:
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    await state.update_data(ban_target=target["id"])
    await state.set_state(AdminPanel.ban_reason)
    await message.answer(
        f"Причина бана для <code>{target['id']}</code>?\n\n"
        "Можно указать срок в начале: <code>7d спам</code> или <code>12h реклама</code>.\n"
        "Без срока — бессрочно."
    )


@router.callback_query(F.data.startswith("adm:ban_id:"))
async def ban_from_card(call: CallbackQuery, state: FSMContext) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await state.update_data(ban_target=target_id)
    await state.set_state(AdminPanel.ban_reason)
    await call.answer()
    await call.message.answer(
        f"Причина бана для <code>{target_id}</code>?\n\n"
        "Срок можно указать в начале: <code>7d спам</code>."
    )


@router.message(AdminPanel.ban_reason, F.text)
async def ban_apply(message: Message, state: FSMContext, bot: Bot,
                    is_admin: bool) -> None:
    data = await state.get_data()
    target_id = int(data.get("ban_target") or 0)
    target = await users_repo.get_user(target_id)
    await state.set_state(AdminPanel.menu)
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    result = await do_ban(bot, message.from_user.id, target, message.text or "",
                          by_admin=is_admin)
    await mod_repo.close_reports_for(target_id, message.from_user.id)
    await message.answer(result)


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
    result = await do_ban(bot, message.from_user.id, target, reason,
                          by_admin=is_admin)
    await mod_repo.close_reports_for(target["id"], message.from_user.id)
    await message.answer(result)


# ─────────────────────────────── Разбан ─────────────────────────────────────

@router.callback_query(F.data == "adm:unban")
async def ask_unban(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.unban_user)
    await call.answer()
    await call.message.answer("Кого разбанить? Пришлите ID или @username:")


@router.message(AdminPanel.unban_user, F.text)
async def unban_apply(message: Message, state: FSMContext, bot: Bot) -> None:
    target = await users_repo.find_user(message.text or "")
    await state.set_state(AdminPanel.menu)
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    await _unban(bot, message.from_user.id, target["id"])
    await message.answer(f"✅ Разбанен: <code>{target['id']}</code>")


@router.callback_query(F.data.startswith("adm:unban_id:"))
async def unban_from_card(call: CallbackQuery, bot: Bot) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await _unban(bot, call.from_user.id, target_id)
    await call.answer("Разбанен")
    target = await users_repo.get_user(target_id)
    if target:
        await send_user_card(call.message, target)


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
    await _unban(bot, message.from_user.id, target["id"])
    await message.answer(f"✅ Разбанен: <code>{target['id']}</code>")


async def _unban(bot: Bot, admin_id: int, target_id: int) -> None:
    await mod_repo.unban_user(target_id, admin_id)
    await safe_send(
        bot, target_id,
        "✅ <b>Блокировка снята</b>\n\nВы снова можете пользоваться ботом. "
        "Пожалуйста, соблюдайте правила.",
    )
    await admin_log(bot, f"✅ <b>Разбан</b>: <code>{target_id}</code> "
                         f"(админ <code>{admin_id}</code>)")


# ───────────────────────────── Верификация ──────────────────────────────────

@router.callback_query(F.data.startswith("adm:req_verify:"))
async def request_verify(call: CallbackQuery, bot: Bot) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    code = await verification_handlers.request_verification(
        bot, target_id, forced=True, admin_id=call.from_user.id
    )
    if code is None:
        await call.answer("Это владелец бота — проверки на него не действуют",
                          show_alert=True)
        return
    await call.answer("Запрос отправлен")
    await call.message.answer(
        f"✅ Пользователю <code>{target_id}</code> отправлено требование "
        f"верификации.\nКод на фото: <code>{code}</code>\n\n"
        "До подтверждения бот для него закрыт, анкета скрыта из поиска."
    )
    await admin_log(bot, f"✅ Запрошена верификация: <code>{target_id}</code> "
                         f"(админ <code>{call.from_user.id}</code>)")


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


@router.callback_query(F.data.startswith("adm:drop_verify:"))
async def drop_verify(call: CallbackQuery, bot: Bot) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await users_repo.update_user(target_id, verify_forced=0, verify_status="none",
                                 verify_code=None)
    await safe_send(bot, target_id,
                    "✅ Требование верификации снято. Можно пользоваться ботом.")
    await call.answer("Требование снято")
    target = await users_repo.get_user(target_id)
    if target:
        await send_user_card(call.message, target)


@router.callback_query(F.data.startswith("adm:unverify:"))
async def remove_verified(call: CallbackQuery, bot: Bot) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await users_repo.update_user(target_id, verify_status="none", verified_at=None)
    await call.answer("Галочка снята")
    await admin_log(bot, f"❎ Снята верификация: <code>{target_id}</code>")
    target = await users_repo.get_user(target_id)
    if target:
        await send_user_card(call.message, target)


@router.callback_query(F.data == "adm:verify")
async def list_verifications(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    rows = await mod_repo.pending_verifications()
    if not rows:
        await call.message.edit_text("✅ Заявок на верификацию нет.",
                                     reply_markup=kb.ADMIN_BACK)
        return
    await call.message.edit_text(
        f"✅ Заявок на проверку: <b>{len(rows)}</b>. Показываю по одной.",
        reply_markup=kb.ADMIN_BACK,
    )
    for row in rows[:10]:
        header = (
            f"✅ Заявка #{row['id']}\n"
            f"<b>{profile_service.esc(row['name'] or '—')}</b> "
            f"<code>{row['user_id']}</code> @{row['username'] or '—'}\n"
            f"Код должен быть: <code>{row['code']}</code>"
        )
        await safe_send(bot, call.from_user.id, header)
        try:
            if row["media_type"] == "photo":
                await bot.send_photo(call.from_user.id, row["media_id"],
                                     reply_markup=kb.verify_review(row["id"]))
            elif row["media_type"] == "video":
                await bot.send_video(call.from_user.id, row["media_id"],
                                     reply_markup=kb.verify_review(row["id"]))
            else:
                await bot.send_video_note(call.from_user.id, row["media_id"])
                await bot.send_message(call.from_user.id, "Решение:",
                                       reply_markup=kb.verify_review(row["id"]))
        except Exception:
            await safe_send(bot, call.from_user.id, "Медиа недоступно.",
                            kb.verify_review(row["id"]))


@router.callback_query(F.data.startswith("vrf:ok:"))
async def approve_verification(call: CallbackQuery, bot: Bot) -> None:
    verification_id = int((call.data or "0").split(":")[-1])
    record = await mod_repo.get_verification(verification_id)
    if record is None or record["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    await mod_repo.review_verification(verification_id, call.from_user.id, True)
    await users_repo.mark_verified(record["user_id"])

    await safe_send(bot, record["user_id"], texts.VERIFY_APPROVED)
    await call.answer("Подтверждено")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(f"✅ Верификация #{verification_id} подтверждена.")
    await admin_log(bot, f"✅ Верификация подтверждена: <code>{record['user_id']}</code> "
                         f"(админ <code>{call.from_user.id}</code>)")


@router.callback_query(F.data.startswith("vrf:no:"))
async def reject_verification(call: CallbackQuery, state: FSMContext) -> None:
    verification_id = int((call.data or "0").split(":")[-1])
    await state.set_state(AdminPanel.verify_reject_reason)
    await state.update_data(verify_id=verification_id)
    await call.answer()
    await call.message.answer(
        "Почему отклоняем? Напишите причину — пользователь её увидит.\n"
        "Например: <i>кода не видно на фото</i>"
    )


@router.message(AdminPanel.verify_reject_reason, F.text)
async def reject_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    verification_id = int(data.get("verify_id") or 0)
    record = await mod_repo.get_verification(verification_id)
    await state.set_state(AdminPanel.menu)
    if record is None:
        await message.answer("Заявка не найдена.")
        return

    reason = (message.text or "").strip()
    await mod_repo.review_verification(verification_id, message.from_user.id, False, reason)
    await users_repo.update_user(record["user_id"], verify_status="rejected")
    await safe_send(bot, record["user_id"], texts.VERIFY_REJECTED.format(
        reason=profile_service.esc(reason)), kb.VERIFY_START)
    await message.answer(f"❌ Заявка #{verification_id} отклонена.")


@router.callback_query(F.data.startswith("vrf:ban:"))
async def ban_from_verification(call: CallbackQuery, state: FSMContext) -> None:
    verification_id = int((call.data or "0").split(":")[-1])
    record = await mod_repo.get_verification(verification_id)
    if record is None:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    await state.update_data(ban_target=record["user_id"])
    await state.set_state(AdminPanel.ban_reason)
    await call.answer()
    await call.message.answer(
        f"Причина бана для <code>{record['user_id']}</code>? "
        "Срок можно указать в начале: <code>7d фейк</code>."
    )


# ──────────────────────────────── Жалобы ────────────────────────────────────

@router.callback_query(F.data == "adm:reports")
async def list_reports(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    rows = await mod_repo.open_reports()
    if not rows:
        await call.message.edit_text("🚨 Открытых жалоб нет.", reply_markup=kb.ADMIN_BACK)
        return

    await call.message.edit_text(
        f"🚨 Открытых жалоб: <b>{len(rows)}</b>", reply_markup=kb.ADMIN_BACK
    )
    for row in rows[:10]:
        target = await users_repo.get_user(row["target_id"])
        header = (
            f"🚨 <b>Жалоба #{row['id']}</b> от {row['created_at']}\n"
            f"Причина: {texts.REPORT_REASONS.get(row['reason'], row['reason'])}\n"
            f"Комментарий: {profile_service.esc(row['comment']) if row['comment'] else '—'}\n"
            f"На: <code>{row['target_id']}</code> @{row['target_username'] or '—'}"
        )
        await safe_send(bot, call.from_user.id, header)
        if target is not None:
            await profile_service.send_card(
                bot, call.from_user.id, target, admin_view=True, show_distance=False,
                markup=kb.report_actions(row["id"], row["target_id"]),
            )


@router.callback_query(F.data.startswith("rp:ban:"))
async def report_ban(call: CallbackQuery, state: FSMContext) -> None:
    report_id = int((call.data or "0").split(":")[-1])
    record = await mod_repo.get_report(report_id)
    if record is None:
        await call.answer("Жалоба не найдена", show_alert=True)
        return
    await state.update_data(ban_target=record["target_id"])
    await state.set_state(AdminPanel.ban_reason)
    await call.answer()
    await call.message.answer(
        f"Причина бана для <code>{record['target_id']}</code>? "
        "Срок можно указать в начале: <code>30d мошенничество</code>."
    )


@router.callback_query(F.data.startswith("rp:verify:"))
async def report_request_verify(call: CallbackQuery, bot: Bot) -> None:
    report_id = int((call.data or "0").split(":")[-1])
    record = await mod_repo.get_report(report_id)
    if record is None:
        await call.answer("Жалоба не найдена", show_alert=True)
        return
    code = await verification_handlers.request_verification(
        bot, record["target_id"], forced=True, admin_id=call.from_user.id
    )
    if code is None:
        await call.answer("Это владелец бота — проверки на него не действуют",
                          show_alert=True)
        return
    await mod_repo.close_report(report_id, call.from_user.id, "done")
    await call.answer("Верификация запрошена")
    await call.message.answer(
        f"✅ По жалобе #{report_id} запрошена верификация "
        f"<code>{record['target_id']}</code>. Код: <code>{code}</code>"
    )


@router.callback_query(F.data.startswith("rp:skip:"))
async def report_decline(call: CallbackQuery) -> None:
    report_id = int((call.data or "0").split(":")[-1])
    await mod_repo.close_report(report_id, call.from_user.id, "declined")
    await call.answer("Жалоба отклонена")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
