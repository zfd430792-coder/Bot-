"""Админ-панель: меню, статистика, поиск пользователя, настройки бота."""
from __future__ import annotations

from typing import Any, Mapping

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.db import moderation as mod_repo
from app.db import stats as stats_repo
from app.db import users as users_repo
from app.handlers.admin.filters import IsAdmin, IsStaff
from app.keyboards import inline as kb
from app.services import profile as profile_service
from app.services.notify import safe_send
from app.states import AdminPanel

# Панель доступна всему персоналу; кнопки владельца собираются отдельно
router = Router(name="staff-panel")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

# Разделы, которые модератору недоступны
admin_router = Router(name="admin-panel")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

ADMIN_HELP = (
    "🛠 <b>Админ-панель</b>\n\n"
    "Быстрые команды:\n"
    "<code>/find 123456789</code> — карточка пользователя\n"
    "<code>/ban 123456789 причина</code> — бан\n"
    "<code>/unban 123456789</code> — снять бан\n"
    "<code>/verify 123456789</code> — потребовать верификацию\n"
    "<code>/say 123456789 текст</code> — написать пользователю"
)

MOD_HELP = (
    "👮 <b>Панель модератора</b>\n\n"
    "Вам доступны жалобы, верификация и блокировки.\n\n"
    "Быстрые команды:\n"
    "<code>/find 123456789</code> — карточка пользователя\n"
    "<code>/ban 123456789 причина</code> — бан\n"
    "<code>/unban 123456789</code> — снять бан\n"
    "<code>/verify 123456789</code> — потребовать верификацию\n\n"
    "<i>Каждое действие попадает в журнал с вашим именем.</i>"
)


async def panel_view(is_admin: bool) -> tuple[str, Any]:
    reports = await mod_repo.count_open_reports()
    verify = await mod_repo.count_pending_verifications()
    text = ADMIN_HELP if is_admin else MOD_HELP
    return text, kb.admin_menu(reports, verify, is_admin=is_admin)


