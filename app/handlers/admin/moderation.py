"""Модерация: баны, жалобы, верификация.

Жалобы и заявки на верификацию разбираются по одной: на экране одна жалоба
с карточкой нарушителя (или одна заявка: анкета, кружок и задание) и
inline-кнопки решения.
Кнопки несут номер жалобы или заявки, поэтому решение относится ровно к
той, под которой нажато. «⏭ Дальше» откладывает текущую до следующего
захода в раздел.
"""
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
from app.handlers.admin import panel
from app.handlers.admin.filters import IsStaff
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log, appeal_contact, safe_send
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
            contact=await appeal_contact(),
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


def _parts(call: CallbackQuery) -> tuple[str, int]:
    """adm:<раздел>:<действие>:<номер> -> (действие, номер)."""
    parts = (call.data or "").split(":")
    number = parts[3] if len(parts) > 3 else ""
    return parts[2] if len(parts) > 2 else "", int(number) if number.isdigit() else 0


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
    await state.update_data(report_skip=sorted(skipped))
    header = (
        (f"{notice}\n\n" if notice else "")
        + f"🚨 <b>Жалоба #{row['id']}</b> · осталось: {len(rows)}\n"
        f"Причина: {texts.REPORT_REASONS.get(row['reason'], row['reason'])}\n"
        f"Комментарий: {profile_service.esc(row['comment']) if row['comment'] else '—'}\n"
        f"От: {row['created_at']}"
    )
    markup = kb.report_view(int(row["id"]))
    target = await users_repo.get_user(row["target_id"])
    if target is None:
        await screen.show(bot, chat_id, state, header + "\n\n<i>Анкета недоступна.</i>",
                          markup)
        return
    await screen.prepare(bot, chat_id, state)
    message_ids = await profile_service.send_card(
        bot, chat_id, target, admin_view=True, show_distance=False,
        header=header, markup=markup,
    )
    await screen.remember(state, message_ids)


