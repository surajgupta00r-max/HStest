from __future__ import annotations

import logging

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .source import SourceError, detect_source

LOG = logging.getLogger(__name__)


def _argument(message) -> str:
    text = message.text or message.caption or ""
    pieces = text.split(maxsplit=1)
    return pieces[1].strip() if len(pieces) > 1 else ""


def _duration(value) -> str:
    if not isinstance(value, int) or value < 0:
        return "Unknown"
    hours, rem = divmod(value, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Quality: Best", callback_data="cfg:q:best"),
            InlineKeyboardButton("1080p", callback_data="cfg:q:1080"),
            InlineKeyboardButton("720p", callback_data="cfg:q:720"),
        ],
        [
            InlineKeyboardButton("480p", callback_data="cfg:q:480"),
            InlineKeyboardButton("Quality: 360p", callback_data="cfg:q:360"),
        ],
        [
            InlineKeyboardButton("Subs: Off", callback_data="cfg:subs:off"),
            InlineKeyboardButton("Subs: English", callback_data="cfg:subs:en"),
        ],
        [
            InlineKeyboardButton("Subs: Hindi", callback_data="cfg:subs:hi"),
            InlineKeyboardButton("Subs: Tamil", callback_data="cfg:subs:ta"),
            InlineKeyboardButton("Subs: Telugu", callback_data="cfg:subs:te"),
        ],
        [InlineKeyboardButton("Audio: Automatic", callback_data="cfg:a:best")],
    ])


def _settings_text(settings: dict) -> str:
    quality = "Best available" if settings.get("quality", "best") == "best" else f"Up to {settings.get('quality')}p"
    subtitles = "Off"
    if settings.get("subtitles_enabled"):
        subtitles = str(settings.get("subtitle_language", "en")).upper()
    audio = str(settings.get("audio_language", "best"))
    return (
        "Your settings\n\n"
        f"Default video quality: {quality}\n"
        f"Default audio preference: {audio.upper() if audio != 'best' else 'Automatic'}\n"
        f"Subtitles: {subtitles}\n\n"
        "These are defaults. You can change the choices for each download before starting it."
    )


