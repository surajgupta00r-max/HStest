from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Any


from .config import Config
from .cookies import CookieStore


class SourceError(ValueError):
    """A submitted URL is invalid or is not a supported provider URL."""


def explain_analysis_error(exc: BaseException) -> str:
    """Return a concise, actionable yt-dlp failure summary without leaking signed URLs.

    Importantly, only an explicit DRM-related extractor message is described as DRM.
    Generic extractor/network errors must not be mislabeled as DRM protection.
    """
    raw = str(exc).strip() or type(exc).__name__
    # Strip terminal styling and keep the diagnostic to a single line.
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    # Extractor errors can include signed/temporary media URLs. Never echo those back.
    raw = re.sub(r"https?://[^\s]+", "[URL redacted]", raw, flags=re.IGNORECASE)
    # Guard against accidental disclosure if an exception contains key/value secrets.
    raw = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|signature|sessionid)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        raw,
    )
    raw = raw[:500]
    low = raw.lower()

    if any(marker in low for marker in ("drm protected", "drm-protected", "widevine", "playready", "content is encrypted with drm")):
        return (
            "The extractor explicitly reports DRM protection. This bot does not bypass DRM or access controls. "
            f"Extractor detail: {raw}"
        )
    if any(marker in low for marker in ("unsupported url", "no suitable extractor", "no extractor found", "is not a valid url")):
        return (
            "The installed yt-dlp extractor does not recognize this URL. Update yt-dlp and confirm that the link "
            f"is a direct Hotstar/SonyLIV title page. Extractor detail: {raw}"
        )
    if "403" in low or "forbidden" in low:
        return (
            "The provider refused the request (HTTP 403/Forbidden). This alone does not prove DRM; it may be an "
            "outdated extractor, expired login/cookies, region restriction, or provider-side rejection. "
            f"Extractor detail: {raw}"
        )
    if "404" in low or "not found" in low or "does not exist" in low:
        return f"The provider reports that this item or page was not found. Verify the link and availability. Extractor detail: {raw}"
    if any(marker in low for marker in ("login", "sign in", "authentication", "cookies", "not logged in", "unauthorized", "401")):
        return (
            "The provider appears to require a valid authorized session or login. If this title normally requires an account, "
            "configure a valid COOKIE_FILE; do not share cookie files. "
            f"Extractor detail: {raw}"
        )
    if any(marker in low for marker in ("geo-restricted", "not available in your country", "region restriction", "outside of your region")):
        return f"The provider reports a regional availability restriction. Extractor detail: {raw}"
    if any(marker in low for marker in ("timed out", "timeout", "connection reset", "temporary failure", "name or service not known", "network is unreachable")):
        return f"A network or timeout error occurred while contacting the provider. Retry later. Extractor detail: {raw}"

    return (
        "yt-dlp failed while reading this link. This error does not by itself mean the item is DRM-protected. "
        f"Extractor detail: {raw}"
    )


@dataclass(frozen=True)
class Source:
    name: str
    host: str


