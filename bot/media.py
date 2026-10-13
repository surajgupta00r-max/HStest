from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

LOG = logging.getLogger(__name__)
LANGUAGES = {
    "eng": "English", "en": "English", "english": "English",
    "hin": "Hindi", "hi": "Hindi", "hindi": "Hindi",
    "tam": "Tamil", "ta": "Tamil", "tamil": "Tamil",
    "tel": "Telugu", "te": "Telugu", "telugu": "Telugu",
    "mal": "Malayalam", "ml": "Malayalam", "malayalam": "Malayalam",
    "kan": "Kannada", "kn": "Kannada", "kannada": "Kannada",
    "mar": "Marathi", "mr": "Marathi", "marathi": "Marathi",
    "ben": "Bengali", "bn": "Bengali", "bengali": "Bengali",
    "guj": "Gujarati", "gu": "Gujarati", "gujarati": "Gujarati",
    "pan": "Punjabi", "pa": "Punjabi", "punjabi": "Punjabi",
    "ori": "Odia", "or": "Odia", "odia": "Odia",
    "asm": "Assamese", "as": "Assamese", "assamese": "Assamese",
    "und": "Unknown", "unknown": "Unknown", "none": "Unknown", "": "Unknown",
}


def normalize_language(value: Any) -> str:
    if not isinstance(value, str):
        return "Unknown"
    raw = value.strip().lower()
    if raw in LANGUAGES:
        return LANGUAGES[raw]
    # ISO-639 language tags may include a region (e.g. en-US).
    return LANGUAGES.get(raw.split("-")[0], value.strip().capitalize() or "Unknown")


def clean_codec(codec: Any, kind: str = "video") -> str:
    if not codec or str(codec).lower() in {"none", "unknown", "n/a"}:
        return "Unknown"
    value = str(codec).lower()
    if any(x in value for x in ("hevc", "h265", "hvc1", "hev1")):
        return "HEVC" if kind == "video" else str(codec).upper()
    if any(x in value for x in ("h264", "avc", "avc1")):
        return "AVC" if kind == "video" else str(codec).upper()
    if "av01" in value or value == "av1":
        return "AV1"
    if "vp09" in value or "vp9" in value:
        return "VP9"
    if "vp8" in value:
        return "VP8"
    if "mp4a" in value or value == "aac":
        return "AAC"
    if any(x in value for x in ("eac3", "ec-3", "ec3")):
        return "E-AC3"
    if "ac3" in value:
        return "AC3"
    if "opus" in value:
        return "Opus"
    if "vorbis" in value:
        return "Vorbis"
    if "flac" in value:
        return "FLAC"
    if "mp3" in value or "mp3float" in value:
        return "MP3"
    return str(codec).upper()


