"""Рекламные посты между анкетами. Только для владельцев.

Список постов — нижние кнопки «📣 #N Название». Сам пост бот не удаляет:
в ленту уходит его копия, а копируется он именно из этого чата. Кнопка со
ссылкой под постом остаётся inline — ссылку Telegram открывает только так.
"""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.db import ads as ads_repo
from app.handlers.admin.filters import IsAdmin
from app.keyboards import reply as rkb
from app.services import ads as ads_service
from app.services import profile as profile_service
from app.services import screen
from app.states import AdminPanel

router = Router(name="admin-ads")
router.message.filter(IsAdmin())

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
                    notice: str | None = None, extra_ids: list[int] | None = None) -> None:
    """Список постов. extra_ids — сообщения, которые остаются на экране под
    списком (например, только что созданный пост)."""
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
    sent = await bot.send_message(chat_id, f"{notice}\n\n{text}" if notice else text,
                                  reply_markup=rkb.ads_list(rows))
    await screen.replace(bot, chat_id, state, [sent.message_id, *(extra_ids or [])])


@router.message(Command("ads"))
@router.message(F.text == rkb.A_ADS)
@router.message(AdminPanel.ad_view, F.text == rkb.A_AD_LIST)
async def open_ads(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await show_list(message.bot, message.chat.id, state)


# ──────────────────────── Управление постами ────────────────────────────────

async def show_ad(bot: Bot, chat_id: int, state: FSMContext, ad_id: int,
                  notice: str | None = None) -> None:
    ad = await ads_repo.get(ad_id)
    if ad is None:
        await show_list(bot, chat_id, state, "<i>Пост не найден</i>")
        return
    await state.set_state(AdminPanel.ad_view)
    await state.update_data(ad_id=ad_id)
    header = (
        (f"{notice}\n\n" if notice else "")
        + f"📣 <b>#{ad['id']} {profile_service.esc(ad['title'])}</b>\n"
        f"Показов: {ad['shows']} · раз в {ad['every_n']} анкет\n"
        f"Статус: {'🟢 активен' if ad['is_active'] else '⚪️ выключен'}\n\n"
        "Вот как его видят люди 👇"
    )
    # Клавиатура — на заголовке: у копии поста своя кнопка-ссылка
    head = await bot.send_message(chat_id, header, reply_markup=rkb.ad_view(ad["is_active"]))
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
    await screen.replace(bot, chat_id, state, ids)


@router.message(AdminPanel.ads_list, F.text.regexp(rkb.AD_RE))
async def open_ad(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    ad_id = int(rkb.AD_RE.match(message.text or "").group(1))
    await show_ad(message.bot, message.chat.id, state, ad_id)


@router.message(AdminPanel.ad_view, F.text.in_({rkb.A_AD_OFF, rkb.A_AD_ON}))
async def toggle_ad(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    ad_id = int((await state.get_data()).get("ad_id") or 0)
    ad = await ads_repo.get(ad_id)
    if ad is None:
        await show_list(message.bot, message.chat.id, state, "<i>Пост не найден</i>")
        return
    await ads_repo.set_active(ad_id, not ad["is_active"])
    await show_ad(message.bot, message.chat.id, state, ad_id,
                  "⏸ <i>Пост выключен</i>" if ad["is_active"] else "▶️ <i>Пост включён</i>")


@router.message(AdminPanel.ad_view, F.text == rkb.A_AD_DELETE)
async def delete_ad(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    ad_id = int((await state.get_data()).get("ad_id") or 0)
    await ads_repo.delete(ad_id)
    await show_list(message.bot, message.chat.id, state, f"🗑 <i>Пост #{ad_id} удалён</i>")


# ─────────────────────────── Создание поста ─────────────────────────────────

async def _ask(message: Message, state: FSMContext, new_state, text: str,
               markup=rkb.ADMIN_BACK) -> None:
    await state.set_state(new_state)
    await screen.send(message.bot, message.chat.id, state, text, markup)


@router.message(AdminPanel.ads_list, F.text == rkb.A_AD_NEW)
async def ask_title(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await _ask(message, state, AdminPanel.ad_title,
               "📣 <b>Новый пост</b>\n\nКак назовём его в списке? "
               "Название видите только вы.\n\n<i>Например: «Канал знакомств, март»</i>")


@router.message(AdminPanel.ad_title, F.text)
async def set_title(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    title = (message.text or "").strip()[:60]
    if len(title) < 2:
        await _ask(message, state, AdminPanel.ad_title,
                   "⚠️ Название слишком короткое. Как назовём пост?")
        return
    await state.update_data(ad_title=title)
    await _ask(message, state, AdminPanel.ad_content,
               "Теперь пришлите сам пост — текст, фото, видео, гифку, что угодно.\n\n"
               "Он уйдёт людям ровно в том виде, в каком придёт сюда.\n\n"
               "⚠️ <i>Не удаляйте это сообщение из чата: бот копирует пост именно "
               "из него.</i>")


@router.message(AdminPanel.ad_content)
async def set_content(message: Message, state: FSMContext) -> None:
    # Пост не удаляем: в ленту уходит его копия
    preview = (message.text or message.caption or "медиа без подписи")[:120]
    await state.update_data(ad_chat=message.chat.id, ad_message=message.message_id,
                            ad_preview=preview)
    await _ask(message, state, AdminPanel.ad_button_text,
               "Нужна кнопка-ссылка под постом? Пришлите её текст.\n\n"
               "<i>Например: «Перейти в канал»</i>", rkb.AD_BUTTON)


@router.message(AdminPanel.ad_button_text, F.text == rkb.A_AD_NO_BUTTON)
async def skip_button(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.update_data(ad_button_text=None, ad_button_url=None)
    await _ask_every(message, state)


@router.message(AdminPanel.ad_button_text, F.text)
async def set_button_text(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    await state.update_data(ad_button_text=(message.text or "").strip()[:40])
    await _ask(message, state, AdminPanel.ad_button_url,
               "Куда ведёт кнопка? Пришлите ссылку.\n\n"
               "<i>https://t.me/канал или https://сайт.ру</i>")


@router.message(AdminPanel.ad_button_url, F.text)
async def set_button_url(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    url = (message.text or "").strip()
    if not URL_RE.match(url):
        await _ask(message, state, AdminPanel.ad_button_url,
                   "⚠️ Ссылка должна начинаться с https:// — например, "
                   "<code>https://t.me/mychannel</code>")
        return
    await state.update_data(ad_button_url=url)
    await _ask_every(message, state)


async def _ask_every(message: Message, state: FSMContext, error: str | None = None) -> None:
    text = ("Как часто показывать? Пришлите число — раз во сколько анкет.\n\n"
            "<i>10 — золотая середина. Чаще 5 раздражает и люди уходят.</i>")
    await _ask(message, state, AdminPanel.ad_every,
               f"⚠️ {error}\n\n{text}" if error else text)


@router.message(AdminPanel.ad_every, F.text)
async def set_every(message: Message, state: FSMContext) -> None:
    await screen.drop(message)
    bot, chat_id = message.bot, message.chat.id
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (3 <= int(raw) <= 100):
        await _ask_every(message, state, "Нужно число от 3 до 100.")
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
