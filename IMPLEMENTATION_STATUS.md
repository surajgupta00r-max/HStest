# Implementation status

## Included and implemented

- Telegram bot bootstrapping via Pyrogram; no local Telegram Bot API server requirement.
- The command surface: `/start`, `/help`, `/about`, `/info`, `/download`, `/dl`, `/status`, `/cancel`, `/settings`, `/mysettings`.
- Configurable strict HTTPS host validation for Hotstar/SonyLIV domains.
- yt-dlp info extraction and a subprocess download workflow, isolated by task ID.
- Inline per-task quality/audio-language preference/subtitle selection when extractor metadata supports those choices.
- Telegram video upload, progress updates, per-user task limit, worker queue, task ownership checks, task cancellation, and startup interruption marking.
- SQLite default persistence and optional MongoDB persistence.
- Tests for host allowlisting, lookalike-host rejection, SQLite settings/tasks, and startup recovery semantics.

## Deliberately not claimed as finished

- SonyLIV extraction has not been integration-tested against real, authorized media. It uses the installed yt-dlp extractor like Hotstar.
- No DRM circumvention or access-control bypass.
- Original CLI's full metadata probing, TMDB enrichment, episode-date filenames, detailed audio stream normalization, MKV cover-art attachment, custom thumbnail fallback, and cloud upload providers are not yet connected to bot tasks.
- Queue state is process-local; MongoDB stores task records but does not itself provide a distributed job queue. Do not scale to multiple bot instances without a distributed queue/worker-lease design.
- Interrupted downloads are marked failed and must be resubmitted; they do not resume from persisted state.
- No live endpoint or Telegram upload integration test was possible in this environment.

## Before public deployment

1. Fill `.env` with real credentials; keep `.env` and cookies out of source control.
2. Set `ALLOWED_USER_IDS` to an allowlist unless the bot is intentionally public.
3. Test one authorized Hotstar item, then separately test SonyLIV with the current yt-dlp version.
4. Configure a durable database and inspect host filesystem limits.
5. Add operational monitoring, queue bounds, rate limits, and resource quotas appropriate to the target host.
