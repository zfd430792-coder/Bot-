"""Рекламные посты между анкетами. Только для владельцев.

Список постов — inline-кнопки «📣 #N Название». Сам пост бот не удаляет:
в ленту уходит его копия, а копируется он именно из этого чата. Поэтому
вопросы после поста приходят новыми сообщениями под ним.
"""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.db import ads as ads_repo
from app.handlers.admin.filters import IsAdmin
from app.keyboards import inline as kb
from app.services import ads as ads_service
from app.services import profile as profile_service
from app.services import screen
from app.states import AdminPanel

router = Router(name="admin-ads")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

URL_RE = re.compile(r"^(https?://|tg://)\S+$", re.I)

INTRO = (
    "📣 <b>Реклама</b>\n\n"
    "Посты показываются в ленте между анкетами — там, где на них точно "
    "смотрят.\n\n"
    "Постов может быть несколько: они идут по очереди, первым — тот, "
    "который показывали реже.\n\n"
    "<i>Переходы по ссылке Telegram ботам не сообщает. Чтобы считать их, "
    "добавьте к ссылке свою метку (utm_source и подобные).</i>"
)


async def show_list(bot: Bot, chat_id: int, state: FSMContext,
                    notice: str | None = None) -> None:
    rows = await ads_repo.list_all()
    if not rows:
        text = INTRO + "\n\n<b>Постов пока нет.</b>"
    else:
        shows = await ads_repo.total_shows()
        lines = [INTRO, f"\n<b>Постов: {len(rows)}</b> · показов всего: {shows}\n"]
        for ad in rows:
            mark = "🟢 активен" if ad["is_active"] else "⚪️ выключен"
            lines.append(
                f"<b>#{ad['id']} {profile_service.esc(ad['title'])}</b> — {mark}\n"
                f"   раз в {ad['every_n']} анкет · показов: {ad['shows']}"
            )
        text = "\n".join(lines)
    await state.set_state(AdminPanel.ads_list)
    await screen.show(bot, chat_id, state, f"{notice}\n\n{text}" if notice else text,
                      kb.ads_list(rows))


