# Hotstar + SonyLIV Telegram Bot (starter build)

This ZIP contains a modular Telegram bot starter based on the supplied `hs2.py` CLI and implementation plan. It adds a Telegram command surface and isolated task workspaces rather than running the original interactive CLI inside a handler.

## Implemented in this build

- `/start`, `/help`, `/about`
- `/info URL` — analyze a direct URL without downloading
- `/download URL` and `/dl URL` — analyze and present inline choices for quality, audio-language preference when yt-dlp exposes it, and subtitle language
- `/status` and `/cancel TASK_ID`
- `/settings` and `/mysettings`
- Strict HTTPS host allowlisting for configured Hotstar/SonyLIV domains
- SQLite persistence by default, with MongoDB persistence when `MONGO_URI` is configured
- Per-user active task limits, a bounded worker count, queueing, task-state updates, and real subprocess termination for active yt-dlp downloads
- Task-specific working directories; successful output is cleaned up only after Telegram confirms the upload; failed output is retained by default
- Telegram uploads through Pyrogram/MTProto; no local `telegram-bot-api` binary is required
- Startup recovery marks interrupted tasks failed rather than pretending they resumed

## Important limitations

This is a **starter/MVP, not a claim that every source link works**. Hotstar and SonyLIV support depends on the installed yt-dlp version and its native extractors for the exact item. Live provider extraction was not possible in the build environment. The two provider classes share yt-dlp's normal extractor mechanism; SonyLIV has not been independently verified end-to-end.

This bot does not bypass DRM, paywalls, or access controls. Use only media and accounts you are authorized to access. Some URLs may fail because the extractor does not support them or the item is DRM-protected.

The following features from the original CLI / plan are **not yet wired into this bot**: TMDB metadata/date enrichment, the original filename-normalization engine, MKV cover embedding, Storage.to/Gofile/Buzzheavier uploads and cloud fallback, detailed FFprobe/MediaInfo captions, and durable resume of interrupted downloads. The existing `hs2.py` is not imported as a module because its main pipeline depends on terminal prompts and global filesystem behavior.

Audio selection is a preference, not a guarantee: only formats that expose language metadata can be selected, and yt-dlp may fall back when a preferred language-specific format is unavailable. Subtitle availability also depends on extractor metadata. The bot only downloads subtitles for a language selected in the UI.

## Requirements

- Python 3.10 or newer (Python 3.11 recommended)
- Telegram Bot API token from BotFather
- Telegram `API_ID` and `API_HASH` from https://my.telegram.org
- `ffmpeg` installed if yt-dlp needs to merge separate audio/video streams or embed subtitles
- Optional MongoDB Atlas/local MongoDB; SQLite works without a database server

## Install and run locally

1. Extract the ZIP and open a terminal in `hs2_telegram_bot/`.
2. Create a virtual environment:

   ```bash
   python -m venv .venv
   ```

   Activate it (`.venv\\Scripts\\activate` on Windows, or `source .venv/bin/activate` on macOS/Linux).
3. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and enter `API_ID`, `API_HASH`, and `BOT_TOKEN`.
5. Optional: set `MONGO_URI` for MongoDB. If omitted, state is written to `data/bot.sqlite3`.
6. Optional: set `DESTINATION_CHAT_ID` to send finished videos to a channel/group instead of the requesting user's private chat. The bot must have permission to post there.
7. Run:

   ```bash
   python main.py
   ```

The bot creates `data/` and `work/` as needed. Do not commit `.env`, cookies, Telegram session files, or generated media.

## Optional cookies

If a provider requires an authenticated session and you are allowed to access the media, set `COOKIE_FILE` to a cookies.txt file on the bot host. Do not send that file to the Telegram bot or commit it. Keep the cookie file private and rotate the session if it is exposed. Cookies do not make DRM-protected media downloadable.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | Telegram credentials | Required |
| `MONGO_URI` | Enable MongoDB persistence | Empty (SQLite) |
| `MONGO_DATABASE` | Mongo database name | `hs2_telegram_bot` |
| `SQLITE_PATH` | SQLite state file | `data/bot.sqlite3` |
| `DESTINATION_CHAT_ID` | Optional upload destination | User's private chat |
| `ALLOWED_USER_IDS` | Optional comma-separated Telegram user ID allowlist; blank means public access | Empty |
| `COOKIE_FILE` | Optional local yt-dlp cookies | Empty |
| `WORK_DIR` | Task workspace root | `work` |
| `MAX_WORKERS` | Concurrent worker count, clamped to 1–8 | `2` |
| `MAX_ACTIVE_TASKS_PER_USER` | User task limit | `1` |
| `MAX_UPLOAD_SIZE_GB` | Reject files larger than this before Telegram upload | `1.9` |
| `KEEP_FAILED_FILES` | Retain failed/cancelled task files | `true` |
| `HOTSTAR_DOMAINS` | Comma-separated Hotstar domains | `hotstar.com,jiohotstar.com` |
| `SONYLIV_DOMAINS` | Comma-separated SonyLIV domains | `sonyliv.com,sonyliv.in` |

Do not use wildcard domain strings. The validator accepts each configured hostname and its proper subdomains. If the bot is not intended for public use, set `ALLOWED_USER_IDS` before deploying it.

## Deployment notes

- Use a worker/service process, not a short-lived web dyno. `Procfile` is included for process types that support the Heroku-style worker syntax; verify current limits and persistent-storage behavior on your target host.
- A Heroku-style ephemeral filesystem can be wiped during restart/deploy. Use MongoDB for durable state and understand that interrupted media downloads are marked failed and must be submitted again.
- Do not run multiple bot instances using the same Pyrogram session name on a shared filesystem. For more than one worker dyno, move the queue to a distributed queue/lease model before scaling; this starter queue is process-local.
- Keep the Telegram token, Mongo URI, and any cookies in environment secrets.

## Tests

Run the local unit tests with:

```bash
python -m unittest discover -s tests -v
```

These tests validate URL host allowlisting and the local persistence adapter. They do not prove that external Hotstar/SonyLIV endpoints work; that requires live, authorized integration tests with your own account and current yt-dlp version.
