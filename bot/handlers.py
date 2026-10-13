from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .cookies import CookieValidationError, MAX_COOKIE_FILE_BYTES
from .media import is_public_https_url
from .source import SourceError, detect_source

LOG = logging.getLogger(__name__)

SETTING_LANGUAGE_CODES = {
    "best", "en", "hi", "ta", "te", "ml", "kn", "mr", "bn", "gu", "pa", "or", "as",
}
SETTING_QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
CATEGORY_NAMES = {
    "video": "Video settings",
    "audio": "Audio settings",
    "subtitles": "Subtitle settings",
    "metadata": "Metadata & naming",
    "thumbnail": "Thumbnail settings",
    "upload": "Upload settings",
    "cookies": "Cookie / session settings",
}


def _argument(message) -> str:
    text = message.text or message.caption or ""
    pieces = text.split(maxsplit=1)
    return pieces[1].strip() if len(pieces) > 1 else ""


def _duration(value) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "Unknown"
    hours, rem = divmod(int(value), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _bool_text(value: bool) -> str:
    return "On" if value else "Off"


def _settings_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Video", callback_data="cfg:cat:video"), InlineKeyboardButton("Audio", callback_data="cfg:cat:audio")],
        [InlineKeyboardButton("Subtitles", callback_data="cfg:cat:subtitles"), InlineKeyboardButton("Metadata & Naming", callback_data="cfg:cat:metadata")],
        [InlineKeyboardButton("Thumbnail", callback_data="cfg:cat:thumbnail"), InlineKeyboardButton("Upload", callback_data="cfg:cat:upload")],
        [InlineKeyboardButton("Cookies / Session", callback_data="cfg:cat:cookies")],
    ])


