"""Сквозной прогон бота без обращения к Telegram.

Проходит весь путь: капча -> предупреждение -> анкета -> лента -> лайки ->
совпадение -> жалоба -> админка -> верификация -> бан. Полезно запускать
после любых правок: python3 tests/smoke_test.py
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

from app import handlers, middlewares                                  # noqa: E402
from app.config import get_settings                                    # noqa: E402
from app.db import ads as ads_repo                                     # noqa: E402
from app.db import captcha as captcha_repo                             # noqa: E402
from app.db import moderation as mod_repo                              # noqa: E402
from app.db import reactions as reactions_repo                         # noqa: E402
from app.db import users as users_repo                                 # noqa: E402
from app.db.database import db                                         # noqa: E402
from app.services import antifraud, reengagement                       # noqa: E402
from app.keyboards import inline as kb_inline                           # noqa: E402
from app.services import captcha as captcha_service                    # noqa: E402
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

    async def click(self, user_id: int, data: str, **kwargs) -> None:
        await self.feed(callback_update(self.bot, user_id, data, **kwargs))

    async def photo(self, user_id: int) -> None:
        await self.feed(photo_update(self.bot, user_id))

    async def state_data(self, user_id: int) -> dict:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.dp.storage.get_data(key)

    def said(self, needle: str) -> bool:
        return any(needle.lower() in t.lower() for t in self.session.texts())

    def clear(self) -> None:
        self.session.clear()


async def solve_captcha(h: Harness, user_id: int, *, correctly: bool = True,
                        wait: bool = True) -> None:
    """Читает правильный ответ из состояния — так может только тест, не бот."""
    if wait:
        await asyncio.sleep(0.5)
    data = await h.state_data(user_id)
    tokens: dict[str, int] = data["cap_tokens"]
    correct = set(data["cap_correct"])
    target = correct if correctly else ({1, 2, 3} - correct or {max(tokens.values())})
    for token, label in tokens.items():
        if label in target:
            await h.click(user_id, f"cap:tok:{token}")
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
    await h.click(user_id, "reg:scope:city")
    await h.click(user_id, "reg:confirm")


async def make_profile(user_id: int, *, gender: str, name: str,
                       city: str = "Волгоград", region: str = "Волгоградская область",
                       lat: float = 48.708, lon: float = 44.513) -> None:
    """Готовая анкета напрямую в базе — чтобы не проходить мастер ради массовки."""
    await users_repo.ensure_user(user_id, f"user{user_id}", name)
    await users_repo.update_user(
        user_id, captcha_passed=1, rules_accepted=1, registered=1, is_active=1,
        name=name, gender=gender, looking_for="any", age=25, about="Тестовая анкета",
        media_type="photo", media_id=f"photo-{user_id}", city=city, region=region,
        country="RU", lat=lat, lon=lon, geo_source="city", search_scope="city",
        age_min=18, age_max=99,
    )


async def main() -> int:
    settings = get_settings()
    await db.connect(settings.db_path)
    storage = await build_storage(settings)
    await storage.redis.flushdb()          # прогон должен начинаться с чистого листа
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

    markup = kb_inline.captcha([(f"t{i}", i) for i in range(1, 16)], {2, 7})
    labels = [b.text for row in markup.inline_keyboard[:3] for b in row]
    check("✅2" in labels and "✅7" in labels,
          "выбранная клетка показывает номер, а не только галочку")
    check(labels.count("✅") == 0, "номер не теряется при выборе")

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
    check(any("onb:next" in str(getattr(c, "reply_markup", ""))
              for c in h.session.calls), "под приветствием кнопка «Далее»")
    h.clear()

    # ── 3. Предупреждение с задержкой ───────────────────────────────────────
    section("3. Предупреждение о мошенниках")
    await h.click(ALICE, "onb:next")
    check(h.session.of_type("DeleteMessage"), "приветствие удаляется")
    check(h.said("мошенник"), "показано предупреждение")
    check(h.said("Кнопка появится через"), "идёт обратный отсчёт")
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
    h.clear()

    # ── 4. Анкета ───────────────────────────────────────────────────────────
    section("4. Заполнение анкеты")
    await h.click(ALICE, "reg:gender:f")
    await h.click(ALICE, "reg:look:m")
    await h.text(ALICE, "семнадцать")
    check(h.said("Введите возраст числом"), "возраст словами не принимается")
    await h.text(ALICE, "16")
    check(h.said("Минимальный возраст"), "младше настроенного минимума не пускает")
    await h.text(ALICE, "26")
    await h.text(ALICE, "http://spam.example")
    check(h.said("Имя должно быть"), "ссылку вместо имени не берём")
    await h.text(ALICE, "Алиса")
    h.clear()

    await h.feed(video_update(h.bot, ALICE, duration=40))
    check(h.said("длиннее"), "видео длиннее 15 секунд отклоняется")
    await h.feed(video_update(h.bot, ALICE, duration=12))
    user = await users_repo.get_user(ALICE)
    check(user["media_type"] == "video", "короткое видео принято")
    h.clear()

    await h.text(ALICE, "Захожу сюда за живым общением. Пишите: t.me/spamchannel")
    check(h.said("нельзя оставлять ссылки"), "ссылки в описании блокируются")
    await h.text(ALICE, "Люблю книги, горы и настолки.")
    check(h.said("Откуда вы"), "дальше спрашивается город")
    h.clear()

    await h.text(ALICE, "Урюпинск")
    check(h.said("Не нашёл такой город"), "незнакомый город честно не найден")
    check(h.said("область"), "предложен запасной путь через область")
    await h.text(ALICE, "Волгоградская область")
    user = await users_repo.get_user(ALICE)
    check(user["region"] == "Волгоградская область", "область определена")
    check(user["lat"] is not None, "координаты области подставлены")
    h.clear()

    await h.click(ALICE, "reg:scope:region")
    check(h.said("Вот как её увидят другие"), "показан предпросмотр анкеты")
    await h.click(ALICE, "reg:confirm")
    user = await users_repo.get_user(ALICE)
    check(user["registered"] == 1 and user["is_active"] == 1, "анкета опубликована")
    check(user["age_min"] == 21 and user["age_max"] == 31,
          "возрастные рамки поиска выставлены по умолчанию")
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

    await h.click(BOB, "reg:scope:near")
    await h.click(BOB, "reg:confirm")
    user = await users_repo.get_user(BOB)
    check(user["search_scope"] == "near", "включён поиск по расстоянию")

    # ── 6. Лента и совпадение ───────────────────────────────────────────────
    section("6. Лента, лайки и совпадение")
    h.clear()
    await h.text(BOB, "🔍 Смотреть анкеты")
    check(h.said("Алиса"), "Борису показана анкета Алисы")
    check(h.said("км от вас"), "в режиме «рядом» показано расстояние")
    h.clear()

    await h.click(BOB, f"br:like:{ALICE}")
    check(await users_repo.count_incoming_likes(ALICE) == 1, "лайк дошёл до Алисы")
    check(h.said("понравились"), "Алисе пришло уведомление о симпатии")
    h.clear()

    await h.text(ALICE, "🔍 Смотреть анкеты")
    check(h.said("Борис"), "Алисе показан Борис")
    h.clear()
    await h.click(ALICE, f"br:like:{BOB}")
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
    await h.text(BOB, "🔍 Смотреть анкеты")
    await h.click(BOB, f"br:like:{CAROL}")
    check(h.said("Лимит лайков на сегодня исчерпан"), "лимит лайков срабатывает")
    check(not await users_repo.get_matches(CAROL), "лайк сверх лимита не засчитан")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 8. Жалоба ───────────────────────────────────────────────────────────
    section("8. Жалоба на анкету")
    await h.text(ALICE, "🔍 Смотреть анкеты")
    h.clear()
    await h.click(ALICE, f"br:report:{CAROL}")
    check(h.said("На что жалуемся"), "предложены причины жалобы")
    await h.click(ALICE, f"rep:scam:{CAROL}")
    await h.text(ALICE, "Просит перевести деньги на карту")
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
    h.clear()
    await h.click(ADMIN, "adm:stats", username="boss")
    check(h.said("Статистика бота"), "статистика собирается")
    check(h.said("Совпадений"), "в статистике есть совпадения")
    check(h.said("Антинакрутка") and h.said("Напоминания"),
          "в статистике есть разделы защиты и напоминаний")
    h.clear()
    await h.text(ADMIN, f"/find {CAROL}", username="boss")
    check(h.said("Карина"), "поиск пользователя работает")
    h.clear()

    await h.click(ADMIN, "adm:cfg", username="boss")
    await h.click(ADMIN, "adm:set:registration", username="boss")
    check(await mod_repo.get_setting("registration_open") == "0",
          "приём новых анкет закрыт")
    h.clear()
    await h.text(999123, "/start", username="newbie")
    check(h.said("Регистрация временно приостановлена"),
          "новичок не может начать регистрацию")
    await h.click(ADMIN, "adm:set:registration", username="boss")
    check(await mod_repo.get_setting("registration_open") == "1",
          "приём анкет снова открыт")
    h.clear()

    # ── 10. Верификация по требованию админа ────────────────────────────────
    section("10. Принудительная верификация")
    await h.click(ADMIN, f"adm:req_verify:{CAROL}", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_forced"] == 1, "требование верификации выставлено")
    code = user["verify_code"]
    h.clear()

    await h.text(CAROL, "🔍 Смотреть анкеты")
    check(h.said("Требуется верификация"), "до проверки бот закрыт")
    check(h.said(code), "пользователю показан код для фото")
    h.clear()

    candidates = await users_repo.search_candidates(await users_repo.get_user(BOB))
    check(all(c["id"] != CAROL for c in candidates),
          "анкета на проверке скрыта из поиска")

    await h.click(CAROL, "ver:start")
    await h.photo(CAROL)
    check(h.said("Заявка отправлена"), "фото проверки принято")
    verifications = await mod_repo.pending_verifications()
    check(len(verifications) == 1, "заявка ждёт админа")
    h.clear()

    await h.click(ADMIN, f"vrf:ok:{verifications[0]['id']}", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["verify_status"] == "verified", "верификация подтверждена")
    check(user["verify_forced"] == 0, "блокировка снята")
    h.clear()
    await h.text(CAROL, "👤 Моя анкета")
    check(h.said("☑️"), "в анкете появилась галочка")
    h.clear()

    # ── 11. Бан и разбан ────────────────────────────────────────────────────
    section("11. Бан и разбан")
    await h.text(ADMIN, f"/ban {CAROL} 2d спам в анкете", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["is_banned"] == 1, "пользователь забанен")
    check(user["banned_until"] is not None, "срок бана записан")
    check(h.said("Доступ заблокирован"), "пользователь уведомлён")
    h.clear()

    await h.text(CAROL, "🔍 Смотреть анкеты")
    check(h.said("Доступ заблокирован"), "забаненный не может пользоваться ботом")
    h.clear()

    await h.text(ADMIN, f"/unban {CAROL}", username="boss")
    user = await users_repo.get_user(CAROL)
    check(user["is_banned"] == 0, "бан снят")
    h.clear()

    # ── 12. Рассылка ────────────────────────────────────────────────────────
    section("12. Рассылка")
    await h.click(ADMIN, "adm:bc", username="boss")
    await h.click(ADMIN, "adm:bc_aud:registered", username="boss")
    check(h.said("Получателей"), "аудитория посчитана")
    await h.text(ADMIN, "Привет! У нас новые анкеты 🎉", username="boss")
    check(h.said("Так это увидят люди"), "показан предпросмотр")
    h.clear()
    await h.click(ADMIN, "adm:bc_go", username="boss")
    await asyncio.sleep(1.0)
    copies = h.session.of_type("CopyMessage")
    check(len(copies) >= 3, f"сообщения разосланы ({len(copies)} шт.)")
    row = await db.fetchone("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    check(row is not None and row["total"] >= 3, "рассылка записана в журнал")
    h.clear()

    # ── 13. Настройки поиска ────────────────────────────────────────────────
    section("13. Настройки поиска")
    await h.text(BOB, "⚙️ Настройки поиска")
    check(h.said("Настройки поиска"), "настройки открываются")
    h.clear()
    await h.click(BOB, "st:age")
    await h.text(BOB, "20-45")
    user = await users_repo.get_user(BOB)
    check(user["age_min"] == 20 and user["age_max"] == 45, "возрастные рамки сохранены")
    await h.click(BOB, "st:radius:100")
    user = await users_repo.get_user(BOB)
    check(user["search_radius"] == 100, "радиус поиска сохранён")
    h.clear()

    # ── 14. Скрытие и удаление анкеты ───────────────────────────────────────
    section("14. Управление анкетой")
    await h.click(ALICE, "pr:hide")
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 0, "анкета скрыта из поиска")
    await h.click(ALICE, "pr:show")
    user = await users_repo.get_user(ALICE)
    check(user["is_active"] == 1, "анкета снова видна")
    h.clear()
    await h.click(ALICE, "pr:delete")
    await h.click(ALICE, "pr:delete_yes")
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

    await h.text(DAVE, "🔍 Смотреть анкеты")
    for extra in EXTRAS[:6]:
        await h.click(DAVE, f"br:dislike:{extra}")

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

    await h.text(DAVE, "🔍 Смотреть анкеты")
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
    await h.click(FRANK, "remind:off")
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
    await make_profile(GLEB, gender="m", name="Глеб")
    await make_profile(HELEN, gender="f", name="Елена")
    h.clear()

    await h.text(GLEB, "🔍 Смотреть анкеты")
    has_note_button = any(
        "br:note" in str(getattr(c, "reply_markup", "")) for c in h.session.calls
    )
    check(has_note_button, "в ленте есть кнопка «С сообщением»")
    h.clear()

    await h.click(GLEB, f"br:note:{HELEN}")
    check(h.said("Что написать"), "бот просит текст сообщения")
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
    check(bool(to_helen), "Елене пришло уведомление")
    delivered = " ".join(
        (getattr(c, "text", "") or getattr(c, "caption", "") or "") for c in to_helen
    )
    check("понравилась" in delivered, "в уведомлении сказано, что анкета понравилась")
    check("Глеб" in delivered, "показана анкета отправителя")
    check("где снимали фото" in delivered, "показан текст сообщения")
    check(any("ans:like" in str(getattr(c, "reply_markup", "")) for c in to_helen),
          "есть кнопки ответить или пропустить")
    h.clear()

    await h.click(HELEN, f"ans:like:{GLEB}")
    check(h.said("Взаимная симпатия"), "ответ взаимностью создаёт совпадение")
    check(len(await users_repo.get_matches(HELEN)) == 1, "совпадение сохранено")
    h.clear()

    # Если не ответить сразу, сообщение видно в разделе «кто меня лайкнул»
    await make_profile(200100, gender="m", name="Игорь")
    await reactions_repo.add_reaction(200100, HELEN, "like", "Сообщение из инбокса")
    await h.text(HELEN, "❤️ Кто меня лайкнул")
    check(h.said("Сообщение из инбокса"), "текст виден и в списке лайков")
    h.clear()

    # ── 20. Модераторы ──────────────────────────────────────────────────────
    section("20. Модераторы с урезанными правами")
    await make_profile(MOD1, gender="m", name="Модератор")
    await make_profile(MOD2, gender="m", name="Модератор Два")
    h.clear()

    await h.click(ADMIN, "adm:staff_add", username="boss")
    await h.text(ADMIN, str(MOD1), username="boss")
    user = await users_repo.get_user(MOD1)
    check(user["is_moderator"] == 1, "модератор назначен")
    check(h.said("Вас назначили модератором"), "модератор уведомлён")
    h.clear()

    await h.text(MOD1, "/start")
    keyboards = " ".join(str(getattr(c, "reply_markup", "")) for c in h.session.calls)
    check("👮 Модератор" in keyboards, "в меню появилась кнопка модератора")
    check("🛠 Админ-панель" not in keyboards, "кнопки админки у него нет")
    h.clear()

    await h.text(MOD1, "👮 Модератор")
    check(h.said("Панель модератора"), "панель модератора открывается")
    panel = " ".join(str(getattr(c, "reply_markup", "")) for c in h.session.calls)
    check("adm:reports" in panel and "adm:ban" in panel, "жалобы и баны доступны")
    check("adm:bc" not in panel, "рассылки в меню нет")
    check("adm:ads" not in panel and "adm:staff" not in panel,
          "рекламы и модераторов в меню нет")
    check("adm:cfg" not in panel, "настроек бота в меню нет")
    h.clear()

    await h.click(MOD1, "adm:bc")
    check(not h.said("Рассылка"), "прямое нажатие на рассылку не срабатывает")
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

    await h.click(ADMIN, f"adm:staff_del:{MOD1}", username="boss")
    check((await users_repo.get_user(MOD1))["is_moderator"] == 0, "права сняты")
    h.clear()
    await h.text(MOD1, "👮 Модератор")
    check(not h.said("Панель модератора"), "бывший модератор в панель не попадает")
    h.clear()

    # ── 21. Рекламные посты ─────────────────────────────────────────────────
    section("21. Реклама между анкетами")
    await h.click(ADMIN, "adm:ads", username="boss")
    check(h.said("Реклама"), "раздел рекламы открывается")
    h.clear()

    await h.click(ADMIN, "adm:ad_new", username="boss")
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
        await h.text(viewer, "🔍 Смотреть анкеты")
        for _ in range(steps):
            data = await h.state_data(viewer)
            current = data.get("current")
            if current:
                await h.click(viewer, f"br:dislike:{current}")
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

    await h.click(ADMIN, f"adm:ad_toggle:{ad_id}", username="boss")
    ad = await ads_repo.get(ad_id)
    check(ad["is_active"] == 0, "пост выключается")
    shows_before = ad["shows"]
    h.clear()

    copies = await browse_until_ad(GLEB, 3)
    ad = await ads_repo.get(ad_id)
    check(ad["shows"] == shows_before and not copies,
          "выключенный пост не показывается")
    h.clear()

    await h.click(ADMIN, f"adm:ad_del:{ad_id}", username="boss")
    check(not await ads_repo.list_all(), "пост удаляется")

    # ── 22. Ответный лайк не тратит лимит ───────────────────────────────────
    section("22. Ответ на чужой лайк не упирается в лимит")
    await make_profile(NINA, gender="f", name="Нина")
    await make_profile(OLEG, gender="m", name="Олег")
    for index, extra in enumerate(range(200200, 200204)):
        await make_profile(extra, gender="m", name=f"Прохожий {index + 1}")

    await mod_repo.set_setting("likes_limit", "1")
    h.clear()

    # Нина тратит весь суточный лимит на поиск
    await h.text(NINA, "🔍 Смотреть анкеты")
    await h.click(NINA, "br:like:200200")
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "лимит израсходован")
    h.clear()

    await h.click(NINA, "br:like:200201")
    check(h.said("Лимит лайков на сегодня исчерпан"),
          "новый лайк в поиске блокируется")
    check(h.said("Отвечать тем, кто лайкнул вас"),
          "бот подсказывает, что ответы не ограничены")
    h.clear()

    # А теперь её лайкнули — ответить она должна мочь
    await reactions_repo.add_reaction(OLEG, NINA, "like")
    await h.text(NINA, "❤️ Кто меня лайкнул")
    check(h.said("Олег"), "анкета отправителя показана несмотря на лимит")
    counters = [str(getattr(c, "reply_markup", "")) for c in h.session.calls]
    check(not any("❤️ (0)" in c for c in counters),
          "на кнопке нет счётчика — лайк бесплатный")
    h.clear()

    await h.click(NINA, f"br:like:{OLEG}")
    check(h.said("Взаимная симпатия"), "ответный лайк проходит при нулевом лимите")
    check(len(await users_repo.get_matches(NINA)) == 1, "совпадение создано")
    check(await users_repo.likes_left(await users_repo.get_user(NINA), 1) == 0,
          "ответ не ушёл в минус и лимит не тронут")
    h.clear()

    # Из уведомления — тоже бесплатно
    await reactions_repo.add_reaction(200202, NINA, "like", "Привет из уведомления")
    await h.click(NINA, "ans:like:200202")
    check(len(await users_repo.get_matches(NINA)) == 2,
          "ответ из уведомления тоже не требует лимита")
    h.clear()

    # Но исходящий лайк тому, кто её не лайкал, по-прежнему закрыт
    await h.text(NINA, "🔍 Смотреть анкеты")
    await h.click(NINA, "br:like:200203")
    check(h.said("Лимит лайков"), "лимит на исходящие лайки продолжает работать")
    await mod_repo.set_setting("likes_limit", "50")
    h.clear()

    # ── 23. На владельца ограничения не действуют ───────────────────────────
    section("23. Владелец без ограничений")
    await make_profile(ADMIN, gender="m", name="Владелец")
    await mod_repo.set_setting("likes_limit", "1")
    cfg.af_fast_streak, cfg.af_ratio_window = 3, 5
    h.clear()

    await h.text(ADMIN, "🔍 Смотреть анкеты", username="boss")
    for target in (200200, 200201, 200202, 200203):
        await h.click(ADMIN, f"br:like:{target}", username="boss")
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
    await h.click(ADMIN, f"adm:req_verify:{ADMIN}", username="boss")
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
    h.clear()

    await h.click(SCREEN, "reg:scope:city", username="screenuser")
    check(h.said("Вот как её увидят другие"), "предпросмотр показан")
    await h.click(SCREEN, "reg:confirm", username="screenuser")
    preview_removed = bool(h.session.of_type("DeleteMessage"))
    check(preview_removed, "после подтверждения предпросмотр убирается")
    check((await users_repo.get_user(SCREEN))["registered"] == 1,
          "анкета опубликована")

    print(f"\n\033[1mИтог: {passed} успешно, {failed} с ошибкой\033[0m")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
