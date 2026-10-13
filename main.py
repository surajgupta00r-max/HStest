from __future__ import annotations

import asyncio
import logging

from pyrogram import Client, idle
from pyrogram.enums import ParseMode

from bot.config import Config
from bot.database import Database
from bot.handlers import register_handlers
from bot.manager import TaskManager


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def run_bot() -> None:
    config = Config.from_env()
    configure_logging(config.log_level)
    logger = logging.getLogger("hs2bot")

    db = Database(config)
    app = Client(
        "hs2_telegram_bot",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        parse_mode=ParseMode.DISABLED,
        workers=16,
    )
    manager = TaskManager(app, config, db)
    register_handlers(app, manager, db, config)

    await app.start()
    await manager.start()
    me = await app.get_me()
    logger.info("Bot started as @%s | persistence=%s", me.username, db.backend)
    try:
        await idle()
    finally:
        await manager.stop()
        await app.stop()


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("Startup failed: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
