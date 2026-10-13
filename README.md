# Hotstar + SonyLIV Telegram Bot

A source-specific Telegram bot built around yt-dlp and Pyrogram. It is not a generic mirror/leech bot. The original `hs2.py` CLI is used as a source of media-processing behavior, but is not imported directly because it includes terminal prompts, global paths, and legacy CLI lifecycle behavior.

## Design boundary

This project borrows general task UX and media-workflow ideas from the supplied WZML-X reference; it does not import WZML-X modules or turn this project into a generic mirror/leech bot. The source remains focused on authorized Hotstar and SonyLIV workflows.

## What is implemented

### Commands and task UX

- `/start`, `/help`, `/about`
- `/info URL` — inspect one direct media URL without downloading
- `/download URL`, `/dl URL` — analyze media, choose quality/audio/subtitles, then enqueue
- `/status` — paginated task cards with All/Active/Failed/Completed filters, refresh, stage details, progress, speed/ETA when available, queue position, and inline cancellation
- `/cancel TASK_ID` — cancel a task owned by the requesting user
- `/settings`, `/mysettings` — categorized Video, Audio, Subtitles, Metadata & Naming, Thumbnail, Upload, and Cookies / Session settings
- `/setthumbnail HTTPS_URL`, `/clearthumbnail` — set or clear a per-user custom poster URL

### Queue and reliability

- Separate download and upload queues and independently configurable worker counts (`MAX_DOWNLOAD_WORKERS`, `MAX_UPLOAD_WORKERS`). `MAX_WORKERS` remains a backwards-compatible default for download workers.
- Task IDs, per-user task limits, queue positions, durable SQLite task/user state, optional MongoDB persistence, restart marking for unfinished tasks, and owner checks on callbacks.
- Task-specific workspaces. Successful workspaces are removed only after Telegram confirms upload; failed output is retained by default.
- yt-dlp download progress and Pyrogram upload progress are throttled before updating Telegram messages. Repeated callback presses that do not change the message are handled without a dispatcher traceback.
- URL validation requires HTTPS and matches configured provider hostnames or their proper subdomains. Custom thumbnail URLs are restricted to public HTTPS destinations, including redirect checks.

### Media processing restored from `hs2.py`

- FFprobe-based video/audio/subtitle probing, with optional MediaInfo supplements.
- Correct parsing of Matroska `BPS` tags as bits per second; unknown audio language remains `Unknown` rather than being guessed as Hindi.
- Optional TMDB title/year/genre/episode-air-date enrichment via `TMDB_API_KEY`, with source metadata fallback when missing or unavailable.
- Separate pre-normalization local filename, physical saved filename, and Smart Caption filename. The pre-normalization local filename is the filename yt-dlp wrote before this bot renamed the downloaded media; it is not guaranteed to match a provider's internal original filename.
- Smart Caption V1 (detailed per-audio-track metadata) and V2 (compact audio summary), plus detailed/simple physical naming styles.
- Thumbnail fallback based on user setting: custom image, TMDB image, source image, yt-dlp-generated source thumbnail, and/or screenshot fallback. Optional MKV cover embedding uses `mkvpropedit` when available and FFmpeg as a fallback.

## Diagnosing analysis failures

When link analysis fails, the task message now includes a sanitized yt-dlp diagnostic. Generic errors and HTTP 403 responses are not automatically called DRM. The detail can help identify an unsupported URL/extractor, an authentication or cookie requirement, region restriction, item-not-found response, or a network/provider problem. Full request URLs are redacted because they may contain temporary signatures or tokens. If a link still fails, share the task's displayed extractor detail (not your cookies, credentials, or the full signed URL) when asking for help.

## Limitations and verification status

This is still an integration starter, not a guarantee that every link is supported. Hotstar and SonyLIV extraction depends on the installed yt-dlp extractors and the account's authorized access. SonyLIV has not been independently validated end-to-end in a live run. This bot does not bypass DRM, paywalls, or access controls.

TMDB requires an optional API key; without it, source/extractor metadata remains available. FFprobe/FFmpeg are recommended system dependencies; MediaInfo and mkvpropedit are optional enhancements. If probing tools are missing, the pipeline attempts to continue with incomplete metadata. Thumbnail downloads can fail and should not block upload.

Phase 3 remains deferred: Gofile, Storage.to and Buzzheavier upload providers, cloud fallback, and owner-only operational diagnostics are not wired into this version. Durable resume is also not implemented. The queue is process-local, so do not run multiple bot instances against the same database/session and expect a shared queue.

## Requirements