@router.message(Command("ads"))
async def ads_command(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await show_list(message.bot, message.chat.id, state)


@router.callback_query(F.data == "adm:ads")
async def ads_button(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await show_list(call.bot, screen.chat_id(call), state)


# ──────────────────────── Управление постами ────────────────────────────────

async def show_ad(bot: Bot, chat_id: int, state: FSMContext, ad_id: int,
                  notice: str | None = None) -> None:
    ad = await ads_repo.get(ad_id)
    if ad is None:
        await show_list(bot, chat_id, state, "<i>Пост не найден</i>")
        return
    await state.set_state(AdminPanel.ad_view)
    header = (
        (f"{notice}\n\n" if notice else "")
        + f"📣 <b>#{ad['id']} {profile_service.esc(ad['title'])}</b>\n"
        f"Показов: {ad['shows']} · раз в {ad['every_n']} анкет\n"
        f"Статус: {'🟢 активен' if ad['is_active'] else '⚪️ выключен'}\n\n"
        "Вот как его видят люди 👇"
    )
    await screen.prepare(bot, chat_id, state)
    # Кнопки — на заголовке: у копии поста своя кнопка-ссылка
    head = await bot.send_message(chat_id, header,
                                  reply_markup=kb.ad_view(ad_id, bool(ad["is_active"])))
    ids = [head.message_id]
    try:
        copy = await bot.copy_message(chat_id, ad["src_chat_id"], ad["src_message_id"],
                                      reply_markup=ads_service.markup(ad))
        ids.append(copy.message_id)
    except Exception:
        warn = await bot.send_message(
            chat_id, "⚠️ Исходное сообщение недоступно — видимо, его удалили. "
                     "Создайте пост заново.")
        ids.append(warn.message_id)
    await screen.remember(state, ids)


@router.callback_query(F.data.startswith("adm:ad:"))
async def ad_action(call: CallbackQuery, state: FSMContext) -> None:
    """adm:ad:<действие>[:<id>] — открыть, включить/выключить, удалить, новый."""
    await call.answer()
    bot, chat_id = call.bot, screen.chat_id(call)
    parts = (call.data or "").split(":")
    action = parts[2] if len(parts) > 2 else ""
    ad_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    if action == "new":
        await state.set_state(AdminPanel.ad_title)
        await screen.show(bot, chat_id, state,
                          "📣 <b>Новый пост</b>\n\nКак назовём его в списке? "
                          "Название видите только вы.\n\n"
                          "<i>Например: «Канал знакомств, март»</i>", kb.ADS_BACK)
        return
    if action == "nobutton":
        if await state.get_state() == AdminPanel.ad_button_text.state:
            await state.update_data(ad_button_text=None, ad_button_url=None)
            await _ask_every(bot, chat_id, state)
        return
    if action == "open":
        await show_ad(bot, chat_id, state, ad_id)
        return

    ad = await ads_repo.get(ad_id)
    if ad is None:
        await show_list(bot, chat_id, state, "<i>Пост не найден</i>")
        return
    if action in {"on", "off"}:
        await ads_repo.set_active(ad_id, action == "on")
        await show_ad(bot, chat_id, state, ad_id,
                      "▶️ <i>Пост включён</i>" if action == "on" else "⏸ <i>Пост выключен</i>")
    elif action == "del":
        await ads_repo.delete(ad_id)
        await show_list(bot, chat_id, state, f"🗑 <i>Пост #{ad_id} удалён</i>")


# ─────────────────────────── Создание поста ─────────────────────────────────

async def _ask(bot: Bot, chat_id: int, state: FSMContext, new_state, text: str,
               markup=kb.ADS_BACK) -> None:
    """Вопрос новым сообщением: присланный пост остаётся в чате выше."""
    await state.set_state(new_state)
    await screen.send(bot, chat_id, state, text, markup)


@router.message(AdminPanel.ad_title, F.text)
async def set_title(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    title = (message.text or "").strip()[:60]
    if len(title) < 2:
        await screen.show(bot, chat_id, state,
                          "⚠️ Название слишком короткое. Как назовём пост?", kb.ADS_BACK)
        return
    await state.update_data(ad_title=title)
    await state.set_state(AdminPanel.ad_content)
    await screen.show(bot, chat_id, state,
                      "Теперь пришлите сам пост — текст, фото, видео, гифку, что угодно.\n\n"
                      "Он уйдёт людям ровно в том виде, в каком придёт сюда.\n\n"
                      "⚠️ <i>Не удаляйте это сообщение из чата: бот копирует пост именно "
                      "из него.</i>", kb.ADS_BACK)


@router.message(AdminPanel.ad_content)
async def set_content(message: Message, state: FSMContext) -> None:
    # Пост не удаляем: в ленту уходит его копия
    preview = (message.text or message.caption or "медиа без подписи")[:120]
    await state.update_data(ad_chat=message.chat.id, ad_message=message.message_id,
                            ad_preview=preview)
    await _ask(message.bot, message.chat.id, state, AdminPanel.ad_button_text,
               "Нужна кнопка-ссылка под постом? Пришлите её текст.\n\n"
               "<i>Например: «Перейти в канал»</i>", kb.AD_BUTTON)


@router.message(AdminPanel.ad_button_text, F.text)
async def set_button_text(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.update_data(ad_button_text=(message.text or "").strip()[:40])
    await _ask(message.bot, message.chat.id, state, AdminPanel.ad_button_url,
               "Куда ведёт кнопка? Пришлите ссылку.\n\n"
               "<i>https://t.me/канал или https://сайт.ру</i>")


@router.message(AdminPanel.ad_button_url, F.text)
async def set_button_url(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    url = (message.text or "").strip()
    if not URL_RE.match(url):
        await _ask(message.bot, message.chat.id, state, AdminPanel.ad_button_url,
                   "⚠️ Ссылка должна начинаться с https:// — например, "
                   "<code>https://t.me/mychannel</code>")
        return
    await state.update_data(ad_button_url=url)
    await _ask_every(message.bot, message.chat.id, state)


async def _ask_every(bot: Bot, chat_id: int, state: FSMContext,
                     error: str | None = None) -> None:
    text = ("Как часто показывать? Пришлите число — раз во сколько анкет.\n\n"
            "<i>10 — золотая середина. Чаще 5 раздражает и люди уходят.</i>")
    await _ask(bot, chat_id, state, AdminPanel.ad_every,
               f"⚠️ {error}\n\n{text}" if error else text)


@router.message(AdminPanel.ad_every, F.text)
async def set_every(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (3 <= int(raw) <= 100):
        await _ask_every(bot, chat_id, state, "Нужно число от 3 до 100.")
        return

    data = await state.get_data()
    if not data.get("ad_message"):
        await show_list(bot, chat_id, state, "⚠️ <i>Пост потерялся — начните заново</i>")
        return

    ad_id = await ads_repo.create(
        title=data["ad_title"],
        src_chat_id=int(data["ad_chat"]),
        src_message_id=int(data["ad_message"]),
        preview=data.get("ad_preview", ""),
        button_text=data.get("ad_button_text"),
        button_url=data.get("ad_button_url"),
        every_n=int(raw),
        created_by=message.from_user.id,
    )
    await show_ad(bot, chat_id, state, ad_id,
                  f"✅ <i>Пост #{ad_id} создан и уже показывается — раз в {raw} анкет</i>")
