"""Сквозной прогон бота без обращения к Telegram.

Проходит весь путь: капча -> предупреждение -> анкета -> лента -> лайки ->
совпадение -> жалоба -> админка -> верификация -> бан. Полезно запускать
после любых правок: python3 tests/smoke_test.py

Управление ботом — нижними кнопками, поэтому нажатие кнопки здесь — это
обычное сообщение с её надписью (h.press). Inline-кнопка одна — «Принимаю»
под правилами (h.click).
"""
from __future__ import annotations

import asyncio
import os
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
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove     # noqa: E402

from app import handlers, middlewares, texts                           # noqa: E402
from app.config import get_settings                                    # noqa: E402
from app.db import ads as ads_repo                                     # noqa: E402
from app.db import captcha as captcha_repo                             # noqa: E402
from app.db import moderation as mod_repo                              # noqa: E402
from app.db import reactions as reactions_repo                         # noqa: E402
from app.db import users as users_repo                                 # noqa: E402
from app.db.database import db                                         # noqa: E402
from app.keyboards import reply as rkb                                 # noqa: E402
from app.services import antifraud, reengagement, screen               # noqa: E402
from app.services import captcha as captcha_service                    # noqa: E402
from app.services import profile as profile_service                    # noqa: E402
from main import build_storage                                         # noqa: E402
from tests.fake_telegram import (                                      # noqa: E402
    FakeSession, callback_update, location_update, message_update,
    photo_update, video_update,
)

ALICE, BOB, CAROL, ADMIN = 100001, 100002, 100003, 900001
DAVE, EVE, FRANK = 100004, 100005, 100006
GLEB, HELEN, MOD1, MOD2 = 100007, 100008, 100009, 100010
NINA, OLEG = 100011, 100012
SCREEN = 100013
RESTART, NEWBIE, CAPTCHA_LOOK = 100014, 100015, 100016
M_SAMARA, M_REGION, F_SAMARA, F_REGION, F_TLT, F_MSK, F_LEGACY = range(100020, 100027)
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

    # Нажатие нижней кнопки — это сообщение с её надписью
    press = text

    async def screen_ids(self, user_id: int) -> list[int]:
        """Сообщения, которые бот сейчас считает экраном пользователя."""
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id,
                         destiny=screen.DESTINY)
        return list((await self.dp.storage.get_data(key)).get(screen.MESSAGES) or [])

    async def click(self, user_id: int, data: str, **kwargs) -> None:
        """Inline-кнопка — на сообщении, которое сейчас на экране."""
        ids = await self.screen_ids(user_id)
        await self.feed(callback_update(self.bot, user_id, data,
                                        message_id=ids[-1] if ids else None, **kwargs))

    async def photo(self, user_id: int) -> None:
        await self.feed(photo_update(self.bot, user_id))

    async def state_data(self, user_id: int) -> dict:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.dp.storage.get_data(key)

    def keyboard(self, user_id: int) -> list[str]:
        return self.session.keyboard(user_id)

    def said(self, needle: str) -> bool:
        return any(needle.lower() in t.lower() for t in self.session.texts())

    def clear(self) -> None:
        self.session.clear()


async def solve_captcha(h: Harness, user_id: int, *, correctly: bool = True,
                        wait: bool = True) -> None:
    """Читает правильный ответ из состояния — так может только тест, не бот."""
    if wait:
        await asyncio.sleep(0.5)
    correct = set((await h.state_data(user_id))["cap_correct"])
    target = correct if correctly else ({1, 2, 3} - correct or {15})
    for label in sorted(target):
        await h.press(user_id, str(label))
    await h.press(user_id, rkb.CAPTCHA_DONE)


GENDER_BUTTON = {"m": rkb.GENDER_M, "f": rkb.GENDER_F}
LOOK_BUTTON = {"m": rkb.LOOK_M, "f": rkb.LOOK_F, "any": rkb.LOOK_ANY}


async def register(h: Harness, user_id: int, *, gender: str, looking: str,
                   age: str, name: str, city: str) -> None:
    await h.text(user_id, "/start")
    await solve_captcha(h, user_id)
    await h.press(user_id, rkb.NEXT)
    await asyncio.sleep(get_settings().rules_delay_seconds + 0.4)
    await h.click(user_id, "onb:accept")
    await h.press(user_id, GENDER_BUTTON[gender])
    await h.press(user_id, LOOK_BUTTON[looking])
    await h.text(user_id, age)
    await h.text(user_id, name)
    await h.photo(user_id)
    await h.text(user_id, "Люблю горы, кофе и долгие разговоры.")
    await h.text(user_id, city)
    await h.press(user_id, rkb.CONFIRM)