def _settings_category_keyboard(settings: dict, category: str, has_user_cookies: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if category == "video":
        values = [("best", "Best"), ("2160", "2160p"), ("1440", "1440p"), ("1080", "1080p"), ("720", "720p"), ("480", "480p"), ("360", "360p")]
        rows = [[InlineKeyboardButton(("✓ " if settings.get("quality", "best") == v else "") + label, callback_data=f"cfg:set:quality:{v}") for v, label in values[i:i+3]] for i in range(0, len(values), 3)]
    elif category == "audio":
        values = [("best", "Automatic"), ("en", "English"), ("hi", "Hindi"), ("ta", "Tamil"), ("te", "Telugu"), ("ml", "Malayalam"), ("kn", "Kannada"), ("mr", "Marathi"), ("bn", "Bengali")]
        rows = [[InlineKeyboardButton(("✓ " if settings.get("audio_language", "best") == v else "") + label, callback_data=f"cfg:set:audio_language:{v}") for v, label in values[i:i+3]] for i in range(0, len(values), 3)]
    elif category == "subtitles":
        rows.append([
            InlineKeyboardButton(("✓ " if not settings.get("subtitles_enabled") else "") + "Off", callback_data="cfg:set:subtitles_enabled:off"),
            InlineKeyboardButton(("✓ " if settings.get("subtitles_enabled") and settings.get("subtitle_language") == "en" else "") + "English", callback_data="cfg:set:subtitle_language:en"),
        ])
        values = [("hi", "Hindi"), ("ta", "Tamil"), ("te", "Telugu"), ("ml", "Malayalam"), ("kn", "Kannada"), ("mr", "Marathi"), ("bn", "Bengali")]
        rows.extend([[InlineKeyboardButton(("✓ " if settings.get("subtitles_enabled") and settings.get("subtitle_language") == v else "") + label, callback_data=f"cfg:set:subtitle_language:{v}") for v, label in values[i:i+3]] for i in range(0, len(values), 3)])
    elif category == "metadata":
        providers = [("tmdb", "TMDB"), ("source", "Source only"), ("none", "None")]
        rows.append([InlineKeyboardButton(("✓ " if settings.get("metadata_provider", "tmdb") == v else "") + label, callback_data=f"cfg:set:metadata_provider:{v}") for v, label in providers])
        rows.append([InlineKeyboardButton(f"Episode air dates: {_bool_text(bool(settings.get('episode_dates', True)))}", callback_data=f"cfg:set:episode_dates:{'off' if settings.get('episode_dates', True) else 'on'}")])
        rows.append([InlineKeyboardButton(f"Filename style: {settings.get('naming_style', 'detailed').title()}", callback_data=f"cfg:set:naming_style:{'simple' if settings.get('naming_style', 'detailed') == 'detailed' else 'detailed'}")])
        rows.append([InlineKeyboardButton(f"Smart Caption: {str(settings.get('smart_caption_version', 'v2')).upper()}", callback_data=f"cfg:set:smart_caption_version:{'v1' if settings.get('smart_caption_version', 'v2') == 'v2' else 'v2'}")])
    elif category == "thumbnail":
        modes = [("auto", "Auto"), ("tmdb", "TMDB"), ("source", "Source"), ("custom", "Custom"), ("screenshot", "Screenshot")]
        rows.extend([[InlineKeyboardButton(("✓ " if settings.get("thumbnail_mode", "auto") == v else "") + label, callback_data=f"cfg:set:thumbnail_mode:{v}") for v, label in modes[i:i+3]] for i in range(0, len(modes), 3)])
        rows.append([InlineKeyboardButton(f"Embed cover in MKV: {_bool_text(bool(settings.get('embed_thumbnail', False)))}", callback_data=f"cfg:set:embed_thumbnail:{'off' if settings.get('embed_thumbnail', False) else 'on'}")])
    elif category == "upload":
        rows = []
    elif category == "cookies":
        rows.append([InlineKeyboardButton("Upload / Replace cookies.txt", callback_data="cfg:cookies:upload")])
        if has_user_cookies:
            rows.append([InlineKeyboardButton("Remove my uploaded cookies", callback_data="cfg:cookies:remove")])
    rows.append([InlineKeyboardButton("‹ Settings", callback_data="cfg:home")])
    return InlineKeyboardMarkup(rows)


def _settings_text(settings: dict, category: str | None = None, config=None, cookie_status: str = "Not configured") -> str:
    if category is None:
        custom_set = bool(settings.get("custom_thumbnail_url") or getattr(config, "custom_thumbnail_url", ""))
        quality = "Best available" if settings.get("quality", "best") == "best" else f"Up to {settings.get('quality')}p"
        audio = str(settings.get("audio_language", "best"))
        subtitles = "Off" if not settings.get("subtitles_enabled") else str(settings.get("subtitle_language", "en")).upper()
        return (
            "Your settings\n\n"
            f"Video quality: {quality}\n"
            f"Audio default: {audio.upper() if audio != 'best' else 'Automatic'}\n"
            f"Subtitles: {subtitles}\n"
            f"Metadata: {settings.get('metadata_provider', 'tmdb').upper()}\n"
            f"Episode dates: {_bool_text(bool(settings.get('episode_dates', True)))}\n"
            f"Filename style: {settings.get('naming_style', 'detailed').title()}\n"
            f"Smart Caption: {str(settings.get('smart_caption_version', 'v2')).upper()}\n"
            f"Thumbnail: {settings.get('thumbnail_mode', 'auto').title()} (custom {'set' if custom_set else 'not set'})\n"
            f"MKV cover embedding: {_bool_text(bool(settings.get('embed_thumbnail', False)))}\n"
            f"Cookies: {cookie_status}\n\n"
            "Choose a category to adjust defaults. Per-download choices can still override quality, audio and subtitles."
        )
    if category == "video":
        value = settings.get("quality", "best")
        current = "Best available" if value == "best" else f"Up to {value}p"
        return f"Video settings\n\nDefault quality: {current}\nApplied to new tasks unless changed before download."
    if category == "audio":
        value = settings.get("audio_language", "best")
        return f"Audio settings\n\nPreferred language: {value.upper() if value != 'best' else 'Automatic'}\n\nIf the source does not expose this language, the task falls back to automatic audio selection."
    if category == "subtitles":
        value = str(settings.get("subtitle_language", "en")).upper() if settings.get("subtitles_enabled") else "Off"
        return f"Subtitle settings\n\nDefault subtitles: {value}\nSubtitle availability is determined by the provider extractor."
    if category == "metadata":
        return (
            "Metadata & naming\n\n"
            f"Metadata provider: {settings.get('metadata_provider', 'tmdb').upper()}\n"
            f"Episode air dates: {_bool_text(bool(settings.get('episode_dates', True)))}\n"
            f"Filename style: {settings.get('naming_style', 'detailed').title()}\n"
            f"Smart Caption format: {str(settings.get('smart_caption_version', 'v2')).upper()}\n\n"
            "TMDB enrichment requires TMDB_API_KEY. If unavailable, the bot gracefully uses extractor metadata."
        )
    if category == "thumbnail":
        custom_set = bool(settings.get("custom_thumbnail_url") or getattr(config, "custom_thumbnail_url", ""))
        return (
            "Thumbnail settings\n\n"
            f"Mode: {settings.get('thumbnail_mode', 'auto').title()}\n"
            f"Custom image URL: {'Configured' if custom_set else 'Not configured'}\n"
            f"Embed cover in MKV: {_bool_text(bool(settings.get('embed_thumbnail', False)))}\n\n"
            "Auto priority: custom image → TMDB → source image → generated screenshot. "
            "Use /setthumbnail URL or /clearthumbnail to manage your custom image. "
            "MKV cover embedding requires mkvpropedit."
        )
    if category == "cookies":
        return (
            "Cookies / session\n\n"
            f"Active cookie source: {cookie_status}\n\n"
            "Upload a cookies.txt file exported in Netscape format using the button below. "
            "The bot keeps a separate file for your Telegram account, filters entries to the configured Hotstar/SonyLIV domains, "
            "and never displays or forwards its contents. Files are stored on the bot host with owner-only file permissions.\n\n"
            "Only upload cookies for an account you are authorized to use. Cookies are sensitive login credentials; "
            "do not send them in groups or share them with anyone. They do not bypass DRM or subscription restrictions."
        )
    return (
        "Upload settings\n\nTelegram upload is currently the active provider. "
        "Separate download and upload worker limits can be set in the environment. "
        "Additional cloud upload providers are planned for Phase 3."
    )


def register_handlers(app, manager, db, config) -> None:
    private = filters.private
    cookie_store = manager.sources.cookie_store
    pending_cookie_upload: dict[int, float] = {}
    pending_cookie_upload_ttl = 10 * 60

    def cookie_status(user_id: int) -> str:
        return cookie_store.status_for_user(user_id)

    def is_allowed(user) -> bool:
        return bool(user) and (not config.allowed_user_ids or int(user.id) in config.allowed_user_ids)

    async def reject_if_not_allowed(message) -> bool:
        if not message.from_user:
            return True
        if not is_allowed(message.from_user):
            await message.reply_text("You are not authorized to use this bot.")
            return True
        return False

    @app.on_message(private & filters.command("start"))
    async def start_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        db.register_user(int(message.from_user.id), message.from_user.username)
        await message.reply_text(
            "Hotstar / SonyLIV Media Bot\n\n"
            "Use /info URL to inspect a supported media link.\n"
            "Use /download URL or /dl URL to choose quality, audio preference and subtitles.\n"
            "Use /status for interactive task cards and /settings for configurable defaults.\n\n"
            "Only use links and account sessions you are authorized to access. DRM and access controls are not bypassed."
        )

    @app.on_message(private & filters.command("help"))
    async def help_command(client, message):
        if await reject_if_not_allowed(message):
            return
        await message.reply_text(
            "Commands\n"
            "/start — register and show the menu\n"
            "/help — show commands\n"
            "/about — bot information and limitations\n"
            "/info URL — inspect media without downloading\n"
            "/download URL — choose options and queue a download\n"
            "/dl URL — alias for /download\n"
            "/status — paginated task cards, filters and cancellation\n"
            "/cancel TASK_ID — cancel your task\n"
            "/settings — configure defaults, including Cookies / Session upload and removal\n"
            "/mysettings — display your defaults\n"
            "/setthumbnail URL — set a custom HTTPS thumbnail\n"
            "/clearthumbnail — clear your custom thumbnail\n\n"
            "Playlists are rejected. Availability depends on yt-dlp's native extractor and access permissions. DRM-protected items are not supported."
        )

    @app.on_message(private & filters.command("about"))
    async def about_command(client, message):
        if await reject_if_not_allowed(message):
            return
        await message.reply_text(
            "Hotstar / SonyLIV Telegram Bot\n"
            "A task-based media workflow with interactive task management, probing, metadata enrichment, captions and thumbnail handling.\n\n"
            "The providers use yt-dlp's normal extraction capabilities; support is not guaranteed for every link. "
            "This bot does not bypass DRM, paywalls, or access controls. Telegram is the implemented upload provider; cloud providers remain deferred."
        )

    @app.on_message(private & filters.command("info"))
    async def info_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        url = _argument(message)
        if not url:
            await message.reply_text("Usage: /info https://supported-provider.example/media-link")
            return
        db.register_user(int(message.from_user.id), message.from_user.username)
        try:
            source = detect_source(url, config)
            status = await message.reply_text(f"Analyzing {source.name} link…")
            info = await manager.sources.analyze(url, user_id=int(message.from_user.id))
            heights = ", ".join(f"{h}p" for h in info.get("heights", [])[:8]) or "Not reported"
            audios = ", ".join(info.get("audio_languages", [])[:12]) or "Not reported by extractor"
            subs = ", ".join(info.get("subtitle_languages", [])[:12]) or "None reported"
            episode = ""
            if info.get("season_number") is not None or info.get("episode_number") is not None:
                episode = f"\nSeason/Episode: S{int(info.get('season_number') or 0):02d}E{int(info.get('episode_number') or 0):02d}"
            await status.edit_text(
                f"Media information\n\nTitle: {info.get('title', 'Unknown')}\n"
                f"Series: {info.get('series') or '—'}{episode}\n"
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
        if await reject_if_not_allowed(message):
            return
        url = _argument(message)
        if not url:
            await message.reply_text("Usage: /download https://supported-provider.example/media-link")
            return
        await manager.prepare_download(message, url)

    @app.on_message(private & filters.command("status"))
    async def status_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        db.register_user(int(message.from_user.id), message.from_user.username)
        text, keyboard = manager.render_status_view(int(message.from_user.id))
        await message.reply_text(text, reply_markup=keyboard)

    @app.on_message(private & filters.command("cancel"))
    async def cancel_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        argument = _argument(message)
        task_id = argument.split()[0].upper() if argument else ""
        if not task_id:
            await message.reply_text("Usage: /cancel TASK_ID")
            return
        cancelled = await manager.cancel_task(task_id, int(message.from_user.id))
        await message.reply_text(f"Cancellation requested for task {task_id}." if cancelled else "Task not found, not owned by you, or already finished.")

    @app.on_message(private & filters.command("settings"))
    async def settings_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        await message.reply_text(
            _settings_text(db.get_settings(user_id), config=config, cookie_status=cookie_status(user_id)),
            reply_markup=_settings_home_keyboard(),
        )

    @app.on_message(private & filters.command("mysettings"))
    async def mysettings_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        await message.reply_text(_settings_text(db.get_settings(user_id), config=config, cookie_status=cookie_status(user_id)))

    @app.on_message(private & filters.command("setthumbnail"))
    async def set_thumbnail_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        url = _argument(message)
        if not url:
            await message.reply_text("Usage: /setthumbnail https://example.com/poster.jpg")
            return
        if not await asyncio.to_thread(is_public_https_url, url):
            await message.reply_text("Use a valid public HTTPS image URL. Local/private network addresses are not accepted.")
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        db.update_setting(user_id, "custom_thumbnail_url", url)
        db.update_setting(user_id, "thumbnail_mode", "custom")
        await message.reply_text("Custom thumbnail saved for new tasks. Use /settings → Thumbnail to change the mode.")

    @app.on_message(private & filters.command("clearthumbnail"))
    async def clear_thumbnail_command(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        user_id = int(message.from_user.id)
        db.register_user(user_id, message.from_user.username)
        db.update_setting(user_id, "custom_thumbnail_url", "")
        if db.get_settings(user_id).get("thumbnail_mode") == "custom":
            db.update_setting(user_id, "thumbnail_mode", "auto")
        await message.reply_text("Custom thumbnail cleared.")

    @app.on_message(private & filters.document)
    async def cookie_document_handler(client, message):
        if await reject_if_not_allowed(message) or not message.from_user:
            return
        user_id = int(message.from_user.id)
        expires_at = pending_cookie_upload.get(user_id, 0.0)
        if expires_at <= time.monotonic():
            pending_cookie_upload.pop(user_id, None)
            await message.reply_text("To upload cookies, open /settings → Cookies / Session → Upload / Replace first.")
            return

        document = message.document
        filename = Path(document.file_name or "").name
        if not filename.lower().endswith(".txt"):
            await message.reply_text("Please upload a .txt cookies file exported in Netscape cookies.txt format. Your upload request is still active.")
            return
        if document.file_size is not None and document.file_size > MAX_COOKIE_FILE_BYTES:
            await message.reply_text("Cookie files must be 2 MB or smaller. Your upload request is still active.")
            return

        # Use a random temporary path; ignore the user-provided filename for filesystem paths.
        cookie_store.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".incoming-cookies-", suffix=".txt", dir=cookie_store.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            downloaded = await client.download_media(message, file_name=str(temp_path))
            if not downloaded:
                await message.reply_text("Telegram could not download that file. Please try again.")
                return
            actual_path = Path(downloaded)
            if not actual_path.is_file() or actual_path.stat().st_size > MAX_COOKIE_FILE_BYTES:
                await message.reply_text("Cookie files must be readable and 2 MB or smaller. Your upload request is still active.")
                return
            try:
                count = await asyncio.to_thread(cookie_store.save_file, user_id, actual_path)
            except CookieValidationError as exc:
                await message.reply_text(f"Could not save this cookies file. {exc}")
                return
            pending_cookie_upload.pop(user_id, None)
            await message.reply_text(
                f"Cookies saved for your account. Stored {count} relevant cookie entr{'y' if count == 1 else 'ies'} for supported Hotstar/SonyLIV domains.\n\n"
                "The file was not forwarded and its contents will not be displayed. It will be used for your own link analysis and downloads. "
                "For extra safety, delete the original uploaded document from this chat.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cookies / Session settings", callback_data="cfg:cat:cookies")]]),
            )
        except Exception as exc:
            LOG.warning("Cookie upload failed for user %s (%s)", user_id, type(exc).__name__)
            await message.reply_text("Could not process the cookies file. Please retry from /settings → Cookies / Session.")
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            # Pyrogram may return a normalized path; clean that too if it differs.
            try:
                if 'downloaded' in locals() and downloaded and Path(downloaded) != temp_path:
                    Path(downloaded).unlink(missing_ok=True)
            except OSError:
                pass

    @app.on_callback_query(filters.regex(r"^(d:|cfg:|st:)"))
    async def callback_router(client, callback):
        if callback.data.startswith("d:"):
            await manager.handle_download_callback(callback)
            return
        if callback.data.startswith("st:"):
            await manager.handle_status_callback(callback)
            return
        parts = callback.data.split(":")
        if not callback.from_user or not is_allowed(callback.from_user):
            await callback.answer("You are not authorized to use this bot.", show_alert=True)
            return
        user_id = int(callback.from_user.id)
        db.register_user(user_id, callback.from_user.username)
        settings = db.get_settings(user_id)
        active_cookie_status = cookie_status(user_id)
        has_user_cookies = cookie_store.has_user_cookie(user_id)
        if len(parts) == 3 and parts[1] == "cat" and parts[2] in CATEGORY_NAMES:
            category = parts[2]
            if category == "cookies":
                pending_cookie_upload.pop(user_id, None)
            try:
                await callback.message.edit_text(
                    _settings_text(settings, category, config, active_cookie_status),
                    reply_markup=_settings_category_keyboard(settings, category, has_user_cookies),
                )
            except MessageNotModified:
                pass
            await callback.answer()
            return
        if parts == ["cfg", "home"]:
            try:
                await callback.message.edit_text(
                    _settings_text(settings, config=config, cookie_status=active_cookie_status),
                    reply_markup=_settings_home_keyboard(),
                )
            except MessageNotModified:
                pass
            await callback.answer()
            return
        if parts == ["cfg", "cookies", "upload"]:
            pending_cookie_upload[user_id] = time.monotonic() + pending_cookie_upload_ttl
            try:
                await callback.message.edit_text(
                    "Upload cookies.txt\n\nNow send your Netscape-format cookies file as a document in this private chat. "
                    "The upload request expires in 10 minutes. Only relevant Hotstar/SonyLIV cookie entries are retained.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel upload", callback_data="cfg:cookies:cancel")],
                                                       [InlineKeyboardButton("‹ Cookies settings", callback_data="cfg:cat:cookies")]]),
                )
            except MessageNotModified:
                pass
            await callback.answer("Send the cookies.txt file in this private chat.")
            return
        if parts == ["cfg", "cookies", "cancel"]:
            pending_cookie_upload.pop(user_id, None)
            settings = db.get_settings(user_id)
            try:
                await callback.message.edit_text(
                    _settings_text(settings, "cookies", config, cookie_status(user_id)),
                    reply_markup=_settings_category_keyboard(settings, "cookies", cookie_store.has_user_cookie(user_id)),
                )
            except MessageNotModified:
                pass
            await callback.answer("Cookie upload cancelled.")
            return
        if parts == ["cfg", "cookies", "remove"]:
            pending_cookie_upload.pop(user_id, None)
            removed = cookie_store.remove_user_cookie(user_id)
            settings = db.get_settings(user_id)
            try:
                await callback.message.edit_text(
                    _settings_text(settings, "cookies", config, cookie_status(user_id)),
                    reply_markup=_settings_category_keyboard(settings, "cookies", cookie_store.has_user_cookie(user_id)),
                )
            except MessageNotModified:
                pass
            await callback.answer("Your uploaded cookies were removed." if removed else "No uploaded cookies were found.")
            return
        if len(parts) == 4 and parts[1] == "set":
            _, _, key, value = parts
            valid = False
            if key == "quality" and value in SETTING_QUALITIES:
                db.update_setting(user_id, key, value); valid = True
            elif key == "audio_language" and value in SETTING_LANGUAGE_CODES:
                db.update_setting(user_id, key, value); valid = True
            elif key == "subtitles_enabled" and value in {"on", "off"}:
                db.update_setting(user_id, key, value == "on"); valid = True
            elif key == "subtitle_language" and value in (SETTING_LANGUAGE_CODES - {"best"}):
                db.update_setting(user_id, "subtitle_language", value)
                db.update_setting(user_id, "subtitles_enabled", True)
                valid = True
            elif key == "metadata_provider" and value in {"tmdb", "source", "none"}:
                db.update_setting(user_id, key, value); valid = True
            elif key in {"episode_dates", "embed_thumbnail"} and value in {"on", "off"}:
                db.update_setting(user_id, key, value == "on"); valid = True
            elif key == "naming_style" and value in {"detailed", "simple"}:
                db.update_setting(user_id, key, value); valid = True
            elif key == "smart_caption_version" and value in {"v1", "v2"}:
                db.update_setting(user_id, key, value); valid = True
            elif key == "thumbnail_mode" and value in {"auto", "tmdb", "source", "custom", "screenshot"}:
                db.update_setting(user_id, key, value); valid = True
            if not valid:
                await callback.answer("Unsupported setting.", show_alert=True)
                return
            settings = db.get_settings(user_id)
            # Re-render the relevant section after an update.
            category = {"quality": "video", "audio_language": "audio", "subtitles_enabled": "subtitles",
                        "subtitle_language": "subtitles", "metadata_provider": "metadata", "episode_dates": "metadata",
                        "naming_style": "metadata", "smart_caption_version": "metadata",
                        "thumbnail_mode": "thumbnail", "embed_thumbnail": "thumbnail"}.get(key, "")
            try:
                await callback.message.edit_text(
                    _settings_text(settings, category, config, cookie_status(user_id)),
                    reply_markup=_settings_category_keyboard(settings, category, cookie_store.has_user_cookie(user_id)),
                )
            except MessageNotModified:
                await callback.answer("That setting is already selected.")
            else:
                await callback.answer("Default updated.")
            return
        await callback.answer("Invalid settings action.", show_alert=True)