@router.callback_query(F.data.in_({"adm:reports", "n:reports"}))
async def list_reports(call: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    await call.answer()
    await state.update_data(report_skip=[])
    await show_report(call.bot, screen.chat_id(call), state, is_admin)


@router.callback_query(F.data.startswith("adm:rep:"))
async def report_action(call: CallbackQuery, state: FSMContext, bot: Bot,
                        is_admin: bool) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    action, report_id = _parts(call)
    report = await mod_repo.get_report(report_id)
    if report is None or report["status"] != "open":
        await show_report(bot, chat_id, state, is_admin, "<i>Эта жалоба уже разобрана</i>")
        return
    target_id = int(report["target_id"])
    admin_id = call.from_user.id

    if action == "ban":
        await panel.ask_ban_reason(bot, chat_id, state, target_id, "report")
        return
    if action == "next":
        skipped = set((await state.get_data()).get("report_skip") or []) | {report_id}
        await state.update_data(report_skip=sorted(skipped))
        await show_report(bot, chat_id, state, is_admin)
        return
    if action == "decline":
        await mod_repo.close_report(report_id, admin_id, "declined")
        await show_report(bot, chat_id, state, is_admin,
                          f"👌 <i>Жалоба #{report_id} отклонена</i>")
        return
    if action == "req":
        if not await verification_handlers.request_verification(
                bot, target_id, forced=True, admin_id=admin_id):
            notice = "🛡 <i>Это владелец бота — проверки на него не действуют</i>"
        else:
            await mod_repo.close_report(report_id, admin_id, "done")
            notice = (f"✅ <i>По жалобе #{report_id} запрошена верификация "
                      f"<code>{target_id}</code></i>")
        await show_report(bot, chat_id, state, is_admin, notice)


# ───────────────────────────── Верификация ──────────────────────────────────

async def show_verification(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                            notice: str | None = None) -> None:
    """Следующая заявка (кроме отложенных) — или панель. На экране подряд:
    анкета, чтобы сверить лицо, кружок и что в нём должно быть."""
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
    await state.update_data(verify_skip=sorted(skipped))
    caption = (
        (f"{notice}\n\n" if notice else "")
        + f"✅ <b>Заявка #{row['id']}</b> · осталось: {len(rows)}\n"
        f"<b>{profile_service.esc(row['name'] or '—')}</b> "
        f"<code>{row['user_id']}</code> @{row['username'] or '—'}\n"
        f"{verification_handlers.task_summary(row)}\n"
        f"Тип: {'запрошена админом' if row['forced'] else 'по своей инициативе'}\n\n"
        + ("☝️ Сверьте лицо в анкете и в кружке, код и действие." if row["action"]
           else "☝️ Сверьте лицо в анкете и на фото проверки, код на листе.")
    )
    markup = kb.verify_view(int(row["id"]))
    await screen.prepare(bot, chat_id, state)
    sent_ids: list[int] = []
    target = await users_repo.get_user(row["user_id"])
    if target is not None and target["registered"]:
        sent_ids += await profile_service.send_card(
            bot, chat_id, target, show_distance=False, header="👤 <b>Анкета</b>")
    try:
        # Фото и обычное видео — заявки прежней версии, до кружков
        if row["media_type"] == "photo":
            media = await bot.send_photo(chat_id, row["media_id"])
        elif row["media_type"] == "video":
            media = await bot.send_video(chat_id, row["media_id"])
        else:
            media = await bot.send_video_note(chat_id, row["media_id"])
        sent_ids.append(media.message_id)
    except Exception:
        caption += "\n\n<i>Медиа недоступно.</i>"
    sent = await bot.send_message(chat_id, caption, reply_markup=markup)
    await screen.remember(state, sent_ids + [sent.message_id])


@router.callback_query(F.data.in_({"adm:verify", "n:verify"}))
async def list_verifications(call: CallbackQuery, state: FSMContext,
                             is_admin: bool) -> None:
    await call.answer()
    await state.update_data(verify_skip=[])
    await show_verification(call.bot, screen.chat_id(call), state, is_admin)


@router.callback_query(F.data.startswith("adm:ver:"))
async def verification_action(call: CallbackQuery, state: FSMContext, bot: Bot,
                              is_admin: bool) -> None:
    await call.answer()
    chat_id = screen.chat_id(call)
    action, verification_id = _parts(call)
    record = await mod_repo.get_verification(verification_id)
    if record is None or record["status"] != "pending":
        await show_verification(bot, chat_id, state, is_admin,
                                "<i>Заявка уже обработана</i>")
        return
    user_id = int(record["user_id"])

    if action == "ok":
        await mod_repo.review_verification(verification_id, call.from_user.id, True)
        await users_repo.mark_verified(user_id)
        await safe_send(bot, user_id, texts.VERIFY_APPROVED)
        await admin_log(bot, f"✅ Верификация подтверждена: <code>{user_id}</code> "
                             f"(админ <code>{call.from_user.id}</code>)")
        await show_verification(bot, chat_id, state, is_admin,
                                f"✅ <i>Заявка #{verification_id} подтверждена</i>")
    elif action == "no":
        await state.set_state(AdminPanel.verify_reject_reason)
        await state.update_data(verify_id=verification_id)
        await screen.show(bot, chat_id, state,
                          "❌ <b>Почему отклоняем?</b>\n\nВыберите причину или "
                          "напишите свою — пользователь её увидит.",
                          kb.verify_reject(verification_id, texts.VERIFY_REJECT_REASONS))
    elif action == "ban":
        await panel.ask_ban_reason(bot, chat_id, state, user_id, "verify")
    elif action == "next":
        skipped = set((await state.get_data()).get("verify_skip") or []) | {verification_id}
        await state.update_data(verify_skip=sorted(skipped))
        await show_verification(bot, chat_id, state, is_admin)


async def _reject(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                  verification_id: int, admin_id: int, reason: str) -> None:
    record = await mod_repo.get_verification(verification_id)
    if record is None or record["status"] != "pending":
        await show_verification(bot, chat_id, state, is_admin, "<i>Заявка уже обработана</i>")
        return

    await mod_repo.review_verification(verification_id, admin_id, False, reason)
    await users_repo.update_user(record["user_id"], verify_status="rejected")
    if record["forced"]:
        await safe_send(bot, record["user_id"], texts.VERIFY_REJECTED_FORCED.format(
            reason=profile_service.esc(reason)), kb.VERIFY_REQUIRED)
    else:
        await safe_send(bot, record["user_id"], texts.VERIFY_REJECTED.format(
            reason=profile_service.esc(reason)))
    await show_verification(bot, chat_id, state, is_admin,
                            f"❌ <i>Заявка #{verification_id} отклонена</i>")


@router.callback_query(F.data.startswith("adm:vrj:"))
async def reject_ready_reason(call: CallbackQuery, state: FSMContext, bot: Bot,
                              is_admin: bool) -> None:
    """Готовая причина отказа: adm:vrj:<причина>:<номер заявки>."""
    await call.answer()
    key, verification_id = _parts(call)
    _, reason = texts.VERIFY_REJECT_REASONS.get(key, ("", "проверка не пройдена"))
    await _reject(bot, screen.chat_id(call), state, is_admin, verification_id,
                  call.from_user.id, reason)


@router.message(AdminPanel.verify_reject_reason, F.text)
async def reject_reason(message: Message, state: FSMContext, bot: Bot,
                        is_admin: bool) -> None:
    """Своя причина — одним сообщением."""
    await screen.drop(message)
    verification_id = int((await state.get_data()).get("verify_id") or 0)
    await _reject(bot, message.chat.id, state, is_admin, verification_id,
                  message.from_user.id, (message.text or "").strip())


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


async def _ask_target(bot: Bot, chat_id: int, state: FSMContext, new_state,
                      question: str, error: str | None = None) -> None:
    await state.set_state(new_state)
    await screen.show(bot, chat_id, state,
                      f"⚠️ {error}\n\n{question}" if error else question, kb.ADMIN_BACK)


BAN_WHO = "🚫 Кого банить? Пришлите ID или @username:"
UNBAN_WHO = "✅ Кого разбанить? Пришлите ID или @username:"


@router.callback_query(F.data == "adm:ban")
async def ask_ban_user(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _ask_target(call.bot, screen.chat_id(call), state, AdminPanel.ban_user, BAN_WHO)


@router.message(AdminPanel.ban_user, F.text)
async def ban_pick_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_target(message.bot, message.chat.id, state, AdminPanel.ban_user,
                          BAN_WHO, "Пользователь не найден.")
        return
    await panel.ask_ban_reason(message.bot, message.chat.id, state, target["id"], "panel")


@router.callback_query(F.data == "adm:unban")
async def ask_unban(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _ask_target(call.bot, screen.chat_id(call), state, AdminPanel.unban_user,
                      UNBAN_WHO)


@router.message(AdminPanel.unban_user, F.text)
async def unban_apply(message: Message, state: FSMContext, bot: Bot,
                      is_admin: bool) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_target(bot, message.chat.id, state, AdminPanel.unban_user,
                          UNBAN_WHO, "Пользователь не найден.")
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
    if not await verification_handlers.request_verification(
            bot, target["id"], forced=True, admin_id=message.from_user.id):
        await message.answer("Это владелец бота — проверки на него не действуют.")
        return
    await message.answer(
        "✅ Требование отправлено: пользователь запишет кружок с кодом.\n"
        "Пока он не пройдёт проверку, бот для него закрыт."
    )
