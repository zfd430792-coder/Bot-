"""География: разбор города, обратный геокодинг и расстояния.

Локальный справочник (app/data/cities.json) — основной источник: бот работает
без интернета. Онлайн-геокодер (OpenStreetMap Nominatim) подключается по
желанию через GEOCODER_ENABLED и нужен только для городов, которых нет в
справочнике; результаты кешируются в базе.
"""
from __future__ import annotations

import difflib
import json
import logging
import math
import random
from dataclasses import dataclass
from functools import lru_cache

from app.config import BASE_DIR
from app.db.database import db, haversine

log = logging.getLogger(__name__)

DATA_FILE = BASE_DIR / "app" / "data" / "cities.json"

COUNTRY_NAMES = {
    "RU": "Россия", "BY": "Беларусь", "KZ": "Казахстан", "UA": "Украина",
    "MD": "Молдова", "AM": "Армения", "GE": "Грузия", "AZ": "Азербайджан",
    "KG": "Кыргызстан", "UZ": "Узбекистан", "TJ": "Таджикистан",
    "TM": "Туркменистан", "LV": "Латвия", "LT": "Литва", "EE": "Эстония",
    "IL": "Израиль", "DE": "Германия", "CZ": "Чехия", "PL": "Польша",
    "TR": "Турция", "AE": "ОАЭ",
}


@dataclass(slots=True, frozen=True)
class City:
    name: str
    region: str
    country: str
    lat: float
    lon: float
    pop: int = 0

    @property
    def country_name(self) -> str:
        return COUNTRY_NAMES.get(self.country, self.country)

    @property
    def title(self) -> str:
        if self.region and self.region != self.name:
            return f"{self.name}, {self.region}"
        return self.name


def normalize(text: str) -> str:
    """Приводит ввод к сравнимому виду: регистр, ё, дефисы, лишние слова."""
    text = (text or "").strip().lower().replace("ё", "е")
    for junk in ("город ", "г. ", "г.", "гор. ", "пос. ", "посёлок ", "поселок "):
        if text.startswith(junk):
            text = text[len(junk):]
    out = []
    for ch in text:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
        elif ch in "-–—":
            out.append(" ")
    return " ".join("".join(out).split())


@lru_cache(maxsize=1)
def _load() -> tuple[list[City], dict[str, list[int]]]:
    if not DATA_FILE.is_file():
        log.warning("Справочник городов не найден: %s", DATA_FILE)
        return [], {}
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    cities = [
        City(r["name"], r["region"], r["country"], r["lat"], r["lon"], r.get("pop", 0))
        for r in raw
    ]
    index: dict[str, list[int]] = {}
    for i, (city, row) in enumerate(zip(cities, raw)):
        keys = {normalize(city.name), *(normalize(a) for a in row.get("aliases", []))}
        for key in keys:
            if key:
                index.setdefault(key, []).append(i)
    return cities, index


def all_cities() -> list[City]:
    return _load()[0]


def find(query: str, limit: int = 5) -> list[City]:
    """Ищет город: точное совпадение -> начало строки -> нечёткое сравнение."""
    cities, index = _load()
    key = normalize(query)
    if not key or len(key) < 2:
        return []

    # Пользователь мог написать «Казань, Татарстан» — берём первую часть
    head = key.split(",")[0].strip() if "," in key else key

    for candidate in (key, head):
        if candidate in index:
            found = [cities[i] for i in index[candidate]]
            return sorted(found, key=lambda c: -c.pop)[:limit]

    starts = [c for c in cities if normalize(c.name).startswith(head)]
    if starts:
        return sorted(starts, key=lambda c: -c.pop)[:limit]

    # Нечёткое сравнение — с запасом на опечатку, но без «Урюпинск -> Пинск»:
    # сильно различающиеся по длине названия отбрасываем.
    names = [n for n in index if abs(len(n) - len(head)) <= 2]
    close = difflib.get_close_matches(head, names, n=limit, cutoff=0.82)
    result: list[City] = []
    for name in close:
        for i in index[name]:
            if cities[i] not in result:
                result.append(cities[i])
    return sorted(result, key=lambda c: -c.pop)[:limit]


def nearest(lat: float, lon: float, max_km: float = 220.0) -> City | None:
    """Ближайший город к координатам — обратный геокодинг без интернета."""
    cities = all_cities()
    if not cities:
        return None
    best: City | None = None
    best_km = float("inf")
    for city in cities:
        km = haversine(lat, lon, city.lat, city.lon)
        if km is not None and km < best_km:
            best, best_km = city, km
    if best is None or best_km > max_km:
        return best if best_km <= max_km * 3 else None
    return best


def regions(country: str | None = None) -> list[str]:
    seen = {c.region for c in all_cities() if country is None or c.country == country}
    return sorted(seen)


@lru_cache(maxsize=1)
def _region_anchors() -> dict[str, City]:
    """Опорная точка региона — его крупнейший город из справочника."""
    anchors: dict[str, City] = {}
    for city in all_cities():
        key = normalize(city.region)
        if not key:
            continue
        current = anchors.get(key)
        if current is None or city.pop > current.pop:
            anchors[key] = city
    return anchors


