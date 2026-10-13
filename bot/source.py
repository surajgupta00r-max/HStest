from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Any


from .config import Config


class SourceError(ValueError):
    """A submitted URL is invalid or is not a supported provider URL."""


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


def _ydl_options(config: Config) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    if config.cookie_file:
        options["cookiefile"] = str(config.cookie_file)
    return options


def _extract_info_sync(url: str, config: Config) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt") from exc
    with yt_dlp.YoutubeDL(_ydl_options(config)) as ydl:
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
    return {
        "source": source.name,
        "title": str(data.get("title") or data.get("id") or "Untitled media")[:300],
        "webpage_url": str(data.get("webpage_url") or ""),
        "duration": int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
        "thumbnail": data.get("thumbnail") if isinstance(data.get("thumbnail"), str) else None,
        "heights": heights,
        "audio_languages": sorted(audio_languages),
        "subtitle_languages": sorted(subtitles),
    }


class YtDlpAdapter:
    """Uses yt-dlp's normal extractors; this does not bypass DRM or access controls."""

    def __init__(self, config: Config):
        self.config = config

    async def analyze(self, url: str) -> dict[str, Any]:
        source = detect_source(url, self.config)
        data = await asyncio.to_thread(_extract_info_sync, url, self.config)
        return normalize_info(data, source)


class HotstarAdapter(YtDlpAdapter):
    pass


class SonyLIVAdapter(YtDlpAdapter):
    pass


class SourceManager:
    def __init__(self, config: Config):
        self.config = config
        self.hotstar = HotstarAdapter(config)
        self.sonyliv = SonyLIVAdapter(config)

    def for_url(self, url: str) -> YtDlpAdapter:
        source = detect_source(url, self.config)
        return self.sonyliv if source.name == "SonyLIV" else self.hotstar

    async def analyze(self, url: str) -> dict[str, Any]:
        return await self.for_url(url).analyze(url)
