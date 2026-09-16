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
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "dating"

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

    # Автоматическая защита от накрутки лайков
    antifraud_enabled: bool = True
    af_fast_seconds: float = 1.2      # быстрее этого реакция считается машинной
    af_fast_streak: int = 12          # столько быстрых реакций подряд = сигнал
    af_ratio_window: int = 30         # на скольких последних реакциях смотрим долю
    af_ratio_threshold: float = 0.95  # доля лайков, после которой это накрутка
    af_ban_hours: int = 24            # срок автобана на втором нарушении

    # Напоминания уснувшим пользователям
    reengagement_enabled: bool = True
    inactive_hours: int = 24          # через сколько молчания напоминать
    reminder_cooldown_hours: int = 72 # не чаще одного напоминания в этот срок
    reminder_max_count: int = 3       # после стольких проигнорированных — молчим
    quiet_hours_start: int = 22       # ночью не пишем (по местному времени)
    quiet_hours_end: int = 9

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
            min_age=max(1, _int("MIN_AGE", 18)),
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
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0").strip(),
            redis_prefix=os.getenv("REDIS_PREFIX", "dating").strip() or "dating",
            antifraud_enabled=_bool("ANTIFRAUD_ENABLED", True),
            af_fast_seconds=_float("AF_FAST_SECONDS", 1.2),
            af_fast_streak=_int("AF_FAST_STREAK", 12),
            af_ratio_window=_int("AF_RATIO_WINDOW", 30),
            af_ratio_threshold=_float("AF_RATIO_THRESHOLD", 0.95),
            af_ban_hours=_int("AF_BAN_HOURS", 24),
            reengagement_enabled=_bool("REENGAGEMENT_ENABLED", True),
            inactive_hours=_int("INACTIVE_HOURS", 24),
            reminder_cooldown_hours=_int("REMINDER_COOLDOWN_HOURS", 72),
            reminder_max_count=_int("REMINDER_MAX_COUNT", 3),
            quiet_hours_start=_int("QUIET_HOURS_START", 22),
            quiet_hours_end=_int("QUIET_HOURS_END", 9),
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