# Народные названия регионов
REGION_ALIASES = {
    "подмосковье": "московская область",
    "ленобласть": "ленинградская область",
    "ленинградская": "ленинградская область",
    "кубань": "краснодарский край",
    "башкирия": "республика башкортостан",
    "якутия": "республика саха якутия",
    "чувашия": "чувашская республика",
    "удмуртия": "удмуртская республика",
    "кбр": "кабардино балкарская республика",
    "кчр": "карачаево черкесская республика",
    "хмао": "ханты мансийский ао",
    "янао": "ямало ненецкий ао",
    "нао": "ненецкий ао",
    "еао": "еврейская ао",
}


def find_region(query: str, limit: int = 5) -> list[City]:
    """Ищет регион (область/край/республику) и возвращает его опорный город.

    Нужно, когда посёлка нет в справочнике: человек называет область, а бот
    получает и название региона для поиска «по всей области», и координаты
    для примерного расстояния.
    """
    anchors = _region_anchors()
    key = REGION_ALIASES.get(normalize(query), normalize(query))
    if not key or len(key) < 3:
        return []
    if key in anchors:
        return [anchors[key]]

    # «волгоградская» вместо «волгоградская область» и наоборот
    hits = [city for name, city in anchors.items() if key in name or name in key]
    if hits:
        return sorted(hits, key=lambda c: -c.pop)[:limit]

    close = difflib.get_close_matches(key, list(anchors), n=limit, cutoff=0.7)
    return [anchors[name] for name in close][:limit]


# По этим словам видно, что назвали регион, а не город
REGION_WORDS = ("област", " обл ", "край", "республик", "округ", " ао ")


def region_place(anchor: City) -> City:
    """Место «регион целиком»: название — сам регион, точка — его центр.

    Так сохраняется человек, который указал только область: в анкете видно
    «📍 Самарская область», а в поиске он свой для любого города этой области.
    """
    return City(anchor.region, anchor.region, anchor.country, anchor.lat, anchor.lon)


def find_whole_region(query: str) -> City | None:
    """Узнаёт регион, названный вместо города: «Самарская область», «Подмосковье».

    None — это похоже на обычный город или посёлок; тогда бот, как и раньше,
    уточнит область отдельным вопросом.
    """
    key = REGION_ALIASES.get(normalize(query), normalize(query))
    if len(key) < 4:
        return None
    anchors = _region_anchors()
    if key in anchors:
        return region_place(anchors[key])
    # «Самарская» вместо «Самарская область»
    starts = [name for name in anchors if name.startswith(key + " ")]
    if len(starts) == 1:
        return region_place(anchors[starts[0]])
    # «Самарская обл», «Пермский кр.» и опечатки — только если назвали сам тип региона
    if any(word in f" {key} " for word in REGION_WORDS):
        hits = find_region(query, limit=1)
        if hits:
            return region_place(hits[0])
    return None


def jitter(lat: float, lon: float, meters: int = 350) -> tuple[float, float]:
    """Небольшой случайный сдвиг координат.

    Храним точку не точнее ~350 м: расстояние до собеседника от этого почти не
    меняется, а вычислить чей-то адрес перебором лайков становится нельзя.
    """
    d_lat = random.uniform(-meters, meters) / 111_320
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    d_lon = random.uniform(-meters, meters) / (111_320 * cos_lat)
    return round(lat + d_lat, 5), round(lon + d_lon, 5)


def distance_text(km: float | None) -> str:
    """Человеческая формулировка расстояния (без выдачи точных координат)."""
    if km is None:
        return ""
    if km < 1:
        return "меньше 1 км от вас"
    if km < 10:
        return f"~{km:.0f} км от вас"
    if km < 100:
        return f"~{round(km / 5) * 5} км от вас"
    return f"~{round(km / 10) * 10} км от вас"


# ───────────────────────── Онлайн-геокодер (опция) ──────────────────────────

async def geocode_online(query: str, email: str = "") -> City | None:
    """Ищет город через Nominatim. Требует GEOCODER_ENABLED=1."""
    key = normalize(query)
    cached = await db.fetchone("SELECT * FROM geo_cache WHERE query = ?", (key,))
    if cached is not None:
        if cached["lat"] is None:
            return None
        return City(cached["name"], cached["region"] or "", cached["country"] or "",
                    cached["lat"], cached["lon"])

    try:
        import aiohttp

        params = {
            "q": query, "format": "jsonv2", "limit": "1",
            "addressdetails": "1", "accept-language": "ru",
        }
        headers = {"User-Agent": f"dating-bot/1.0 ({email or 'contact-not-set'})"}
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(
                "https://nominatim.openstreetmap.org/search", params=params
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception as exc:  # сеть/лимиты не должны ронять регистрацию
        log.warning("Геокодер недоступен: %s", exc)
        return None

    if not payload:
        await db.execute(
            "INSERT OR REPLACE INTO geo_cache (query, lat) VALUES (?, NULL)", (key,)
        )
        return None

    item = payload[0]
    address = item.get("address", {})
    name = (address.get("city") or address.get("town") or address.get("village")
            or item.get("name") or query.strip())
    region = address.get("state") or address.get("region") or ""
    country = (address.get("country_code") or "").upper()
    city = City(name, region, country, float(item["lat"]), float(item["lon"]))
    await db.execute(
        "INSERT OR REPLACE INTO geo_cache (query, name, region, country, lat, lon) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (key, city.name, city.region, city.country, city.lat, city.lon),
    )
    return city


async def resolve(query: str, use_online: bool, email: str = "") -> list[City]:
    """Основная точка входа: локальный справочник, при неудаче — геокодер."""
    local = find(query)
    if local:
        return local
    if use_online:
        city = await geocode_online(query, email)
        if city:
            return [city]
    return []