async def make_profile(user_id: int, *, gender: str, name: str,
                       city: str = "Волгоград", region: str | None = "Волгоградская область",
                       lat: float = 48.708, lon: float = 44.513, age: int = 25,
                       age_min: int = 18, age_max: int = 99) -> None:
    """Готовая анкета напрямую в базе — чтобы не проходить мастер ради массовки."""
    await users_repo.ensure_user(user_id, f"user{user_id}", name)
    await users_repo.update_user(
        user_id, captcha_passed=1, rules_accepted=1, registered=1, is_active=1,
        name=name, gender=gender, looking_for="any", age=age, about="Тестовая анкета",
        media_type="photo", media_id=f"photo-{user_id}", city=city, region=region,
        country="RU", lat=lat, lon=lon, geo_source="city", search_scope="city",
        age_min=age_min, age_max=age_max,
    )


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
    check(h.keyboard(CAROL) == [rkb.USERNAME_DONE], "кнопка «Я поставил username» — внизу")
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
    keyboard = h.keyboard(ALICE)
    check(keyboard[:15] == [str(n) for n in range(1, 16)] and rkb.CAPTCHA_DONE in keyboard,
          "номера клеток и «Готово» — нижними кнопками")
    h.clear()

    tap = await h.press(ALICE, "7")
    check(tap in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "нажатие номера не остаётся в чате")
    check(any("Выбрано: <b>7</b>" in (getattr(c, "caption", "") or "")
              for c in h.session.of_type("EditMessageCaption")),
          "выбранная клетка видна в подписи к картинке")
    await h.press(ALICE, "7")
    check((await h.state_data(ALICE))["cap_selected"] == [], "повторное нажатие снимает выбор")
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
    check(h.keyboard(ALICE) == [rkb.NEXT], "под приветствием кнопка «Далее»")
    h.clear()

    # ── 3. Предупреждение с задержкой ───────────────────────────────────────
    section("3. Предупреждение о мошенниках")
    await h.press(ALICE, rkb.NEXT)
    check(h.session.of_type("DeleteMessage"), "приветствие удаляется")
    check(h.said("мошенник"), "показано предупреждение")
    check(h.said("Кнопка появится через"), "идёт обратный отсчёт")
    check(any(isinstance(getattr(c, "reply_markup", None), ReplyKeyboardRemove)
              for c in h.session.calls), "нижняя клавиатура на время правил убрана")
    accept_before = any(
        "onb:accept" in str(getattr(c, "reply_markup", "")) for c in h.session.calls
    )
    check(not accept_before, "кнопки «Принимаю» ещё нет")

    await asyncio.sleep(get_settings().rules_delay_seconds + 0.5)
    accept_after = any(
        "onb:accept" in str(getattr(c, "reply_markup", "")) for c in h.session.calls
    )
    check(accept_after, "через паузу появилась кнопка «Принимаю»")
    h.clear()

    await h.click(ALICE, "onb:accept")
    user = await users_repo.get_user(ALICE)
    check(user["rules_accepted"] == 1, "согласие с правилами сохранено")
    check(h.said("Ваш пол"), "сразу начинается анкета")
    check(h.keyboard(ALICE) == [rkb.GENDER_M, rkb.GENDER_F], "пол выбирается нижней кнопкой")
    h.clear()

    # ── 4. Анкета ───────────────────────────────────────────────────────────
    section("4. Заполнение анкеты")
    await h.press(ALICE, rkb.GENDER_F)
    await h.press(ALICE, rkb.LOOK_M)
    await h.text(ALICE, "семнадцать")
    check(h.said("Введите возраст числом"), "возраст словами не принимается")
    await h.text(ALICE, "16")
    check(h.said("Минимальный возраст"), "младше настроенного минимума не пускает")
    await h.text(ALICE, "26")
    check(h.keyboard(ALICE) == ["Тест"], "имя из Telegram предложено кнопкой")
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
    check(h.keyboard(ALICE) == [rkb.SKIP], "описание можно пропустить кнопкой")
    h.clear()

    await h.text(ALICE, "Захожу сюда за живым общением. Пишите: t.me/spamchannel")
    check(h.said("нельзя оставлять ссылки"), "ссылки в описании блокируются")
    await h.text(ALICE, "Люблю книги, горы и настолки.")
    check(h.said("Откуда вы"), "дальше спрашивается город")
    check(h.keyboard(ALICE) == [rkb.LOCATION], "геопозиция — нижней кнопкой")
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
    check(h.keyboard(ALICE) == [rkb.CONFIRM, rkb.REFILL], "подтверждение — нижними кнопками")
    check(user["search_scope"] == "city", "лента начнётся с её места")
    h.clear()

    await h.press(ALICE, rkb.CONFIRM)
    user = await users_repo.get_user(ALICE)
    check(user["registered"] == 1 and user["is_active"] == 1, "анкета опубликована")
    check(user["age_min"] == 21 and user["age_max"] == 31,
          "возрастные рамки поиска выставлены по умолчанию")
    check(rkb.SEARCH in h.keyboard(ALICE), "после публикации — меню внизу")
    h.clear()

    # ── 5. Второй пользователь и геопозиция ─────────────────────────────────
    section("5. Второй пользователь и поиск по геопозиции")
    await h.text(BOB, "/start")
    await solve_captcha(h, BOB)
    await h.press(BOB, rkb.NEXT)
    await asyncio.sleep(get_settings().rules_delay_seconds + 0.4)
    await h.click(BOB, "onb:accept")
    await h.press(BOB, rkb.GENDER_M)
    await h.press(BOB, rkb.LOOK_F)
    await h.text(BOB, "28")
    await h.text(BOB, "Борис")
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

    await h.press(BOB, rkb.CONFIRM)
    user = await users_repo.get_user(BOB)
    check(user["search_scope"] == "near", "с геопозицией лента начинается с тех, кто рядом")

    # ── 6. Лента и совпадение ───────────────────────────────────────────────
    section("6. Лента, лайки и совпадение")
    h.clear()
    await h.press(BOB, rkb.SEARCH)
    check(h.said("Алиса"), "Борису показана анкета Алисы")
    check(h.said("км от вас"), "в режиме «рядом» показано расстояние")
    feed_keys = h.keyboard(BOB)
    check(feed_keys[0].startswith(rkb.LIKE) and rkb.NOTE in feed_keys
          and rkb.DISLIKE in feed_keys and rkb.REPORT in feed_keys and rkb.HOME in feed_keys,
          "под анкетой нижние кнопки ❤️ 💌 👎 🚨 🏠")
    h.clear()

    await h.press(BOB, rkb.LIKE)
    check(await users_repo.count_incoming_likes(ALICE) == 1, "лайк дошёл до Алисы")
    check(h.said("понравились"), "Алисе пришло уведомление о симпатии")
    h.clear()

    await h.press(ALICE, rkb.SEARCH)
    check(h.said("Борис"), "Алисе показан Борис")
    h.clear()
    await h.press(ALICE, rkb.LIKE)
    check(h.said("Взаимная симпатия"), "сработало совпадение")
    check(h.said("@tester"), "выданы контакты для переписки")
    matches = await users_repo.get_matches(ALICE)
    check(len(matches) == 1, "совпадение сохранено в базе")
    h.clear()

    # ── 7. Лимит лайков ─────────────────────────────────────────────────────
    section("7. Лимит лайков")
    await mod_repo.set_setting("likes_limit", "1")
    await users_repo.update_user(CAROL, username="carol")
    await register(h, CAROL, gender="f", looking="m", age="24",
                   name="Карина", city="Волгоград")
    h.clear()
    await h.press(BOB, rkb.SEARCH)
    check((await h.state_data(BOB)).get("current") == CAROL, "Борису показана Карина")
    await h.press(BOB, rkb.LIKE)
    check(h.said("Лимит лайков на сегодня исчерпан"), "лимит лайков срабатывает")
    check(not await users_repo.get_matches(CAROL), "лайк сверх лимита не засчитан")
    check((await h.state_data(BOB)).get("current") == CAROL,
          "анкета осталась на экране — её можно пропустить")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 8. Жалоба ───────────────────────────────────────────────────────────
    section("8. Жалоба на анкету")
    await h.press(BOB, rkb.REPORT)
    check(h.said("На что жалуемся"), "предложены причины жалобы")
    check(texts.REPORT_REASONS["scam"] in h.keyboard(BOB), "причины — нижними кнопками")
    await h.press(BOB, texts.REPORT_REASONS["scam"])
    check(rkb.NO_COMMENT in h.keyboard(BOB), "можно отправить без комментария")
    await h.text(BOB, "Просит перевести деньги на карту")
    check(h.said("Жалоба отправлена"), "жалоба принята")
    admin_texts = [c for c in h.session.calls
                   if getattr(c, "chat_id", None) == ADMIN]
    check(bool(admin_texts), "жалоба ушла администратору")
    check(await mod_repo.count_open_reports() == 1, "жалоба записана в базу")
    h.clear()

    # ── 9. Админ-панель ─────────────────────────────────────────────────────
    section("9. Админ-панель")
    await h.text(ADMIN, "/admin", username="boss")
    check(h.said("Админ-панель"), "панель открывается")
    panel_keys = h.keyboard(ADMIN)
    check(rkb.A_STATS in panel_keys and rkb.A_BROADCAST in panel_keys
          and rkb.HOME in panel_keys, "разделы админки — нижними кнопками")
    h.clear()
    await h.press(ADMIN, rkb.A_STATS, username="boss")
    check(h.said("Статистика бота"), "статистика собирается")
    check(h.said("Совпадений"), "в статистике есть совпадения")
    check(h.said("Антинакрутка") and h.said("Напоминания"),
          "в статистике есть разделы защиты и напоминаний")
    h.clear()
    await h.text(ADMIN, f"/find {CAROL}", username="boss")
    check(h.said("Карина"), "поиск пользователя работает")
    check(rkb.A_BAN in h.keyboard(ADMIN), "действия с человеком — под его карточкой")
    h.clear()

    await h.press(ADMIN, rkb.A_CONFIG, username="boss")
    await h.press(ADMIN, rkb.A_REG_OPEN, username="boss")
    check(await mod_repo.get_setting("registration_open") == "0",
          "приём новых анкет закрыт")
    h.clear()
    await h.text(999123, "/start", username="newbie")
    check(h.said("Регистрация временно приостановлена"),
          "новичок не может начать регистрацию")
    await h.press(ADMIN, rkb.A_REG_CLOSED, username="boss")
    check(await mod_repo.get_setting("registration_open") == "1",
          "приём анкет снова открыт")
    h.clear()

    await h.press(ADMIN, rkb.A_BACK, username="boss")
    await h.press(ADMIN, rkb.counted(rkb.A_REPORTS, 1), username="boss")
    check(h.said("Жалоба #") and h.said("Карина"), "жалобы разбираются по одной с анкетой")
    await h.press(ADMIN, rkb.A_DECLINE, username="boss")
    check(await mod_repo.count_open_reports() == 0, "жалоба отклонена")
    check(h.said("Открытых жалоб больше нет"), "после последней — назад в панель")
    h.clear()

    # ── 10. Верификация по требованию админа ────────────────────────────────
    section("10. Принудительная верификация")
    await h.text(ADMIN, f"/find {CAROL}", username="boss")
    await h.press(ADMIN, rkb.A_REQ_VERIFY, username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_forced"] == 1, "требование верификации выставлено")
    code = user["verify_code"]
    h.clear()

    await h.press(CAROL, rkb.SEARCH)
    check(h.said("Требуется верификация"), "до проверки бот закрыт")
    check(h.said(code), "пользователю показан код для фото")
    check(h.keyboard(CAROL) == [rkb.VERIFY_SEND], "отправить фото — нижней кнопкой")
    h.clear()

    candidates = await users_repo.search_candidates(await users_repo.get_user(BOB))
    check(all(c["id"] != CAROL for c in candidates),
          "анкета на проверке скрыта из поиска")

    await h.press(CAROL, rkb.VERIFY_SEND)
    await h.photo(CAROL)
    check(h.said("Заявка отправлена"), "фото проверки принято")
    verifications = await mod_repo.pending_verifications()
    check(len(verifications) == 1, "заявка ждёт админа")
    h.clear()

    await h.press(ADMIN, rkb.A_BACK, username="boss")
    await h.press(ADMIN, rkb.counted(rkb.A_VERIFY, 1), username="boss")
    check(h.said("Заявка #"), "заявка открывается с фото")
    await h.press(ADMIN, rkb.A_APPROVE, username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_status"] == "verified", "верификация подтверждена")
    check(user["verify_forced"] == 0, "блокировка снята")
    h.clear()
    await h.press(CAROL, rkb.PROFILE)
    check(h.said("24 года ✅"), "в анкете появилась зелёная галочка")
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

    await h.press(CAROL, rkb.SEARCH)
    check(h.said("Доступ заблокирован"), "забаненный не может пользоваться ботом")
    h.clear()

    await h.text(ADMIN, f"/unban {CAROL}", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["is_banned"] == 0, "бан снят")
    h.clear()

    # ── 12. Рассылка ────────────────────────────────────────────────────────
    section("12. Рассылка")
    await h.press(ADMIN, rkb.A_BROADCAST, username="boss")
    await h.press(ADMIN, "📋 С анкетой", username="boss")
    check(h.said("Получателей"), "аудитория посчитана")
    source = await h.text(ADMIN, "Привет! У нас новые анкеты 🎉", username="boss")
    check(h.said("Так это увидят люди"), "показан предпросмотр")
    check(source not in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "сообщение для рассылки не удалено — его копируют")
    h.clear()
    await h.press(ADMIN, rkb.A_SEND, username="boss")
    await asyncio.sleep(1.0)
    copies = h.session.of_type("CopyMessage")
    check(len(copies) >= 3, f"сообщения разосланы ({len(copies)} шт.)")
    row = await db.fetchone("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    check(row is not None and row["total"] >= 3, "рассылка записана в журнал")
    h.clear()

    # ── 13. Настройки поиска ────────────────────────────────────────────────
    section("13. Настройки поиска")
    await h.press(BOB, rkb.SETTINGS)
    check(h.said("Настройки поиска"), "настройки открываются")
    check(rkb.scope_button("near", "Сначала те, кто рядом", "near") in h.keyboard(BOB),
          "выбранный вариант отмечен 🔘")
    h.clear()
    await h.press(BOB, rkb.AGE_RANGE)
    await h.text(BOB, "20-45")
    user = await users_repo.get_user(BOB)
    check(user["age_min"] == 20 and user["age_max"] == 45, "возрастные рамки сохранены")
    await h.press(BOB, rkb.RADIUS)
    await h.press(BOB, "100 км")
    user = await users_repo.get_user(BOB)
    check(user["search_radius"] == 100, "радиус поиска сохранён")
    h.clear()

    # ── 14. Скрытие и удаление анкеты ───────────────────────────────────────
    section("14. Управление анкетой")
    await h.press(ALICE, rkb.PROFILE)
    await h.press(ALICE, rkb.HIDE)
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 0, "анкета скрыта из поиска")
    check(rkb.SHOW in h.keyboard(ALICE), "кнопка сменилась на «Показывать в поиске»")
    await h.press(ALICE, rkb.SHOW)
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 1, "анкета снова видна")
    h.clear()
    await h.press(ALICE, rkb.DELETE)
    await h.press(ALICE, rkb.DELETE_YES)
    user = await users_repo.get_user(ALICE)
    check(user["registered"] == 0 and user["name"] is None, "анкета удалена")
    check(not await users_repo.get_matches(BOB), "совпадения удалённого убраны")

    # ── 15. Антинакрутка: скорость ──────────────────────────────────────────
    section("15. Антинакрутка: слишком быстрые реакции")
    cfg = get_settings()
    saved = (cfg.af_fast_streak, cfg.af_ratio_window, cfg.min_age)
    cfg.af_fast_streak, cfg.af_ratio_window = 4, 10_000   # долю лайков не проверяем

    await make_profile(DAVE, gender="m", name="Дмитрий")
    for i, extra in enumerate(EXTRAS):
        await make_profile(extra, gender="f", name=f"Гостья {i + 1}")
    h.clear()

    await h.press(DAVE, rkb.SEARCH)
    for _ in range(6):
        await h.press(DAVE, rkb.DISLIKE)

    user = await users_repo.get_user(DAVE)
    check(user["af_strikes"] == 1, "зафиксировано первое нарушение")
    check(user["captcha_passed"] == 0, "первое нарушение сбрасывает капчу")
    check(user["is_banned"] == 0, "с первого раза не банит")
    check(h.said("слишком быстро"), "пользователь предупреждён")
    admin_notified = any(getattr(c, "chat_id", None) == ADMIN and
                         "Антинакрутка" in (getattr(c, "text", "") or "")
                         for c in h.session.calls)
    check(admin_notified, "администратор уведомлён")
    h.clear()

    await h.press(DAVE, rkb.SEARCH)
    check(h.said("повторная проверка"), "до новой капчи бот закрыт")
    h.clear()
    await h.text(DAVE, "/start")
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
    check(h.keyboard(FRANK) == [rkb.SEARCH, rkb.STOP_REMINDERS],
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
    h.clear()
    await h.press(FRANK, rkb.STOP_REMINDERS)
    user = await users_repo.get_user(FRANK)
    check(user["notify_enabled"] == 0, "кнопка «не напоминать» работает")
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
    await h.press(700001, rkb.GENDER_M, username="teen")
    await h.press(700001, rkb.LOOK_F, username="teen")
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
    await make_profile(GLEB, gender="m", name="Глеб", age=33, age_min=32, age_max=34)
    await make_profile(HELEN, gender="f", name="Елена", age=33, age_min=32, age_max=34)
    h.clear()

    await h.press(GLEB, rkb.SEARCH)
    check((await h.state_data(GLEB)).get("current") == HELEN, "Глебу показана Елена")
    check(rkb.NOTE in h.keyboard(GLEB), "в ленте есть кнопка «Сообщение»")
    h.clear()

    await h.press(GLEB, rkb.NOTE)
    check(h.said("Что написать"), "бот просит текст сообщения")
    check(h.keyboard(GLEB) == [rkb.CANCEL], "передумать можно кнопкой «Отмена»")
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

    to_helen = [c for c in h.session.calls if getattr(c, "chat_id", None) == HELEN]
    delivered = " ".join((getattr(c, "text", "") or "") for c in to_helen)
    check("написали вместе с лайком" in delivered, "Елене пришло уведомление")
    check("Кто меня лайкнул" in delivered, "уведомление говорит, где ответить")
    check(not any(getattr(c, "reply_markup", None) for c in to_helen),
          "уведомление без кнопок — клавиатуру Елены не сбивает")
    h.clear()

    await h.press(HELEN, rkb.counted(rkb.LIKES, 1))
    check(h.said("Глеб"), "в «Кто меня лайкнул» — анкета отправителя")
    check(h.said("где снимали фото"), "и его сообщение")
    await h.press(HELEN, rkb.LIKE)
    check(h.said("Взаимная симпатия"), "ответ взаимностью создаёт совпадение")
    check(len(await users_repo.get_matches(HELEN)) == 1, "совпадение сохранено")
    h.clear()

    # Если не ответить сразу, сообщение ждёт в «кто меня лайкнул»
    await make_profile(200100, gender="m", name="Игорь")
    await reactions_repo.add_reaction(200100, HELEN, "like", "Сообщение из инбокса")
    await h.press(HELEN, rkb.LIKES)
    check(h.said("Сообщение из инбокса"), "текст виден и в списке лайков")
    # Для разделов ниже Глеб и Елена снова обычного возраста
    for uid in (GLEB, HELEN):
        await users_repo.update_user(uid, age=25, age_min=18, age_max=99)
    h.clear()

    # ── 20. Модераторы ──────────────────────────────────────────────────────
    section("20. Модераторы с урезанными правами")
    await make_profile(MOD1, gender="m", name="Модератор")
    await make_profile(MOD2, gender="m", name="Модератор Два")
    h.clear()

    await h.press(ADMIN, rkb.A_STAFF, username="boss")
    await h.press(ADMIN, rkb.A_STAFF_ADD, username="boss")
    await h.text(ADMIN, str(MOD1), username="boss")
    user = await users_repo.get_user(MOD1)
    check(user["is_moderator"] == 1, "модератор назначен")
    check(h.said("Вас назначили модератором"), "модератор уведомлён")
    h.clear()

    await h.text(MOD1, "/start")
    check(rkb.MODERATOR in h.keyboard(MOD1), "в меню появилась кнопка модератора")
    check(rkb.ADMIN not in h.keyboard(MOD1), "кнопки админки у него нет")
    h.clear()

    await h.press(MOD1, rkb.MODERATOR)
    check(h.said("Панель модератора"), "панель модератора открывается")
    panel = h.keyboard(MOD1)
    check(rkb.A_REPORTS in panel and rkb.A_BAN in panel, "жалобы и баны доступны")
    check(rkb.A_BROADCAST not in panel, "рассылки в меню нет")
    check(rkb.A_ADS not in panel and rkb.A_STAFF not in panel,
          "рекламы и модераторов в меню нет")
    check(rkb.A_CONFIG not in panel, "настроек бота в меню нет")
    h.clear()

    await h.press(MOD1, rkb.A_BROADCAST)
    check(not h.said("Кому отправляем"), "нажатие на рассылку не срабатывает")
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

    await h.press(ADMIN, rkb.A_STAFF, username="boss")
    mod1_button = rkb.staff_button({"name": "Модератор", "tg_name": None, "id": MOD1})
    check(mod1_button in h.keyboard(ADMIN), "у каждого модератора своя кнопка снятия")
    await h.press(ADMIN, mod1_button, username="boss")
    check((await users_repo.get_user(MOD1))["is_moderator"] == 0, "права сняты")
    h.clear()
    await h.press(MOD1, rkb.MODERATOR)
    check(not h.said("Панель модератора"), "бывший модератор в панель не попадает")
    h.clear()

    # ── 21. Рекламные посты ─────────────────────────────────────────────────
    section("21. Реклама между анкетами")
    await h.press(ADMIN, rkb.A_ADS, username="boss")
    check(h.said("Реклама"), "раздел рекламы открывается")
    h.clear()

    await h.press(ADMIN, rkb.A_AD_NEW, username="boss")
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
    ad_button = rkb.ad_title(ads[0])
    h.clear()

    # Массовка, чтобы было что листать до появления поста
    for index, extra in enumerate(range(200110, 200116)):
        await make_profile(extra, gender="m", name=f"Гость {index + 1}")

    async def browse_until_ad(viewer: int, steps: int) -> list:
        await h.press(viewer, rkb.SEARCH)
        for _ in range(steps):
            await h.press(viewer, rkb.DISLIKE)
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

    await h.press(ADMIN, rkb.A_ADS, username="boss")
    check(ad_button in h.keyboard(ADMIN), "пост открывается кнопкой из списка")
    await h.press(ADMIN, ad_button, username="boss")
    await h.press(ADMIN, rkb.A_AD_OFF, username="boss")
    ad = await ads_repo.get(ad_id)
    check(ad["is_active"] == 0, "пост выключается")
    shows_before = ad["shows"]
    h.clear()

    copies = await browse_until_ad(GLEB, 3)
    ad = await ads_repo.get(ad_id)
    check(ad["shows"] == shows_before and not copies,
          "выключенный пост не показывается")
    h.clear()

    await h.press(ADMIN, rkb.A_ADS, username="boss")
    await h.press(ADMIN, ad_button, username="boss")
    await h.press(ADMIN, rkb.A_AD_DELETE, username="boss")
    check(not await ads_repo.list_all(), "пост удаляется")

    # ── 22. Ответный лайк не тратит лимит ───────────────────────────────────
    section("22. Ответ на чужой лайк не упирается в лимит")
    # Возраст 41 — чтобы Нина листала только своих, без массовки
    await make_profile(NINA, gender="f", name="Нина", age=41, age_min=40, age_max=42)
    for index, extra in enumerate(range(200200, 200204)):
        await make_profile(extra, gender="m", name=f"Прохожий {index + 1}",
                           age=41, age_min=40, age_max=42)
    # Олег и Пётр старше её рамок: в ленте их нет, но лайкнуть её они могут
    await make_profile(OLEG, gender="m", name="Олег", age=45, age_min=40, age_max=46)
    await make_profile(200204, gender="m", name="Пётр", age=45, age_min=40, age_max=46)

    await mod_repo.set_setting("likes_limit", "1")
    h.clear()

    # Нина тратит весь суточный лимит на поиск
    await h.press(NINA, rkb.SEARCH)
    await h.press(NINA, rkb.LIKE)
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "лимит израсходован")
    h.clear()

    await h.press(NINA, rkb.LIKE)
    check(h.said("Лимит лайков на сегодня исчерпан"),
          "новый лайк в поиске блокируется")
    check(h.said("Отвечать тем, кто лайкнул вас"),
          "бот подсказывает, что ответы не ограничены")
    h.clear()

    # А теперь её лайкнули — ответить она должна мочь
    await reactions_repo.add_reaction(OLEG, NINA, "like")
    await h.press(NINA, rkb.counted(rkb.LIKES, 1))
    check(h.said("Олег"), "анкета отправителя показана несмотря на лимит")
    check(h.keyboard(NINA)[0] == rkb.LIKE, "на кнопке нет счётчика — лайк бесплатный")
    h.clear()

    await h.press(NINA, rkb.LIKE)
    check(h.said("Взаимная симпатия"), "ответный лайк проходит при нулевом лимите")
    check(len(await users_repo.get_matches(NINA)) == 1, "совпадение создано")
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "ответ не ушёл в минус и лимит не тронут")
    h.clear()

    # Лайк с сообщением — тоже бесплатно
    await reactions_repo.add_reaction(200204, NINA, "like", "Привет из уведомления")
    await h.press(NINA, rkb.LIKES)
    await h.press(NINA, rkb.LIKE)
    check(len(await users_repo.get_matches(NINA)) == 2,
          "ответ на лайк с сообщением тоже не требует лимита")
    h.clear()

    # Но исходящий лайк тому, кто её не лайкал, по-прежнему закрыт
    await h.press(NINA, rkb.SEARCH)
    await h.press(NINA, rkb.LIKE)
    check(h.said("Лимит лайков"), "лимит на исходящие лайки продолжает работать")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 23. На владельца ограничения не действуют ───────────────────────────
    section("23. Владелец без ограничений")
    await make_profile(ADMIN, gender="m", name="Владелец")
    await mod_repo.set_setting("likes_limit", "1")
    cfg.af_fast_streak, cfg.af_ratio_window = 3, 5
    h.clear()

    await h.press(ADMIN, rkb.SEARCH, username="boss")
    for _ in range(4):
        await h.press(ADMIN, rkb.LIKE, username="boss")
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
    await h.press(ADMIN, rkb.A_REQ_VERIFY, username="boss")
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
    await h.press(SCREEN, rkb.GENDER_M, username="screenuser")
    await h.press(SCREEN, rkb.LOOK_F, username="screenuser")

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

    sent = len([c for c in h.session.calls
                if type(c).__name__ == "SendMessage"
                and getattr(c, "chat_id", None) == SCREEN])
    removed = len([c for c in h.session.of_type("DeleteMessage")
                   if getattr(c, "chat_id", None) == SCREEN])
    check(removed >= sent - 3,
          f"бот убирает за собой: отправлено {sent}, удалено {removed}")

    progress_shown = any("Экранов" in (getattr(c, "text", "") or "")
                         for c in h.session.calls)
    check(progress_shown, "заполненное видно строкой прогресса, а не сообщениями")
    check(h.said("Вот как её увидят другие"), "предпросмотр показан")
    h.clear()

    await h.press(SCREEN, rkb.CONFIRM, username="screenuser")
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
    section("26. Главное меню на нижних кнопках")
    await h.text(SCREEN, "/start", username="screenuser")
    menus = [c for c in h.session.calls if getattr(c, "chat_id", None) == SCREEN
             and "Главное меню" in (getattr(c, "text", "") or "")]
    check(bool(menus), "меню показано")
    check(bool(menus) and "Выберите, что нужно" in menus[-1].text,
          "в меню только заголовок и «Выберите, что нужно»")
    check(not h.said("Вас лайкнули"), "сводки в меню нет")
    markup = menus[-1].reply_markup if menus else None
    check(isinstance(markup, ReplyKeyboardMarkup) and rkb.SEARCH in h.keyboard(SCREEN),
          "разделы — нижними кнопками")
    check(h.session.visible(SCREEN) == 1, "в чате одно сообщение — меню")
    h.clear()

    tap = await h.press(SCREEN, rkb.HELP, username="screenuser")
    check(h.said("Справка") and tap in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "справка открывается, нажатие кнопки убрано")
    check(h.keyboard(SCREEN) == [rkb.HOME], "из справки — «🏠 Меню»")
    await h.press(SCREEN, rkb.HOME, username="screenuser")
    check(h.session.visible(SCREEN) == 1, "после переходов сообщение по-прежнему одно")
    h.clear()

    await h.press(SCREEN, rkb.MATCHES, username="screenuser")
    check(h.said("пока нет") and h.said("Главное меню"),
          "пустой раздел — подсказкой над меню, без лишних экранов")
    junk = await h.text(SCREEN, "как дела?", username="screenuser")
    check(junk in {c.message_id for c in h.session.of_type("DeleteMessage")},
          "непонятное сообщение убирается")
    check(h.said("Не понял"), "меню подсказывает, что нажать")
    check(h.session.visible(SCREEN) == 1, "меню так и осталось одним сообщением")
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
        await make_profile(uid, gender=gender, name=name, age=50, age_min=48, age_max=52,
                           **place)

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
        await h.press(viewer, rkb.SEARCH)
        for _ in range(20):
            current = (await h.state_data(viewer)).get("current")
            if not current or current in order:
                break
            order.append(current)
            await h.press(viewer, rkb.DISLIKE)
        return order

    h.clear()
    order = await feed_order(M_SAMARA)
    check(set(order[:2]) == {F_SAMARA, F_REGION},
          "житель Самары первыми видит Самару и тех, кто указал область")
    check(order[2:] == [F_TLT, F_MSK], f"дальше область, потом другой город: {order}")
    captions = {c.caption: c for c in h.session.of_type("SendPhoto")
                if c.chat_id == M_SAMARA and c.caption}
    tlt_card = next((cap for cap in captions if "Тольяттинка" in cap), "")
    msk_card = next((cap for cap in captions if "Москвичка" in cap), "")
    check("дальше вся Самарская область" in tlt_card,
          "над первой анкетой из области — строка, что город кончился")
    check("других городов" in msk_card, "над первой анкетой издалека — тоже")
    check("км от вас" in msk_card, "у анкеты из другого города видно расстояние")
    check(h.said("Вы посмотрели все анкеты"), "в конце — новый экран, а не «😔 закончились»")
    reset_button = rkb.counted(rkb.RESET_SKIPS, 4)
    check(reset_button in h.keyboard(M_SAMARA), "в конце можно вернуть пропущенных")
    check(h.session.visible(M_SAMARA) == 1, "лента не оставляет старых карточек")
    h.clear()

    order = await feed_order(M_REGION)
    check(set(order[:3]) == {F_SAMARA, F_REGION, F_TLT},
          "кто указал область, видит всю область сразу")
    check(order[3:] == [F_MSK], "другие регионы — после неё")
    h.clear()

    await h.press(M_SAMARA, reset_button)
    check((await h.state_data(M_SAMARA)).get("current") in {F_SAMARA, F_REGION},
          "пропущенные вернулись, лента началась заново со своих")
    cfg.af_fast_streak = saved[0]
    h.clear()

    # Прежняя версия записывала область как город с другим регистром
    await make_profile(F_LEGACY, gender="f", name="Старожилка", city="Самарская Область",
                       region="Самарская область", lat=53.1959, lon=50.1002,
                       age=50, age_min=48, age_max=52)
    rows = await users_repo.search_candidates(await users_repo.get_user(M_SAMARA))
    tiers = {int(r["id"]): int(r["area_tier"]) for r in rows}
    check(tiers.get(F_LEGACY) == users_repo.AREA_LOCAL,
          "«Самарская Область» из старой записи — тоже своя для Самары")
    legacy_card = profile_service.render_card(await users_repo.get_user(F_LEGACY))
    check("Область, Самарская" not in legacy_card, "в карточке область не повторяется")

    # ── 28. Оформление и кнопки ─────────────────────────────────────────────
    section("28. Цитата, капча и inline-кнопки")
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

    callbacks = {data for data, url in h.session.inline_buttons if data}
    urls = {url for data, url in h.session.inline_buttons if url}
    check(callbacks == {"onb:accept"},
          f"inline-кнопка с действием одна — «Принимаю» ({sorted(callbacks)})")
    check(urls <= {"https://t.me/example"}, "остальные inline — только ссылки под рекламой")

    print(f"\n\033[1mИтог: {passed} успешно, {failed} с ошибкой\033[0m")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
