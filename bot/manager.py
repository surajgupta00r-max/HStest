from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Config
from .database import Database
from .source import SourceError, SourceManager, detect_source

LOG = logging.getLogger(__name__)
MEDIA_SUFFIXES = {".mkv", ".mp4", ".webm", ".mov", ".m4v", ".avi"}
LANG_LABELS = {
    "en": "English", "eng": "English", "hi": "Hindi", "hin": "Hindi",
    "ta": "Tamil", "tam": "Tamil", "te": "Telugu", "tel": "Telugu",
    "ml": "Malayalam", "mal": "Malayalam", "kn": "Kannada", "kan": "Kannada",
    "mr": "Marathi", "mar": "Marathi", "bn": "Bengali", "ben": "Bengali",
}


class UserTaskError(RuntimeError):
    pass


class TaskRuntime:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.job_task: asyncio.Task | None = None
        self.cancel_event = asyncio.Event()


class TaskManager:
    def __init__(self, app, config: Config, db: Database):
        self.app = app
        self.config = config
        self.db = db
        self.sources = SourceManager(config)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.runtimes: dict[str, TaskRuntime] = {}
        self.analyses: dict[str, dict[str, Any]] = {}
        self.user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.progress_update_interval = 3.0

    async def start(self) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        interrupted = self.db.mark_interrupted_tasks()
        if interrupted:
            LOG.info("Marked %s unfinished tasks as interrupted", interrupted)
        self.workers = [asyncio.create_task(self._worker(i + 1), name=f"download-worker-{i + 1}")
                        for i in range(self.config.max_workers)]
        LOG.info("Started %s worker(s)", len(self.workers))

    async def stop(self) -> None:
        for runtime in list(self.runtimes.values()):
            runtime.cancel_event.set()
            if runtime.process and runtime.process.returncode is None:
                runtime.process.terminate()
            if runtime.job_task and not runtime.job_task.done():
                runtime.job_task.cancel()
        for worker in self.workers:
            worker.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
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
                await message.reply_text(
                    "You already have an active task. Use /status to check it or /cancel TASK_ID to cancel it."
                )
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
                "title": "Analyzing link", "status": "ANALYZING", "progress": 0,
                "quality": settings.get("quality", "best"),
                "audio_language": settings.get("audio_language", "best"),
                "subtitles_enabled": bool(settings.get("subtitles_enabled", False)),
                "subtitle_language": settings.get("subtitle_language", "en"),
                "error": None, "output_url": None, "workdir": None,
            })
            status_msg = await message.reply_text(f"Analyzing {source.name} link…\nTask: {task_id}")
            self.db.update_task(task_id, message_chat_id=int(status_msg.chat.id), message_id=int(status_msg.id))
            try:
                info = await self.sources.analyze(url)
                # A /cancel command may arrive while extraction runs in a worker thread.
                latest = self.db.get_task(task_id) or {}
                if latest.get("status") != "ANALYZING":
                    return None
                self.analyses[task_id] = info
                updated_settings = self.db.get_settings(user_id)
                available_audio = info.get("audio_languages", [])
                desired_audio = updated_settings.get("audio_language", "best")
                if desired_audio != "best" and desired_audio not in available_audio:
                    desired_audio = "best"
                available_subs = info.get("subtitle_languages", [])
                desired_sub = str(updated_settings.get("subtitle_language", "en")).lower()
                subs_enabled = bool(updated_settings.get("subtitles_enabled", False))
                if desired_sub not in available_subs:
                    desired_sub = available_subs[0] if (subs_enabled and available_subs) else "off"
                if not available_subs:
                    subs_enabled = False
                current = self.db.get_task(task_id) or {}
                desired_quality = str(current.get("quality", "best"))
                if desired_quality != "best" and (not desired_quality.isdigit()):
                    desired_quality = "best"
                self.db.update_task(
                    task_id, title=info["title"], status="WAITING_FOR_SELECTION",
                    quality=desired_quality, audio_language=desired_audio,
                    subtitles_enabled=subs_enabled, subtitle_language=desired_sub if subs_enabled else "off",
                )
                await self._edit_task_message(task_id, self._selection_text(task_id), self.selection_keyboard(task_id))
                return task_id
            except asyncio.CancelledError:
                self.db.update_task(task_id, status="CANCELLED", error="Cancelled during analysis.")
                raise
            except Exception as exc:
                # Avoid exposing raw extractor exceptions, which may contain signed URLs or private query parameters.
                LOG.warning("Task %s analysis failed (%s)", task_id, type(exc).__name__)
                self.db.update_task(task_id, status="FAILED", error="Link analysis failed or this item is not supported by the installed yt-dlp extractor.")
                await self._edit_task_message(
                    task_id,
                    f"Could not analyze this link.\nTask: {task_id}\n\nThe installed yt-dlp extractor may not support this item, or it may be DRM-protected. This bot does not bypass DRM or access controls.",
                    None,
                )
                self.analyses.pop(task_id, None)
                return None

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
        details = [
            "Download options",
            f"Title: {info.get('title') or task.get('title') or 'Unknown'}",
            f"Source: {task.get('source', 'Unknown')}",
            f"Quality: {quality}",
            f"Audio preference: {audio_text}",
            f"Subtitles: {subs}",
            f"Task: {task_id}",
            "\nSelect options below, then press Start download.",
        ]
        return "\n".join(details)

    async def handle_download_callback(self, callback) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
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
        if parts[2] == "cancel":
            cancelled = await self.cancel_task(task_id, int(callback.from_user.id))
            await callback.answer("Task cancelled." if cancelled else "Task is already finished or cancelled.")
            return
        if parts[2] in {"q", "a", "s"}:
            if task.get("status") != "WAITING_FOR_SELECTION" or task_id not in self.analyses:
                await callback.answer("These options have expired. Submit the URL again.", show_alert=True)
                return
            if len(parts) != 4:
                await callback.answer("Invalid selection.", show_alert=True)
                return
            value = parts[3]
            info = self.analyses[task_id]
            if parts[2] == "q":
                if value not in self._quality_choices(info):
                    await callback.answer("That quality is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, quality=value)
            elif parts[2] == "a":
                if value not in (["best"] + info.get("audio_languages", [])[:4]):
                    await callback.answer("That audio preference is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, audio_language=value)
            else:
                if value not in (["off"] + info.get("subtitle_languages", [])[:5]):
                    await callback.answer("That subtitle language is not available.", show_alert=True)
                    return
                self.db.update_task(task_id, subtitles_enabled=(value != "off"), subtitle_language=value)
            await callback.message.edit_text(self._selection_text(task_id), reply_markup=self.selection_keyboard(task_id))
            await callback.answer("Selection updated.")
            return
        if parts[2] == "start":
            if task.get("status") != "WAITING_FOR_SELECTION" or task_id not in self.analyses:
                await callback.answer("This task is no longer waiting for selection.", show_alert=True)
                return
            self.db.update_task(task_id, status="QUEUED", progress=0, error=None)
            self.queue.put_nowait(task_id)
            pos = self.queue.qsize()
            await callback.message.edit_text(
                f"Task queued\nTitle: {task.get('title') or 'Media'}\nTask: {task_id}\nQueue position: about {pos}",
                reply_markup=None,
            )
            await callback.answer("Added to the download queue.")
            return
        await callback.answer("Unknown action.", show_alert=True)

    async def _worker(self, worker_number: int) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                task = self.db.get_task(task_id)
                if not task or task.get("status") != "QUEUED":
                    continue
                runtime = TaskRuntime()
                self.runtimes[task_id] = runtime
                runtime.job_task = asyncio.create_task(self._process(task_id, runtime), name=f"job-{task_id}")
                try:
                    await runtime.job_task
                except asyncio.CancelledError:
                    current = self.db.get_task(task_id) or {}
                    if current.get("status") != "CANCELLED":
                        self.db.update_task(task_id, status="CANCELLED", error="Cancelled by user.")
                    await self._edit_task_message(task_id, f"Task cancelled.\nTask: {task_id}", None)
                except Exception as exc:
                    LOG.warning("Task %s failed (%s)", task_id, type(exc).__name__)
                    self.db.update_task(task_id, status="FAILED", error="Processing or upload failed.")
                    await self._edit_task_message(
                        task_id,
                        f"Task failed. The local output may be retained on the bot host.\nTask: {task_id}\nUse /status for details.",
                        None,
                    )
                finally:
                    self.runtimes.pop(task_id, None)
            finally:
                self.queue.task_done()

    def _format_selector(self, task: dict[str, Any], info: dict[str, Any]) -> str:
        quality = str(task.get("quality", "best"))
        if quality == "best":
            video = "bestvideo"
            fallback = "best"
        else:
            height = int(quality)
            video = f"bestvideo[height<={height}]"
            fallback = f"best[height<={height}]"
        audio = str(task.get("audio_language", "best"))
        if audio != "best" and audio in info.get("audio_languages", []):
            audio_format = f"bestaudio[language^={audio}]"
            return f"{video}+{audio_format}/{video}+bestaudio/{fallback}/best"
        return f"{video}+bestaudio/{fallback}/best"

    async def _download_process(self, task_id: str, task: dict[str, Any], info: dict[str, Any], runtime: TaskRuntime, workdir: Path) -> Path:
        output_template = str(workdir / "%(title).160B [%(id)s].%(ext)s")
        selector = self._format_selector(task, info)
        cmd = [
            os.sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--newline", "--no-warnings",
            "--format", selector,
            "--max-filesize", f"{self.config.max_upload_size_bytes}B",
            "--merge-output-format", "mkv",
            "--output", output_template,
            "--write-thumbnail", "--convert-thumbnails", "jpg",
        ]
        if self.config.cookie_file:
            cmd += ["--cookies", str(self.config.cookie_file)]
        if task.get("subtitles_enabled"):
            language = str(task.get("subtitle_language", "en"))
            if language != "off" and re.fullmatch(r"[a-zA-Z0-9-]{2,12}", language):
                cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", language, "--embed-subs"]
        cmd.append(task["url"])
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        runtime.process = process
        last_update = 0.0
        percent_re = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%", re.I)
        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                match = percent_re.search(decoded)
                now = time.monotonic()
                if match and now - last_update >= self.progress_update_interval:
                    percent = max(0.0, min(100.0, float(match.group(1))))
                    self.db.update_task(task_id, progress=percent)
                    speed_match = re.search(r"\bat\s+([^\s]+)", decoded, re.I)
                    eta_match = re.search(r"\bETA\s+([^\s]+)", decoded, re.I)
                    speed = speed_match.group(1) if speed_match else "Unknown"
                    eta = eta_match.group(1) if eta_match else "Unknown"
                    await self._edit_task_message(
                        task_id,
                        f"Downloading\nTitle: {task.get('title') or 'Media'}\nProgress: {percent:.1f}%\nSpeed: {speed}\nETA: {eta}\nTask: {task_id}",
                        None,
                    )
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
        # Prefer the newest/largest valid media file within this task's own workspace.
        output_files.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
        return output_files[0]

    async def _process(self, task_id: str, runtime: TaskRuntime) -> None:
        task = self.db.get_task(task_id)
        info = self.analyses.get(task_id)
        if not task or not info:
            raise RuntimeError("Task metadata expired")
        workdir = (self.config.work_dir / task_id).resolve()
        allowed_root = self.config.work_dir.resolve()
        if allowed_root not in workdir.parents:
            raise RuntimeError("Invalid task workspace")
        workdir.mkdir(parents=True, exist_ok=False)
        self.db.update_task(task_id, status="DOWNLOADING", progress=0, workdir=str(workdir))
        await self._edit_task_message(
            task_id,
            f"Download started\nTitle: {task.get('title') or 'Media'}\nTask: {task_id}\nProgress: 0%",
            None,
        )
        uploaded_ok = False
        try:
            output_path = await self._download_process(task_id, task, info, runtime, workdir)
            size = output_path.stat().st_size
            if size <= 0:
                raise RuntimeError("Output file is empty")
            if size > self.config.max_upload_size_bytes:
                raise RuntimeError("Output exceeds configured Telegram upload size limit")
            thumb_candidates = [p for p in workdir.glob("*.jpg") if p.is_file() and p.stat().st_size <= 200 * 1024]
            thumb = max(thumb_candidates, key=lambda p: p.stat().st_size, default=None)
            destination = self.config.destination_chat_id or int(task["user_id"])
            self.db.update_task(task_id, status="UPLOADING", progress=100)
            await self._edit_task_message(task_id, f"Uploading to Telegram\nTitle: {task.get('title') or output_path.stem}\nSize: {size / 1024**2:.1f} MiB\nTask: {task_id}", None)
            sent = await self.app.send_video(
                chat_id=destination,
                video=str(output_path),
                caption=f"{task.get('title') or output_path.stem}\nSource: {task.get('source', 'Unknown')}\nTask: {task_id}",
                supports_streaming=True,
                thumb=str(thumb) if thumb else None,
                duration=int(info.get("duration") or 0) or None,
                parse_mode=None,
            )
            output_ref = f"telegram://{destination}/{sent.id}"
            self.db.update_task(task_id, status="COMPLETED", progress=100, output_url=output_ref, error=None)
            uploaded_ok = True
            await self._edit_task_message(
                task_id,
                f"Completed\nTitle: {task.get('title') or output_path.stem}\nSize: {size / 1024**2:.1f} MiB\nUploaded message ID: {sent.id}\nTask: {task_id}",
                None,
            )
        finally:
            self.analyses.pop(task_id, None)
            # Successful output is removed only after Telegram confirms the upload.
            # Failed outputs are retained by default for diagnosis/retry.
            if uploaded_ok or not self.config.keep_failed_files:
                try:
                    shutil.rmtree(workdir, ignore_errors=True)
                except Exception:
                    LOG.warning("Could not clean workspace for task %s", task_id)

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        task = self.db.get_task(task_id)
        if not task or int(task["user_id"]) != int(user_id):
            return False
        if task.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
            return False
        self.db.update_task(task_id, status="CANCELLED", error="Cancelled by user.")
        runtime = self.runtimes.get(task_id)
        if runtime:
            runtime.cancel_event.set()
            if runtime.process and runtime.process.returncode is None:
                runtime.process.terminate()
            if runtime.job_task and not runtime.job_task.done():
                runtime.job_task.cancel()
        else:
            self.analyses.pop(task_id, None)
            workdir_text = task.get("workdir")
            if workdir_text and not self.config.keep_failed_files:
                shutil.rmtree(workdir_text, ignore_errors=True)
        await self._edit_task_message(task_id, f"Task cancelled.\nTask: {task_id}", None)
        return True

    def render_status(self, user_id: int) -> str:
        tasks = self.db.list_user_tasks(user_id, limit=10)
        if not tasks:
            return "No tasks found. Use /download URL to start one."
        lines = ["Your recent tasks"]
        for task in tasks:
            status = task.get("status", "UNKNOWN")
            title = task.get("title") or task.get("source") or "Media"
            progress = float(task.get("progress") or 0)
            lines.extend([
                f"\n{task['task_id']} — {status}",
                str(title)[:90],
                f"Progress: {progress:.1f}%" if status in {"DOWNLOADING", "UPLOADING", "COMPLETED"} else "",
                f"Error: {task.get('error')}" if status == "FAILED" and task.get("error") else "",
            ])
        return "\n".join(line for line in lines if line != "")

    async def _edit_task_message(self, task_id: str, text: str, reply_markup) -> None:
        task = self.db.get_task(task_id) or {}
        chat_id, message_id = task.get("message_chat_id"), task.get("message_id")
        if not chat_id or not message_id:
            return
        try:
            await self.app.edit_message_text(int(chat_id), int(message_id), text, reply_markup=reply_markup)
        except Exception:
            # Edits may fail if content has not changed or message is no longer editable.
            pass
