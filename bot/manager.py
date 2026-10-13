from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Config
from .database import Database, now_iso
from .media import prepare_media_assets, prepare_telegram_thumbnail
from .source import SourceError, SourceManager, detect_source, explain_analysis_error

LOG = logging.getLogger(__name__)
MEDIA_SUFFIXES = {".mkv", ".mp4", ".webm", ".mov", ".m4v", ".avi"}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
LANG_LABELS = {
    "en": "English", "eng": "English", "hi": "Hindi", "hin": "Hindi",
    "ta": "Tamil", "tam": "Tamil", "te": "Telugu", "tel": "Telugu",
    "ml": "Malayalam", "mal": "Malayalam", "kn": "Kannada", "kan": "Kannada",
    "mr": "Marathi", "mar": "Marathi", "bn": "Bengali", "ben": "Bengali",
    "gu": "Gujarati", "guj": "Gujarati", "pa": "Punjabi", "pan": "Punjabi",
}
STATUS_FILTERS = ("all", "active", "failed", "completed")
PAGE_SIZE = 4


class UserTaskError(RuntimeError):
    pass


@dataclass
class UploadPayload:
    task_id: str
    output_path: Path
    info: dict[str, Any]
    assets: dict[str, Any]


class TaskRuntime:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.job_task: asyncio.Task | None = None
        self.cancel_event = asyncio.Event()