def safe_filename(name: str, max_length: int = 220) -> str:
    value = str(name or "media").replace("\x00", "")
    value = re.sub(r'[\\/*?:"<>|\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value or value in {".", ".."}:
        value = "media"
    return value[:max_length].rstrip(" .") or "media"


def _run_json_command(command: list[str], timeout: int = 45) -> dict[str, Any] | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode == 0 and result.stdout.strip():
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return None


def _bitrate_kbps(stream: dict[str, Any], mediainfo_track: dict[str, Any] | None = None) -> int:
    value = stream.get("bit_rate")
    if value:
        try:
            n = int(value)
            if n > 0:
                return round(n / 1000)
        except (TypeError, ValueError):
            pass
    tags = stream.get("tags") or {}
    # Matroska BPS is bits/second, not bytes/second.
    for key in ("BPS", "BPS-eng"):
        try:
            n = int(tags.get(key, 0))
            if n > 0:
                return round(n / 1000)
        except (TypeError, ValueError):
            continue
    if mediainfo_track:
        try:
            n = int(mediainfo_track.get("BitRate", 0))
            if n > 0:
                return round(n / 1000)
        except (TypeError, ValueError):
            pass
    return 0


def _channel_layout(stream: dict[str, Any]) -> str:
    layout = stream.get("channel_layout")
    if isinstance(layout, str) and layout.strip():
        return layout.strip().replace("side ", "").replace("front ", "")
    try:
        channels = int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0
    return {1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(channels, f"{channels}ch" if channels else "Unknown")


def probe_media_sync(file_path: Path) -> dict[str, Any]:
    """Read media technical metadata; optional MediaInfo supplements FFprobe."""
    path = Path(file_path)
    size = path.stat().st_size if path.exists() else 0
    data = _run_json_command([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)
    ])
    mediainfo = _run_json_command(["mediainfo", "--Output=JSON", str(path)])
    mi_tracks = {"Audio": [], "Video": []}
    if mediainfo:
        for track in mediainfo.get("media", {}).get("track", []) or []:
            kind = track.get("@type")
            if kind in mi_tracks:
                mi_tracks[kind].append(track)

    streams = (data or {}).get("streams", []) or []
    format_data = (data or {}).get("format", {}) or {}
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    duration_raw = format_data.get("duration")
    try:
        duration = max(0, int(float(duration_raw))) if duration_raw is not None else 0
    except (TypeError, ValueError):
        duration = 0
    try:
        total_size = int(format_data.get("size") or size)
    except (TypeError, ValueError):
        total_size = size

    resolution = int(video.get("height") or 0) if video else 0
    pix_fmt = str(video.get("pix_fmt") or "") if video else ""
    raw_bits = str(video.get("bits_per_raw_sample") or "") if video else ""
    if raw_bits.isdigit() and int(raw_bits) > 0:
        bit_depth = f"{raw_bits}bit"
    elif "12" in pix_fmt:
        bit_depth = "12bit"
    elif "10" in pix_fmt:
        bit_depth = "10bit"
    elif video:
        bit_depth = "8bit"
    else:
        bit_depth = "Unknown"
    fps = 0
    if video:
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
        try:
            if "/" in rate:
                num, den = (float(part) for part in rate.split("/", 1))
                fps = round(num / den) if den else 0
            elif rate:
                fps = round(float(rate))
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0
    video_codec_raw = video.get("codec_name") if video else None
    video_codec = clean_codec(video_codec_raw, "video")
    video_bitrate = _bitrate_kbps(video, (mi_tracks["Video"] or [None])[0]) if video else 0

    audio_streams = []
    subtitle_languages: list[str] = []
    a_index = 0
    for stream in streams:
        kind = stream.get("codec_type")
        tags = stream.get("tags") or {}
        if kind == "audio":
            lang = normalize_language(tags.get("language") or tags.get("title"))
            codec = clean_codec(stream.get("codec_name"), "audio")
            audio_streams.append({
                "language": lang,
                "language_code": str(tags.get("language") or "und").lower(),
                "codec": codec,
                "codec_raw": stream.get("codec_name") or "unknown",
                "channels": _channel_layout(stream),
                "channel_count": stream.get("channels"),
                "bitrate_kbps": _bitrate_kbps(stream, mi_tracks["Audio"][a_index] if a_index < len(mi_tracks["Audio"]) else None),
                "default": bool((stream.get("disposition") or {}).get("default")),
            })
            a_index += 1
        elif kind == "subtitle":
            label = normalize_language(tags.get("language") or tags.get("title"))
            if label != "Unknown" and label not in subtitle_languages:
                subtitle_languages.append(label)

    known_subs = [s for s in subtitle_languages if s != "Unknown"]
    return {
        "probe_available": bool(data),
        "size_bytes": total_size,
        "duration": duration,
        "resolution": resolution,
        "fps": fps,
        "video_codec": video_codec,
        "video_codec_raw": video_codec_raw or "unknown",
        "bit_depth": bit_depth,
        "video_bitrate_kbps": video_bitrate,
        "audio_streams": audio_streams,
        "audio_languages": list(dict.fromkeys(a["language"] for a in audio_streams if a["language"] != "Unknown")),
        "subtitle_languages": known_subs,
        "subtitle_stream_count": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
    }


def _http_json(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": "HS2TelegramBot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read(2 * 1024 * 1024)
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def resolve_tmdb_sync(title: str, is_show: bool, api_key: str, season: int | None = None, episode: int | None = None) -> dict[str, Any]:
    if not api_key or not title.strip():
        return {}
    kind = "tv" if is_show else "movie"
    query = urllib.parse.urlencode({"api_key": api_key, "query": title})
    search = _http_json(f"https://api.themoviedb.org/3/search/{kind}?{query}") or {}
    results = search.get("results") or []
    if not results:
        return {}
    chosen = results[0]
    item_id = chosen.get("id")
    if not item_id:
        return {}
    detail = _http_json(f"https://api.themoviedb.org/3/{kind}/{item_id}?api_key={urllib.parse.quote(api_key)}") or {}
    date = detail.get("first_air_date" if is_show else "release_date") or chosen.get("first_air_date" if is_show else "release_date") or ""
    genres = [x.get("name") for x in detail.get("genres", []) or [] if x.get("name")]
    out = {
        "provider": "tmdb", "tmdb_id": item_id,
        "title": detail.get("name" if is_show else "title") or chosen.get("name" if is_show else "title") or title,
        "year": date[:4] if len(date) >= 4 else "",
        "genres": genres[:3],
        "poster_url": f"https://image.tmdb.org/t/p/w780{detail.get('poster_path') or chosen.get('poster_path')}" if (detail.get("poster_path") or chosen.get("poster_path")) else None,
        "backdrop_url": f"https://image.tmdb.org/t/p/w1280{detail.get('backdrop_path') or chosen.get('backdrop_path')}" if (detail.get("backdrop_path") or chosen.get("backdrop_path")) else None,
    }
    if is_show and season is not None and episode is not None and season > 0 and episode > 0:
        ep_data = _http_json(f"https://api.themoviedb.org/3/tv/{item_id}/season/{season}/episode/{episode}?api_key={urllib.parse.quote(api_key)}") or {}
        air = ep_data.get("air_date")
        if air:
            out["air_date"] = air
        if ep_data.get("name"):
            out["episode_title"] = ep_data["name"]
    return out


async def resolve_tmdb(title: str, is_show: bool, api_key: str, season: int | None = None, episode: int | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(resolve_tmdb_sync, title, is_show, api_key, season, episode)


def _url_is_public_https(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"}:
            return False
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            return bool(infos) and all(ipaddress.ip_address(item[4][0]).is_global for item in infos)
    except (ValueError, OSError):
        return False


class _PublicHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not _url_is_public_https(target):
            return None
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download_image_sync(url: str, destination: Path) -> Path | None:
    if not _url_is_public_https(url):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 HS2TelegramBot"})
        opener = urllib.request.build_opener(_PublicHTTPSRedirectHandler())
        with opener.open(request, timeout=12) as response:
            final_url = response.geturl()
            if not _url_is_public_https(final_url):
                return None
            content = response.read(10 * 1024 * 1024 + 1)
            if not content or len(content) > 10 * 1024 * 1024:
                return None
        raw = destination.with_suffix(".source")
        raw.write_bytes(content)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            result = subprocess.run([ffmpeg, "-y", "-i", str(raw), "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "4", str(destination)], capture_output=True, timeout=30, check=False)
            raw.unlink(missing_ok=True)
            if result.returncode == 0 and destination.exists() and destination.stat().st_size:
                return destination
        # Only keep already-JPEG data when ffmpeg is unavailable.
        if content.startswith(b"\xff\xd8\xff"):
            raw.rename(destination)
            return destination
        raw.unlink(missing_ok=True)
    except (OSError, URLError, HTTPError, subprocess.SubprocessError):
        LOG.debug("Thumbnail fetch failed", exc_info=True)
    return None


async def download_image(url: str, destination: Path) -> Path | None:
    return await asyncio.to_thread(_download_image_sync, url, destination)


def screenshot_sync(video_path: Path, duration: int, destination: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    seek = max(1, min(60, duration // 3 if duration else 10))
    try:
        result = subprocess.run([ffmpeg, "-y", "-ss", str(seek), "-i", str(video_path), "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "4", str(destination)], capture_output=True, timeout=30, check=False)
        return destination if result.returncode == 0 and destination.exists() and destination.stat().st_size else None
    except (OSError, subprocess.SubprocessError):
        return None


async def resolve_thumbnail(workdir: Path, video_path: Path, info: dict[str, Any], tmdb: dict[str, Any], settings: dict[str, Any], config) -> Path | None:
    mode = settings.get("thumbnail_mode", "auto")
    candidates: list[tuple[str, str | None]] = []
    custom = (settings.get("custom_thumbnail_url") or config.custom_thumbnail_url or "").strip()
    if mode in {"auto", "custom"} and custom:
        candidates.append(("custom", custom))
    if mode in {"auto", "tmdb"}:
        candidates.append(("tmdb", tmdb.get("backdrop_url") or tmdb.get("poster_url")))
    if mode in {"auto", "source"}:
        candidates.append(("source", info.get("thumbnail")))
    # Keep the yt-dlp generated poster in the task workspace as another source candidate.
    for label, url in candidates:
        if not url:
            continue
        destination = workdir / f"thumbnail_{label}.jpg"
        fetched = await download_image(str(url), destination)
        if fetched:
            return fetched
    existing = sorted([p for p in workdir.glob("*.jpg") if p.is_file() and p != video_path and not p.name.startswith("thumbnail_")], key=lambda p: p.stat().st_size, reverse=True)
    if mode in {"auto", "source"} and existing:
        return existing[0]
    if mode in {"auto", "screenshot"}:
        dest = workdir / "thumbnail_screenshot.jpg"
        path = await asyncio.to_thread(screenshot_sync, video_path, int(info.get("duration") or 0), dest)
        if path:
            return path
    # A requested mode may not be available. Fall back through remaining safe candidates.
    if mode not in {"auto", "source"}:
        for label, url in [("tmdb", tmdb.get("backdrop_url") or tmdb.get("poster_url")), ("source", info.get("thumbnail"))]:
            if url:
                fetched = await download_image(str(url), workdir / f"thumbnail_fallback_{label}.jpg")
                if fetched:
                    return fetched
        path = await asyncio.to_thread(screenshot_sync, video_path, int(info.get("duration") or 0), workdir / "thumbnail_fallback_screenshot.jpg")
        if path:
            return path
    return None


def embed_thumbnail_sync(video_path: Path, thumbnail: Path) -> bool:
    if video_path.suffix.lower() != ".mkv" or not video_path.exists() or not thumbnail.exists():
        return False
    mkvpropedit = shutil.which("mkvpropedit")
    if mkvpropedit:
        try:
            result = subprocess.run([
                mkvpropedit, str(video_path), "--delete-attachment", "name:cover.jpg",
                "--attachment-name", "cover.jpg", "--attachment-mime-type", "image/jpeg",
                "--add-attachment", str(thumbnail),
            ], capture_output=True, timeout=120, check=False)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        temp_path = video_path.with_name(video_path.stem + ".cover-tmp.mkv")
        try:
            result = subprocess.run([
                ffmpeg, "-y", "-i", str(video_path), "-attach", str(thumbnail), "-map", "0", "-c", "copy",
                "-metadata:s:t", "mimetype=image/jpeg", "-metadata:s:t:0", "filename=cover.jpg", str(temp_path),
            ], capture_output=True, timeout=600, check=False)
            if result.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                os.replace(temp_path, video_path)
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            temp_path.unlink(missing_ok=True)
    return False


async def embed_thumbnail(video_path: Path, thumbnail: Path) -> bool:
    return await asyncio.to_thread(embed_thumbnail_sync, video_path, thumbnail)



def prepare_telegram_thumbnail_sync(source: Path, destination: Path) -> Path | None:
    """Create a Telegram-friendly JPEG thumbnail (small dimensions/size) when possible."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run([ffmpeg, "-y", "-i", str(source), "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "6", str(destination)], capture_output=True, timeout=30, check=False)
            if result.returncode == 0 and destination.exists() and 0 < destination.stat().st_size <= 200 * 1024:
                return destination
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if source.exists() and source.suffix.lower() in {".jpg", ".jpeg"} and source.stat().st_size <= 200 * 1024:
            return source
    except OSError:
        pass
    return None


async def prepare_telegram_thumbnail(source: Path | None, destination: Path) -> Path | None:
    if not source:
        return None
    return await asyncio.to_thread(prepare_telegram_thumbnail_sync, source, destination)

def _audio_summary(probe: dict[str, Any], compact: bool = False) -> str:
    tracks = probe.get("audio_streams") or []
    if not tracks:
        return "AudioUnknown"
    labels = []
    for track in tracks:
        lang = track.get("language") or "Unknown"
        codec = track.get("codec") or "Unknown"
        channels = track.get("channels") or "Unknown"
        bitrate = int(track.get("bitrate_kbps") or 0)
        item = f"{lang}.{codec}.{channels}"
        if bitrate > 0:
            item += f".{bitrate}kbps"
        labels.append(item)
    if compact:
        if len(tracks) == 1:
            return labels[0]
        return "MultiAudio"
    return "+".join(labels)


def _media_stem(info: dict[str, Any], tmdb: dict[str, Any], is_show: bool) -> str:
    source_title = str(info.get("title") or "Media").strip()
    series = str(info.get("series") or "").strip()
    base = str(tmdb.get("title") or (series if is_show and series else source_title)).strip()
    season = info.get("season_number")
    episode = info.get("episode_number")
    parts = [base]
    if is_show and isinstance(season, int) and isinstance(episode, int):
        parts.append(f"S{season:02d}E{episode:02d}")
        episode_title = str(info.get("episode_title") or source_title).strip()
        if episode_title and episode_title.casefold() != base.casefold():
            parts.append(episode_title)
    elif not is_show:
        year = str(tmdb.get("year") or info.get("release_year") or info.get("upload_date", "")[:4] or "")
        if year and year.isdigit() and len(year) == 4:
            parts[0] = f"{base} ({year})"
    if is_show and tmdb.get("air_date") and (info.get("settings_episode_dates") is True):
        parts.append(f"[{tmdb['air_date']}]")
    return " - ".join(x for x in parts if x)


def build_smart_caption_filename(info: dict[str, Any], probe: dict[str, Any], tmdb: dict[str, Any], settings: dict[str, Any], source_name: str, source_label: str = "WEB-DL") -> str:
    source_url = str(info.get("webpage_url") or "").lower()
    is_show = bool(info.get("season_number") or info.get("episode_number") or "/shows/" in source_url)
    stem = _media_stem(info, tmdb, is_show)
    title_parts = [re.sub(r"\s+", ".", stem.replace("[", "").replace("]", ""))]
    if probe.get("resolution"):
        title_parts.append(f"{probe['resolution']}p")
    bit_depth = probe.get("bit_depth")
    if bit_depth and bit_depth != "Unknown":
        title_parts.append(bit_depth)
    title_parts.append(re.sub(r"\s+", ".", source_label.strip()) or "WEB-DL")
    codec = probe.get("video_codec") or "Unknown"
    codec_token = {"HEVC": "x265", "AVC": "x264"}.get(codec, codec)
    title_parts.extend([codec_token, codec])
    version = str(settings.get("smart_caption_version", "v2")).lower()
    title_parts.append(_audio_summary(probe, compact=(version != "v1")))
    subs = probe.get("subtitle_languages") or info.get("subtitle_languages") or []
    title_parts.append("MSubs" if len(subs) > 1 else "ESub" if subs else "NoSubs")
    extension = Path(source_name).suffix.lower()
    if extension not in {".mkv", ".mp4", ".webm", ".mov", ".m4v", ".avi"}:
        extension = ".mkv"
    return safe_filename(".".join(part for part in title_parts if part), 390) + extension


def build_physical_filename(info: dict[str, Any], probe: dict[str, Any], tmdb: dict[str, Any], settings: dict[str, Any], source_name: str, source_label: str, ripper_tag: str) -> str:
    url = str(info.get("webpage_url") or "").lower()
    is_show = bool(info.get("season_number") or info.get("episode_number") or "/shows/" in url)
    stem = _media_stem(info, tmdb, is_show)
    if settings.get("naming_style", "detailed") == "simple":
        parts = [stem]
        if probe.get("resolution"):
            parts.append(f"{probe['resolution']}p")
        parts.append(source_label)
        codec = probe.get("video_codec") or "Unknown"
        parts.append({"HEVC": "x265", "AVC": "x264"}.get(codec, codec))
        ext = Path(source_name).suffix.lower() or ".mkv"
        return safe_filename(" - ".join(parts)) + ext

    parts = [stem]
    if probe.get("resolution"):
        parts.append(f"{probe['resolution']}p")
    parts.append(source_label)
    bit_depth = probe.get("bit_depth")
    codec = probe.get("video_codec") or "Unknown"
    codec_token = {"HEVC": "x265 HEVC", "AVC": "x264 AVC"}.get(codec, codec)
    technical = " ".join(x for x in (bit_depth if bit_depth != "Unknown" else "", codec_token) if x)
    audios = []
    for item in probe.get("audio_streams") or []:
        label = f"{item.get('language', 'Unknown')} - {item.get('codec', 'Unknown')} {item.get('channels', 'Unknown')}"
        if item.get("bitrate_kbps"):
            label += f" {item['bitrate_kbps']}kbps"
        audios.append(label)
    audio_desc = " + ".join(audios) if audios else "AudioUnknown"
    parts.append(f"[{technical} - ({audio_desc})]")
    if ripper_tag:
        parts.append(f"~ {ripper_tag}")
    ext = Path(source_name).suffix.lower() or ".mkv"
    return safe_filename(" - ".join(parts), 230) + ext


def build_caption(info: dict[str, Any], probe: dict[str, Any], tmdb: dict[str, Any], task: dict[str, Any], source_label: str) -> tuple[str, str]:
    settings = task.get("settings") or {}
    caption_filename = build_smart_caption_filename(info, probe, tmdb, settings, task.get("original_filename") or "media.mkv", source_label)
    genres = tmdb.get("genres") or []
    original = str(task.get("original_filename") or "Unknown")
    details = [
        caption_filename,
        f"Original filename: {original}",
        f"Source: {task.get('source') or source_label}",
        f"Resolution: {probe.get('resolution') or 'Unknown'}p | FPS: {probe.get('fps') or 'Unknown'} | Video: {probe.get('bit_depth') or 'Unknown'} {probe.get('video_codec') or 'Unknown'}",
        f"Audio: {_audio_summary(probe, compact=False)}",
        f"Subtitles: {', '.join(probe.get('subtitle_languages') or info.get('subtitle_languages') or []) or 'None detected'}",
    ]
    if genres:
        details.append("Genre: " + ", ".join(genres[:3]))
    if tmdb.get("year"):
        details.append(f"Year: {tmdb['year']}")
    if probe.get("duration"):
        mins, secs = divmod(int(probe["duration"]), 60)
        hours, mins = divmod(mins, 60)
        duration = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
        details.append(f"Duration: {duration}")
    if probe.get("size_bytes"):
        details.append(f"Size: {probe['size_bytes'] / 1024**2:.1f} MiB")
    physical = str(task.get("physical_filename") or "Unknown")
    if physical != original:
        details.append(f"Saved as: {physical}")
    body = "\n".join(details)
    # Telegram video captions are limited; prioritize the title and original filename.
    if len(body) > 1000:
        body = body[:990].rsplit("\n", 1)[0] + "\n…"
    return caption_filename, body


async def prepare_media_assets(video_path: Path, info: dict[str, Any], task: dict[str, Any], config, workdir: Path) -> dict[str, Any]:
    original_filename = video_path.name
    settings = task.get("settings") or {}
    source_url = str(info.get("webpage_url") or task.get("url") or "")
    is_show = bool(info.get("season_number") or info.get("episode_number") or "/shows/" in source_url.lower())
    probe = await asyncio.to_thread(probe_media_sync, video_path)
    query_title = str((info.get("series") if is_show else info.get("title")) or "").strip()
    if not query_title:
        query_title = str(info.get("title") or video_path.stem)
    tmdb = {}
    if settings.get("metadata_provider", "tmdb") == "tmdb" and config.tmdb_api_key:
        try:
            tmdb = await resolve_tmdb(query_title, is_show, config.tmdb_api_key, info.get("season_number"), info.get("episode_number"))
        except Exception:
            LOG.debug("TMDB resolution failed for task title", exc_info=True)
    if settings.get("metadata_provider", "tmdb") == "none":
        tmdb = {}
    info_for_naming = dict(info)
    if settings.get("episode_dates"):
        info_for_naming["settings_episode_dates"] = True
    source_label = f"{task.get('source') or info.get('source') or 'Media'} {config.source_label}".strip()
    physical_filename = build_physical_filename(info_for_naming, probe, tmdb, settings, original_filename, source_label, config.ripper_tag)
    final_path = video_path.with_name(physical_filename)
    if final_path.resolve() != video_path.resolve():
        # Avoid silently deleting a prior output even inside a task-owned workspace.
        candidate = final_path
        idx = 2
        while candidate.exists():
            candidate = final_path.with_name(f"{final_path.stem} ({idx}){final_path.suffix}")
            idx += 1
        video_path.rename(candidate)
        final_path = candidate
        physical_filename = final_path.name
    thumb = await resolve_thumbnail(workdir, final_path, info, tmdb, settings, config)
    if thumb and settings.get("embed_thumbnail") and final_path.suffix.lower() == ".mkv":
        try:
            await embed_thumbnail(final_path, thumb)
        except Exception:
            LOG.debug("Cover embedding failed", exc_info=True)
    stored_task = dict(task)
    stored_task.update({"original_filename": original_filename, "physical_filename": physical_filename, "settings": settings})
    caption_filename, caption = build_caption(info_for_naming, probe, tmdb, stored_task, source_label)
    return {
        "original_filename": original_filename,
        "physical_filename": physical_filename,
        "caption_filename": caption_filename,
        "caption": caption,
        "probe": probe,
        "tmdb": tmdb,
        "thumbnail": str(thumb) if thumb and thumb.exists() else None,
        "path": str(final_path),
        "metadata": {"probe": probe, "tmdb": tmdb},
    }


def is_public_https_url(url: str) -> bool:
    """Validate a user-provided image URL before accepting it as a bot setting."""
    return _url_is_public_https(url)
