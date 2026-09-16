"""Сводная статистика для админ-панели."""
from __future__ import annotations

from typing import Any

from app.db import ads as ads_repo
from app.db import moderation as mod_repo
from app.db.database import db


async def _val(sql: str, params: tuple = ()) -> int:
    return int(await db.fetchval(sql, params, default=0) or 0)


async def collect() -> dict[str, Any]:
    users_total = await _val("SELECT COUNT(*) FROM users")
    registered = await _val("SELECT COUNT(*) FROM users WHERE registered = 1")
    unfinished = await _val(
        "SELECT COUNT(*) FROM users WHERE registered = 0 AND captcha_passed = 1"
    )
    new_today = await _val("SELECT COUNT(*) FROM users WHERE date(created_at) = date('now')")
    new_yesterday = await _val(
        "SELECT COUNT(*) FROM users WHERE date(created_at) = date('now', '-1 day')"
    )
    new_week = await _val(
        "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 days')"
    )
    active_day = await _val(
        "SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-1 day')"
    )
    active_week = await _val(
        "SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-7 days')"
    )
    active_month = await _val(
        "SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-30 days')"
    )
    males = await _val("SELECT COUNT(*) FROM users WHERE gender = 'm' AND registered = 1")
    females = await _val("SELECT COUNT(*) FROM users WHERE gender = 'f' AND registered = 1")
    hidden = await _val("SELECT COUNT(*) FROM users WHERE registered = 1 AND is_active = 0")
    banned = await _val("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    verified = await _val("SELECT COUNT(*) FROM users WHERE verify_status = 'verified'")
    verify_wait = await _val(
        "SELECT COUNT(*) FROM verifications WHERE status = 'pending' AND media_id IS NOT NULL"
    )
    verify_forced = await _val("SELECT COUNT(*) FROM users WHERE verify_forced = 1")

    likes = await _val("SELECT COUNT(*) FROM reactions WHERE kind = 'like'")
    likes_today = await _val(
        "SELECT COUNT(*) FROM reactions WHERE kind = 'like' AND date(created_at) = date('now')"
    )
    dislikes = await _val("SELECT COUNT(*) FROM reactions WHERE kind = 'dislike'")
    matches = await _val("SELECT COUNT(*) FROM matches")
    matches_today = await _val(
        "SELECT COUNT(*) FROM matches WHERE date(created_at) = date('now')"
    )
    reports_open = await _val("SELECT COUNT(*) FROM reports WHERE status = 'open'")
    reports_total = await _val("SELECT COUNT(*) FROM reports")

    captcha_pass = await _val("SELECT COALESCE(SUM(passes), 0) FROM captcha_state")
    captcha_fail = await _val("SELECT COALESCE(SUM(total_fails), 0) FROM captcha_state")
    captcha_blocked = await _val(
        "SELECT COUNT(*) FROM captcha_state WHERE blocked_until > datetime('now')"
    )

    ad_shows = await ads_repo.total_shows()
    ad_active = await ads_repo.count_active()
    moderators = await _val("SELECT COUNT(*) FROM users WHERE is_moderator = 1")
    notes = await _val("SELECT COUNT(*) FROM reactions WHERE note IS NOT NULL")

    af_triggers = await mod_repo.get_int_setting("af_triggers", 0)
    af_autobans = await mod_repo.get_int_setting("af_autobans", 0)
    reminders_sent = await mod_repo.get_int_setting("reminders_sent", 0)
    reminders_off = await _val("SELECT COUNT(*) FROM users WHERE notify_enabled = 0")
    sleeping = await _val(
        "SELECT COUNT(*) FROM users WHERE registered = 1 AND is_banned = 0 "
        "AND last_active < datetime('now', '-1 day')"
    )

    avg_age = await db.fetchval(
        "SELECT ROUND(AVG(age), 1) FROM users WHERE registered = 1", default=0
    )
    top_cities = await db.fetchall(
        "SELECT city, COUNT(*) AS cnt FROM users WHERE registered = 1 AND city IS NOT NULL "
        "GROUP BY city ORDER BY cnt DESC LIMIT 7"
    )

    conversion = round(registered / users_total * 100, 1) if users_total else 0.0
    match_rate = round(matches * 2 / likes * 100, 1) if likes else 0.0

    return {
        "users_total": users_total,
        "registered": registered,
        "unfinished": unfinished,
        "new_today": new_today,
        "new_yesterday": new_yesterday,
        "new_week": new_week,
        "active_day": active_day,
        "active_week": active_week,
        "active_month": active_month,
        "males": males,
        "females": females,
        "hidden": hidden,
        "banned": banned,
        "verified": verified,
        "verify_wait": verify_wait,
        "verify_forced": verify_forced,
        "likes": likes,
        "likes_today": likes_today,
        "dislikes": dislikes,
        "matches": matches,
        "matches_today": matches_today,
        "reports_open": reports_open,
        "reports_total": reports_total,
        "captcha_pass": captcha_pass,
        "captcha_fail": captcha_fail,
        "captcha_blocked": captcha_blocked,
        "ad_shows": ad_shows,
        "ad_active": ad_active,
        "moderators": moderators,
        "notes": notes,
        "af_triggers": af_triggers,
        "af_autobans": af_autobans,
        "reminders_sent": reminders_sent,
        "reminders_off": reminders_off,
        "sleeping": sleeping,
        "avg_age": avg_age or 0,
        "top_cities": [(r["city"], r["cnt"]) for r in top_cities],
        "conversion": conversion,
        "match_rate": match_rate,
    }


def render_short(s: dict[str, Any]) -> str:
    """Сводка для модератора: только то, что нужно для дежурства."""
    return (
        "📊 <b>Сводка</b>\n\n"
        f"👥 Анкет: <b>{s['registered']}</b>   Новых сегодня: +{s['new_today']}\n"
        f"🔥 Активны за сутки: {s['active_day']}\n\n"
        f"🚨 Открытых жалоб: <b>{s['reports_open']}</b>\n"
        f"☑️ Ждут верификации: <b>{s['verify_wait']}</b>\n"
        f"🚫 Забанено: {s['banned']}\n"
        f"🛡 Автоблокировок: {s['af_autobans']}"
    )


def render(s: dict[str, Any]) -> str:
    bar_total = max(1, s["males"] + s["females"])
    male_pct = round(s["males"] / bar_total * 100)
    cities = "\n".join(
        f"   {i}. {city} — {cnt}" for i, (city, cnt) in enumerate(s["top_cities"], 1)
    ) or "   пока пусто"

    return (
        "📊 <b>Статистика бота</b>\n\n"
        "👥 <b>Пользователи</b>\n"
        f"   Всего зашли: <b>{s['users_total']}</b>\n"
        f"   С анкетой: <b>{s['registered']}</b> (конверсия {s['conversion']}%)\n"
        f"   Не дозаполнили: {s['unfinished']}\n"
        f"   Скрыли анкету: {s['hidden']}\n\n"
        "📈 <b>Приток</b>\n"
        f"   Сегодня: <b>+{s['new_today']}</b>   Вчера: +{s['new_yesterday']}\n"
        f"   За неделю: +{s['new_week']}\n\n"
        "🔥 <b>Активность</b>\n"
        f"   За сутки: <b>{s['active_day']}</b>\n"
        f"   За неделю: {s['active_week']}   За месяц: {s['active_month']}\n\n"
        "🚻 <b>Аудитория</b>\n"
        f"   👨 {s['males']} ({male_pct}%)   👩 {s['females']} ({100 - male_pct}%)\n"
        f"   Средний возраст: {s['avg_age']}\n\n"
        "❤️ <b>Активность в поиске</b>\n"
        f"   Лайков всего: <b>{s['likes']}</b> (сегодня {s['likes_today']})\n"
        f"   Пропусков: {s['dislikes']}\n"
        f"   Лайков с сообщением: <b>{s['notes']}</b>\n"
        f"   Совпадений: <b>{s['matches']}</b> (сегодня {s['matches_today']})\n"
        f"   Доля взаимности: {s['match_rate']}%\n\n"
        "🛡 <b>Модерация</b>\n"
        f"   Забанено: {s['banned']}\n"
        f"   Жалобы: <b>{s['reports_open']}</b> открытых / {s['reports_total']} всего\n"
        f"   Верифицировано: {s['verified']}\n"
        f"   Ждут проверки: <b>{s['verify_wait']}</b>   Заблокированы до проверки: {s['verify_forced']}\n\n"
        "🤖 <b>Капча</b>\n"
        f"   Пройдено: {s['captcha_pass']}   Провалов: {s['captcha_fail']}\n"
        f"   Сейчас в блоке: {s['captcha_blocked']}\n\n"
        "🛡 <b>Антинакрутка</b>\n"
        f"   Срабатываний: {s['af_triggers']}   Автобанов: <b>{s['af_autobans']}</b>\n"
        f"   Модераторов: {s['moderators']}\n\n"
        "📣 <b>Реклама</b>\n"
        f"   Активных постов: {s['ad_active']}   Показов: <b>{s['ad_shows']}</b>\n\n"
        "🔔 <b>Напоминания</b>\n"
        f"   Отправлено всего: {s['reminders_sent']}\n"
        f"   Не заходили сутки+: <b>{s['sleeping']}</b>   Отписались: {s['reminders_off']}\n\n"
        "🏙 <b>Топ городов</b>\n" + cities
    )