def detect_source(url: str, config: Config) -> Source:
    """Validate HTTPS URLs against configured provider host allowlists."""
    if not isinstance(url, str) or not url.strip():
        raise SourceError("Please provide a URL.")
    url = url.strip()
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as exc:
        raise SourceError("That URL is malformed.") from exc

    if parsed.scheme.lower() != "https":
        raise SourceError("Only HTTPS links are accepted.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceError("That URL contains an invalid port.") from exc
    if port not in (None, 443):
        raise SourceError("Non-standard URL ports are not accepted.")
    if parsed.username or parsed.password:
        raise SourceError("URLs containing embedded credentials are not accepted.")
    if not host:
        raise SourceError("The URL does not contain a valid hostname.")

    def matches(allowed: tuple[str, ...]) -> bool:
        return any(host == domain or host.endswith("." + domain) for domain in allowed)

    if matches(config.hotstar_domains):
        return Source("Hotstar", host)
    if matches(config.sonyliv_domains):
        return Source("SonyLIV", host)
    raise SourceError("Unsupported host. Submit a supported Hotstar or SonyLIV HTTPS URL.")


def _ydl_options(config: Config, cookie_file: Path | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    selected_cookie_file = cookie_file or config.cookie_file
    if selected_cookie_file:
        options["cookiefile"] = str(selected_cookie_file)
    return options


def _extract_info_sync(url: str, config: Config, cookie_file: Path | None = None) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt") from exc
    with yt_dlp.YoutubeDL(_ydl_options(config, cookie_file)) as ydl:
        data = ydl.extract_info(url, download=False)
    if not isinstance(data, dict):
        raise RuntimeError("The source did not return a single media item.")
    # Refuse playlists rather than unexpectedly downloading a full series/channel.
    if data.get("_type") in {"playlist", "multi_video"} or data.get("entries"):
        raise RuntimeError("Playlist URLs are not supported. Submit a direct movie or episode URL.")
    return data


def _valid_language_code(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value or value in {"und", "unknown", "none"}:
        return None
    if not re.fullmatch(r"[a-z0-9-]{2,12}", value):
        return None
    return value


def normalize_info(data: dict[str, Any], source: Source) -> dict[str, Any]:
    formats = data.get("formats") or []
    heights = sorted({
        int(fmt["height"])
        for fmt in formats
        if isinstance(fmt, dict) and isinstance(fmt.get("height"), (int, float)) and fmt["height"] > 0
    }, reverse=True)
    audio_languages: set[str] = set()
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        code = _valid_language_code(fmt.get("language") or fmt.get("audio_lang"))
        if code and fmt.get("acodec") not in (None, "none"):
            audio_languages.add(code)
    subtitles: set[str] = set()
    for group in (data.get("subtitles") or {}, data.get("automatic_captions") or {}):
        if isinstance(group, dict):
            for code in group.keys():
                if _valid_language_code(code):
                    subtitles.add(code.lower())
    duration = data.get("duration")
    season = data.get("season_number")
    episode = data.get("episode_number")
    def safe_int(value):
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            return None
    return {
        "source": source.name,
        "title": str(data.get("title") or data.get("id") or "Untitled media")[:300],
        "series": str(data.get("series") or data.get("playlist_title") or "")[:300],
        "episode_title": str(data.get("episode") or data.get("title") or "")[:300],
        "season_number": safe_int(season),
        "episode_number": safe_int(episode),
        "release_year": str(data.get("release_year") or "")[:4],
        "upload_date": str(data.get("upload_date") or "")[:8],
        "webpage_url": str(data.get("webpage_url") or ""),
        "duration": int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
        "thumbnail": data.get("thumbnail") if isinstance(data.get("thumbnail"), str) else None,
        "heights": heights,
        "audio_languages": sorted(audio_languages),
        "subtitle_languages": sorted(subtitles),
    }


class YtDlpAdapter:
    """Uses yt-dlp's normal extractors; this does not bypass DRM or access controls."""

    def __init__(self, config: Config, cookie_store: CookieStore | None = None):
        self.config = config
        self.cookie_store = cookie_store

    async def analyze(self, url: str, user_id: int | None = None) -> dict[str, Any]:
        source = detect_source(url, self.config)
        cookie_file = self.cookie_store.get_for_user(user_id) if self.cookie_store else self.config.cookie_file
        data = await asyncio.to_thread(_extract_info_sync, url, self.config, cookie_file)
        return normalize_info(data, source)


class HotstarAdapter(YtDlpAdapter):
    pass


class SonyLIVAdapter(YtDlpAdapter):
    pass


class SourceManager:
    def __init__(self, config: Config):
        self.config = config
        self.cookie_store = CookieStore(
            root=config.sqlite_path.parent / "cookies",
            global_cookie_file=config.cookie_file,
            allowed_domains=(*config.hotstar_domains, *config.sonyliv_domains),
        )
        self.hotstar = HotstarAdapter(config, self.cookie_store)
        self.sonyliv = SonyLIVAdapter(config, self.cookie_store)

    def cookie_file_for_user(self, user_id: int | None) -> Path | None:
        return self.cookie_store.get_for_user(user_id)

    def for_url(self, url: str) -> YtDlpAdapter:
        source = detect_source(url, self.config)
        return self.sonyliv if source.name == "SonyLIV" else self.hotstar

    async def analyze(self, url: str, user_id: int | None = None) -> dict[str, Any]:
        return await self.for_url(url).analyze(url, user_id=user_id)