class TaskManager:
    """Two-stage download/upload task manager with task-owned workspaces."""

    def __init__(self, app, config: Config, db: Database):
        self.app = app
        self.config = config
        self.db = db
        self.sources = SourceManager(config)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.upload_queue: asyncio.Queue[UploadPayload] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.download_workers: list[asyncio.Task] = []
        self.upload_workers: list[asyncio.Task] = []
        self.runtimes: dict[str, TaskRuntime] = {}
        self.analyses: dict[str, dict[str, Any]] = {}
        self.user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.progress_update_interval = 3.0

    async def start(self) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        interrupted = self.db.mark_interrupted_tasks()
        if interrupted:
            LOG.info("Marked %s unfinished tasks as interrupted", interrupted)
        self.download_workers = [
            asyncio.create_task(self._download_worker(i + 1), name=f"download-worker-{i + 1}")
            for i in range(self.config.max_download_workers)
        ]
        self.upload_workers = [
            asyncio.create_task(self._upload_worker(i + 1), name=f"upload-worker-{i + 1}")
            for i in range(self.config.max_upload_workers)
        ]
        self.workers = [*self.download_workers, *self.upload_workers]
        LOG.info("Started %s download worker(s) and %s upload worker(s)", len(self.download_workers), len(self.upload_workers))

    async def stop(self) -> None:
        # Cancel both worker-owned stages and in-flight analysis tasks started by
        # command handlers. Await all of them before closing MongoDB/persistence,
        # otherwise a handler's cancellation cleanup can race the database close.
        active_jobs: set[asyncio.Task] = set()
        for runtime in list(self.runtimes.values()):
            runtime.cancel_event.set()
            if runtime.process and runtime.process.returncode is None:
                runtime.process.terminate()
            if runtime.job_task and not runtime.job_task.done():
                active_jobs.add(runtime.job_task)
                runtime.job_task.cancel()
        for worker in self.workers:
            worker.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        if active_jobs:
            await asyncio.gather(*active_jobs, return_exceptions=True)
        self.db.close()

    async def prepare_download(self, message, url: str) -> str | None:
        user = message.from_user
        if user is None:
            await message.reply_text("This command must be used by a Telegram user.")
            return None
        user_id = int(user.id)
        if self.config.allowed_user_ids and user_id not in self.config.allowed_user_ids:
            await message.reply_text("You are not authorized to use this bot.")
            return None
        async with self.user_locks[user_id]:
            self.db.register_user(user_id, user.username)
            if self.db.count_active_tasks(user_id) >= self.config.max_active_tasks_per_user:
                await message.reply_text("You already have an active task. Use /status to check it or /cancel TASK_ID to cancel it.")
                return None
            try:
                source = detect_source(url, self.config)
            except SourceError as exc:
                await message.reply_text(str(exc))
                return None

            task_id = uuid.uuid4().hex[:8].upper()
            settings = self.db.get_settings(user_id)
            self.db.create_task({
                "task_id": task_id, "user_id": user_id, "url": url, "source": source.name,
                "title": "Analyzing link", "status": "ANALYZING", "stage": "Analyzing link", "progress": 0,
                "quality": settings.get("quality", "best"),
                "audio_language": settings.get("audio_language", "best"),
                "subtitles_enabled": bool(settings.get("subtitles_enabled", False)),
                "subtitle_language": settings.get("subtitle_language", "en"),
                "error": None, "output_url": None, "workdir": None,
            })
            self.runtimes[task_id] = TaskRuntime()
            status_msg = await message.reply_text(f"Analyzing {source.name} link…\nTask: {task_id}")
            self.db.update_task(task_id, message_chat_id=int(status_msg.chat.id), message_id=int(status_msg.id))
            runtime = self.runtimes[task_id]
            analysis_task = asyncio.create_task(self.sources.analyze(url, user_id=user_id), name=f"analyze-{task_id}")
            runtime.job_task = analysis_task
            try:
                info = await analysis_task
                current_task = self.db.get_task(task_id) or {}
                if current_task.get("status") != "ANALYZING" or runtime.cancel_event.is_set():
                    self.analyses.pop(task_id, None)
                    return None
                self.analyses[task_id] = info
                updated_settings = self.db.get_settings(user_id)
                available_audio = info.get("audio_languages", [])
                desired_audio = str(updated_settings.get("audio_language", "best"))
                if desired_audio != "best" and desired_audio not in available_audio:
                    desired_audio = "best"
                available_subs = info.get("subtitle_languages", [])
                desired_sub = str(updated_settings.get("subtitle_language", "en")).lower()
                subs_enabled = bool(updated_settings.get("subtitles_enabled", False))
                # The source extractor may report a canonical label while settings use an ISO code;
                # select only exact values the downloader can request.
                if desired_sub not in available_subs:
                    desired_sub = available_subs[0] if (subs_enabled and available_subs) else "off"
                if not available_subs:
                    subs_enabled = False
                current = self.db.get_task(task_id) or {}
                desired_quality = str(current.get("quality", "best"))
                if desired_quality != "best" and desired_quality not in [str(x) for x in info.get("heights", [])]:
                    desired_quality = "best"
                self.db.update_task(
                    task_id, title=info["title"], status="WAITING_FOR_SELECTION", stage="Waiting for selection",
                    quality=desired_quality, audio_language=desired_audio,
                    subtitles_enabled=subs_enabled, subtitle_language=desired_sub if subs_enabled else "off",
                )
                await self._edit_task_message(task_id, self._selection_text(task_id), self.selection_keyboard(task_id))
                return task_id
            except asyncio.CancelledError:
                current = self.db.get_task(task_id) or {}
                if current.get("status") != "CANCELLED":
                    self.db.update_task(task_id, status="CANCELLED", stage="Cancelled", error="Cancelled during analysis.")
                self.analyses.pop(task_id, None)
                await self._edit_task_message(task_id, f"Task cancelled.\nTask: {task_id}", None)
                return None
            except Exception as exc:
                diagnostic = explain_analysis_error(exc)
                # Use the sanitized diagnostic so signed URLs/session values from an
                # extractor exception are not copied into the application log.
                LOG.warning("Task %s analysis failed (%s): %s", task_id, type(exc).__name__, diagnostic)
                self.db.update_task(task_id, status="FAILED", stage="Failed", error=diagnostic[:500])
                await self._edit_task_message(
                    task_id,
                    f"Could not analyze this link.\nTask: {task_id}\n\n{diagnostic}",
                    None,
                )
                self.analyses.pop(task_id, None)
                self.runtimes.pop(task_id, None)
                return None
            finally:
                if runtime.job_task is analysis_task:
                    runtime.job_task = None
                latest = self.db.get_task(task_id) or {}
                if latest.get("status") in TERMINAL_STATES:
                    self.runtimes.pop(task_id, None)

    def _quality_choices(self, info: dict[str, Any]) -> list[str]:
        heights = info.get("heights") or []
        common = [h for h in (2160, 1440, 1080, 720, 480, 360) if h in heights]
        for height in heights:
            if height not in common:
                common.append(height)
        return ["best", *[str(h) for h in common[:5]]]

    def selection_keyboard(self, task_id: str) -> InlineKeyboardMarkup:
        task = self.db.get_task(task_id) or {}
        info = self.analyses.get(task_id, {})
        current_quality = str(task.get("quality", "best"))
        current_audio = str(task.get("audio_language", "best"))
        current_subs = str(task.get("subtitle_language", "off")) if task.get("subtitles_enabled") else "off"
        rows = []
        quality_buttons = []
        for value in self._quality_choices(info):
            label = "Best" if value == "best" else f"{value}p"
            if value == current_quality:
                label = f"✓ {label}"
            quality_buttons.append(InlineKeyboardButton(label, callback_data=f"d:{task_id}:q:{value}"))
            if len(quality_buttons) == 3:
                rows.append(quality_buttons)
                quality_buttons = []
        if quality_buttons:
            rows.append(quality_buttons)

        audio_values = ["best"] + [code for code in info.get("audio_languages", [])[:4] if code != "best"]
        audio_buttons = []
        for code in audio_values:
            label = "Audio: Auto" if code == "best" else f"Audio: {LANG_LABELS.get(code, code.upper())}"
            if code == current_audio:
                label = f"✓ {label}"
            audio_buttons.append(InlineKeyboardButton(label, callback_data=f"d:{task_id}:a:{code}"))
            if len(audio_buttons) == 2:
                rows.append(audio_buttons)
                audio_buttons = []
        if audio_buttons:
            rows.append(audio_buttons)

        sub_values = ["off"] + [code for code in info.get("subtitle_languages", [])[:5] if code != "off"]
        sub_buttons = []
        for code in sub_values:
            label = "Subs: Off" if code == "off" else f"Subs: {LANG_LABELS.get(code, code.upper())}"
            if code == current_subs:
                label = f"✓ {label}"
            sub_buttons.append(InlineKeyboardButton(label, callback_data=f"d:{task_id}:s:{code}"))
            if len(sub_buttons) == 2:
                rows.append(sub_buttons)
                sub_buttons = []
        if sub_buttons:
            rows.append(sub_buttons)
        rows.append([
            InlineKeyboardButton("Start download", callback_data=f"d:{task_id}:start"),
            InlineKeyboardButton("Cancel", callback_data=f"d:{task_id}:cancel"),
        ])
        return InlineKeyboardMarkup(rows)

    def _selection_text(self, task_id: str) -> str:
        task = self.db.get_task(task_id) or {}
        info = self.analyses.get(task_id, {})
        quality = "Best available" if task.get("quality", "best") == "best" else f"Up to {task.get('quality')}p"
        audio = task.get("audio_language", "best")
        audio_text = "Automatic" if audio == "best" else LANG_LABELS.get(audio, audio.upper())
        subs = "Off"
        if task.get("subtitles_enabled"):
            code = task.get("subtitle_language", "en")
            subs = LANG_LABELS.get(code, code.upper())
        rows = [
            "Download options", "",
            f"Title: {info.get('title') or task.get('title') or 'Unknown'}",
            f"Source: {task.get('source', 'Unknown')}",
            f"Quality: {quality}", f"Audio preference: {audio_text}", f"Subtitles: {subs}",
            f"Task: {task_id}", "", "Select options below, then press Start download.",
        ]
        return "\n".join(rows)

    async def handle_download_callback(self, callback) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) < 3 or not callback.from_user:
            await callback.answer("Invalid action.", show_alert=True)
            return
        task_id = parts[1]
        if self.config.allowed_user_ids and int(callback.from_user.id) not in self.config.allowed_user_ids:
            await callback.answer("You are not authorized to use this bot.", show_alert=True)
            return
        task = self.db.get_task(task_id)
        if not task or int(task["user_id"]) != int(callback.from_user.id):
            await callback.answer("Task not found or does not belong to you.", show_alert=True)
            return
        action = parts[2]
        if action == "cancel":
            cancelled = await self.cancel_task(task_id, int(callback.from_user.id))
            await callback.answer("Cancellation requested." if cancelled else "Task is already finished or cancelled.")
            return
        if action in {"q", "a", "s"}:
            if task.get("status") != "WAITING_FOR_SELECTION" or task_id not in self.analyses:
                await callback.answer("These options have expired. Submit the URL again.", show_alert=True)
                return
            if len(parts) != 4:
                await callback.answer("Invalid selection.", show_alert=True)
                return
            value = parts[3]
            info = self.analyses[task_id]
            if action == "q":
                if value not in self._quality_choices(info):
                    await callback.answer("That quality is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, quality=value)
            elif action == "a":
                if value not in (["best"] + info.get("audio_languages", [])[:4]):
                    await callback.answer("That audio preference is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, audio_language=value)
            else:
                if value not in (["off"] + info.get("subtitle_languages", [])[:5]):
                    await callback.answer("That subtitle language is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, subtitles_enabled=(value != "off"), subtitle_language=value)
            try:
                await callback.message.edit_text(self._selection_text(task_id), reply_markup=self.selection_keyboard(task_id))
            except MessageNotModified:
                await callback.answer("That option is already selected.")
            else:
                await callback.answer("Selection updated.")
            return
        if action == "start":
            if task.get("status") != "WAITING_FOR_SELECTION" or task_id not in self.analyses:
                await callback.answer("This task is no longer waiting for selection.", show_alert=True)
                return
            self.db.update_task(task_id, status="QUEUED", stage="Queued for download", progress=0, speed=None, eta=None, error=None)
            self.queue.put_nowait(task_id)
            pos = self.queue_position(task_id, "download")
            await self._edit_task_message(
                task_id,
                f"Task queued for download\nTitle: {task.get('title') or 'Media'}\nTask: {task_id}\nQueue position: {pos or 'Starting'}",
                None,
            )
            await callback.answer("Added to the download queue.")
            return
        await callback.answer("Unknown action.", show_alert=True)

    async def _download_worker(self, worker_number: int) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                task = self.db.get_task(task_id)
                if not task or task.get("status") != "QUEUED":
                    continue
                runtime = self.runtimes.setdefault(task_id, TaskRuntime())
                stage_task = asyncio.create_task(self._download_stage(task_id, runtime), name=f"download-job-{task_id}")
                runtime.job_task = stage_task
                try:
                    await stage_task
                except asyncio.CancelledError:
                    # During service shutdown the worker itself is cancelled; let that
                    # cancellation propagate so TaskManager.stop() can join every worker.
                    worker_task = asyncio.current_task()
                    if worker_task and worker_task.cancelling():
                        raise
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") not in TERMINAL_STATES:
                        self.db.update_task(task_id, status="CANCELLED", stage="Cancelled", error="Download cancelled.")
                    await self._edit_task_message(task_id, f"Task cancelled.\nTask: {task_id}", None)
                    self.analyses.pop(task_id, None)
                except Exception as exc:
                    LOG.warning("Download stage failed for task %s (%s)", task_id, type(exc).__name__)
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") not in TERMINAL_STATES:
                        self.db.update_task(task_id, status="FAILED", stage="Failed", error="Download or media processing failed. Check server logs for details.")
                    await self._edit_task_message(
                        task_id,
                        f"Task failed during download or media processing.\nTask: {task_id}\nUse /status for details.",
                        None,
                    )
                    self.analyses.pop(task_id, None)
                finally:
                    if runtime.job_task is stage_task:
                        runtime.job_task = None
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") in TERMINAL_STATES:
                        self.runtimes.pop(task_id, None)
            finally:
                self.queue.task_done()

    async def _upload_worker(self, worker_number: int) -> None:
        while True:
            payload = await self.upload_queue.get()
            task_id = payload.task_id
            try:
                task = self.db.get_task(task_id)
                if not task or task.get("status") != "UPLOAD_QUEUED":
                    continue
                runtime = self.runtimes.setdefault(task_id, TaskRuntime())
                stage_task = asyncio.create_task(self._upload_stage(payload, runtime), name=f"upload-job-{task_id}")
                runtime.job_task = stage_task
                try:
                    await stage_task
                except asyncio.CancelledError:
                    worker_task = asyncio.current_task()
                    if worker_task and worker_task.cancelling():
                        raise
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") not in TERMINAL_STATES:
                        self.db.update_task(task_id, status="CANCELLED", stage="Cancelled", error="Upload cancelled.")
                    await self._edit_task_message(task_id, f"Task cancelled.\nTask: {task_id}", None)
                except Exception as exc:
                    LOG.warning("Upload stage failed for task %s (%s)", task_id, type(exc).__name__)
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") not in TERMINAL_STATES:
                        self.db.update_task(task_id, status="FAILED", stage="Failed", error="Telegram upload failed. The local output may be retained.")
                    await self._edit_task_message(task_id, f"Upload failed. The output may be retained on the bot host.\nTask: {task_id}\nUse /status for details.", None)
                finally:
                    if runtime.job_task is stage_task:
                        runtime.job_task = None
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") in TERMINAL_STATES:
                        self.runtimes.pop(task_id, None)
                        self.analyses.pop(task_id, None)
                        if current.get("status") == "FAILED" and not self.config.keep_failed_files:
                            workdir = current.get("workdir")
                            if workdir:
                                shutil.rmtree(workdir, ignore_errors=True)
            finally:
                self.upload_queue.task_done()

    def _format_selector(self, task: dict[str, Any], info: dict[str, Any]) -> str:
        quality = str(task.get("quality", "best"))
        if quality == "best":
            video, fallback = "bestvideo", "best"
        else:
            height = int(quality)
            video, fallback = f"bestvideo[height<={height}]", f"best[height<={height}]"
        audio = str(task.get("audio_language", "best"))
        if audio != "best" and audio in info.get("audio_languages", []):
            audio_format = f"bestaudio[language^={audio}]"
            return f"{video}+{audio_format}/{video}+bestaudio/{fallback}/best"
        return f"{video}+bestaudio/{fallback}/best"

    @staticmethod
    def _parse_size(text: str) -> int | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)", text, re.I)
        if not match:
            return None
        try:
            value = float(match.group(1))
            unit = match.group(2).lower()
            multipliers = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
                           "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}
            return int(value * multipliers[unit])
        except (ValueError, KeyError):
            return None

    async def _download_process(self, task_id: str, task: dict[str, Any], info: dict[str, Any], runtime: TaskRuntime, workdir: Path) -> Path:
        output_template = str(workdir / "%(title).160B [%(id)s].%(ext)s")
        selector = self._format_selector(task, info)
        cmd = [
            os.sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--no-warnings", "--no-colors",
            "--format", selector, "--max-filesize", f"{self.config.max_upload_size_bytes}B",
            "--merge-output-format", "mkv", "--output", output_template,
            "--write-thumbnail", "--convert-thumbnails", "jpg",
        ]
        cookie_file = self.sources.cookie_file_for_user(int(task["user_id"]))
        if cookie_file:
            cmd += ["--cookies", str(cookie_file)]
        if task.get("subtitles_enabled"):
            language = str(task.get("subtitle_language", "en"))
            if language != "off" and re.fullmatch(r"[a-zA-Z0-9-]{2,12}", language):
                cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", language, "--embed-subs"]
        cmd.append(task["url"])
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(workdir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        runtime.process = process
        last_update = 0.0
        percent_re = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%", re.I)
        speed_re = re.compile(r"\bat\s+([\d.]+\s*(?:B|KiB|MiB|GiB|KB|MB|GB)/s)", re.I)
        eta_re = re.compile(r"\bETA\s+([^\s]+)", re.I)
        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                match = percent_re.search(decoded)
                now = time.monotonic()
                if match:
                    percent = max(0.0, min(100.0, float(match.group(1))))
                    speed_match, eta_match = speed_re.search(decoded), eta_re.search(decoded)
                    total_bytes = self._parse_size(decoded.split(" of ", 1)[1]) if " of " in decoded else None
                    updates: dict[str, Any] = {"progress": percent, "stage": "Downloading"}
                    if speed_match:
                        updates["speed"] = speed_match.group(1).replace(" ", "")
                    if eta_match:
                        updates["eta"] = eta_match.group(1)
                    if total_bytes:
                        updates["total_bytes"] = total_bytes
                        updates["downloaded_bytes"] = int(total_bytes * percent / 100)
                    self.db.update_task(task_id, **updates)
                    if now - last_update >= self.progress_update_interval:
                        task_view = self.db.get_task(task_id) or task
                        await self._edit_task_message(task_id, self.render_task_detail(task_view), self._task_control_keyboard(task_id, task_view))
                        last_update = now
            return_code = await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        finally:
            runtime.process = None
        if return_code != 0:
            raise RuntimeError(f"yt-dlp exited with code {return_code}")
        output_files = [p for p in workdir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
        if not output_files:
            raise RuntimeError("No output media file was produced")
        output_files.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
        return output_files[0]

    async def _download_stage(self, task_id: str, runtime: TaskRuntime) -> None:
        task = self.db.get_task(task_id)
        info = self.analyses.get(task_id)
        if not task or not info:
            raise RuntimeError("Task metadata expired")
        workdir = (self.config.work_dir / task_id).resolve()
        allowed_root = self.config.work_dir.resolve()
        if allowed_root not in workdir.parents:
            raise RuntimeError("Invalid task workspace")
        workdir.mkdir(parents=True, exist_ok=False)
        self.db.update_task(task_id, status="DOWNLOADING", stage="Downloading", progress=0, speed=None, eta=None,
                            workdir=str(workdir), started_at=now_iso())
        current_view = self.db.get_task(task_id) or task
        await self._edit_task_message(task_id, self.render_task_detail(current_view), self._task_control_keyboard(task_id, current_view))
        handed_to_upload = False
        try:
            output_path = await self._download_process(task_id, task, info, runtime, workdir)
            if runtime.cancel_event.is_set():
                raise asyncio.CancelledError
            size = output_path.stat().st_size
            if size <= 0:
                raise RuntimeError("Output media file is empty")
            if size > self.config.max_upload_size_bytes:
                raise RuntimeError("Output media exceeds the configured upload size limit")
            self.db.update_task(task_id, status="PROCESSING", stage="Inspecting media and building caption", size_bytes=size, progress=0, speed=None, eta=None)
            current_view = self.db.get_task(task_id) or task
            await self._edit_task_message(task_id, self.render_task_detail(current_view), self._task_control_keyboard(task_id, current_view))
            task_for_assets = dict(self.db.get_task(task_id) or task)
            task_for_assets["settings"] = self.db.get_settings(int(task["user_id"]))
            assets = await prepare_media_assets(output_path, info, task_for_assets, self.config, workdir)
            final_path = Path(assets["path"])
            final_size = final_path.stat().st_size
            if final_size <= 0 or final_size > self.config.max_upload_size_bytes:
                raise RuntimeError("Processed output is empty or exceeds the configured upload size limit")
            if runtime.cancel_event.is_set():
                raise asyncio.CancelledError
            probe = assets.get("probe") or {}
            self.db.update_task(
                task_id, status="UPLOAD_QUEUED", stage="Waiting for upload worker", progress=100,
                size_bytes=final_size, duration=int(probe.get("duration") or info.get("duration") or 0),
                original_filename=assets.get("original_filename"), physical_filename=assets.get("physical_filename"),
                caption_filename=assets.get("caption_filename"), caption=assets.get("caption"),
                media_metadata=assets.get("metadata"), speed=None, eta=None,
            )
            self.upload_queue.put_nowait(UploadPayload(task_id, final_path, info, assets))
            handed_to_upload = True
            refreshed = self.db.get_task(task_id) or task
            await self._edit_task_message(task_id, self.render_task_detail(refreshed), self._task_control_keyboard(task_id, refreshed))
        finally:
            current = self.db.get_task(task_id) or {}
            # Keep the workspace while waiting for or performing upload. For failures/cancellations,
            # retention is controlled by KEEP_FAILED_FILES.
            if not handed_to_upload and current.get("status") != "COMPLETED" and not self.config.keep_failed_files:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _upload_stage(self, payload: UploadPayload, runtime: TaskRuntime) -> None:
        task_id, output_path, assets = payload.task_id, payload.output_path, payload.assets
        task = self.db.get_task(task_id)
        if not task:
            raise RuntimeError("Task was removed")
        self.db.update_task(task_id, status="UPLOADING", stage="Uploading to Telegram", progress=0, speed=None, eta=None)
        await self._edit_task_message(task_id, self.render_task_detail(self.db.get_task(task_id) or task), self._task_control_keyboard(task_id, task))
        uploaded_ok = False
        try:
            if runtime.cancel_event.is_set():
                raise asyncio.CancelledError
            source_thumb = Path(assets["thumbnail"]) if assets.get("thumbnail") else None
            thumb_path = await prepare_telegram_thumbnail(source_thumb, output_path.parent / "telegram_thumb.jpg")
            destination = self.config.destination_chat_id or int(task["user_id"])
            upload_started = time.monotonic()
            last_upload_update = 0.0

            async def upload_progress(current: int, total: int, progress_task_id: str) -> None:
                nonlocal last_upload_update
                now = time.monotonic()
                if total <= 0:
                    return
                percent = min(100.0, max(0.0, current * 100.0 / total))
                elapsed = max(0.1, now - upload_started)
                bytes_per_second = current / elapsed
                speed_text = f"{bytes_per_second / 1024**2:.2f} MiB/s" if bytes_per_second >= 1024**2 else f"{bytes_per_second / 1024:.1f} KiB/s"
                eta_seconds = max(0, int((total - current) / bytes_per_second)) if bytes_per_second > 0 else 0
                eta_text = f"{eta_seconds // 60:02d}:{eta_seconds % 60:02d}" if bytes_per_second > 0 else "Unknown"
                self.db.update_task(progress_task_id, progress=percent, speed=speed_text, eta=eta_text, total_bytes=total,
                                    downloaded_bytes=current, stage="Uploading to Telegram")
                if now - last_upload_update >= self.progress_update_interval or current >= total:
                    view = self.db.get_task(progress_task_id)
                    if view:
                        await self._edit_task_message(progress_task_id, self.render_task_detail(view), self._task_control_keyboard(progress_task_id, view))
                    last_upload_update = now

            sent = await self.app.send_video(
                chat_id=destination,
                video=str(output_path),
                caption=assets.get("caption") or f"{task.get('title') or output_path.stem}\nSource: {task.get('source', 'Unknown')}\nTask: {task_id}",
                supports_streaming=True,
                thumb=str(thumb_path) if thumb_path else None,
                duration=int((assets.get("probe") or {}).get("duration") or task.get("duration") or 0) or None,
                parse_mode=None,
                progress=upload_progress,
                progress_args=(task_id,),
            )
            output_ref = f"telegram://{destination}/{sent.id}"
            self.db.update_task(task_id, status="COMPLETED", stage="Completed", progress=100, output_url=output_ref, error=None, speed=None, eta=None)
            uploaded_ok = True
            completed = self.db.get_task(task_id) or task
            await self._edit_task_message(
                task_id,
                self.render_task_detail(completed) + f"\n\nUploaded Telegram message: {sent.id}",
                self._task_control_keyboard(task_id, completed),
            )
        finally:
            # Delete task workspace only after confirmed upload, or if retention is disabled.
            current = self.db.get_task(task_id) or {}
            if uploaded_ok or not self.config.keep_failed_files:
                shutil.rmtree(output_path.parent, ignore_errors=True)

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        task = self.db.get_task(task_id)
        if not task or int(task["user_id"]) != int(user_id):
            return False
        if task.get("status") in TERMINAL_STATES:
            return False
        self.db.update_task(task_id, status="CANCELLED", stage="Cancelled", error="Cancelled by user.", speed=None, eta=None)
        runtime = self.runtimes.get(task_id)
        active_job = bool(runtime and runtime.job_task and not runtime.job_task.done())
        if runtime:
            runtime.cancel_event.set()
            if runtime.process and runtime.process.returncode is None:
                runtime.process.terminate()
            if active_job:
                runtime.job_task.cancel()
        if not active_job:
            self.analyses.pop(task_id, None)
            self.runtimes.pop(task_id, None)
        if not self.config.keep_failed_files:
            workdir = task.get("workdir")
            if workdir and not active_job:
                shutil.rmtree(workdir, ignore_errors=True)
        await self._edit_task_message(task_id, self.render_task_detail(self.db.get_task(task_id) or task), None)
        return True

    def queue_position(self, task_id: str, queue_name: str = "download") -> int | None:
        queue = self.queue if queue_name == "download" else self.upload_queue
        try:
            for i, item in enumerate(list(queue._queue), start=1):  # asyncio.Queue has no public peek API.
                queued_id = item if isinstance(item, str) else item.task_id
                if queued_id == task_id:
                    return i
        except Exception:
            return None
        return None

    @staticmethod
    def _elapsed(started_at: str | None) -> str:
        if not started_at:
            return "—"
        try:
            start = datetime.fromisoformat(started_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            seconds = max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
        except (ValueError, TypeError):
            return "—"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"

    @staticmethod
    def _progress_bar(progress: float, width: int = 12) -> str:
        filled = max(0, min(width, round(progress / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    def render_task_detail(self, task: dict[str, Any]) -> str:
        status = str(task.get("status") or "UNKNOWN")
        title = str(task.get("title") or task.get("source") or "Media")[:100]
        lines = [f"{title}", f"Task: {task.get('task_id', '—')} | {status.replace('_', ' ')}", f"Stage: {task.get('stage') or status.replace('_', ' ').title()}"]
        if status in {"DOWNLOADING", "UPLOADING", "COMPLETED"}:
            progress = float(task.get("progress") or 0)
            lines.append(f"{self._progress_bar(progress)} {progress:.1f}%")
        elif status == "UPLOAD_QUEUED":
            lines.append("Download completed; waiting for upload worker")
        queue_status = self.queue_position(str(task.get("task_id")), "download")
        upload_status = self.queue_position(str(task.get("task_id")), "upload")
        if status == "QUEUED":
            lines.append(f"Download queue position: {queue_status or 'Starting'}")
        elif status == "UPLOAD_QUEUED":
            lines.append(f"Upload queue position: {upload_status or 'Starting'}")
        if task.get("speed"):
            lines.append(f"Speed: {task['speed']}")
        if task.get("eta"):
            lines.append(f"ETA: {task['eta']}")
        size = task.get("size_bytes")
        if size:
            lines.append(f"Size: {size / 1024**2:.1f} MiB")
        elif task.get("total_bytes"):
            current_bytes = int(task.get("downloaded_bytes") or 0)
            lines.append(f"Transferred: {current_bytes / 1024**2:.1f} / {int(task['total_bytes']) / 1024**2:.1f} MiB")
        elapsed = self._elapsed(task.get("started_at"))
        if elapsed != "—":
            lines.append(f"Elapsed: {elapsed}")
        if status == "FAILED" and task.get("error"):
            lines.append(f"Reason: {str(task['error'])[:250]}")
        if status == "COMPLETED" and task.get("output_url"):
            lines.append(f"Result: {task['output_url']}")
        return "\n".join(lines)

    def _task_control_keyboard(self, task_id: str, task: dict[str, Any]) -> InlineKeyboardMarkup | None:
        if task.get("status") in self.db.ACTIVE_STATES:
            return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel task", callback_data=f"st:cancel:{task_id}")]])
        return None

    def render_status_view(self, user_id: int, page: int = 0, status_filter: str = "all") -> tuple[str, InlineKeyboardMarkup]:
        if status_filter not in STATUS_FILTERS:
            status_filter = "all"
        total = self.db.count_user_tasks(user_id, status_filter)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        tasks = self.db.list_user_tasks(user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE, status=status_filter)
        label = {"all": "All", "active": "Active", "failed": "Failed", "completed": "Completed"}[status_filter]
        lines = [f"Your tasks · {label} · page {page + 1}/{total_pages}", f"Tasks matching filter: {total}"]
        rows: list[list[InlineKeyboardButton]] = []
        filter_buttons = []
        for value in STATUS_FILTERS:
            name = {"all": "All", "active": "Active", "failed": "Failed", "completed": "Done"}[value]
            if value == status_filter:
                name = f"✓ {name}"
            filter_buttons.append(InlineKeyboardButton(name, callback_data=f"st:filter:{value}:0"))
        rows.append(filter_buttons)
        for task in tasks:
            lines.extend(["", self.render_task_detail(task)])
            if task.get("status") in self.db.ACTIVE_STATES:
                rows.append([InlineKeyboardButton(f"Cancel {task['task_id']}", callback_data=f"st:cancel:{task['task_id']}:{page}:{status_filter}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"st:page:{page - 1}:{status_filter}"))
        nav.append(InlineKeyboardButton("Refresh", callback_data=f"st:refresh:{page}:{status_filter}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Next ›", callback_data=f"st:page:{page + 1}:{status_filter}"))
        rows.append(nav)
        if not tasks:
            lines.append("\nNo tasks in this view. Use /download URL to start one.")
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def handle_status_callback(self, callback) -> None:
        if not callback.from_user:
            await callback.answer("Invalid user.", show_alert=True)
            return
        user_id = int(callback.from_user.id)
        if self.config.allowed_user_ids and user_id not in self.config.allowed_user_ids:
            await callback.answer("You are not authorized to use this bot.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
            await callback.answer("Invalid status action.", show_alert=True)
            return
        action = parts[1]
        if action == "cancel" and len(parts) >= 3:
            task_id = parts[2]
            page = 0
            status_filter = "all"
            if len(parts) >= 5:
                try:
                    page, status_filter = int(parts[3]), parts[4]
                except ValueError:
                    page, status_filter = 0, "all"
            cancelled = await self.cancel_task(task_id, user_id)
            await callback.answer("Cancellation requested." if cancelled else "Task not found or already finished.")
            # A status-panel cancellation must refresh that panel. A task-progress
            # cancellation should keep the task message showing its terminal state.
            if len(parts) >= 5:
                try:
                    text, keyboard = self.render_status_view(user_id, page, status_filter)
                    await callback.message.edit_text(text, reply_markup=keyboard)
                except MessageNotModified:
                    pass
                except Exception as exc:
                    LOG.debug("Status panel refresh failed (%s)", type(exc).__name__)
            return
        try:
            if action in {"filter", "refresh", "page"}:
                if action == "filter":
                    filter_value, page = parts[2], int(parts[3] or 0)
                elif action == "refresh":
                    page, filter_value = int(parts[2]), parts[3]
                else:
                    page, filter_value = int(parts[2]), parts[3]
                text, keyboard = self.render_status_view(user_id, page, filter_value)
                try:
                    await callback.message.edit_text(text, reply_markup=keyboard)
                except MessageNotModified:
                    pass
                await callback.answer("Updated.")
                return
        except (ValueError, IndexError):
            await callback.answer("Invalid status view.", show_alert=True)
            return
        await callback.answer("Unknown status action.", show_alert=True)

    async def _edit_task_message(self, task_id: str, text: str, reply_markup) -> None:
        task = self.db.get_task(task_id) or {}
        chat_id, message_id = task.get("message_chat_id"), task.get("message_id")
        if not chat_id or not message_id:
            return
        try:
            await self.app.edit_message_text(int(chat_id), int(message_id), text, reply_markup=reply_markup)
        except MessageNotModified:
            return
        except Exception as exc:
            LOG.debug("Task message edit failed for %s (%s)", task_id, type(exc).__name__)
