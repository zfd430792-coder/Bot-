"""Рекламные посты между анкетами. Только для владельцев."""
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


async def ads_view() -> tuple[str, object]:
    rows = await ads_repo.list_all()
    if not rows:
        return INTRO + "\n\n<b>Постов пока нет.</b>", kb.ads_list([])

    shows = await ads_repo.total_shows()
    lines = [INTRO, f"\n<b>Постов: {len(rows)}</b> · показов всего: {shows}\n"]
    for ad in rows:
        mark = "🟢 активен" if ad["is_active"] else "⚪️ выключен"
        lines.append(
            f"<b>#{ad['id']} {profile_service.esc(ad['title'])}</b> — {mark}\n"
            f"   раз в {ad['every_n']} анкет · показов: {ad['shows']}"
        )
    return "\n".join(lines), kb.ads_list(rows)


async def show_list(message: Message, edit: bool = False) -> None:
    text, markup = await ads_view()
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:ads")
async def open_ads(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.menu)
    await call.answer()
    await show_list(call.message, edit=True)


@router.message(Command("ads"))
async def ads_command(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminPanel.menu)
    await show_list(message)


# ─────────────────────────── Создание поста ─────────────────────────────────

@router.callback_query(F.data == "adm:ad_new")
async def ask_title(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanel.ad_title)
    await state.update_data(ad_draft={})
    await call.answer()
    await call.message.answer(
        "📣 <b>Новый пост</b>\n\nКак назовём его в списке? "
        "Название видите только вы.\n\n<i>Например: «Канал знакомств, март»</i>",
        reply_markup=kb.AD_CANCEL,
    )


@router.message(AdminPanel.ad_title, F.text)
async def set_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:60]
    if len(title) < 2:
        await message.answer("Название слишком короткое.")
        return
    await state.update_data(ad_title=title)
    await state.set_state(AdminPanel.ad_content)
    await message.answer(
        "Теперь пришлите сам пост — текст, фото, видео, гифку, что угодно.\n\n"
        "Он уйдёт людям ровно в том виде, в каком придёт сюда.\n\n"
        "⚠️ <i>Не удаляйте это сообщение из чата: бот копирует пост именно "
        "из него.</i>",
        reply_markup=kb.AD_CANCEL,
    )


@router.message(AdminPanel.ad_content)
async def set_content(message: Message, state: FSMContext) -> None:
    preview = (message.text or message.caption or "медиа без подписи")[:120]
    await state.update_data(ad_chat=message.chat.id, ad_message=message.message_id,
                            ad_preview=preview)
    await state.set_state(AdminPanel.ad_button_text)
    await message.answer(
        "Нужна кнопка под постом? Пришлите её текст.\n\n"
        "<i>Например: «Перейти в канал»</i>",
        reply_markup=kb.AD_NO_BUTTON,
    )


@router.callback_query(F.data == "adm:ad_nobutton", AdminPanel.ad_button_text)
async def skip_button(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ad_button_text=None, ad_button_url=None)
    await state.set_state(AdminPanel.ad_every)
    await call.answer()
    await ask_every(call.message)


@router.message(AdminPanel.ad_button_text, F.text)
async def set_button_text(message: Message, state: FSMContext) -> None:
    await state.update_data(ad_button_text=(message.text or "").strip()[:40])
    await state.set_state(AdminPanel.ad_button_url)
    await message.answer(
        "Куда ведёт кнопка? Пришлите ссылку.\n\n"
        "<i>https://t.me/канал или https://сайт.ру</i>",
        reply_markup=kb.AD_CANCEL,
    )


@router.message(AdminPanel.ad_button_url, F.text)
async def set_button_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not URL_RE.match(url):
        await message.answer(
            "Ссылка должна начинаться с https:// — например, "
            "<code>https://t.me/mychannel</code>"
        )
        return
    await state.update_data(ad_button_url=url)
    await state.set_state(AdminPanel.ad_every)
    await ask_every(message)


async def ask_every(message: Message) -> None:
    await message.answer(
        "Как часто показывать? Пришлите число — раз во сколько анкет.\n\n"
        "<i>10 — золотая середина. Чаще 5 раздражает и люди уходят.</i>",
        reply_markup=kb.AD_CANCEL,
    )


@router.message(AdminPanel.ad_every, F.text)
async def set_every(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (3 <= int(raw) <= 100):
        await message.answer("Нужно число от 3 до 100.")
        return

    data = await state.get_data()
    if not data.get("ad_message"):
        await state.set_state(AdminPanel.menu)
        await message.answer("Пост потерялся — начните заново.")
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
    await state.set_state(AdminPanel.menu)
    await message.answer(
        f"✅ Пост #{ad_id} создан и уже показывается — раз в {raw} анкет.\n\n"
        "Вот как его увидят люди:"
    )

    ad = await ads_repo.get(ad_id)
    await message.bot.copy_message(
        message.chat.id, ad["src_chat_id"], ad["src_message_id"],
        reply_markup=ads_service.markup(ad),
    )
    await show_list(message)


# ──────────────────────── Управление постами ────────────────────────────────

@router.callback_query(F.data.startswith("adm:ad_view:"))
async def preview_ad(call: CallbackQuery, bot: Bot) -> None:
    ad_id = int((call.data or "0").split(":")[-1])
    ad = await ads_repo.get(ad_id)
    if ad is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"📣 <b>#{ad['id']} {profile_service.esc(ad['title'])}</b>\n"
        f"Показов: {ad['shows']} · раз в {ad['every_n']} анкет\n"
        f"Статус: {'🟢 активен' if ad['is_active'] else '⚪️ выключен'}"
    )
    try:
        await bot.copy_message(call.message.chat.id, ad["src_chat_id"],
                               ad["src_message_id"],
                               reply_markup=ads_service.markup(ad))
    except Exception:
        await call.message.answer(
            "⚠️ Исходное сообщение недоступно — видимо, его удалили. "
            "Создайте пост заново."
        )
    await call.message.answer("Действия:", reply_markup=kb.ad_confirm(ad_id))


@router.callback_query(F.data.startswith("adm:ad_toggle:"))
async def toggle_ad(call: CallbackQuery) -> None:
    ad_id = int((call.data or "0").split(":")[-1])
    ad = await ads_repo.get(ad_id)
    if ad is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    await ads_repo.set_active(ad_id, not ad["is_active"])
    await call.answer("Выключен" if ad["is_active"] else "Включён")
    await show_list(call.message, edit=True)


@router.callback_query(F.data.startswith("adm:ad_del:"))
async def delete_ad(call: CallbackQuery) -> None:
    ad_id = int((call.data or "0").split(":")[-1])
    await ads_repo.delete(ad_id)
    await call.answer("Пост удалён")
    await show_list(call.message, edit=True)