def register_handlers(app, manager, db, config) -> None:
    private = filters.private

    @app.on_message(private & filters.command("start"))
    async def start_command(client, message):
        if not message.from_user:
            return
        if config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        db.register_user(int(message.from_user.id), message.from_user.username)
        await message.reply_text(
            "Hotstar / SonyLIV Media Bot\n\n"
            "Use /info URL to inspect a supported media link.\n"
            "Use /download URL or /dl URL to choose quality, audio preference and subtitles.\n\n"
            "Only use links and account sessions you are authorized to access. DRM and access controls are not bypassed."
        )

    @app.on_message(private & filters.command("help"))
    async def help_command(client, message):
        await message.reply_text(
            "Commands\n"
            "/start — register and show the menu\n"
            "/help — show commands\n"
            "/about — bot information and limitations\n"
            "/info URL — inspect media without downloading\n"
            "/download URL — choose options and queue a download\n"
            "/dl URL — alias for /download\n"
            "/status — show your recent tasks\n"
            "/cancel TASK_ID — cancel your task\n"
            "/settings — configure your default choices\n"
            "/mysettings — display your defaults\n\n"
            "Playlists are rejected. Availability depends on yt-dlp's native extractor and access permissions. DRM-protected items are not supported."
        )

    @app.on_message(private & filters.command("about"))
    async def about_command(client, message):
        await message.reply_text(
            "Hotstar / SonyLIV Telegram Bot\n"
            "A task-based wrapper around yt-dlp and Telegram uploads.\n\n"
            "The provider adapters use yt-dlp's normal extraction capabilities; support is not guaranteed for every link. This bot does not bypass DRM, paywalls, or access controls. The current build uploads completed files to Telegram; cloud upload providers from the original CLI are not yet wired into this bot."
        )

    @app.on_message(private & filters.command("info"))
    async def info_command(client, message):
        if not message.from_user:
            return
        if config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        url = _argument(message)
        if not url:
            await message.reply_text("Usage: /info https://supported-provider.example/media-link")
            return
        db.register_user(int(message.from_user.id), message.from_user.username)
        try:
            source = detect_source(url, config)
            status = await message.reply_text(f"Analyzing {source.name} link…")
            info = await manager.sources.analyze(url)
            heights = ", ".join(f"{h}p" for h in info.get("heights", [])[:8]) or "Not reported"
            audios = ", ".join(info.get("audio_languages", [])[:12]) or "Not reported by extractor"
            subs = ", ".join(info.get("subtitle_languages", [])[:12]) or "None reported"
            await status.edit_text(
                f"Media information\n\nTitle: {info.get('title', 'Unknown')}\n"
                f"Source: {info.get('source', source.name)}\n"
                f"Duration: {_duration(info.get('duration'))}\n"
                f"Video qualities: {heights}\nAudio language hints: {audios}\nSubtitles: {subs}\n\n"
                "To download, submit /download URL. The available selections depend on metadata reported by yt-dlp."
            )
        except SourceError as exc:
            await message.reply_text(str(exc))
        except Exception as exc:
            LOG.warning("Info analysis failed (%s)", type(exc).__name__)
            await message.reply_text(
                "Could not inspect this link. The installed yt-dlp extractor may not support it, or the item may require an access method that this bot does not support."
            )

    @app.on_message(private & filters.command(["download", "dl"]))
    async def download_command(client, message):
        if message.from_user and config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        url = _argument(message)
        if not url:
            await message.reply_text("Usage: /download https://supported-provider.example/media-link")
            return
        await manager.prepare_download(message, url)

    @app.on_message(private & filters.command("status"))
    async def status_command(client, message):
        if message.from_user and config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        if message.from_user:
            db.register_user(int(message.from_user.id), message.from_user.username)
            await message.reply_text(manager.render_status(int(message.from_user.id)))

    @app.on_message(private & filters.command("cancel"))
    async def cancel_command(client, message):
        if not message.from_user:
            return
        if config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        task_id = _argument(message).split()[0].upper() if _argument(message) else ""
        if not task_id:
            await message.reply_text("Usage: /cancel TASK_ID")
            return
        cancelled = await manager.cancel_task(task_id, int(message.from_user.id))
        if cancelled:
            await message.reply_text(f"Cancellation requested for task {task_id}.")
        else:
            await message.reply_text("Task not found, not owned by you, or already finished.")

    @app.on_message(private & filters.command("settings"))
    async def settings_command(client, message):
        if not message.from_user:
            return
        if config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        await message.reply_text(_settings_text(db.get_settings(user_id)), reply_markup=_settings_keyboard())

    @app.on_message(private & filters.command("mysettings"))
    async def mysettings_command(client, message):
        if not message.from_user:
            return
        if config.allowed_user_ids and int(message.from_user.id) not in config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        await message.reply_text(_settings_text(db.get_settings(user_id)))

    @app.on_callback_query(filters.regex(r"^(d:|cfg:)"))
    async def callback_router(client, callback):
        if callback.data.startswith("d:"):
            await manager.handle_download_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) != 3 or not callback.from_user:
            await callback.answer("Invalid setting.", show_alert=True)
            return
        _, key, value = parts
        user_id = int(callback.from_user.id)
        if config.allowed_user_ids and user_id not in config.allowed_user_ids:
            await callback.answer("You are not authorized to use this bot.", show_alert=True)
            return
        db.register_user(user_id, callback.from_user.username)
        if key == "q" and value in {"best", "2160", "1440", "1080", "720", "480", "360"}:
            db.update_setting(user_id, "quality", value)
        elif key == "a" and value == "best":
            db.update_setting(user_id, "audio_language", "best")
        elif key == "subs":
            if value == "off":
                db.update_setting(user_id, "subtitles_enabled", False)
            elif value in {"en", "hi", "ta", "te"}:
                db.update_setting(user_id, "subtitles_enabled", True)
                db.update_setting(user_id, "subtitle_language", value)
            else:
                await callback.answer("Unsupported subtitle setting.", show_alert=True)
                return
        else:
            await callback.answer("Unsupported setting.", show_alert=True)
            return
        await callback.message.edit_text(_settings_text(db.get_settings(user_id)), reply_markup=_settings_keyboard())
        await callback.answer("Default updated.")
