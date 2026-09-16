"""Конфигурация бота: читается из переменных окружения либо из файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Минималистичный загрузчик .env без внешних зависимостей."""
    path = path or BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Переменные окружения имеют приоритет над .env
        os.environ.setdefault(key, value)


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _ids(key: str) -> list[int]:
    raw = os.getenv(key, "")
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


@dataclass(slots=True)
class Settings:
    bot_token: str
    admin_ids: list[int] = field(default_factory=list)
    log_chat_id: int | None = None

    db_path: Path = BASE_DIR / "data" / "bot.db"

    likes_limit_per_day: int = 50
    min_age: int = 18
    max_age: int = 99
    default_radius_km: int = 50
    max_video_seconds: int = 15
    rules_delay_seconds: int = 5

    captcha_max_attempts: int = 3
    captcha_max_refresh: int = 3
    captcha_block_minutes: int = 15
    captcha_timeout_seconds: int = 150
    captcha_min_solve_ms: int = 2000

    throttle_seconds: float = 0.4

    geocoder_enabled: bool = False
    geocoder_email: str = ""

    # Ограничения профиля
    name_min_len: int = 2
    name_max_len: int = 24
    about_max_len: int = 600
    max_radius_km: int = 500

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен "
                "бота, полученный у @BotFather."
            )
        admins = _ids("ADMIN_IDS")
        if not admins:
            raise RuntimeError(
                "ADMIN_IDS не заданы. Укажите хотя бы один Telegram ID администратора."
            )

        db_raw = os.getenv("DB_PATH", "data/bot.db").strip()
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path

        log_chat = os.getenv("LOG_CHAT_ID", "").strip()
        log_chat_id: int | None
        try:
            log_chat_id = int(log_chat) if log_chat else None
        except ValueError:
            log_chat_id = None

        return cls(
            bot_token=token,
            admin_ids=admins,
            log_chat_id=log_chat_id,
            db_path=db_path,
            likes_limit_per_day=_int("LIKES_LIMIT_PER_DAY", 50),
            min_age=max(18, _int("MIN_AGE", 18)),  # 18+ жёстко, ниже опускать нельзя
            max_age=_int("MAX_AGE", 99),
            default_radius_km=_int("DEFAULT_RADIUS_KM", 50),
            max_video_seconds=_int("MAX_VIDEO_SECONDS", 15),
            rules_delay_seconds=_int("RULES_DELAY_SECONDS", 5),
            captcha_max_attempts=_int("CAPTCHA_MAX_ATTEMPTS", 3),
            captcha_max_refresh=_int("CAPTCHA_MAX_REFRESH", 3),
            captcha_block_minutes=_int("CAPTCHA_BLOCK_MINUTES", 15),
            captcha_timeout_seconds=_int("CAPTCHA_TIMEOUT_SECONDS", 150),
            captcha_min_solve_ms=_int("CAPTCHA_MIN_SOLVE_MS", 2000),
            throttle_seconds=_float("THROTTLE_SECONDS", 0.4),
            geocoder_enabled=_bool("GEOCODER_ENABLED", False),
            geocoder_email=os.getenv("GEOCODER_EMAIL", "").strip(),
        )

    @property
    def log_target(self) -> int:
        """Куда слать служебные логи."""
        return self.log_chat_id or self.admin_ids[0]

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


settings: Settings | None = None


def get_settings() -> Settings:
    """Ленивая инициализация настроек (удобно для тестов)."""
    global settings
    if settings is None:
        settings = Settings.load()
    return settings
