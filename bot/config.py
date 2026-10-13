from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in os.getenv(name, default).split(",") if part.strip())


def _optional_chat_id(value: str) -> int | str | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    mongo_uri: str
    mongo_database: str
    sqlite_path: Path
    destination_chat_id: int | str | None
    cookie_file: Path | None
    work_dir: Path
    max_workers: int
    max_active_tasks_per_user: int
    max_upload_size_bytes: int
    keep_failed_files: bool
    log_level: str
    hotstar_domains: tuple[str, ...]
    sonyliv_domains: tuple[str, ...]
    allowed_user_ids: tuple[int, ...]

    @classmethod
    def from_env(cls) -> "Config":
        missing = [key for key in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(key, "").strip()]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing) +
                ". Copy .env.example to .env and fill in your Telegram credentials."
            )
        if any(os.getenv(key, "").strip().lower() in {"replace_me", "changeme"} for key in ("API_HASH", "BOT_TOKEN")):
            raise RuntimeError("Replace the placeholder API_HASH and BOT_TOKEN values in .env before starting the bot.")
        try:
            api_id = int(os.environ["API_ID"])
        except ValueError as exc:
            raise RuntimeError("API_ID must be an integer.") from exc

        allowed_ids = []
        for value in _csv("ALLOWED_USER_IDS", ""):
            try:
                allowed_ids.append(int(value))
            except ValueError as exc:
                raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated numeric Telegram user IDs.") from exc

        cookie_text = os.getenv("COOKIE_FILE", "").strip()
        cookie_file = Path(cookie_text).expanduser() if cookie_text else None
        if cookie_file and not cookie_file.is_file():
            raise RuntimeError(f"COOKIE_FILE is configured but does not exist: {cookie_file}")

        max_workers = max(1, min(8, int(os.getenv("MAX_WORKERS", "2"))))
        max_active = max(1, int(os.getenv("MAX_ACTIVE_TASKS_PER_USER", "1")))
        upload_gb = max(0.1, float(os.getenv("MAX_UPLOAD_SIZE_GB", "1.9")))
        bool_text = os.getenv("KEEP_FAILED_FILES", "true").strip().lower()

        sqlite_path = Path(os.getenv("SQLITE_PATH", "data/bot.sqlite3")).expanduser()
        work_dir = Path(os.getenv("WORK_DIR", "work")).expanduser()
        return cls(
            api_id=api_id,
            api_hash=os.environ["API_HASH"].strip(),
            bot_token=os.environ["BOT_TOKEN"].strip(),
            mongo_uri=os.getenv("MONGO_URI", "").strip(),
            mongo_database=os.getenv("MONGO_DATABASE", "hs2_telegram_bot").strip(),
            sqlite_path=sqlite_path,
            destination_chat_id=_optional_chat_id(os.getenv("DESTINATION_CHAT_ID", "")),
            cookie_file=cookie_file,
            work_dir=work_dir,
            max_workers=max_workers,
            max_active_tasks_per_user=max_active,
            max_upload_size_bytes=int(upload_gb * 1024 ** 3),
            keep_failed_files=bool_text not in {"0", "false", "no", "off"},
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            hotstar_domains=_csv("HOTSTAR_DOMAINS", "hotstar.com,jiohotstar.com"),
            sonyliv_domains=_csv("SONYLIV_DOMAINS", "sonyliv.com,sonyliv.in"),
            allowed_user_ids=tuple(allowed_ids),
        )
