"""Админ-панель: вход, статистика, карточка пользователя, настройки бота.

Всё на нижних кнопках, как и у пользователей. Действия над конкретным
человеком (бан, верификация, сообщение) относятся к открытой карточке:
её id хранится в состоянии, поэтому кнопкам не нужно нести номер.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.db import moderation as mod_repo
from app.db import stats as stats_repo
from app.db import users as users_repo
from app.handlers import verification as verification_handlers
from app.handlers.admin.filters import IsAdmin, IsStaff
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

# Вход в панель и «⬅️ В админку» — раньше разделов с вводом текста, иначе
# кнопку «назад» принял бы за ответ, например, шаг рассылки
nav_router = Router(name="staff-nav")
nav_router.message.filter(IsStaff())

# Панель доступна всему персоналу
router = Router(name="staff-panel")
router.message.filter(IsStaff())

# Разделы, которые модератору недоступны
admin_router = Router(name="admin-panel")
admin_router.message.filter(IsAdmin())

# Только заголовок: всё остальное — на кнопках. Команды (/find, /ban, /say…)
# работают как прежде и видны в меню команд Telegram.
ADMIN_TITLE = "🛠 <b>Админ-панель</b>\n\nВыберите, что нужно:"
MOD_TITLE = "👮 <b>Панель модератора</b>\n\nВыберите, что нужно:"

BAN_REASON_HINT = (
    "Срок можно указать в начале: <code>7d спам</code> или <code>12h реклама</code>. "
    "Без срока — бессрочно."
)

SUPPORT_ASK = (
    "💬 <b>Контакт поддержки</b>\n\n"
    "Пришлите @username, куда людям писать с вопросами. Кнопка "
    "«💬 Поддержка» появится в меню у всех, а в сообщении о бане — этот контакт.\n\n"
    "<i>Убрать кнопку — отправьте «-».</i>"
)
# @name, name или ссылка t.me/name. Username в Telegram — от 5 символов
SUPPORT_RE = re.compile(
    r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?$"
)


async def open_panel(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                     notice: str | None = None) -> None:
    """Главный экран админки. notice — строка о том, что сейчас сделано."""
    await state.clear()
    await state.set_state(AdminPanel.menu)
    reports = await mod_repo.count_open_reports()
    verify = await mod_repo.count_pending_verifications()
    text = ADMIN_TITLE if is_admin else MOD_TITLE
    await screen.send(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      rkb.admin_menu(reports, verify, is_admin=is_admin))


@nav_router.message(Command("admin"))
@nav_router.message(Command("mod"))
@nav_router.message(F.text.in_({rkb.ADMIN, rkb.MODERATOR, rkb.A_BACK}))
async def admin_home(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    await open_panel(message.bot, message.chat.id, state, is_admin)


# ─────────────────────── Карточка пользователя ──────────────────────────────

async def show_user_card(bot: Bot, chat_id: int, state: FSMContext,
                         target: Mapping[str, Any], notice: str | None = None) -> None:
    """Карточка с кнопками действий над этим человеком."""
    await state.set_state(AdminPanel.user_card)
    await state.update_data(card_user=target["id"])
    message_ids = await profile_service.send_card(
        bot, chat_id, target, admin_view=True, show_distance=False,
        header=notice or "",
        markup=rkb.admin_user_card(bool(target["is_banned"]),
                                   target["verify_status"] == "verified",
                                   bool(target["verify_forced"])),
    )
    await screen.replace(bot, chat_id, state, message_ids)


async def _card_user(state: FSMContext) -> Mapping[str, Any] | None:
    return await users_repo.get_user(int((await state.get_data()).get("card_user") or 0))


async def ask_ban_reason(bot: Bot, chat_id: int, state: FSMContext, target_id: int,
                         back: str) -> None:
    """Вопрос о причине бана. back — куда вернуться после: panel|card|report|verify."""
    await state.set_state(AdminPanel.ban_reason)
    await state.update_data(ban_target=target_id, ban_back=back)
    await screen.send(bot, chat_id, state,
                      f"Причина бана для <code>{target_id}</code>?\n\n{BAN_REASON_HINT}",
                      rkb.ADMIN_BACK)


@router.message(AdminPanel.user_card, F.text == rkb.A_BAN)
async def card_ban(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is not None:
        await ask_ban_reason(message.bot, message.chat.id, state, target["id"], "card")


@router.message(AdminPanel.user_card, F.text == rkb.A_UNBAN)
async def card_unban(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        return
    await unban(bot, message.from_user.id, target["id"])
    await show_user_card(bot, message.chat.id, state, await users_repo.get_user(target["id"]),
                         notice="✅ <i>Бан снят</i>")


@router.message(AdminPanel.user_card, F.text == rkb.A_REQ_VERIFY)
async def card_request_verify(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        return
    code = await verification_handlers.request_verification(
        bot, target["id"], forced=True, admin_id=message.from_user.id)
    notice = ("🛡 <i>Это владелец бота — проверки на него не действуют</i>" if code is None
              else f"✅ <i>Верификация запрошена, код на фото: <code>{code}</code>. "
                   "До проверки бот для него закрыт.</i>")
    if code is not None:
        await admin_log(bot, f"✅ Запрошена верификация: <code>{target['id']}</code> "
                             f"(админ <code>{message.from_user.id}</code>)")
    await show_user_card(bot, message.chat.id, state, await users_repo.get_user(target["id"]),
                         notice=notice)


@router.message(AdminPanel.user_card, F.text == rkb.A_DROP_VERIFY)
async def card_drop_verify(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        return
    await users_repo.update_user(target["id"], verify_forced=0, verify_status="none",
                                 verify_code=None)
    await safe_send(bot, target["id"],
                    "✅ Требование верификации снято. Можно пользоваться ботом.")
    await show_user_card(bot, message.chat.id, state, await users_repo.get_user(target["id"]),
                         notice="🔓 <i>Требование снято</i>")


@router.message(AdminPanel.user_card, F.text == rkb.A_UNVERIFY)
async def card_unverify(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        return
    await users_repo.update_user(target["id"], verify_status="none", verified_at=None)
    await admin_log(bot, f"❎ Снята верификация: <code>{target['id']}</code>")
    await show_user_card(bot, message.chat.id, state, await users_repo.get_user(target["id"]),
                         notice="❎ <i>Галочка снята</i>")


@router.message(AdminPanel.user_card, F.text == rkb.A_MESSAGE)
async def card_message(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        return
    await state.set_state(AdminPanel.message_user)
    await screen.send(message.bot, message.chat.id, state,
                      f"Напишите текст для пользователя <code>{target['id']}</code>.\n"
                      "Он придёт от имени бота, с пометкой «Сообщение от администрации».",
                      rkb.CANCEL_ONLY)


@router.message(AdminPanel.message_user, F.text)
async def send_message_to_user(message: Message, state: FSMContext, bot: Bot) -> None:
    await screen.drop(message)
    target = await _card_user(state)
    if target is None:
        await open_panel(bot, message.chat.id, state, True)
        return
    if message.text == rkb.CANCEL:
        await show_user_card(bot, message.chat.id, state, target)
        return
    ok = await safe_send(
        bot, target["id"],
        f"📨 <b>Сообщение от администрации</b>\n\n{profile_service.esc(message.text)}",
    )
    await show_user_card(bot, message.chat.id, state, target,
                         notice="✅ <i>Отправлено</i>" if ok
                         else "❌ <i>Не доставлено — пользователь заблокировал бота</i>")


async def unban(bot: Bot, admin_id: int, target_id: int) -> None:
    await mod_repo.unban_user(target_id, admin_id)
    await safe_send(
        bot, target_id,
        "✅ <b>Блокировка снята</b>\n\nВы снова можете пользоваться ботом. "
        "Пожалуйста, соблюдайте правила.",
    )
    await admin_log(bot, f"✅ <b>Разбан</b>: <code>{target_id}</code> "
                         f"(админ <code>{admin_id}</code>)")


# ───────────────────────────── Статистика ───────────────────────────────────

@router.message(Command("stats"))
@router.message(F.text.in_({rkb.A_STATS, rkb.A_SUMMARY}))
async def show_stats(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    data = await stats_repo.collect()
    await screen.send(message.bot, message.chat.id, state,
                      stats_repo.render(data) if is_admin else stats_repo.render_short(data),
                      rkb.ADMIN_BACK)


# ───────────────────────── Поиск пользователя ───────────────────────────────

async def _ask_user(message: Message, state: FSMContext, error: str | None = None) -> None:
    await state.set_state(AdminPanel.find_user)
    text = "🔎 Пришлите ID или @username пользователя:"
    await screen.send(message.bot, message.chat.id, state,
                      f"⚠️ {error}\n\n{text}" if error else text, rkb.ADMIN_BACK)


@router.message(AdminPanel.find_user, F.text)
async def find_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_user(message, state, "Пользователь не найден. Попробуйте другой ID "
                                        "или @username.")
        return
    await show_user_card(message.bot, message.chat.id, state, target)


@router.message(Command("find"))
async def find_command(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await _ask_user(message, state)
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await _ask_user(message, state, "Пользователь не найден.")
        return
    await show_user_card(message.bot, message.chat.id, state, target)


@router.message(F.text == rkb.A_FIND)
async def ask_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask_user(message, state)


@router.message(Command("say"))
async def say_command(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: <code>/say 123456789 текст сообщения</code>")
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    ok = await safe_send(
        bot, target["id"],
        f"📨 <b>Сообщение от администрации</b>\n\n{profile_service.esc(parts[2])}",
    )
    await message.answer("✅ Отправлено" if ok else "❌ Не доставлено.")


# ─────────────────────────── Настройки бота ─────────────────────────────────

async def show_config(bot: Bot, chat_id: int, state: FSMContext, settings: Settings,
                      notice: str | None = None) -> None:
    likes = await mod_repo.get_int_setting("likes_limit", settings.likes_limit_per_day)
    reg_open = await mod_repo.get_setting("registration_open", "1") == "1"
    support = await mod_repo.support_username()
    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"❤️ Лимит лайков в сутки: <b>{likes}</b>\n"
        f"💬 Поддержка: <b>{'@' + support if support else 'не указана'}</b>\n"
        f"📝 Регистрация новых анкет: <b>{'открыта' if reg_open else 'закрыта'}</b>\n\n"
        "<i>Значения применяются сразу и переживают перезапуск. Нажмите на "
        "настройку, чтобы изменить её.</i>"
    )
    await state.set_state(AdminPanel.bot_settings)
    await screen.send(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      rkb.bot_settings(likes, reg_open, support))


@admin_router.message(F.text == rkb.A_CONFIG)
async def open_config(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    await show_config(message.bot, message.chat.id, state, settings)


@admin_router.message(AdminPanel.bot_settings, F.text.in_({rkb.A_REG_OPEN, rkb.A_REG_CLOSED}))
async def toggle_registration(message: Message, state: FSMContext,
                              settings: Settings) -> None:
    await screen.drop(message)
    current = await mod_repo.get_setting("registration_open", "1") == "1"
    await mod_repo.set_setting("registration_open", "0" if current else "1")
    await show_config(message.bot, message.chat.id, state, settings,
                      notice="🔴 <i>Приём анкет закрыт</i>" if current
                      else "🟢 <i>Приём анкет открыт</i>")


@admin_router.message(AdminPanel.bot_settings, F.text.startswith(rkb.A_LIKES_LIMIT))
async def ask_likes_limit(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(AdminPanel.setting_value)
    await state.update_data(setting_key="likes_limit")
    await screen.send(message.bot, message.chat.id, state,
                      "Введите новый суточный лимит лайков (число от 1 до 1000):",
                      rkb.BACK_ONLY)


@admin_router.message(AdminPanel.bot_settings, F.text.startswith(rkb.A_SUPPORT))
async def ask_support(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.set_state(AdminPanel.setting_value)
    await state.update_data(setting_key="support")
    await screen.send(message.bot, message.chat.id, state, SUPPORT_ASK, rkb.BACK_ONLY)


@admin_router.message(AdminPanel.setting_value, F.text)
async def save_setting(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    raw = (message.text or "").strip()
    if raw == rkb.BACK:
        await show_config(bot, chat_id, state, settings)
        return
    key = (await state.get_data()).get("setting_key") or "likes_limit"
    if key == "support":
        await _save_support(message, state, settings, raw)
        return
    if not raw.isdigit() or not (1 <= int(raw) <= 1000):
        await screen.send(bot, chat_id, state,
                          "⚠️ Нужно число от 1 до 1000.\n\nВведите новый суточный лимит "
                          "лайков:", rkb.BACK_ONLY)
        return
    await mod_repo.set_setting(key, raw)
    await show_config(bot, chat_id, state, settings, notice="✅ <i>Сохранено</i>")


async def _save_support(message: Message, state: FSMContext, settings: Settings,
                        raw: str) -> None:
    bot, chat_id = message.bot, message.chat.id
    if raw in {"-", "—"}:
        await mod_repo.set_setting("support", "")
        await show_config(bot, chat_id, state, settings,
                          notice="🗑 <i>Контакт поддержки убран — кнопки в меню больше нет</i>")
        return
    match = SUPPORT_RE.match(raw)
    if match is None:
        await screen.send(bot, chat_id, state,
                          "⚠️ Не похоже на username — нужно, например, "
                          f"<code>@help_support</code>.\n\n{SUPPORT_ASK}", rkb.BACK_ONLY)
        return
    username = match.group(1)
    await mod_repo.set_setting("support", username)
    await show_config(bot, chat_id, state, settings,
                      notice=f"✅ <i>Поддержка: @{username} — кнопка появилась в меню</i>")
