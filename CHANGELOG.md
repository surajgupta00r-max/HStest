# Changelog

## Extractor failure diagnostics fix

- Replaced the misleading generic analysis-failure message with a sanitized explanation based on the actual yt-dlp exception.
- Distinguishes explicit DRM errors from unsupported URLs, HTTP 403/404 responses, login/session requirements, regional restrictions, and network timeouts.
- Does not label generic errors or HTTP 403 responses as DRM by default.
- Redacts full URLs and common token/cookie fields from user-visible diagnostics and logs.
- Added regression tests for generic failures, explicit DRM, 403 errors, and URL redaction.

## Approved Phase 1 + Phase 2 upgrade

- Replaced the simple task list with paginated status views, filters, progress details, queue positions and inline cancellation.
- Separated download/media-processing workers from Telegram upload workers with independent concurrency settings.
- Improved cancellation and shutdown behavior; handled unchanged Telegram message edits.
- Added safer per-task workspace/retention and prevented deleting successful-source files before confirmed Telegram upload.
- Expanded categorized per-user settings for metadata, naming, Smart Caption and thumbnails.
- Integrated FFprobe/MediaInfo probing, corrected Matroska BPS parsing and unknown-language handling, and added configurable physical naming/Smart Caption V1/V2.
- Added optional TMDB enrichment, guarded thumbnail retrieval, screenshot fallback and optional MKV cover embedding.
- Added SQLite schema migration that preserves existing tasks and derives a useful stage label for legacy task states.
- Added database migration, pagination/filtering, media metadata, naming and codec/language regression tests.

## Explicitly deferred

- Gofile, Storage.to, Buzzheavier and cloud fallback.
- Operational health/statistics commands, distributed queues, and true task resume after restart.
- Live provider and Telegram upload validation.

## Final release validation

- Confirmed Python compileall, `unittest` discovery, and `pytest` on the upgraded source tree.
- Await in-flight analysis and stage tasks before closing the persistence backend on shutdown.
- Removed non-functional placeholder buttons from settings and made custom-thumbnail commands explicit in the settings text.
- Archive excludes `.env`, Python caches, test caches, session files, database data, and media workspaces.


## Per-user cookies via Telegram settings

- Added `/settings` → Cookies / Session with upload/replace and remove controls.
- Accepts private `.txt` documents in Netscape cookies format after an explicit upload prompt; upload prompt expires after 10 minutes.
- Enforces a 2 MB cap and stores only cookie rows for configured Hotstar/SonyLIV domains.
- Stores one file per Telegram user with owner-only POSIX permissions where supported; cookie values are never put in the database or bot logs.
- Applies a user's cookies to both yt-dlp metadata analysis and download; server-side `COOKIE_FILE` remains an optional fallback.
- Added tests for domain filtering, per-user isolation, file permissions, fallback, removal, invalid input and size limits.
