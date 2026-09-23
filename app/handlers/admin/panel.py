"""Админ-панель: вход, статистика, карточка пользователя, настройки бота.

Всё на inline-кнопках, экран правится на месте. Действия над конкретным
человеком (бан, верификация, сообщение) несут его id в самой кнопке, поэтому
карточка выше по чату действует ровно на того, кто на ней.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.db import moderation as mod_repo
from app.db import stats as stats_repo
from app.db import users as users_repo
from app.handlers import verification as verification_handlers
from app.handlers.admin.filters import IsAdmin, IsStaff
from app.keyboards import inline as kb
from app.keyboards import reply as rkb
from app.services import profile as profile_service
from app.services import screen
from app.services.notify import admin_log, safe_send
from app.states import AdminPanel

# Панель доступна всему персоналу
router = Router(name="staff-panel")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

# Разделы, которые модератору недоступны
admin_router = Router(name="admin-panel")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

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


def _tail_id(call: CallbackQuery) -> int:
    """id из хвоста callback_data: adm:<раздел>:<действие>:<id>."""
    tail = (call.data or "").rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


async def open_panel(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool,
                     notice: str | None = None) -> None:
    """Главный экран админки. notice — строка о том, что сейчас сделано."""
    await state.clear()
    await state.set_state(AdminPanel.menu)
    reports = await mod_repo.count_open_reports()
    verify = await mod_repo.count_pending_verifications()
    text = ADMIN_TITLE if is_admin else MOD_TITLE
    await screen.show(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      kb.admin_menu(reports, verify, is_admin=is_admin))


@router.message(Command("admin"))
@router.message(Command("mod"))
@router.message(F.text.in_({rkb.L_ADMIN, rkb.L_MODERATOR}))
async def admin_command(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    await open_panel(message.bot, message.chat.id, state, is_admin)


@router.callback_query(F.data.in_({"m:admin", "adm:home"}))
async def admin_button(call: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    await call.answer()
    await open_panel(call.bot, screen.chat_id(call), state, is_admin)


# ─────────────────────── Карточка пользователя ──────────────────────────────

async def show_user_card(bot: Bot, chat_id: int, state: FSMContext,
                         target: Mapping[str, Any], notice: str | None = None) -> None:
    """Карточка с кнопками действий над этим человеком."""
    await state.set_state(AdminPanel.user_card)
    await state.update_data(card_user=target["id"])
    await screen.prepare(bot, chat_id, state)
    message_ids = await profile_service.send_card(
        bot, chat_id, target, admin_view=True, show_distance=False,
        header=notice or "",
        markup=kb.admin_user_card(int(target["id"]), bool(target["is_banned"]),
                                  target["verify_status"] == "verified",
                                  bool(target["verify_forced"])),
    )
    await screen.remember(state, message_ids)


async def ask_ban_reason(bot: Bot, chat_id: int, state: FSMContext, target_id: int,
                         back: str) -> None:
    """Вопрос о причине бана. back — куда вернуться после: panel|card|report|verify."""
    await state.set_state(AdminPanel.ban_reason)
    await state.update_data(ban_target=target_id, ban_back=back)
    await screen.show(bot, chat_id, state,
                      f"Причина бана для <code>{target_id}</code>?\n\n{BAN_REASON_HINT}",
                      kb.ADMIN_BACK)


async def unban(bot: Bot, admin_id: int, target_id: int) -> None:
    await mod_repo.unban_user(target_id, admin_id)
    await safe_send(
        bot, target_id,
        "✅ <b>Блокировка снята</b>\n\nВы снова можете пользоваться ботом. "
        "Пожалуйста, соблюдайте правила.",
    )
    await admin_log(bot, f"✅ <b>Разбан</b>: <code>{target_id}</code> "
                         f"(админ <code>{admin_id}</code>)")


@router.callback_query(F.data.startswith("adm:card:"))
async def card_action(call: CallbackQuery, state: FSMContext, bot: Bot,
                      is_admin: bool) -> None:
    """Кнопки под карточкой: adm:card:<действие>:<id>."""
    await call.answer()
    chat_id = screen.chat_id(call)
    action = (call.data or "").split(":")[2]
    target = await users_repo.get_user(_tail_id(call))
    if target is None:
        await open_panel(bot, chat_id, state, is_admin, "<i>Пользователь не найден</i>")
        return
    target_id = int(target["id"])
    admin_id = call.from_user.id

    if action == "ban":
        await ask_ban_reason(bot, chat_id, state, target_id, "card")
        return
    if action == "msg":
        await state.set_state(AdminPanel.message_user)
        await state.update_data(card_user=target_id)
        await screen.show(bot, chat_id, state,
                          f"Напишите текст для пользователя <code>{target_id}</code>.\n"
                          "Он придёт от имени бота, с пометкой «Сообщение от администрации».",
                          kb.card_back(target_id))
        return

    notice = None
    if action == "unban":
        await unban(bot, admin_id, target_id)
        notice = "✅ <i>Бан снят</i>"
    elif action == "req":
        code = await verification_handlers.request_verification(
            bot, target_id, forced=True, admin_id=admin_id)
        if code is None:
            notice = "🛡 <i>Это владелец бота — проверки на него не действуют</i>"
        else:
            await admin_log(bot, f"✅ Запрошена верификация: <code>{target_id}</code> "
                                 f"(админ <code>{admin_id}</code>)")
            notice = (f"✅ <i>Верификация запрошена, код на фото: <code>{code}</code>. "
                      "До проверки бот для него закрыт.</i>")
    elif action == "drop":
        await users_repo.update_user(target_id, verify_forced=0, verify_status="none",
                                     verify_code=None)
        await safe_send(bot, target_id,
                        "✅ Требование верификации снято. Можно пользоваться ботом.")
        notice = "🔓 <i>Требование снято</i>"
    elif action == "unverify":
        await users_repo.update_user(target_id, verify_status="none", verified_at=None)
        await admin_log(bot, f"❎ Снята верификация: <code>{target_id}</code>")
        notice = "❎ <i>Галочка снята</i>"
    await show_user_card(bot, chat_id, state, await users_repo.get_user(target_id),
                         notice=notice)


@router.message(AdminPanel.message_user, F.text)
async def send_message_to_user(message: Message, state: FSMContext, bot: Bot,
                               is_admin: bool) -> None:
    await screen.drop(message)
    target = await users_repo.get_user(int((await state.get_data()).get("card_user") or 0))
    if target is None:
        await open_panel(bot, message.chat.id, state, is_admin)
        return
    ok = await safe_send(
        bot, target["id"],
        f"📨 <b>Сообщение от администрации</b>\n\n{profile_service.esc(message.text)}",
    )
    await show_user_card(bot, message.chat.id, state, target,
                         notice="✅ <i>Отправлено</i>" if ok
                         else "❌ <i>Не доставлено — пользователь заблокировал бота</i>")


# ───────────────────────────── Статистика ───────────────────────────────────

async def _stats(bot: Bot, chat_id: int, state: FSMContext, is_admin: bool) -> None:
    data = await stats_repo.collect()
    await screen.show(bot, chat_id, state,
                      stats_repo.render(data) if is_admin else stats_repo.render_short(data),
                      kb.ADMIN_BACK)


@router.message(Command("stats"))
async def stats_command(message: Message, state: FSMContext, is_admin: bool) -> None:
    await screen.drop(message)
    await _stats(message.bot, message.chat.id, state, is_admin)


@router.callback_query(F.data == "adm:stats")
async def stats_button(call: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    await call.answer()
    await _stats(call.bot, screen.chat_id(call), state, is_admin)


# ───────────────────────── Поиск пользователя ───────────────────────────────

async def _ask_user(bot: Bot, chat_id: int, state: FSMContext,
                    error: str | None = None) -> None:
    await state.set_state(AdminPanel.find_user)
    text = "🔎 Пришлите ID или @username пользователя:"
    await screen.show(bot, chat_id, state,
                      f"⚠️ {error}\n\n{text}" if error else text, kb.ADMIN_BACK)


@router.callback_query(F.data == "adm:find")
async def find_button(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _ask_user(call.bot, screen.chat_id(call), state)


@router.message(AdminPanel.find_user, F.text)
async def find_user(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    target = await users_repo.find_user(message.text or "")
    if target is None:
        await _ask_user(message.bot, message.chat.id, state,
                        "Пользователь не найден. Попробуйте другой ID или @username.")
        return
    await show_user_card(message.bot, message.chat.id, state, target)


@router.message(Command("find"))
async def find_command(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await _ask_user(message.bot, message.chat.id, state)
        return
    target = await users_repo.find_user(parts[1])
    if target is None:
        await _ask_user(message.bot, message.chat.id, state, "Пользователь не найден.")
        return
    await show_user_card(message.bot, message.chat.id, state, target)


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
    await screen.show(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      kb.bot_settings(likes, reg_open, support))


@admin_router.callback_query(F.data == "adm:config")
async def open_config(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await call.answer()
    await show_config(call.bot, screen.chat_id(call), state, settings)


@admin_router.callback_query(F.data == "adm:cfg:reg")
async def toggle_registration(call: CallbackQuery, state: FSMContext,
                              settings: Settings) -> None:
    await call.answer()
    current = await mod_repo.get_setting("registration_open", "1") == "1"
    await mod_repo.set_setting("registration_open", "0" if current else "1")
    await show_config(call.bot, screen.chat_id(call), state, settings,
                      notice="🔴 <i>Приём анкет закрыт</i>" if current
                      else "🟢 <i>Приём анкет открыт</i>")


@admin_router.callback_query(F.data == "adm:cfg:likes")
async def ask_likes_limit(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminPanel.setting_value)
    await state.update_data(setting_key="likes_limit")
    await screen.show(call.bot, screen.chat_id(call), state,
                      "Введите новый суточный лимит лайков (число от 1 до 1000):",
                      kb.CONFIG_BACK)


@admin_router.callback_query(F.data == "adm:cfg:support")
async def ask_support(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminPanel.setting_value)
    await state.update_data(setting_key="support")
    await screen.show(call.bot, screen.chat_id(call), state, SUPPORT_ASK, kb.CONFIG_BACK)


@admin_router.message(AdminPanel.setting_value, F.text)
async def save_setting(message: Message, state: FSMContext, settings: Settings) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    raw = (message.text or "").strip()
    key = (await state.get_data()).get("setting_key") or "likes_limit"
    if key == "support":
        await _save_support(bot, chat_id, state, settings, raw)
        return
    if not raw.isdigit() or not (1 <= int(raw) <= 1000):
        await screen.show(bot, chat_id, state,
                          "⚠️ Нужно число от 1 до 1000.\n\nВведите новый суточный лимит "
                          "лайков:", kb.CONFIG_BACK)
        return
    await mod_repo.set_setting(key, raw)
    await show_config(bot, chat_id, state, settings, notice="✅ <i>Сохранено</i>")


async def _save_support(bot: Bot, chat_id: int, state: FSMContext, settings: Settings,
                        raw: str) -> None:
    if raw in {"-", "—"}:
        await mod_repo.set_setting("support", "")
        await show_config(bot, chat_id, state, settings,
                          notice="🗑 <i>Контакт поддержки убран — кнопки в меню больше нет</i>")
        return
    match = SUPPORT_RE.match(raw)
    if match is None:
        await screen.show(bot, chat_id, state,
                          "⚠️ Не похоже на username — нужно, например, "
                          f"<code>@help_support</code>.\n\n{SUPPORT_ASK}", kb.CONFIG_BACK)
        return
    username = match.group(1)
    await mod_repo.set_setting("support", username)
    await show_config(bot, chat_id, state, settings,
                      notice=f"✅ <i>Поддержка: @{username} — кнопка появилась в меню</i>")