- Python 3.10 or newer (Python 3.11 recommended)
- Telegram bot token from BotFather
- `API_ID` and `API_HASH` from https://my.telegram.org
- `ffmpeg`/`ffprobe` for merge/probing, screenshots and thumbnail resizing (recommended)
- `mkvpropedit` for fast MKV attachment edits (optional)
- `mediainfo` for supplementary technical metadata (optional)
- Optional MongoDB Atlas/local MongoDB; SQLite is sufficient for a single instance

## Install and run

1. Extract the ZIP and open a terminal in the `hs2_telegram_bot/` directory.
2. Create and activate an isolated environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate       # Linux/macOS/Cloud Shell
   # .venv\\Scripts\\activate    # Windows PowerShell
   ```

3. Install dependencies **after activating** the environment:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Create your environment file:

   ```bash
   cp .env.example .env
   ```

   Fill in `API_ID`, `API_HASH` and `BOT_TOKEN`. Optional TMDB, MongoDB, server-default cookie and custom-thumbnail settings are documented in `.env.example`. Personal cookie files can be uploaded through the bot settings.

5. Start the bot:

   ```bash
   python main.py
   ```

The bot writes its SQLite file under `data/` and task workspaces under `work/`. Keep `.env`, cookies, Telegram session files, and downloaded media out of source control.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | Telegram credentials | Required |
| `MONGO_URI` | Enable MongoDB persistence | Empty (SQLite) |
| `MONGO_DATABASE` | MongoDB database name | `hs2_telegram_bot` |
| `SQLITE_PATH` | SQLite state file | `data/bot.sqlite3` |
| `DESTINATION_CHAT_ID` | Optional Telegram upload destination | Requester's private chat |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs | Empty (public to users who can message it) |
| `COOKIE_FILE` | Optional server-side default yt-dlp cookies file; used when a user has not uploaded their own | Empty |
| `WORK_DIR` | Task workspace root | `work` |
| `MAX_DOWNLOAD_WORKERS` | Concurrent download/media-processing stages, 1–8 | `MAX_WORKERS` or `2` |
| `MAX_UPLOAD_WORKERS` | Concurrent Telegram uploads, 1–8 | `1` |
| `MAX_WORKERS` | Backwards-compatible default for download workers | `2` |
| `MAX_ACTIVE_TASKS_PER_USER` | Active task limit per user | `1` |
| `MAX_UPLOAD_SIZE_GB` | Maximum final upload size | `1.9` |
| `KEEP_FAILED_FILES` | Retain failed/cancelled output workspaces | `true` |
| `HOTSTAR_DOMAINS`, `SONYLIV_DOMAINS` | Source host allowlists | Documented in `.env.example` |
| `TMDB_API_KEY` | Optional title/year/genre/episode metadata and poster lookup | Empty |
| `CUSTOM_THUMBNAIL_URL` | Optional global fallback custom poster URL | Empty |
| `RIPPER_TAG` | Suffix added to detailed filenames | `HS2` |
| `SOURCE_LABEL` | Source quality tag in filenames | `WEB-DL` |
| `LOG_LEVEL` | Python logging level | `INFO` |

If the bot is not intended for public use, set `ALLOWED_USER_IDS` before deploying. Under `/settings` → `Cookies / Session`, press **Upload / Replace cookies.txt**, then send a `.txt` document exported in Netscape cookies format within 10 minutes. Files are limited to 2 MB, validated, and filtered so only cookie rows for configured Hotstar/SonyLIV domains are retained. Each Telegram user gets a separate file under `data/cookies/`; database records do not contain cookie values. Files use owner-only POSIX permissions when supported. Removing a user's uploaded file falls back to the optional server-default `COOKIE_FILE`. Keep the bot host private and protected: the host administrator can still access local files, and uploading the document passes it through Telegram. After the bot confirms saving, delete the original document message from the chat as an additional precaution. Only upload sessions you are authorized to use; cookies do not bypass DRM, subscriptions, or access controls.

## Deployment notes

- Run as a long-lived worker/service process. A local Telegram Bot API server is not required.
- Cloud hosts with ephemeral filesystems may lose SQLite/work files after restart; use MongoDB for durable database state but note that active downloads still need to be resubmitted after interruption.
- The queue is process-local. For multi-instance deployment, add a distributed queue/lease model before scaling horizontally.
- Do not commit secrets, tokens, database URLs, or cookies. The `data/` folder is ignored by Git; maintain persistent private storage if uploaded cookies should survive host restarts.

## Tests

Run:

```bash
python -m unittest discover -s tests -v
```

The test suite covers strict source URL validation, per-user cookie filtering/storage/isolation and analysis selection, SQLite settings/task persistence, interruption marking, media probing normalization, bitrate units, unknown language handling, Smart Caption V1/V2, safe filenames, and preservation of original-versus-physical filenames. These are unit tests; they do not replace a live authorized Hotstar/SonyLIV extraction test, a TMDB API test, or a Telegram upload test.