async def open_panel(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.set_state(AdminPanel.menu)
    text, markup = await panel_view(is_admin)
    await message.answer(text, reply_markup=markup)


@router.message(Command("admin"))
@router.message(Command("mod"))
@router.message(F.text == "🛠 Админ-панель")
@router.message(F.text == "👮 Модератор")
async def admin_command(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    await open_panel(message, state, is_admin)


@router.callback_query(F.data == "adm:menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    await state.set_state(AdminPanel.menu)
    await call.answer()
    text, markup = await panel_view(is_admin)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:close")
async def close_panel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass


# ───────────────────────────── Статистика ───────────────────────────────────

@router.callback_query(F.data == "adm:stats")
async def show_stats(call: CallbackQuery, is_admin: bool) -> None:
    await call.answer()
    data = await stats_repo.collect()
    text = stats_repo.render(data) if is_admin else stats_repo.render_short(data)
    await call.message.edit_text(text, reply_markup=kb.ADMIN_BACK)


@router.message(Command("stats"))
async def stats_command(message: Message, is_admin: bool) -> None:
    data = await stats_repo.collect()
    await message.answer(stats_repo.render(data) if is_admin
                         else stats_repo.render_short(data))


# ─────────────────────── Карточка пользователя ──────────────────────────────

async def send_user_card(message: Message, target: Mapping[str, Any]) -> None:
    await profile_service.send_card(
        message.bot, message.chat.id, target, admin_view=True, show_distance=False,
        markup=kb.admin_user_card(
            target["id"], bool(target["is_banned"]),
            target["verify_status"] == "verified", bool(target["verify_forced"]),
        ),
    )


@router.callback_query(F.data == "adm:find")
async def ask_user(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.find_user)
    await call.answer()
    await call.message.answer("Пришлите ID или @username пользователя:")


@router.message(Command("find"))
async def find_command(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await state.set_state(AdminPanel.find_user)
        await message.answer("Пришлите ID или @username пользователя:")
        return
    await _lookup(message, state, parts[1])


@router.message(AdminPanel.find_user, F.text)
async def find_user(message: Message, state: FSMContext) -> None:
    await _lookup(message, state, message.text or "")


async def _lookup(message: Message, state: FSMContext, query: str) -> None:
    target = await users_repo.find_user(query)
    if target is None:
        await message.answer("Пользователь не найден. Попробуйте другой ID или @username.")
        return
    await state.set_state(AdminPanel.menu)
    await send_user_card(message, target)


@router.callback_query(F.data.startswith("adm:card:"))
async def show_card(call: CallbackQuery, state: FSMContext) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    target = await users_repo.get_user(target_id)
    await call.answer()
    if target is None:
        await call.message.answer("Пользователь не найден.")
        return
    await send_user_card(call.message, target)


# ──────────────────── Сообщение от имени бота ───────────────────────────────

@router.callback_query(F.data.startswith("adm:msg:"))
async def ask_message(call: CallbackQuery, state: FSMContext) -> None:
    target_id = int((call.data or "0").split(":")[-1])
    await state.set_state(AdminPanel.message_user)
    await state.update_data(msg_target=target_id)
    await call.answer()
    await call.message.answer(
        f"Напишите текст для пользователя <code>{target_id}</code>.\n"
        "Он придёт от имени бота, с пометкой «Сообщение от администрации»."
    )


@router.message(AdminPanel.message_user, F.text)
async def send_message_to_user(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    target_id = int(data.get("msg_target") or 0)
    await state.set_state(AdminPanel.menu)
    if not target_id:
        await message.answer("Не понял, кому писать. Откройте карточку заново.")
        return
    ok = await safe_send(
        bot, target_id,
        f"📨 <b>Сообщение от администрации</b>\n\n{profile_service.esc(message.text)}",
    )
    await message.answer("✅ Отправлено" if ok
                         else "❌ Не доставлено — пользователь заблокировал бота.")


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

async def _settings_view(settings: Settings) -> tuple[str, Any]:
    likes = await mod_repo.get_int_setting("likes_limit", settings.likes_limit_per_day)
    reg_open = await mod_repo.get_setting("registration_open", "1") == "1"
    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"❤️ Лимит лайков в сутки: <b>{likes}</b>\n"
        f"📝 Регистрация новых анкет: <b>{'открыта' if reg_open else 'закрыта'}</b>\n\n"
        "<i>Значения применяются сразу и переживают перезапуск.</i>"
    )
    return text, kb.bot_settings(likes, reg_open)


@admin_router.callback_query(F.data == "adm:cfg")
async def show_config(call: CallbackQuery, settings: Settings) -> None:
    await call.answer()
    text, markup = await _settings_view(settings)
    await call.message.edit_text(text, reply_markup=markup)


@admin_router.callback_query(F.data == "adm:set:registration")
async def toggle_registration(call: CallbackQuery, settings: Settings) -> None:
    current = await mod_repo.get_setting("registration_open", "1") == "1"
    await mod_repo.set_setting("registration_open", "0" if current else "1")
    await call.answer("Готово")
    text, markup = await _settings_view(settings)
    await call.message.edit_text(text, reply_markup=markup)


@admin_router.callback_query(F.data == "adm:set:likes_limit")
async def ask_likes_limit(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.setting_value)
    await state.update_data(setting_key="likes_limit")
    await call.answer()
    await call.message.answer("Введите новый суточный лимит лайков (число от 1 до 1000):")


@admin_router.message(AdminPanel.setting_value, F.text)
async def save_setting(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    key = data.get("setting_key")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 1000):
        await message.answer("Нужно число от 1 до 1000.")
        return
    await mod_repo.set_setting(key, raw)
    await state.set_state(AdminPanel.menu)
    text, markup = await _settings_view(settings)
    await message.answer("✅ Сохранено")
    await message.answer(text, reply_markup=markup)
