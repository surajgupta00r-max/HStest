# Implementation status — phases 1 and 2

## Phase 1: task UX and reliability

- [x] Paginated `/status` cards with All/Active/Failed/Completed filters, refresh and previous/next navigation.
- [x] Task details include state, stage, progress bar, elapsed time, speed/ETA and size/queue details when available.
- [x] Inline cancellation on task/status cards; task owner checks apply to buttons and `/cancel`.
- [x] Separate download and upload worker queues with independent concurrency configuration.
- [x] Queue position based on the current process-local queue order.
- [x] Categorized settings: Video, Audio, Subtitles, Metadata & Naming, Thumbnail, Upload, and Cookies / Session.
- [x] Per-user defaults and per-download quality/audio/subtitle selections.
- [x] Handle unchanged Telegram message edits without a `MESSAGE_NOT_MODIFIED` traceback.
- [x] Throttle progress edits; parse yt-dlp download progress and report Pyrogram upload progress.
- [x] Keep task-owned workspaces and never remove outputs before Telegram confirms upload success.

## Phase 2: media-processing integration

- [x] FFprobe metadata probing, with optional MediaInfo metadata supplements.
- [x] Fix Matroska `BPS` units; do not invent Hindi when language metadata is unknown.
- [x] Optional TMDB title/year/genre/episode-air-date enrichment (requires `TMDB_API_KEY`).
- [x] Preserve the pre-normalization local filename separately from the physical final filename and Smart Caption filename.
- [x] Detailed/simple physical naming styles.
- [x] Smart Caption V1 (detailed audio tracks) and V2 (compact multi-audio summary).
- [x] Custom → TMDB → source thumbnail handling with screenshot fallback; optional MKV cover embedding.
- [x] Custom user thumbnail controls via `/setthumbnail` and `/clearthumbnail`.
- [x] Keep optional metadata/thumbnail tool failures graceful where possible.

## Explicitly not included in this approved scope

- Gofile, Storage.to and Buzzheavier upload provider integration/cloud fallback.
- Owner-only `/stats`/health diagnostics.
- Distributed queues, multi-instance workers, and resumable interrupted downloads.
- Live endpoint integration verification for Hotstar/SonyLIV, TMDB, or Telegram upload.

## Extractor error reporting

- [x] Show the actual sanitized yt-dlp failure detail instead of automatically suggesting every failure may be DRM.
- [x] Distinguish explicit DRM diagnostics from unsupported extractor/URL, HTTP 403/404, login/session, region and network errors.
- [x] Redact request URLs that may contain temporary signatures/tokens from the error display.
- [x] Regression tests cover the classifications and URL redaction.

## Before public deployment

1. Activate `.venv` before installing `requirements.txt`.
2. Set `ALLOWED_USER_IDS` to a private allowlist unless intentionally allowing public use.
3. Configure `TMDB_API_KEY` if enrichment is required.
4. Install `ffmpeg`/`ffprobe`; optionally install `mediainfo` and `mkvpropedit`.
5. Test one authorized Hotstar item and one authorized SonyLIV item, then validate upload size, thumbnail, subtitle, and caption behavior.
6. Use a durable database and understand that task state is persisted but work is not resumed after restart.

## Final local validation

- Python compile check: passed.
- `python -m unittest discover -s tests -v`: 25 passed.
- `python -m pytest -q`: 25 passed, 3 subtests passed.
- Live Telegram, Hotstar, SonyLIV and TMDB integration: not run in this environment; verify from the target host before public use.


## Per-user cookie uploads

- [x] `/settings` → Cookies / Session lets a user start a temporary upload window, then send a Netscape-format `.txt` file in the private chat.
- [x] 10-minute upload window, `.txt` extension check, 2 MB size cap, and supported-domain filtering.
- [x] Separate cookie files per Telegram user; cookie contents are not stored in SQLite/MongoDB or printed to logs/messages.
- [x] Owner-only file permissions on POSIX systems; remove/replace controls and optional server-default `COOKIE_FILE` fallback.
- [x] Analysis and download subprocesses use the requesting user's cookie file.
- [!] Cookie files remain sensitive credentials on the host; Telegram transports the uploaded document. Protect bot access and host storage. Live account/provider behavior still requires testing.
