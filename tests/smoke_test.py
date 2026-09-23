"""Сквозной прогон бота без обращения к Telegram.

Проходит весь путь: капча -> предупреждение -> анкета -> лента -> лайки ->
совпадение -> жалоба -> админка -> верификация -> бан. Полезно запускать
после любых правок: python3 tests/smoke_test.py

Управление ботом — inline-кнопками: нажатие здесь — это callback с данными
кнопки (h.click), а h.act нажимает кнопку текущего экрана по началу её
данных — например, «br:like:» у карточки в ленте. Нижняя кнопка осталась
одна — геопозиция на шаге города. Надписи нижних кнопок прежней версии бот
по-прежнему понимает: их шлёт h.press (это обычный текст).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Настройки задаём до импорта приложения
TMP_DB = Path(tempfile.mkdtemp()) / "smoke.db"
os.environ.update(
    BOT_TOKEN="123456:TEST-TOKEN",
    ADMIN_IDS="900001",
    DB_PATH=str(TMP_DB),
    THROTTLE_SECONDS="0",
    RULES_DELAY_SECONDS="1",
    CAPTCHA_MIN_SOLVE_MS="400",
    CAPTCHA_MAX_ATTEMPTS="5",
    LIKES_LIMIT_PER_DAY="50",
    # Отдельная база Redis, чтобы не мешать боевой
    REDIS_URL=os.environ.get("TEST_REDIS_URL", "redis://localhost:6399/15"),
    REDIS_PREFIX="smoketest",
)

from aiogram import Bot, Dispatcher                                    # noqa: E402
from aiogram.client.default import DefaultBotProperties                # noqa: E402
from aiogram.enums import ParseMode                                    # noqa: E402
from aiogram.fsm.storage.base import StorageKey                        # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage                   # noqa: E402
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove    # noqa: E402

from app import handlers, middlewares, texts                           # noqa: E402
from app.config import get_settings                                    # noqa: E402
from app.db import ads as ads_repo                                     # noqa: E402
from app.db import captcha as captcha_repo                             # noqa: E402
from app.db import moderation as mod_repo                              # noqa: E402
from app.db import reactions as reactions_repo                         # noqa: E402
from app.db import users as users_repo                                 # noqa: E402
from app.db.database import db                                         # noqa: E402
from app.handlers import verification as verification_handlers         # noqa: E402
from app.keyboards import reply as rkb                                 # noqa: E402
from app.services import antifraud, reengagement, screen               # noqa: E402
from app.services import captcha as captcha_service                    # noqa: E402
from app.services import profile as profile_service                    # noqa: E402
from main import build_storage                                         # noqa: E402
from tests.fake_telegram import (                                      # noqa: E402
    FakeSession, callback_update, location_update, message_update,
    photo_update, video_note_update, video_update,
)

ALICE, BOB, CAROL, ADMIN = 100001, 100002, 100003, 900001
DAVE, EVE, FRANK = 100004, 100005, 100006
GLEB, HELEN, MOD1, MOD2 = 100007, 100008, 100009, 100010
NINA, OLEG = 100011, 100012
SCREEN = 100013
RESTART, NEWBIE, CAPTCHA_LOOK = 100014, 100015, 100016
M_SAMARA, M_REGION, F_SAMARA, F_REGION, F_TLT, F_MSK, F_LEGACY = range(100020, 100027)
VIEWER, AGE_VIEWER, AGE_28, AGE_29, AGE_32, AGE_33 = range(100030, 100036)
SNEAKY, RETURNING, VERA = 100040, 100041, 100042
EXTRAS = list(range(200001, 200009))        # массовка для ленты

passed = failed = 0


def check(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


class Harness:
    def __init__(self, storage) -> None:
        self.session = FakeSession()
        self.bot = Bot(token=get_settings().bot_token, session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.storage = storage
        self.dp = Dispatcher(storage=storage)
        middlewares.setup(self.dp, get_settings())
        handlers.setup(self.dp)

    async def feed(self, update) -> None:
        await self.dp.feed_update(self.bot, update)

    async def text(self, user_id: int, value: str, **kwargs) -> int:
        """Возвращает id отправленного сообщения — чтобы проверять его удаление."""
        update = message_update(self.bot, user_id, value, **kwargs)
        await self.feed(update)
        return update.message.message_id

    # Нижняя кнопка прежней версии — это сообщение с её надписью
    press = text

    def screen_key(self, user_id: int) -> StorageKey:
        return StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id,
                          destiny=screen.DESTINY)

    async def screen_ids(self, user_id: int) -> list[int]:
        """Сообщения, которые бот сейчас считает экраном пользователя."""
        data = await self.dp.storage.get_data(self.screen_key(user_id))
        return list(data.get(screen.MESSAGES) or [])

    async def click(self, user_id: int, data: str, *, message_id: int | None = None,
                    **kwargs) -> None:
        """Inline-кнопка — по умолчанию на сообщении, которое сейчас на экране."""
        if message_id is None:
            ids = await self.screen_ids(user_id)
            message_id = ids[-1] if ids else None
        await self.feed(callback_update(self.bot, user_id, data,
                                        message_id=message_id, **kwargs))

    def buttons(self, user_id: int) -> list[str]:
        """Надписи inline-кнопок последнего сообщения бота в чате."""
        return [text for text, _ in self.session.buttons(user_id)]

    def data(self, user_id: int) -> list[str]:
        """callback_data inline-кнопок последнего сообщения бота в чате."""
        return [data for _, data in self.session.buttons(user_id) if data]

    async def act(self, user_id: int, prefix: str, **kwargs) -> str | None:
        """Нажимает кнопку текущего экрана, чьи данные начинаются с prefix, —
        на том сообщении экрана, где она есть (под карточкой может висеть
        вопрос со своими кнопками)."""
        ids = await self.screen_ids(user_id)
        for message_id in reversed(ids):
            for _, data in self.session.by_message.get((user_id, message_id), []):
                if data and data.startswith(prefix):
                    await self.click(user_id, data, message_id=message_id, **kwargs)
                    return data
        seen = [self.session.by_message.get((user_id, i), []) for i in ids]
        check(False, f"у {user_id} на экране нет кнопки {prefix!r}: {seen}")
        return None

    async def photo(self, user_id: int) -> None:
        await self.feed(photo_update(self.bot, user_id))

    async def state_data(self, user_id: int) -> dict:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.dp.storage.get_data(key)

    def keyboard(self, user_id: int) -> list[str]:
        """Нижняя клавиатура — осталась только у запроса геопозиции."""
        return self.session.keyboard(user_id)

    def said(self, needle: str) -> bool:
        return any(needle.lower() in t.lower() for t in self.session.texts())

    def to(self, user_id: int) -> list:
        """Всё, что бот отправил этому человеку."""
        return [c for c in self.session.calls if getattr(c, "chat_id", None) == user_id]

    def clear(self) -> None:
        self.session.clear()


def markup_data(call) -> list[str]:
    markup = getattr(call, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def solve_captcha(h: Harness, user_id: int, *, correctly: bool = True,
                        wait: bool = True) -> None:
    """Читает правильный ответ из состояния — так может только тест, не бот."""
    if wait:
        await asyncio.sleep(0.5)
    data = await h.state_data(user_id)
    correct = set(data["cap_correct"])
    token_by_label = {label: token for token, label in data["cap_tokens"].items()}
    target = correct if correctly else ({1, 2, 3} - correct or {15})
    for label in sorted(target):
        await h.click(user_id, f"cap:tok:{token_by_label[label]}")
    await h.click(user_id, "cap:done")


async def register(h: Harness, user_id: int, *, gender: str, looking: str,
                   age: str, name: str, city: str) -> None:
    await h.text(user_id, "/start")
    await solve_captcha(h, user_id)
    await h.click(user_id, "onb:next")
    await asyncio.sleep(get_settings().rules_delay_seconds + 0.4)
    await h.click(user_id, "onb:accept")
    await h.click(user_id, f"reg:gender:{gender}")
    await h.click(user_id, f"reg:look:{looking}")
    await h.text(user_id, age)
    await h.text(user_id, name)
    await h.photo(user_id)
    await h.text(user_id, "Люблю горы, кофе и долгие разговоры.")
    await h.text(user_id, city)
    await h.click(user_id, "reg:confirm")


async def make_profile(user_id: int, *, gender: str, name: str,
                       city: str = "Волгоград", region: str | None = "Волгоградская область",
                       lat: float = 48.708, lon: float = 44.513, age: int = 25) -> None:
    """Готовая анкета напрямую в базе — чтобы не проходить мастер ради массовки."""
    await users_repo.ensure_user(user_id, f"user{user_id}", name)
    await users_repo.update_user(
        user_id, captcha_passed=1, rules_accepted=1, registered=1, is_active=1,
        name=name, gender=gender, looking_for="any", age=age, about="Тестовая анкета",
        media_type="photo", media_id=f"photo-{user_id}", city=city, region=region,
        country="RU", lat=lat, lon=lon, geo_source="city", search_scope="city",
    )


async def matches_of(user_id: int) -> int:
    return int(await db.fetchval(
        "SELECT COUNT(*) FROM matches WHERE user_a = ? OR user_b = ?",
        (user_id, user_id), default=0,
    ))


async def main() -> int:
    settings = get_settings()
    await db.connect(settings.db_path)
    if os.environ.get("TEST_REDIS_URL") == "memory":
        # Без Redis (например, на Windows): диалоги живут в памяти процесса
        storage = MemoryStorage()
    else:
        storage = await build_storage(settings)
        await storage.redis.flushdb()      # прогон должен начинаться с чистого листа
    try:
        return await scenarios(Harness(storage), settings, storage)
    finally:
        # Закрываем даже при падении: иначе процесс виснет на открытых соединениях
        await storage.close()
        await db.close()


async def scenarios(h: "Harness", settings, storage) -> int:

    # ── 1. Проверка username ────────────────────────────────────────────────
    section("1. Доступ без username")
    await h.text(CAROL, "/start", username=None)
    check(h.said("Нужен username"), "без @username бот не пускает")
    check(h.data(CAROL) == ["onb:username"], "кнопка «Я поставил username» — под сообщением")
    h.clear()

    # ── 2. Капча ────────────────────────────────────────────────────────────
    section("2. Капча")
    # Задание должно проверять, человек ли ты, а не остроту зрения:
    # зелёный рядом с бирюзовым путает живых людей
    clashes = 0
    for _ in range(300):
        shape, color, cells, _ = captcha_service._pick_cells()
        colors = {c for _, c in cells}
        shapes = {s_ for s_, _ in cells}
        clashes += len(colors & captcha_service._clashing(
            color, captcha_service.CONFUSABLE_COLORS))
        clashes += len(shapes & captcha_service._clashing(
            shape, captcha_service.CONFUSABLE_SHAPES))
    check(clashes == 0, "похожие цвета и формы не встречаются в одном задании")

    # Первая блокировка короткая, повторные — длиннее
    blocks = []
    for _ in range(3):
        for _ in range(cfg_attempts := get_settings().captcha_max_attempts):
            left, minutes = await captcha_repo.register_fail(
                777777, cfg_attempts, get_settings().captcha_block_minutes)
        blocks.append(minutes)
    check(blocks[0] == get_settings().captcha_block_minutes,
          f"первая блокировка короткая ({blocks[0]} мин)")
    check(blocks[1] > blocks[0] and blocks[2] > blocks[1],
          f"повторные длиннее: {blocks}")
    await captcha_repo.register_pass(777777)
    check(await captcha_repo.blocked_seconds(777777) == 0,
          "успешная проверка снимает блокировку")

    await h.text(ALICE, "/start")
    check(h.session.of_type("SendPhoto"), "капча приходит картинкой")
    check(h.said("выберите"), "задание сформулировано текстом")
    cells = [d for d in h.data(ALICE) if d.startswith("cap:tok:")]
    check(len(cells) == 15 and "cap:done" in h.data(ALICE),
          "номера клеток и «Готово» — кнопками под картинкой")
    check(all(re.fullmatch(r"cap:tok:[0-9a-f]{10}", d) for d in cells),
          "в кнопках случайные токены — ни номера, ни ответа")
    check(not any(isinstance(getattr(c, "reply_markup", None), ReplyKeyboardRemove)
                  for c in h.session.calls),
          "новичку не мигаем служебным сообщением: нижней клавиатуры у него нет")
    h.clear()

    tokens = {label: token for token, label in (await h.state_data(ALICE))["cap_tokens"].items()}
    await h.click(ALICE, f"cap:tok:{tokens[7]}")
    check(h.session.of_type("EditMessageReplyMarkup") and "✅7" in h.buttons(ALICE),
          "выбранная клетка отмечена ✅ прямо на кнопке")
    check(not h.session.of_type("SendMessage") and not h.session.of_type("SendPhoto"),
          "нажатие клетки не присылает новых сообщений")
    await h.click(ALICE, f"cap:tok:{tokens[7]}")
    check((await h.state_data(ALICE))["cap_selected"] == [], "повторное нажатие снимает выбор")
    await h.click(ALICE, "cap:done")
    check(any(getattr(c, "show_alert", False) for c in h.session.of_type("AnswerCallbackQuery"))
          and h.said("хотя бы одну клетку"), "пустой ответ — всплывающая подсказка")
    h.clear()

    await solve_captcha(h, ALICE, correctly=True, wait=False)
    check(h.said("Слишком быстро"), "мгновенный ответ отбивается как ботовский")
    h.clear()

    await solve_captcha(h, ALICE, correctly=False)
    check(h.said("Неверно"), "неправильный набор клеток не проходит")
    user = await users_repo.get_user(ALICE)
    check(user["captcha_passed"] == 0, "после ошибки капча не засчитана")
    h.clear()

    await solve_captcha(h, ALICE, correctly=True)
    user = await users_repo.get_user(ALICE)
    check(user["captcha_passed"] == 1, "верный ответ проходит проверку")
    check(h.said("Привет"), "после капчи появляется приветствие")
    check(h.data(ALICE) == ["onb:next"], "под приветствием кнопка «Далее»")
    h.clear()

    # ── 3. Предупреждение с задержкой ───────────────────────────────────────
    section("3. Предупреждение о мошенниках")
    await h.click(ALICE, "onb:next")
    check(any("мошенник" in (c.text or "") for c in h.session.of_type("EditMessageText")),
          "предупреждение встаёт на место приветствия — правкой того же сообщения")
    check(h.said("Кнопка появится через"), "идёт обратный отсчёт")
    check(h.data(ALICE) == [], "кнопки «Принимаю» ещё нет")

    await asyncio.sleep(get_settings().rules_delay_seconds + 0.5)
    check(h.data(ALICE) == ["onb:accept"], "через паузу появилась кнопка «Принимаю»")
    h.clear()

    await h.click(ALICE, "onb:accept")
    user = await users_repo.get_user(ALICE)
    check(user["rules_accepted"] == 1, "согласие с правилами сохранено")
    check(h.said("Ваш пол"), "сразу начинается анкета")
    check(h.data(ALICE) == ["reg:gender:m", "reg:gender:f"], "пол выбирается кнопкой")
    h.clear()

    # ── 4. Анкета ───────────────────────────────────────────────────────────
    section("4. Заполнение анкеты")
    await h.click(ALICE, "reg:gender:f")
    await h.click(ALICE, "reg:look:m")
    check(h.session.of_type("EditMessageText") and not h.session.of_type("SendMessage"),
          "шаги анкеты правятся на месте, новых сообщений нет")
    await h.text(ALICE, "семнадцать")
    check(h.said("Введите возраст числом"), "возраст словами не принимается")
    await h.text(ALICE, "16")
    check(h.said("Минимальный возраст"), "младше настроенного минимума не пускает")
    await h.text(ALICE, "26")
    check(h.data(ALICE) == ["reg:tgname"] and "Использовать «Тест»" in h.buttons(ALICE),
          "имя из Telegram предложено кнопкой")
    await h.text(ALICE, "http://spam.example")
    check(h.said("Не получилось разобрать имя"), "ссылку вместо имени не берём")
    await h.text(ALICE, "Алиса 🌸✨")
    check((await users_repo.get_user(ALICE))["name"] == "Алиса",
          "эмодзи из имени вырезаются, а не ломают шаг")
    h.clear()

    await h.feed(video_update(h.bot, ALICE, duration=40))
    check(h.said("длиннее"), "видео длиннее 15 секунд отклоняется")
    await h.feed(video_update(h.bot, ALICE, duration=12))
    user = await users_repo.get_user(ALICE)
    check(user["media_type"] == "video", "короткое видео принято")
    check(h.data(ALICE) == ["reg:skip_about"], "описание можно пропустить кнопкой")
    h.clear()

    await h.text(ALICE, "Захожу сюда за живым общением. Пишите: t.me/spamchannel")
    check(h.said("нельзя оставлять ссылки"), "ссылки в описании блокируются")
    await h.text(ALICE, "Люблю книги, горы и настолки.")
    check(h.said("Откуда вы"), "дальше спрашивается город")
    check(h.keyboard(ALICE) == [rkb.LOCATION],
          "геопозиция — единственная нижняя кнопка: иначе Telegram её не отдаёт")
    h.clear()

    await h.text(ALICE, "Урюпинск")
    check(h.said("Не нашёл такой город"), "незнакомый город честно не найден")
    check(h.said("область"), "предложен запасной путь через область")
    await h.text(ALICE, "Волгоградская область")
    user = await users_repo.get_user(ALICE)
    check(user["region"] == "Волгоградская область", "область определена")
    check(user["city"] == "Урюпинск", "название посёлка сохранено как было")
    check(user["lat"] is not None, "координаты области подставлены")
    # Вопроса «где искать» больше нет: лента сама идёт от ближних к дальним
    check(h.said("Вот как её увидят другие"), "сразу показан предпросмотр анкеты")
    check(h.data(ALICE) == ["reg:confirm", "reg:restart"], "подтверждение — кнопками")
    check(h.keyboard(ALICE) == [], "нижняя кнопка геопозиции после шага города убрана")
    check(user["search_scope"] == users_repo.SCOPE_HOME,
          "соседние области — только после её согласия")
    h.clear()

    await h.click(ALICE, "reg:confirm")
    user = await users_repo.get_user(ALICE)
    check(user["registered"] == 1 and user["is_active"] == 1, "анкета опубликована")
    check(users_repo.age_window(18) == (17, 20), "написали «18» — ищем от 17 до 20")
    check(users_repo.age_window(user["age"]) == (25, 28),
          "возраст в ленте — от года младше до двух лет старше")
    check("m:search" in h.data(ALICE), "после публикации — меню с кнопками")
    h.clear()

    # ── 5. Второй пользователь и геопозиция ─────────────────────────────────
    section("5. Второй пользователь и поиск по геопозиции")
    await h.text(BOB, "/start")
    await solve_captcha(h, BOB)
    await h.click(BOB, "onb:next")
    await asyncio.sleep(get_settings().rules_delay_seconds + 0.4)
    await h.click(BOB, "onb:accept")
    await h.click(BOB, "reg:gender:m")
    await h.click(BOB, "reg:look:f")
    await h.text(BOB, "27")
    await h.click(BOB, "reg:tgname")
    check((await users_repo.get_user(BOB))["name"] == "Тест", "имя подставлено кнопкой")
    await h.photo(BOB)
    await h.text(BOB, "Инженер, играю на гитаре.")
    h.clear()

    await h.feed(location_update(h.bot, BOB, 48.71, 44.51))   # Волгоград
    user = await users_repo.get_user(BOB)
    check(user["city"] == "Волгоград", "город определён по геопозиции")
    check(user["geo_source"] == "gps", "источник координат — геопозиция")
    check(abs(user["lat"] - 48.71) < 0.02, "координаты сдвинуты лишь незначительно")
    check(h.said("Геопозиция принята"), "пользователю подтвердили приём")
    h.clear()

    await h.click(BOB, "reg:confirm")
    user = await users_repo.get_user(BOB)
    check(user["registered"] == 1, "анкета с геопозицией опубликована")

    # ── 6. Лента и совпадение ───────────────────────────────────────────────
    section("6. Лента, лайки и совпадение")
    h.clear()
    await h.click(BOB, "m:search")
    check(h.said("Алиса"), "Борису показана анкета Алисы")
    check(h.said("км от вас"), "с геопозицией видно расстояние")
    check({f"br:like:{ALICE}", f"br:note:{ALICE}", f"br:dislike:{ALICE}",
           f"br:report:{ALICE}", "m:home"} <= set(h.data(BOB)),
          "под анкетой inline-кнопки ❤️ 💌 👎 🚨 🏠 — с id этой анкеты")
    h.clear()

    await h.act(BOB, "br:like:")
    check(await users_repo.count_incoming_likes(ALICE) == 1, "лайк дошёл до Алисы")
    note_to_alice = [c for c in h.to(ALICE) if "понравились" in (getattr(c, "text", "") or "")]
    check(bool(note_to_alice), "Алисе пришло уведомление о симпатии")
    check(bool(note_to_alice) and markup_data(note_to_alice[-1]) == ["n:search"],
          "в уведомлении кнопка «❤️ Посмотреть»")
    h.clear()

    await h.click(ALICE, "m:search")
    check(h.said("Борис") or h.said("Тест"), "Алисе показан Борис")
    check(h.said("Вы понравились этому человеку"), "над анкетой сказано, что он её лайкнул")
    h.clear()
    await h.act(ALICE, "br:like:")
    check(h.said("Взаимная симпатия"), "сработало совпадение")
    check(h.said("@tester"), "выданы контакты для переписки")
    check(await matches_of(ALICE) == 1, "совпадение сохранено в базе")
    h.clear()

    # ── 7. Лимит лайков ─────────────────────────────────────────────────────
    section("7. Лимит лайков")
    await mod_repo.set_setting("likes_limit", "1")
    await users_repo.update_user(CAROL, username="carol")
    await register(h, CAROL, gender="f", looking="m", age="28",
                   name="Карина", city="Волгоград")
    h.clear()
    await h.click(BOB, "m:search")
    check((await h.state_data(BOB)).get("current") == CAROL, "Борису показана Карина")
    await h.act(BOB, "br:like:")
    popups = [c for c in h.session.of_type("AnswerCallbackQuery") if c.show_alert]
    check(bool(popups) and "Лимит лайков на сегодня исчерпан" in (popups[-1].text or ""),
          "лимит лайков — всплывающим окном")
    check(await matches_of(CAROL) == 0, "лайк сверх лимита не засчитан")
    check((await h.state_data(BOB)).get("current") == CAROL,
          "анкета осталась на экране — её можно пропустить")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 8. Жалоба ───────────────────────────────────────────────────────────
    section("8. Жалоба на анкету")
    await h.act(BOB, "br:report:")
    check(h.said("На что жалуемся"), "предложены причины жалобы")
    check("rep:scam" in h.data(BOB), "причины — кнопками под вопросом")
    await h.click(BOB, "rep:scam")
    check(h.data(BOB) == ["rep:send", "rep:cancel"], "можно отправить без комментария")
    check(h.session.of_type("EditMessageText"), "вопрос о комментарии — правкой того же сообщения")
    await h.text(BOB, "Просит перевести деньги на карту")
    check(h.said("Жалоба отправлена"), "жалоба принята")
    admin_calls = h.to(ADMIN)
    check(bool(admin_calls), "жалоба ушла администратору")
    check(any(markup_data(c) == ["n:reports"] for c in admin_calls),
          "у уведомления кнопка «Разобрать жалобы»")
    check(await mod_repo.count_open_reports() == 1, "жалоба записана в базу")
    check(await reactions_repo.has_reacted(BOB, CAROL),
          "на кого пожаловались, того лента больше не покажет")
    h.clear()

    # ── 9. Админ-панель ─────────────────────────────────────────────────────
    section("9. Админ-панель")
    await h.text(ADMIN, "/admin", username="boss")
    check(h.said("Админ-панель"), "панель открывается")
    panel_keys = h.data(ADMIN)
    check({"adm:stats", "adm:broadcast", "m:home"} <= set(panel_keys),
          "разделы админки — inline-кнопками")
    check(not h.said("Быстрые команды") and not h.said("не действуют ограничения"),
          "в админке только заголовок и кнопки")
    h.clear()
    await h.click(ADMIN, "adm:stats", username="boss")
    check(h.said("Статистика бота"), "статистика собирается")
    check(h.said("Совпадений"), "в статистике есть совпадения")
    check(h.said("Антинакрутка") and h.said("Напоминания"),
          "в статистике есть разделы защиты и напоминаний")
    check(h.session.of_type("EditMessageText") and not h.session.of_type("SendMessage"),
          "раздел открывается правкой того же сообщения")
    h.clear()
    await h.text(ADMIN, f"/find {CAROL}", username="boss")
    check(h.said("Карина"), "поиск пользователя работает")
    check(f"adm:card:ban:{CAROL}" in h.data(ADMIN),
          "действия с человеком — под его карточкой, с его id")
    h.clear()

    await h.click(ADMIN, "adm:config", username="boss")
    await h.click(ADMIN, "adm:cfg:reg", username="boss")
    check(await mod_repo.get_setting("registration_open") == "0",
          "приём новых анкет закрыт")
    h.clear()
    await h.text(999123, "/start", username="newbie")
    check(h.said("Регистрация временно приостановлена"),
          "новичок не может начать регистрацию")
    await h.click(ADMIN, "adm:cfg:reg", username="boss")
    check(await mod_repo.get_setting("registration_open") == "1",
          "приём анкет снова открыт")
    h.clear()

    await h.click(ADMIN, "adm:home", username="boss")
    await h.click(ADMIN, "adm:reports", username="boss")
    check(h.said("Жалоба #") and h.said("Карина"), "жалобы разбираются по одной с анкетой")
    await h.act(ADMIN, "adm:rep:decline:", username="boss")
    check(await mod_repo.count_open_reports() == 0, "жалоба отклонена")
    check(h.said("Открытых жалоб больше нет"), "после последней — назад в панель")
    h.clear()

    # ── 10. Верификация по требованию админа ────────────────────────────────
    section("10. Принудительная верификация кружком")
    await h.text(ADMIN, f"/find {CAROL}", username="boss")
    await h.act(ADMIN, "adm:card:req:", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_forced"] == 1, "требование верификации выставлено")
    check(h.said("запишет кружок"), "админ видит, что человек запишет кружок")
    h.clear()

    await h.click(CAROL, "m:search")
    check(h.said("Требуется верификация"), "до проверки бот закрыт")
    check(h.said("кружок") and h.said("приготовьте листок и ручку"),
          "пользователю объяснено: нужен кружок и листок")
    check(h.data(CAROL) == ["ver:send"], "записать кружок — кнопкой")
    h.clear()

    await make_profile(VIEWER, gender="m", name="Зритель", age=28)
    candidates = await users_repo.search_candidates(await users_repo.get_user(VIEWER))
    check(all(c["id"] != CAROL for c in candidates),
          "анкета на проверке скрыта из поиска")

    await h.click(CAROL, "ver:send")
    task = await mod_repo.current_verification(CAROL)
    code = task["code"]
    check(len(code) == 4 and code.isdigit(), "код — четыре цифры: их легко написать и назвать")
    check(task["action"] in texts.VERIFY_ACTIONS, "к коду выдано случайное действие")
    check(h.said(f"код <b>{code}</b>, а под ним — ник бота <b>@test_bot</b>"),
          "на листке — код, а под ним ник бота")
    check(h.said("прочитайте код вслух") and h.said(texts.VERIFY_ACTIONS[task["action"]]),
          "в кружке — листок, код вслух и действие")
    check(h.said("10 минут"), "сказано, сколько действует задание")
    check(not h.session.of_type("SendVideoNote"), "пример не загружен — задание без него")
    h.clear()

    await h.photo(CAROL)
    check(h.said("Нужен именно кружок"), "фото не принимается")
    h.clear()
    await h.feed(video_update(h.bot, CAROL, 5))
    check(h.said("Нужен именно кружок"), "обычное видео не принимается")
    h.clear()
    await h.feed(video_note_update(h.bot, CAROL, 6, forwarded=True))
    check(h.said("Пересланный кружок"), "пересланный кружок не принимается")
    h.clear()
    await h.feed(video_note_update(h.bot, CAROL, 1))
    check(h.said("слишком короткий"), "слишком короткий кружок не принимается")
    h.clear()
    await h.feed(video_note_update(h.bot, CAROL, 45))
    check(h.said("длиннее 20 секунд"), "слишком длинный кружок не принимается")
    check(not await mod_repo.awaiting_review(CAROL), "ничего из этого не ушло админу")
    check((await mod_repo.current_verification(CAROL))["code"] == code,
          "пока время не вышло, код прежний")
    h.clear()

    await db.execute(
        "UPDATE verifications SET issued_at = datetime('now', '-11 minutes') "
        "WHERE user_id = ? AND status = 'pending'", (CAROL,)
    )
    await h.feed(video_note_update(h.bot, CAROL, 6))
    check(h.said("Время на запись вышло"), "кружок после 10 минут не принят")
    check(not await mod_repo.awaiting_review(CAROL), "просроченный кружок админу не ушёл")
    task = await mod_repo.current_verification(CAROL)
    check(task["task_age"] is not None and task["task_age"] < 60, "выдано новое задание")
    check(h.said(f"<b>{task['code']}</b>"), "новый код на экране")
    h.clear()

    await h.feed(video_note_update(h.bot, CAROL, 6))
    check(h.said("Кружок отправлен"), "кружок принят")
    verifications = await mod_repo.pending_verifications()
    check(len(verifications) == 1, "заявка ждёт админа")
    check(any(markup_data(c) == ["n:verify"] for c in h.to(ADMIN)),
          "админу пришла заявка с кнопкой «Проверить»")
    check(any(type(c).__name__ == "SendVideoNote" for c in h.to(ADMIN)),
          "админу пришёл сам кружок")
    check(any(f"листок: <b>{task['code']}</b>, под ним <b>@test_bot</b>"
              in (getattr(c, "text", None) or "") for c in h.to(ADMIN)),
          "в уведомлении — что должно быть в кружке")
    h.clear()

    await h.click(ADMIN, "adm:verify", username="boss")
    check(h.said("Заявка #") and h.said("Карина"), "заявка открывается вместе с анкетой")
    check(h.said(f"листок: <b>{task['code']}</b>, под ним <b>@test_bot</b>")
          and h.said(f"код вслух: <b>{task['code']}</b>")
          and h.said(texts.VERIFY_ACTIONS[task["action"]]),
          "админ видит чек-лист: листок с ником бота, код вслух, действие")
    names = h.session.method_names()
    check("SendPhoto" in names and "SendVideoNote" in names
          and names.index("SendPhoto") < names.index("SendVideoNote"),
          "сначала анкета с фото, потом кружок — лицо легко сверить")
    await h.act(ADMIN, "adm:ver:ok:", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_status"] == "verified", "верификация подтверждена")
    check(user["verify_forced"] == 0, "блокировка снята")
    candidates = await users_repo.search_candidates(await users_repo.get_user(VIEWER))
    check(any(c["id"] == CAROL for c in candidates), "после проверки анкета снова в поиске")
    await users_repo.update_user(VIEWER, is_active=0)
    h.clear()
    await h.click(CAROL, "m:profile")
    check(h.said("28 лет ✅"), "в анкете появилась зелёная галочка")
    check(not h.said("☑️"), "серой галочки нигде нет")
    h.clear()

    # ── 11. Бан и разбан ────────────────────────────────────────────────────
    section("11. Бан и разбан")
    await h.text(ADMIN, f"/ban {CAROL} 2d спам в анкете", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["is_banned"] == 1, "пользователь забанен")
    check(user["banned_until"] is not None, "срок бана записан")
    check(h.said("Доступ заблокирован"), "пользователь уведомлён")
    h.clear()

    await h.click(CAROL, "m:search")
    check(h.said("Доступ заблокирован"), "забаненный не может пользоваться ботом")
    h.clear()

    await h.text(ADMIN, f"/unban {CAROL}", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["is_banned"] == 0, "бан снят")
    h.clear()

    # ── 12. Рассылка ────────────────────────────────────────────────────────
    section("12. Рассылка")
    await h.click(ADMIN, "adm:broadcast", username="boss")
    await h.click(ADMIN, "adm:bc:registered", username="boss")
    check(h.said("Получателей"), "аудитория посчитана")
    source = await h.text(ADMIN, "Привет! У нас новые анкеты 🎉", username="boss")
    check(h.said("Так это увидят люди"), "показан предпросмотр")
    check(source not in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "сообщение для рассылки не удалено — его копируют")
    h.clear()
    await h.click(ADMIN, "adm:bc:send", username="boss")
    await asyncio.sleep(1.0)
    copies = h.session.of_type("CopyMessage")
    check(len(copies) >= 3, f"сообщения разосланы ({len(copies)} шт.)")
    row = await db.fetchone("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    check(row is not None and row["total"] >= 3, "рассылка записана в журнал")
    h.clear()

    # ── 13. Настроек нет: бот ищет сам ──────────────────────────────────────
    section("13. Без настроек: возраст и место бот выбирает сам")
    await h.text(BOB, "/start")
    check(h.data(BOB) == ["m:search", "m:profile"],
          "в меню только анкеты и своя анкета (поддержка ещё не указана)")
    h.clear()

    # Нижние кнопки прежней версии никуда не деваются — ведут в меню и в ленту,
    # а сама старая клавиатура снимается
    await h.dp.storage.update_data(h.screen_key(BOB), {screen.KB_CLEAN: False})
    tap = await h.press(BOB, "⚙️ Настройки")
    check(h.said("Главное меню") and not h.said("Не понял"),
          "старая кнопка «Настройки» открывает меню")
    check(tap in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "нажатие старой кнопки убрано из чата")
    check(any(isinstance(getattr(c, "reply_markup", None), ReplyKeyboardRemove)
              for c in h.session.calls), "старая нижняя клавиатура снята")
    h.clear()
    await h.press(BOB, "❤️ Кто меня лайкнул (2)")
    check(h.said("Вы посмотрели все анкеты"),
          "старая кнопка «Кто меня лайкнул» открывает ленту")
    h.clear()
    await h.press(BOB, "✏️ Изменить анкету")
    check("pr:about" in h.data(BOB), "старая «Изменить анкету» ведёт в анкету")
    h.clear()

    # Вернулся спустя неделю, так и не пройдя капчу: экран бот уже забыл,
    # а нижняя клавиатура прежней версии у человека ещё может висеть
    await users_repo.ensure_user(RETURNING, "returning", "Вернувшийся")
    await db.execute("UPDATE users SET created_at = datetime('now', '-7 days') WHERE id = ?",
                     (RETURNING,))
    await h.text(RETURNING, "/start", username="returning")
    check(any(isinstance(getattr(c, "reply_markup", None), ReplyKeyboardRemove)
              for c in h.to(RETURNING)),
          "вернувшемуся спустя неделю старую нижнюю клавиатуру снимаем")
    h.clear()

    # Возраст: 30 лет — лента показывает 29–32, а 28 и 33 уже нет
    penza = dict(city="Пенза", region="Пензенская область", lat=53.195, lon=45.018)
    await make_profile(AGE_VIEWER, gender="m", name="Пензенец", age=30, **penza)
    await users_repo.update_user(AGE_VIEWER, looking_for="f")
    for uid, age in ((AGE_28, 28), (AGE_29, 29), (AGE_32, 32), (AGE_33, 33)):
        await make_profile(uid, gender="f", name=f"Пензячка {age}", age=age, **penza)
    rows = await users_repo.search_candidates(await users_repo.get_user(AGE_VIEWER))
    found = {int(r["id"]) for r in rows}
    check(found == {AGE_29, AGE_32}, f"30 лет — видны 29–32 ({sorted(found)})")
    for uid in (AGE_VIEWER, AGE_28, AGE_29, AGE_32, AGE_33):
        await users_repo.update_user(uid, is_active=0)      # дальше не мешают
    h.clear()

    # ── 14. Своя анкета ─────────────────────────────────────────────────────
    section("14. Управление анкетой")
    await h.click(ALICE, "m:profile")
    keys = h.data(ALICE)
    check({"pr:media", "pr:about", "pr:refill"} <= set(keys),
          "в анкете: новое фото, новое описание, заполнить заново")
    check(not any("Имя" in k or "Возраст" in k or "Город" in k for k in h.buttons(ALICE)),
          "имя, возраст и город отдельно не правятся")
    h.clear()

    await h.click(ALICE, "pr:about")
    await h.text(ALICE, "Теперь люблю ещё и велосипед.")
    check((await users_repo.get_user(ALICE))["about"] == "Теперь люблю ещё и велосипед.",
          "описание обновлено")
    check(h.said("Описание обновлено"), "обновлённая анкета снова на экране")
    await h.click(ALICE, "pr:media")
    await h.photo(ALICE)
    check((await users_repo.get_user(ALICE))["media_type"] == "photo", "фото обновлено")
    h.clear()

    # Заполнить заново: те же шаги, старые ответы в прогресс не попадают
    await h.click(ALICE, "pr:refill")
    check(h.said("Шаг 1 из 7"), "заполнение заново начинается с первого шага")
    check(not h.said("Урюпинск") and not h.said("26 лет"),
          "старые ответы не выдаются за заполненные")
    await h.click(ALICE, "reg:gender:f")
    await h.click(ALICE, "reg:look:m")
    await h.text(ALICE, "27")
    await h.text(ALICE, "Алиса")
    await h.photo(ALICE)
    await h.click(ALICE, "reg:skip_about")
    await h.text(ALICE, "Волгоград")
    check(h.said("Вот как её увидят другие"), "в конце — предпросмотр")
    h.clear()
    await h.click(ALICE, "reg:confirm")
    user = await users_repo.get_user(ALICE)
    check(user["age"] == 27 and user["city"] == "Волгоград" and user["about"] == "",
          "анкета заполнена заново")
    check(h.said("Анкета обновлена"), "бот говорит, что анкета обновлена")
    check(await matches_of(ALICE) == 1, "пары после обновления анкеты сохранились")
    h.clear()

    await h.click(ALICE, "m:profile")
    await h.click(ALICE, "pr:hide")
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 0, "анкета скрыта из поиска")
    check("pr:show" in h.data(ALICE), "кнопка сменилась на «Показывать в поиске»")
    await h.click(ALICE, "pr:show")
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 1, "анкета снова видна")
    h.clear()
    await h.click(ALICE, "pr:del:yes")
    check((await users_repo.get_user(ALICE))["registered"] == 1,
          "без экрана подтверждения анкета не удаляется")
    await h.click(ALICE, "pr:del")
    await h.click(ALICE, "pr:del:yes")
    user = await users_repo.get_user(ALICE)
    check(user["registered"] == 0 and user["name"] is None, "анкета удалена")
    check(await matches_of(BOB) == 0, "совпадения удалённого убраны")

    # ── 15. Антинакрутка: скорость ──────────────────────────────────────────
    section("15. Антинакрутка: слишком быстрые реакции")
    cfg = get_settings()
    saved = (cfg.af_fast_streak, cfg.af_ratio_window, cfg.min_age)
    cfg.af_fast_streak, cfg.af_ratio_window = 4, 10_000   # долю лайков не проверяем

    await make_profile(DAVE, gender="m", name="Дмитрий")
    for i, extra in enumerate(EXTRAS):
        await make_profile(extra, gender="f", name=f"Гостья {i + 1}")
    h.clear()

    await h.click(DAVE, "m:search")
    for _ in range(6):
        target = (await h.state_data(DAVE)).get("current") or EXTRAS[0]
        await h.click(DAVE, f"br:dislike:{target}")

    user = await users_repo.get_user(DAVE)
    check(user["af_strikes"] == 1, "зафиксировано первое нарушение")
    check(user["captcha_passed"] == 0, "первое нарушение сбрасывает капчу")
    check(user["is_banned"] == 0, "с первого раза не банит")
    check(h.said("слишком быстро"), "пользователь предупреждён")
    admin_notified = any("Антинакрутка" in (getattr(c, "text", "") or "") for c in h.to(ADMIN))
    check(admin_notified, "администратор уведомлён")
    h.clear()

    await h.click(DAVE, "m:search")
    check(h.said("повторная проверка") and h.data(DAVE) == ["m:start"],
          "до новой капчи бот закрыт — кнопка ведёт к проверке")
    h.clear()
    await h.click(DAVE, "m:start")
    await solve_captcha(h, DAVE)
    user = await users_repo.get_user(DAVE)
    check(user["captcha_passed"] == 1, "капча пройдена заново")
    check(h.said("Проверка пройдена"), "пользователь с анкетой вернулся в меню")
    h.clear()

    # ── 16. Антинакрутка: лайки без единого пропуска ────────────────────────
    section("16. Антинакрутка: только лайки")
    cfg.af_fast_streak, cfg.af_ratio_window = 10_000, 5   # скорость не проверяем

    await make_profile(EVE, gender="f", name="Ева")
    for extra in EXTRAS[:5]:
        await reactions_repo.add_reaction(EVE, extra, "like")
    h.clear()

    action = await antifraud.check(h.bot, EVE, cfg)
    check(action == "captcha", "серия из одних лайков распознана как накрутка")
    user = await users_repo.get_user(EVE)
    check(user["af_strikes"] == 1, "нарушение засчитано")

    check(await antifraud.check(h.bot, EVE, cfg) is None,
          "повторный страйк не начисляется, пока нет новой серии")

    for extra in EXTRAS[5:8] + [EXTRAS[0] + 900, EXTRAS[0] + 901]:
        await reactions_repo.add_reaction(EVE, extra, "like")
    action = await antifraud.check(h.bot, EVE, cfg)
    check(action == "ban_temp", "вторая серия одних лайков — временный бан")
    user = await users_repo.get_user(EVE)
    check(user["is_banned"] == 1 and user["banned_until"] is not None,
          "бан выдан на срок")

    await mod_repo.unban_user(EVE)
    for i in range(5):
        await reactions_repo.add_reaction(EVE, 300100 + i, "like")
    action = await antifraud.check(h.bot, EVE, cfg)
    check(action == "ban_permanent", "третья серия — бессрочный бан")
    user = await users_repo.get_user(EVE)
    check(user["is_banned"] == 1 and user["banned_until"] is None,
          "бан бессрочный")
    h.clear()

    # Нормальное поведение не должно ловиться
    await make_profile(FRANK, gender="m", name="Фёдор")
    for i, extra in enumerate(EXTRAS[:5]):
        await reactions_repo.add_reaction(FRANK, extra, "like" if i else "dislike")
    check(await antifraud.check(h.bot, FRANK, cfg) is None,
          "один пропуск из пяти — уже не накрутка")
    cfg.af_fast_streak, cfg.af_ratio_window = saved[0], saved[1]
    await mod_repo.unban_user(EVE)
    h.clear()

    # ── 17. Напоминания уснувшим ────────────────────────────────────────────
    section("17. Напоминания тем, кто давно не заходил")
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    day_offset = (12 - now.hour) % 24          # чтобы у адресата был полдень
    night_offset = (3 - now.hour) % 24         # а здесь — три часа ночи
    day_lon = (day_offset if day_offset <= 12 else day_offset - 24) * 15
    night_lon = (night_offset if night_offset <= 12 else night_offset - 24) * 15

    check(reengagement.local_hour(day_lon, now) == 12, "местный час считается по долготе")
    check(reengagement.is_quiet(3, cfg) and not reengagement.is_quiet(12, cfg),
          "ночные часы распознаются")

    await db.execute(
        "UPDATE users SET last_active = datetime('now', '-3 days'), "
        "last_notify_at = NULL, notify_count = 0, lon = ? WHERE id = ?",
        (day_lon, FRANK),
    )
    rows = await reengagement.candidates(cfg)
    check(any(r["id"] == FRANK for r in rows), "уснувший пользователь попал в очередь")

    h.clear()
    sent = await reengagement.send_batch(h.bot, cfg)
    check(sent >= 1, f"напоминания отправлены ({sent} шт.)")
    check(h.data(FRANK) == ["n:search", "n:mute"],
          "в напоминании кнопки «смотреть» и «не напоминать»")
    user = await users_repo.get_user(FRANK)
    check(user["notify_count"] == 1, "счётчик напоминаний увеличен")
    check(user["last_notify_at"] is not None, "время напоминания записано")
    rows = await reengagement.candidates(cfg)
    check(not any(r["id"] == FRANK for r in rows),
          "повторно в тот же день не напоминаем")
    h.clear()

    await db.execute(
        "UPDATE users SET last_active = datetime('now', '-3 days'), "
        "last_notify_at = NULL, notify_count = 0, lon = ? WHERE id = ?",
        (night_lon, FRANK),
    )
    sent = await reengagement.send_batch(h.bot, cfg)
    check(sent == 0, "ночью не беспокоим")
    h.clear()

    # Текст подстраивается: есть лайки — зовём смотреть их
    await db.execute("UPDATE users SET lon = ? WHERE id = ?", (day_lon, FRANK))
    await reactions_repo.add_reaction(EXTRAS[6], FRANK, "like")
    rows = await reengagement.candidates(cfg)
    row = next(r for r in rows if r["id"] == FRANK)
    check(int(row["pending_likes"]) >= 1, "непросмотренные лайки посчитаны")
    check("понравились" in reengagement.compose(row), "текст зовёт посмотреть лайки")

    await reengagement.send_batch(h.bot, cfg)
    reminder_id = next((mid for method, mid in reversed(h.session.sent)
                        if getattr(method, "chat_id", None) == FRANK), None)
    h.clear()
    await h.click(FRANK, "n:mute", message_id=reminder_id)
    user = await users_repo.get_user(FRANK)
    check(user["notify_enabled"] == 0, "кнопка «не напоминать» работает")
    check(reminder_id in h.session.alive.get(FRANK, set()),
          "само напоминание остаётся в переписке")
    rows = await reengagement.candidates(cfg)
    check(not any(r["id"] == FRANK for r in rows), "отписавшемуся больше не пишем")
    h.clear()

    await users_repo.update_user(FRANK, notify_enabled=1, notify_count=3)
    await db.execute("UPDATE users SET last_active = datetime('now', '-9 days'), "
                     "last_notify_at = datetime('now', '-9 days') WHERE id = ?", (FRANK,))
    rows = await reengagement.candidates(cfg)
    check(not any(r["id"] == FRANK for r in rows),
          "после трёх проигнорированных напоминаний бот замолкает")
    await h.text(FRANK, "/start")
    user = await users_repo.get_user(FRANK)
    check(user["notify_count"] == 0, "возврат пользователя обнуляет счётчик")
    h.clear()

    # ── 18. Возрастной порог настраивается ──────────────────────────────────
    section("18. Возрастной порог задаётся настройкой")
    cfg.min_age = 16
    await users_repo.ensure_user(700001, "teen", "Подросток")
    await users_repo.update_user(700001, captcha_passed=1, rules_accepted=1)
    await h.text(700001, "/start", username="teen")
    await h.click(700001, "reg:gender:m", username="teen")
    await h.click(700001, "reg:look:f", username="teen")
    h.clear()
    await h.text(700001, "15", username="teen")
    check(h.said("Минимальный возраст"), "ниже настроенного порога не пускает")
    check(h.said("16"), "в сообщении указан настроенный порог")
    h.clear()
    await h.text(700001, "16", username="teen")
    user = await users_repo.get_user(700001)
    check(user["age"] == 16, "возраст на уровне порога принимается")
    cfg.min_age = saved[2]

    # ── 19. Лайк с сообщением ───────────────────────────────────────────────
    section("19. Лайк с сообщением")
    # Возраст 33 — чтобы Глебу первой попалась именно Елена, а не массовка
    await make_profile(GLEB, gender="m", name="Глеб", age=33)
    await make_profile(HELEN, gender="f", name="Елена", age=33)
    h.clear()

    await h.click(GLEB, "m:search")
    check((await h.state_data(GLEB)).get("current") == HELEN, "Глебу показана Елена")
    check(f"br:note:{HELEN}" in h.data(GLEB), "в ленте есть кнопка «Сообщение»")
    h.clear()

    await h.act(GLEB, "br:note:")
    check(h.said("Что написать"), "бот просит текст сообщения")
    check(h.data(GLEB) == ["br:cancel"], "передумать можно кнопкой «Отмена»")
    h.clear()
    await h.click(GLEB, "br:cancel")
    check(not h.session.of_type("SendPhoto"),
          "отмена просто убирает вопрос — анкета остаётся, заново её не шлём")
    await h.act(GLEB, "br:note:")
    h.clear()

    await h.text(GLEB, "Заходи в мой канал t.me/spam")
    check(h.said("нельзя оставлять ссылки"), "ссылки в сообщении блокируются")
    await h.text(GLEB, "я" * 400)
    check(h.said("Слишком длинно"), "длинное сообщение отклоняется")
    h.clear()

    await h.text(GLEB, "Привет! Тоже люблю горы — где снимали фото?")
    check(h.said("Сообщение отправлено"), "сообщение принято")
    note = await reactions_repo.get_note(GLEB, HELEN)
    check(note is not None and "горы" in note, "текст сохранён вместе с лайком")

    to_helen = h.to(HELEN)
    delivered = " ".join((getattr(c, "text", "") or "") for c in to_helen)
    check("написали вместе с лайком" in delivered, "Елене пришло уведомление")
    check("первыми в ленте" in delivered, "уведомление говорит, где ответить")
    check(any(markup_data(c) == ["n:search"] for c in to_helen),
          "в уведомлении кнопка «❤️ Посмотреть»")
    notice_id = next((mid for method, mid in reversed(h.session.sent)
                      if getattr(method, "chat_id", None) == HELEN), None)
    h.clear()

    # Кнопка уведомления открывает ленту, а само уведомление остаётся в чате
    await h.click(HELEN, "n:search", message_id=notice_id)
    check(h.said("Глеб"), "первой в ленте — анкета отправителя")
    check(h.said("где снимали фото"), "и его сообщение")
    check(notice_id in h.session.alive.get(HELEN, set()), "уведомление осталось в переписке")
    await h.act(HELEN, "br:like:")
    check(h.said("Взаимная симпатия"), "ответ взаимностью создаёт совпадение")
    check(await matches_of(HELEN) == 1, "совпадение сохранено")
    h.clear()

    # Не ответила сразу — лайк ждёт первым в ленте, даже вне её возраста
    await make_profile(200100, gender="m", name="Игорь")
    await reactions_repo.add_reaction(200100, HELEN, "like", "Сообщение из ленты")
    await h.click(HELEN, "m:search")
    check((await h.state_data(HELEN)).get("current") == 200100,
          "лайкнувший первым, даже если старше или младше её рамок")
    check(h.said("Сообщение из ленты"), "его сообщение видно на карточке")
    # Для разделов ниже Глеб и Елена снова обычного возраста
    for uid in (GLEB, HELEN):
        await users_repo.update_user(uid, age=25)
    h.clear()

    # ── 20. Модераторы ──────────────────────────────────────────────────────
    section("20. Модераторы с урезанными правами")
    await make_profile(MOD1, gender="m", name="Модератор")
    await make_profile(MOD2, gender="m", name="Модератор Два")
    h.clear()

    await h.click(ADMIN, "adm:staff", username="boss")
    await h.click(ADMIN, "adm:staff:add", username="boss")
    await h.text(ADMIN, str(MOD1), username="boss")
    user = await users_repo.get_user(MOD1)
    check(user["is_moderator"] == 1, "модератор назначен")
    check(h.said("Вас назначили модератором"), "модератор уведомлён")
    h.clear()

    await h.text(MOD1, "/start")
    check("👮 Модератор" in h.buttons(MOD1), "в меню появилась кнопка модератора")
    check("🛠 Админ-панель" not in h.buttons(MOD1), "кнопки админки у него нет")
    h.clear()

    await h.click(MOD1, "m:admin")
    check(h.said("Панель модератора"), "панель модератора открывается")
    panel = h.data(MOD1)
    check("adm:reports" in panel and "adm:ban" in panel, "жалобы и баны доступны")
    check("adm:broadcast" not in panel, "рассылки в меню нет")
    check("adm:ads" not in panel and "adm:staff" not in panel,
          "рекламы и модераторов в меню нет")
    check("adm:config" not in panel, "настроек бота в меню нет")
    h.clear()

    await h.click(MOD1, "adm:broadcast")
    check(not h.said("Кому отправляем"), "подделанное нажатие на рассылку не срабатывает")
    h.clear()

    await h.text(MOD1, f"/ban {GLEB} 1d проверка прав")
    user = await users_repo.get_user(GLEB)
    check(user["is_banned"] == 1, "модератор может забанить обычного пользователя")
    await h.text(MOD1, f"/unban {GLEB}")
    check((await users_repo.get_user(GLEB))["is_banned"] == 0,
          "модератор может снять бан")
    h.clear()

    await users_repo.update_user(MOD2, is_moderator=1)
    await h.text(MOD1, f"/ban {MOD2} попытка")
    check(h.said("только владелец"), "модератор не может забанить модератора")
    check((await users_repo.get_user(MOD2))["is_banned"] == 0, "цель не забанена")
    h.clear()

    await h.text(MOD1, f"/ban {ADMIN} попытка")
    check(h.said("владелец бота"), "модератор не может забанить владельца")
    h.clear()

    await h.click(ADMIN, "adm:staff", username="boss")
    check(f"adm:staff:del:{MOD1}" in h.data(ADMIN), "у каждого модератора своя кнопка снятия")
    await h.click(ADMIN, f"adm:staff:del:{MOD1}", username="boss")
    check((await users_repo.get_user(MOD1))["is_moderator"] == 0, "права сняты")
    h.clear()
    await h.click(MOD1, "m:admin")
    check(not h.said("Панель модератора"), "бывший модератор в панель не попадает")
    h.clear()

    # ── 21. Рекламные посты ─────────────────────────────────────────────────
    section("21. Реклама между анкетами")
    await h.click(ADMIN, "adm:ads", username="boss")
    check(h.said("Реклама"), "раздел рекламы открывается")
    h.clear()

    await h.click(ADMIN, "adm:ad:new", username="boss")
    await h.text(ADMIN, "Канал знакомств", username="boss")
    await h.text(ADMIN, "Подпишись на наш канал — там анонсы встреч!",
                 username="boss")
    await h.text(ADMIN, "Перейти в канал", username="boss")
    await h.text(ADMIN, "не-ссылка", username="boss")
    check(h.said("должна начинаться"), "неверная ссылка отклоняется")
    await h.text(ADMIN, "https://t.me/example", username="boss")
    await h.text(ADMIN, "2", username="boss")
    check(h.said("число от 3 до 100"), "слишком частый показ не разрешён")
    await h.text(ADMIN, "3", username="boss")

    ads = await ads_repo.list_all()
    check(len(ads) == 1, "пост создан")
    check(ads[0]["title"] == "Канал знакомств", "название сохранено")
    check(ads[0]["button_url"] == "https://t.me/example", "ссылка сохранена")
    check(ads[0]["every_n"] == 3, "частота показа сохранена")
    check(ads[0]["is_active"] == 1, "пост сразу активен")
    ad_id = ads[0]["id"]
    h.clear()

    # Массовка, чтобы было что листать до появления поста
    for index, extra in enumerate(range(200110, 200116)):
        await make_profile(extra, gender="m", name=f"Гость {index + 1}")

    async def browse_until_ad(viewer: int, steps: int) -> list:
        await h.click(viewer, "m:search")
        for _ in range(steps):
            await h.act(viewer, "br:dislike:")
        return [c for c in h.session.calls
                if type(c).__name__ == "CopyMessage"
                and getattr(c, "chat_id", None) == viewer]

    copies = await browse_until_ad(HELEN, 3)
    check(bool(copies), "рекламный пост показан в ленте")
    check(any("t.me/example" in str(getattr(c, "reply_markup", "")) for c in copies),
          "под постом кнопка со ссылкой")
    ad = await ads_repo.get(ad_id)
    check(ad["shows"] >= 1, "показ засчитан")
    h.clear()

    await h.click(ADMIN, "adm:ads", username="boss")
    check(f"adm:ad:open:{ad_id}" in h.data(ADMIN), "пост открывается кнопкой из списка")
    await h.click(ADMIN, f"adm:ad:open:{ad_id}", username="boss")
    await h.act(ADMIN, "adm:ad:off:", username="boss")
    ad = await ads_repo.get(ad_id)
    check(ad["is_active"] == 0, "пост выключается")
    shows_before = ad["shows"]
    h.clear()

    copies = await browse_until_ad(GLEB, 3)
    ad = await ads_repo.get(ad_id)
    check(ad["shows"] == shows_before and not copies,
          "выключенный пост не показывается")
    h.clear()

    await h.click(ADMIN, "adm:ads", username="boss")
    await h.click(ADMIN, f"adm:ad:open:{ad_id}", username="boss")
    await h.act(ADMIN, "adm:ad:del:", username="boss")
    check(not await ads_repo.list_all(), "пост удаляется")

    # ── 22. Ответный лайк не тратит лимит ───────────────────────────────────
    section("22. Ответ на чужой лайк не упирается в лимит")
    # Возраст 41 — чтобы Нина листала только своих, без массовки
    await make_profile(NINA, gender="f", name="Нина", age=41)
    for index, extra in enumerate(range(200200, 200204)):
        await make_profile(extra, gender="m", name=f"Прохожий {index + 1}", age=41)
    # Олег и Пётр старше её рамок: в ленте их нет, но лайкнуть её они могут
    await make_profile(OLEG, gender="m", name="Олег", age=45)
    await make_profile(200204, gender="m", name="Пётр", age=45)

    await mod_repo.set_setting("likes_limit", "1")
    h.clear()

    # Нина тратит весь суточный лимит на поиск
    await h.click(NINA, "m:search")
    await h.act(NINA, "br:like:")
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "лимит израсходован")
    h.clear()

    await h.act(NINA, "br:like:")
    check(h.said("Лимит лайков на сегодня исчерпан"),
          "новый лайк в поиске блокируется")
    check(h.said("Отвечать тем, кто лайкнул вас"),
          "бот подсказывает, что ответы не ограничены")
    h.clear()

    # А теперь её лайкнули — ответить она должна мочь
    await reactions_repo.add_reaction(OLEG, NINA, "like")
    await h.click(NINA, "m:search")
    check(h.said("Олег"), "анкета отправителя показана несмотря на лимит")
    check(h.buttons(NINA)[0] == "❤️", "на кнопке нет счётчика — лайк бесплатный")
    h.clear()

    await h.act(NINA, "br:like:")
    check(h.said("Взаимная симпатия"), "ответный лайк проходит при нулевом лимите")
    check(await matches_of(NINA) == 1, "совпадение создано")
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "ответ не ушёл в минус и лимит не тронут")
    h.clear()

    # Лайк с сообщением — тоже бесплатно
    await reactions_repo.add_reaction(200204, NINA, "like", "Привет из уведомления")
    await h.click(NINA, "m:search")
    await h.act(NINA, "br:like:")
    check(await matches_of(NINA) == 2,
          "ответ на лайк с сообщением тоже не требует лимита")
    h.clear()

    # Но исходящий лайк тому, кто её не лайкал, по-прежнему закрыт
    await h.click(NINA, "m:search")
    await h.act(NINA, "br:like:")
    check(h.said("Лимит лайков"), "лимит на исходящие лайки продолжает работать")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 23. На владельца ограничения не действуют ───────────────────────────
    section("23. Владелец без ограничений")
    await make_profile(ADMIN, gender="m", name="Владелец")
    await mod_repo.set_setting("likes_limit", "1")
    cfg.af_fast_streak, cfg.af_ratio_window = 3, 5
    h.clear()

    await h.click(ADMIN, "m:search", username="boss")
    for _ in range(4):
        await h.act(ADMIN, "br:like:", username="boss")
    check(not h.said("Лимит лайков"), "лимит лайков на владельца не действует")

    owner = await users_repo.get_user(ADMIN)
    check(owner["af_strikes"] == 0, "антинакрутка владельца не трогает")
    check(owner["is_banned"] == 0, "владелец не забанен автоматически")
    check(owner["captcha_passed"] == 1, "капча владельцу не показывается")
    await mod_repo.set_setting("likes_limit", "50")
    cfg.af_fast_streak, cfg.af_ratio_window = saved[0], saved[1]
    h.clear()

    # Капча не выдаётся даже после сброса
    await users_repo.update_user(ADMIN, captcha_passed=0)
    await h.text(ADMIN, "/start", username="boss")
    check(not h.session.of_type("SendPhoto"), "после сброса капча не появляется")
    check((await users_repo.get_user(ADMIN))["captcha_passed"] == 1,
          "проверка отмечена пройденной автоматически")
    h.clear()

    # Отсутствие username владельца не блокирует
    await h.text(ADMIN, "/start", username=None)
    check(not h.said("Нужен username"), "владельцу username не обязателен")
    h.clear()

    # Потребовать верификацию у владельца нельзя
    await h.text(ADMIN, f"/find {ADMIN}", username="boss")
    await h.act(ADMIN, "adm:card:req:", username="boss")
    owner = await users_repo.get_user(ADMIN)
    check(owner["verify_forced"] == 0, "верификацию у владельца не требуют")
    h.clear()

    # Напоминания владельцу не шлём
    await db.execute(
        "UPDATE users SET last_active = datetime('now', '-5 days'), "
        "last_notify_at = NULL, notify_count = 0 WHERE id = ?", (ADMIN,)
    )
    rows = await reengagement.candidates(cfg)
    check(not any(r["id"] == ADMIN for r in rows), "владельцу напоминания не приходят")

    # ── 24. Анкета живёт одним экраном ──────────────────────────────────────
    section("24. Один экран вместо простыни сообщений")
    await users_repo.ensure_user(SCREEN, "screenuser", "Экран")
    await users_repo.update_user(SCREEN, captcha_passed=1, rules_accepted=1)
    h.clear()

    await h.text(SCREEN, "/start", username="screenuser")
    await h.click(SCREEN, "reg:gender:m", username="screenuser")
    await h.click(SCREEN, "reg:look:f", username="screenuser")

    bad_age = await h.text(SCREEN, "не число", username="screenuser")
    deleted = {c.message_id for c in h.session.of_type("DeleteMessage")}
    check(bad_age in deleted, "неверный ответ пользователя удаляется")
    check(h.said("Введите возраст числом"), "ошибка показана в том же экране")

    good_age = await h.text(SCREEN, "30", username="screenuser")
    name_msg = await h.text(SCREEN, "Экранов", username="screenuser")
    deleted = {c.message_id for c in h.session.of_type("DeleteMessage")}
    check(good_age in deleted and name_msg in deleted,
          "верные ответы пользователя тоже удаляются")

    await h.photo(SCREEN)
    about_msg = await h.text(SCREEN, "Проверяю чистоту чата", username="screenuser")
    city_msg = await h.text(SCREEN, "Казань", username="screenuser")
    deleted = {c.message_id for c in h.session.of_type("DeleteMessage")}
    check(about_msg in deleted and city_msg in deleted,
          "описание и город тоже не остаются в чате")

    sent = len([c for c in h.to(SCREEN) if type(c).__name__ == "SendMessage"])
    edits = len([c for c in h.to(SCREEN) if type(c).__name__ == "EditMessageText"])
    check(edits >= 4 and sent <= 4,
          f"шаги правятся на месте: правок {edits}, новых сообщений {sent}")

    progress_shown = any("Экранов" in (getattr(c, "text", "") or "")
                         for c in h.session.calls)
    check(progress_shown, "заполненное видно строкой прогресса, а не сообщениями")
    check(h.said("Вот как её увидят другие"), "предпросмотр показан")
    h.clear()

    await h.click(SCREEN, "reg:confirm", username="screenuser")
    preview_removed = bool(h.session.of_type("DeleteMessage"))
    check(preview_removed, "после подтверждения предпросмотр убирается")
    check((await users_repo.get_user(SCREEN))["registered"] == 1,
          "анкета опубликована")
    check(h.session.visible(SCREEN) == 1, "после регистрации в чате одно сообщение — меню")

    # ── 25. Повторный /start ────────────────────────────────────────────────
    section("25. Повторный /start не копит шаги")
    await users_repo.ensure_user(RESTART, "restarter", "Рестарт")
    await users_repo.update_user(RESTART, captcha_passed=1, rules_accepted=1)
    h.clear()
    commands = [await h.text(RESTART, "/start", username="restarter") for _ in range(3)]
    deleted = {c.message_id for c in h.session.of_type("DeleteMessage")
               if c.chat_id == RESTART}
    check(set(commands) <= deleted, "сами команды /start из чата убраны")
    check(h.session.visible(RESTART) == 1, "в чате один «Шаг 1 из 7», а не три")
    check(h.said("Шаг 1 из 7"), "показан первый шаг анкеты")

    for _ in range(3):
        await h.text(NEWBIE, "/start", username="newbie2")
    check(h.session.visible(NEWBIE) == 1, "капча при повторном /start не дублируется")
    h.clear()

    # ── 26. Главное меню ────────────────────────────────────────────────────
    section("26. Главное меню на inline-кнопках")
    await h.text(SCREEN, "/start", username="screenuser")
    menus = [c for c in h.to(SCREEN) if "Главное меню" in (getattr(c, "text", "") or "")]
    check(bool(menus), "меню показано")
    check(bool(menus) and "Выберите, что нужно" in menus[-1].text,
          "в меню только заголовок и «Выберите, что нужно»")
    check(not h.said("Вас лайкнули"), "сводки в меню нет")
    check(h.data(SCREEN) == ["m:search", "m:profile"],
          "в меню только анкеты и своя анкета — кнопками под сообщением")
    check(h.session.visible(SCREEN) == 1, "в чате одно сообщение — меню")
    h.clear()

    tap = await h.press(SCREEN, "💬 Мои пары (1)", username="screenuser")
    check(h.said("Главное меню")
          and tap in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "старая кнопка «Мои пары» просто открывает меню")
    junk = await h.text(SCREEN, "как дела?", username="screenuser")
    check(junk in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "непонятное сообщение убирается")
    check(h.said("Не понял"), "меню подсказывает, что нажать")
    check(h.session.visible(SCREEN) == 1, "меню так и осталось одним сообщением")
    h.clear()

    # Диалог прежней версии (ввод возраста в настройках) не держит человека
    old_key = StorageKey(bot_id=h.bot.id, chat_id=SCREEN, user_id=SCREEN)
    await h.dp.storage.set_state(old_key, "SearchSettings:age_range")
    await h.text(SCREEN, "20-30", username="screenuser")
    check(h.said("Главное меню") and not h.said("Не понял"),
          "из диалога прежней версии — сразу в меню")
    check(await h.dp.storage.get_state(old_key) is None, "старое состояние сброшено")
    h.clear()

    # Кнопка со старого сообщения выше по чату: оно становится экраном
    old_menu = next(iter(h.session.alive.get(SCREEN, set())))
    await h.click(SCREEN, "m:settings", message_id=old_menu + 777, username="screenuser")
    check(h.said("Главное меню") and h.session.visible(SCREEN) <= 1,
          "неизвестная кнопка из прежней версии ведёт в меню, копий не остаётся")
    h.clear()

    # Поддержка: пока контакт не указан, кнопку видит только владелец
    await h.text(ADMIN, "/start", username="boss")
    check("m:support" in h.data(ADMIN), "владелец видит «Поддержку» сразу")
    await h.click(ADMIN, "m:support", username="boss")
    check(h.said("ещё не указан"), "и подсказку, где указать контакт")
    h.clear()
    await h.text(ADMIN, "/admin", username="boss")
    await h.click(ADMIN, "adm:config", username="boss")
    await h.click(ADMIN, "adm:cfg:support", username="boss")
    await h.text(ADMIN, "не username!", username="boss")
    check(h.said("Не похоже на username"), "мусор вместо username не сохраняется")
    await h.text(ADMIN, "https://t.me/lune_help", username="boss")
    check(await mod_repo.support_username() == "lune_help",
          "контакт поддержки сохранён, даже если прислали ссылку")
    check("💬 Контакт поддержки: @lune_help" in h.buttons(ADMIN),
          "контакт виден в настройках бота")
    h.clear()

    await h.text(SCREEN, "/start", username="screenuser")
    check(h.data(SCREEN) == ["m:search", "m:profile", "m:support"],
          "с контактом «Поддержка» появилась у всех")
    h.clear()
    await h.click(SCREEN, "m:support", username="screenuser")
    check(h.said("@lune_help"), "в поддержке — контакт")
    check(h.data(SCREEN) == ["m:home"], "из поддержки — «🏠 Меню»")
    check(h.session.of_type("EditMessageText") and not h.session.of_type("SendMessage"),
          "поддержка открывается правкой меню на месте")
    h.clear()

    await h.text(ADMIN, f"/ban {SCREEN} проверка", username="boss")
    check(h.said("напишите в поддержку: @lune_help"), "в сообщении о бане — контакт поддержки")
    await h.text(ADMIN, f"/unban {SCREEN}", username="boss")
    h.clear()

    # ── 27. Лента как в Дайвинчике ──────────────────────────────────────────
    section("27. Лента: «Самара» и «Самарская область» — соседи")
    cfg.af_fast_streak = 10_000           # листаем быстро — это тест, не накрутка

    # Возраст 50 — чтобы в ленту не попала массовка из прошлых разделов
    samara = dict(city="Самара", region="Самарская область", lat=53.1959, lon=50.1002)
    tlt = dict(city="Тольятти", region="Самарская область", lat=53.5078, lon=49.4204)
    msk = dict(city="Москва", region="Москва", lat=55.7558, lon=37.6173)
    for uid, gender, name, place in (
            (M_SAMARA, "m", "Самарец", samara), (F_SAMARA, "f", "Самарчанка", samara),
            (F_TLT, "f", "Тольяттинка", tlt), (F_MSK, "f", "Москвичка", msk)):
        await make_profile(uid, gender=gender, name=name, age=50, **place)

    h.clear()
    await register(h, F_REGION, gender="f", looking="m", age="50",
                   name="Областная", city="Самарская область")
    user = await users_repo.get_user(F_REGION)
    check(not h.said("Не нашёл такой город"), "«Самарская область» принята без переспросов")
    check(user["city"] == user["region"] == "Самарская область",
          "запомнена область целиком")
    check(user["registered"] == 1, "анкета с областью опубликована")

    await register(h, M_REGION, gender="m", looking="f", age="50",
                   name="Областной", city="Самарская обл")
    check((await users_repo.get_user(M_REGION))["city"] == "Самарская область",
          "сокращение «обл» тоже понятно")

    async def feed_order(viewer: int) -> list[int]:
        order: list[int] = []
        await h.click(viewer, "m:search")
        for _ in range(20):
            current = (await h.state_data(viewer)).get("current")
            if not current or current in order:
                break
            order.append(current)
            await h.click(viewer, f"br:dislike:{current}")
        return order

    h.clear()
    order = await feed_order(M_SAMARA)
    check(set(order[:2]) == {F_SAMARA, F_REGION},
          "житель Самары первыми видит Самару и тех, кто указал область")
    check(order[2:] == [F_TLT], f"дальше — область: {order}")
    captions = [c.caption for c in h.session.of_type("SendPhoto")
                if c.chat_id == M_SAMARA and c.caption]
    tlt_card = next((cap for cap in captions if "Тольяттинка" in cap), "")
    check("дальше вся Самарская область" in tlt_card,
          "над первой анкетой из области — строка, что город кончился")
    check(h.said("Рядом анкеты закончились") and "br:far" in h.data(M_SAMARA),
          "область кончилась — бот предлагает соседние области")
    check(not h.said("Москвичка"), "без согласия дальние анкеты не показаны")
    h.clear()

    await h.click(M_SAMARA, "br:far")
    check((await h.state_data(M_SAMARA)).get("current") == F_MSK,
          "после согласия — анкеты из соседних областей")
    msk_card = h.session.last("SendPhoto").caption or ""
    check("соседних областей" in msk_card, "над первой такой анкетой — строка об этом")
    check("км от вас" in msk_card, "у анкеты из другого города видно расстояние")
    check((await users_repo.get_user(M_SAMARA))["search_scope"] == users_repo.SCOPE_ALL,
          "согласие запомнено")
    await h.act(M_SAMARA, "br:dislike:")
    check(h.said("Вы посмотрели все анкеты"), "в конце — новый экран, а не «😔 закончились»")
    check("br:reset" in h.data(M_SAMARA)
          and "🔄 Вернуть пропущенных (4)" in h.buttons(M_SAMARA),
          "в конце можно вернуть пропущенных")
    check(h.session.visible(M_SAMARA) == 1, "лента не оставляет старых карточек")
    h.clear()

    order = await feed_order(M_REGION)
    check(set(order) == {F_SAMARA, F_REGION, F_TLT},
          "кто указал область, видит всю область сразу")
    await h.click(M_REGION, "br:far")
    check((await h.state_data(M_REGION)).get("current") == F_MSK,
          "другие регионы — после неё и после вопроса")
    h.clear()

    await h.click(M_SAMARA, "br:reset")
    check((await h.state_data(M_SAMARA)).get("current") in {F_SAMARA, F_REGION},
          "пропущенные вернулись, лента началась заново со своих")
    for _ in range(3):
        await h.act(M_SAMARA, "br:dislike:")
    check((await h.state_data(M_SAMARA)).get("current") == F_MSK,
          "согласившись однажды, дальше лента идёт к соседним сама")
    cfg.af_fast_streak = saved[0]
    h.clear()

    # Прежняя версия записывала область как город с другим регистром
    await make_profile(F_LEGACY, gender="f", name="Старожилка", city="Самарская Область",
                       region="Самарская область", lat=53.1959, lon=50.1002, age=50)
    rows = await users_repo.search_candidates(await users_repo.get_user(M_SAMARA))
    tiers = {int(r["id"]): int(r["area_tier"]) for r in rows}
    check(tiers.get(F_LEGACY) == users_repo.AREA_LOCAL,
          "«Самарская Область» из старой записи — тоже своя для Самары")
    legacy_card = profile_service.render_card(await users_repo.get_user(F_LEGACY))
    check("Область, Самарская" not in legacy_card, "в карточке область не повторяется")

    # ── 28. Оформление и кнопки ─────────────────────────────────────────────
    section("28. Цитата, капча и кнопки")
    warning = texts.WARNING.format(min_age=18)
    check(warning.startswith("<blockquote>") and warning.endswith("</blockquote>"),
          "предупреждение целиком в цитате")
    await h.text(CAPTCHA_LOOK, "/start", username="looker")
    caption = h.session.last("SendPhoto").caption
    check("<blockquote>" in caption and "Выберите" in caption,
          "в капче задание и правило в цитате")
    check("Перед входом" not in caption and len(caption) < 400,
          f"подпись короткая ({len(caption)} симв.)")
    h.clear()

    check(h.session.reply_buttons <= {rkb.LOCATION, rkb.CANCEL},
          f"нижняя кнопка за весь прогон — только геопозиция ({sorted(h.session.reply_buttons)})")
    callbacks = {data for data, url in h.session.inline_buttons if data}
    check({"onb:accept", "m:search", "cap:done"} <= callbacks,
          "остальное управление — inline-кнопками")
    check(all(len(data.encode()) <= 64 for data in callbacks),
          "callback_data укладываются в лимит Telegram (64 байта)")
    urls = {url for data, url in h.session.inline_buttons if url}
    check(urls <= {"https://t.me/example"}, "кнопки-ссылки — только под рекламой")

    # ── 29. Кнопки не обходят проверки ──────────────────────────────────────
    section("29. Подделанное нажатие не открывает то, что ещё рано")
    await h.click(SNEAKY, "onb:next", username="sneaky")
    check(bool(h.session.of_type("SendPhoto")) and not h.said("мошенник"),
          "«Далее» до капчи — сначала капча, а не правила")
    await h.click(SNEAKY, "onb:accept", username="sneaky")
    check((await users_repo.get_user(SNEAKY))["rules_accepted"] == 0,
          "«Принимаю» до капчи не засчитано")
    sneaky_key = StorageKey(bot_id=h.bot.id, chat_id=SNEAKY, user_id=SNEAKY)
    await h.dp.storage.set_state(sneaky_key, None)
    await h.click(SNEAKY, "reg:gender:m", username="sneaky")
    check((await users_repo.get_user(SNEAKY))["gender"] is None
          and not h.said("Кого будем искать"), "шаги анкеты в обход капчи не открываются")
    h.clear()

    await h.dp.storage.set_state(sneaky_key, None)
    await h.click(SNEAKY, "pr:refill", username="sneaky")
    check(not h.said("Шаг 1 из 7"), "«Заполнить анкету заново» без анкеты не открывает шаги")
    await h.dp.storage.set_state(sneaky_key, None)
    await h.click(SNEAKY, "ver:self", username="sneaky")
    await h.click(SNEAKY, "ver:send", username="sneaky")
    requested = await db.fetchone("SELECT 1 FROM verifications WHERE user_id = ?", (SNEAKY,))
    check(requested is None, "без анкеты заявку на верификацию не создать")
    await h.click(SNEAKY, "adm:stats", username="sneaky")
    check(not h.said("Статистика бота"), "в админку без прав не пройти")
    h.clear()

    # ── 30. Верификация по желанию ──────────────────────────────────────────
    section("30. Верификация кружком: пример, отмена и отказ")
    digits = iter("12345678")
    choice = verification_handlers.secrets.choice
    verification_handlers.secrets.choice = lambda seq: next(digits)
    try:
        check(verification_handlers.new_code() == "5678",
              "код из примера (1234) настоящим не выдаётся")
    finally:
        verification_handlers.secrets.choice = choice

    await make_profile(VERA, gender="f", name="Вера")
    h.clear()
    await h.click(ADMIN, "adm:config", username="boss")
    check("adm:cfg:example" in h.data(ADMIN), "в настройках есть пример верификации")
    await h.click(ADMIN, "adm:cfg:example", username="boss")
    check(h.said("Мой код — 1234") and h.said("сверху код 1234, под ним @test_bot"),
          "владельцу подсказано, что снять в примере: листок с ником бота")
    await h.feed(photo_update(h.bot, ADMIN, username="boss"))
    check(h.said("Нужен именно кружок"), "пример — только кружок")
    await h.feed(video_note_update(h.bot, ADMIN, 6, forwarded=True,
                                   file_id="example-circle", username="boss"))
    check(await mod_repo.verify_example() == "example-circle",
          "пересланный кружок сохранён как пример")
    check(h.said("Пример сохранён"), "владелец видит, что пример сохранён")
    h.clear()

    await h.click(VERA, "m:profile")
    await h.act(VERA, "ver:self")
    check(h.said("Верификация анкеты") and h.data(VERA) == ["ver:send", "pr:back"],
          "сначала объяснение и кнопка «Записать кружок»")
    h.clear()
    await h.click(VERA, "ver:send")
    names = h.session.method_names()
    check("SendVideoNote" in names and names.index("SendVideoNote") < names.index("SendMessage"),
          "над заданием — кружок-пример")
    check(h.session.last("SendVideoNote").video_note == "example-circle",
          "показан загруженный пример")
    check(h.said("Код в нём 1234, у вас будет свой"), "под примером — что код у всех свой")
    h.clear()
    await h.click(VERA, "ver:cancel")
    check("ver:self" in h.data(VERA), "после отмены кнопка «Пройти верификацию» на месте")
    check((await users_repo.get_user(VERA))["verify_status"] == "none",
          "статус не меняется, пока кружок не прислан")
    h.clear()

    await h.act(VERA, "ver:self")
    await h.click(VERA, "ver:send")
    await h.feed(video_note_update(h.bot, VERA, 7))
    check(h.said("Кружок отправлен"), "кружок принят")
    h.clear()
    await h.click(VERA, "m:profile")
    check(h.said("на проверке") and "ver:self" not in h.data(VERA),
          "в анкете видно, что заявка на проверке")
    await h.click(VERA, "ver:send")
    check(h.said("Заявка на проверке"), "второй кружок поверх первого не записать")
    h.clear()

    await h.click(ADMIN, "adm:verify", username="boss")
    record = await mod_repo.current_verification(VERA)
    await h.act(ADMIN, "adm:ver:no:", username="boss")
    check(f"adm:vrj:person:{record['id']}" in h.data(ADMIN)
          and f"adm:vrj:paper:{record['id']}" in h.data(ADMIN),
          "готовые причины отказа — кнопками, есть и про листок")
    await h.click(ADMIN, f"adm:vrj:person:{record['id']}", username="boss")
    record = await mod_repo.get_verification(record["id"])
    reason = texts.VERIFY_REJECT_REASONS["person"][1]
    check(record["status"] == "rejected" and record["review_note"] == reason,
          "заявка отклонена с выбранной причиной")
    check(any(reason in (getattr(c, "text", None) or "") for c in h.to(VERA)),
          "человеку пришла причина отказа")
    check((await users_repo.get_user(VERA))["verify_status"] == "rejected",
          "статус — отклонена")
    h.clear()
    await h.click(VERA, "m:profile")
    check("ver:self" in h.data(VERA), "после отказа можно пройти заново")
    h.clear()

    await h.act(VERA, "ver:self")
    await h.click(VERA, "ver:send")
    await h.feed(video_note_update(h.bot, VERA, 7))
    h.clear()
    await h.click(ADMIN, "adm:verify", username="boss")
    await h.act(ADMIN, "adm:ver:no:", username="boss")
    await h.text(ADMIN, "темно, лица не разобрать", username="boss")
    check(any("темно, лица не разобрать" in (getattr(c, "text", None) or "")
              for c in h.to(VERA)), "свою причину админ пишет текстом")
    h.clear()

    await h.click(ADMIN, "adm:config", username="boss")
    await h.click(ADMIN, "adm:cfg:example", username="boss")
    check("adm:cfg:example:del" in h.data(ADMIN), "пример можно убрать")
    await h.click(ADMIN, "adm:cfg:example:del", username="boss")
    check(await mod_repo.verify_example() == "", "пример убран")
    h.clear()
    await h.click(VERA, "ver:send")
    check(not h.session.of_type("SendVideoNote") and h.said("Запишите кружок"),
          "без примера — одно задание")
    await h.click(VERA, "ver:cancel")
    h.clear()

    # Заявка прежней версии — фото с кодом на листе — разбирается как раньше
    await db.execute(
        "INSERT INTO verifications (user_id, code, media_type, media_id) "
        "VALUES (?, 'K7M2', 'photo', 'old-photo')", (VERA,)
    )
    await h.click(ADMIN, "adm:verify", username="boss")
    check(h.said("Код на фото должен быть: <code>K7M2</code>")
          and any(c.photo == "old-photo" for c in h.session.of_type("SendPhoto")),
          "заявка прежней версии с фото открывается у админа")
    await h.act(ADMIN, "adm:ver:ok:", username="boss")
    check((await users_repo.get_user(VERA))["verify_status"] == "verified",
          "и подтверждается как раньше")
    callbacks = {data for data, url in h.session.inline_buttons if data}
    check(all(len(data.encode()) <= 64 for data in callbacks),
          "callback_data верификации укладываются в лимит Telegram")
    h.clear()

    print(f"\n\033[1mИтог: {passed} успешно, {failed} с ошибкой\033[0m")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
