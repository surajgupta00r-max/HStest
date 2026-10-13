from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_SETTINGS: dict[str, Any] = {
    "quality": "best",
    "subtitles_enabled": False,
    "subtitle_language": "en",
    "audio_language": "best",
    "metadata_provider": "tmdb",
    "episode_dates": True,
    "naming_style": "detailed",
    "smart_caption_version": "v2",
    "thumbnail_mode": "auto",
    "embed_thumbnail": False,
    "custom_thumbnail_url": "",
}

TASK_COLUMNS: dict[str, str] = {
    "stage": "TEXT NOT NULL DEFAULT 'Queued'",
    "speed": "TEXT",
    "eta": "TEXT",
    "size_bytes": "INTEGER",
    "total_bytes": "INTEGER",
    "downloaded_bytes": "INTEGER",
    "duration": "INTEGER",
    "original_filename": "TEXT",
    "physical_filename": "TEXT",
    "caption_filename": "TEXT",
    "caption": "TEXT",
    "media_metadata": "TEXT",
    "started_at": "TEXT",
}


class Database:
    """MongoDB when MONGO_URI is configured; otherwise SQLite for simple deployment."""

    ACTIVE_STATES = [
        "ANALYZING", "WAITING_FOR_SELECTION", "QUEUED", "DOWNLOADING", "PROCESSING",
        "UPLOAD_QUEUED", "UPLOADING",
    ]
    TERMINAL_STATES = ["COMPLETED", "FAILED", "CANCELLED"]

    def __init__(self, config):
        self._mongo = None
        self._sqlite_path: Path | None = None
        if config.mongo_uri:
            try:
                from pymongo import MongoClient
                self._mongo = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=5000)
                self._mongo.admin.command("ping")
                database = self._mongo[config.mongo_database]
                self.users = database["users"]
                self.tasks = database["tasks"]
                self.tasks.create_index("task_id", unique=True)
                self.tasks.create_index([("user_id", 1), ("created_at", -1)])
                self.tasks.create_index([("status", 1)])
                self.users.create_index("user_id", unique=True)
                self.backend = "MongoDB"
                return
            except Exception:
                if self._mongo is not None:
                    self._mongo.close()
                self._mongo = None
                raise RuntimeError(
                    "MONGO_URI is configured but MongoDB could not be reached. Check dependency installation, "
                    "network access, credentials, and IP access-list settings."
                ) from None

        self._sqlite_path = config.sqlite_path
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.backend = "SQLite"
        self._initialize_sqlite()

    def _connect(self):
        assert self._sqlite_path is not None
        con = sqlite3.connect(self._sqlite_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextmanager
    def _connection(self):
        con = self._connect()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _initialize_sqlite(self) -> None:
        with self._connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    settings TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    quality TEXT NOT NULL DEFAULT 'best',
                    audio_language TEXT NOT NULL DEFAULT 'best',
                    subtitles_enabled INTEGER NOT NULL DEFAULT 0,
                    subtitle_language TEXT NOT NULL DEFAULT 'en',
                    error TEXT,
                    output_url TEXT,
                    workdir TEXT,
                    message_chat_id INTEGER,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            """)
            existing = {row["name"] for row in con.execute("PRAGMA table_info(tasks)").fetchall()}
            for column, definition in TASK_COLUMNS.items():
                if column not in existing:
                    con.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            # Legacy rows receive the new column's default during ALTER TABLE. Replace that
            # placeholder with a useful stage label derived from the already-persisted state.
            con.execute("""
                UPDATE tasks
                SET stage = CASE status
                    WHEN 'COMPLETED' THEN 'Completed'
                    WHEN 'FAILED' THEN 'Failed'
                    WHEN 'CANCELLED' THEN 'Cancelled'
                    ELSE REPLACE(status, '_', ' ')
                END
                WHERE stage = 'Queued' AND status != 'QUEUED'
            """)

    @staticmethod
    def _decode_task(result: dict[str, Any]) -> dict[str, Any]:
        if "subtitles_enabled" in result:
            result["subtitles_enabled"] = bool(result["subtitles_enabled"])
        metadata = result.get("media_metadata")
        if isinstance(metadata, str):
            try:
                result["media_metadata"] = json.loads(metadata)
            except json.JSONDecodeError:
                result["media_metadata"] = {}
        return result

    def register_user(self, user_id: int, username: str | None) -> None:
        timestamp = now_iso()
        if self._mongo is not None:
            self.users.update_one(
                {"user_id": int(user_id)},
                {"$set": {"username": username, "updated_at": timestamp},
                 "$setOnInsert": {"settings": DEFAULT_SETTINGS.copy(), "created_at": timestamp}},
                upsert=True,
            )
            return
        with self._connection() as con:
            row = con.execute("SELECT user_id FROM users WHERE user_id=?", (int(user_id),)).fetchone()
            if row:
                con.execute("UPDATE users SET username=?, updated_at=? WHERE user_id=?", (username, timestamp, int(user_id)))
            else:
                con.execute("INSERT INTO users(user_id,username,settings,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (int(user_id), username, json.dumps(DEFAULT_SETTINGS), timestamp, timestamp))

    def get_settings(self, user_id: int) -> dict[str, Any]:
        if self._mongo is not None:
            doc = self.users.find_one({"user_id": int(user_id)}) or {}
            return {**DEFAULT_SETTINGS, **(doc.get("settings") or {})}
        with self._connection() as con:
            row = con.execute("SELECT settings FROM users WHERE user_id=?", (int(user_id),)).fetchone()
            return {**DEFAULT_SETTINGS, **(json.loads(row["settings"]) if row else {})}

    def update_setting(self, user_id: int, key: str, value: Any) -> dict[str, Any]:
        if key not in DEFAULT_SETTINGS:
            raise ValueError("Unsupported setting.")
        settings = self.get_settings(user_id)
        settings[key] = value
        timestamp = now_iso()
        if self._mongo is not None:
            self.users.update_one({"user_id": int(user_id)}, {"$set": {f"settings.{key}": value, "updated_at": timestamp}}, upsert=True)
        else:
            with self._connection() as con:
                con.execute("UPDATE users SET settings=?,updated_at=? WHERE user_id=?",
                            (json.dumps(settings), timestamp, int(user_id)))
        return settings

    def create_task(self, task: dict[str, Any]) -> None:
        task = {**task, "created_at": now_iso(), "updated_at": now_iso()}
        task.setdefault("stage", task.get("status", "Queued").replace("_", " ").title())
        if self._mongo is not None:
            self.tasks.insert_one(task)
            return
        names = [
            "task_id", "user_id", "url", "source", "title", "status", "progress", "quality",
            "audio_language", "subtitles_enabled", "subtitle_language", "error", "output_url", "workdir",
            "message_chat_id", "message_id", "created_at", "updated_at", *TASK_COLUMNS.keys(),
        ]
        values = []
        for key in names:
            value = task.get(key)
            if key == "subtitles_enabled":
                value = int(bool(value))
            elif key == "media_metadata" and isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        columns = ",".join(names)
        placeholders = ",".join("?" for _ in names)
        with self._connection() as con:
            con.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", values)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if self._mongo is not None:
            doc = self.tasks.find_one({"task_id": task_id}, {"_id": 0})
            return self._decode_task(doc) if doc else None
        with self._connection() as con:
            row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._decode_task(dict(row)) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        if self._mongo is not None:
            self.tasks.update_one({"task_id": task_id}, {"$set": fields})
            return
        allowed = {
            "url", "source", "title", "status", "progress", "quality", "audio_language",
            "subtitles_enabled", "subtitle_language", "error", "output_url", "workdir",
            "message_chat_id", "message_id", *TASK_COLUMNS.keys(),
        }
        items = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "subtitles_enabled":
                value = int(bool(value))
            elif key == "media_metadata" and isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            items.append((key, value))
        items.append(("updated_at", fields["updated_at"]))
        with self._connection() as con:
            con.execute(f"UPDATE tasks SET {', '.join(f'{key}=?' for key, _ in items)} WHERE task_id=?",
                        [value for _, value in items] + [task_id])

    def list_user_tasks(self, user_id: int, limit: int = 10, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        if self._mongo is not None:
            query: dict[str, Any] = {"user_id": int(user_id)}
            if status == "active":
                query["status"] = {"$in": self.ACTIVE_STATES}
            elif status == "failed":
                query["status"] = "FAILED"
            elif status == "completed":
                query["status"] = "COMPLETED"
            docs = self.tasks.find(query, {"_id": 0}).sort("created_at", -1).skip(max(0, offset)).limit(max(1, limit))
            return [self._decode_task(dict(doc)) for doc in docs]

        where = "user_id=?"
        params: list[Any] = [int(user_id)]
        if status == "active":
            placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
            where += f" AND status IN ({placeholders})"
            params.extend(self.ACTIVE_STATES)
        elif status == "failed":
            where += " AND status='FAILED'"
        elif status == "completed":
            where += " AND status='COMPLETED'"
        params.extend([max(1, min(50, int(limit))), max(0, int(offset))])
        with self._connection() as con:
            rows = con.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params).fetchall()
            return [self._decode_task(dict(row)) for row in rows]

    def count_active_tasks(self, user_id: int) -> int:
        if self._mongo is not None:
            return self.tasks.count_documents({"user_id": int(user_id), "status": {"$in": self.ACTIVE_STATES}})
        placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
        with self._connection() as con:
            row = con.execute(f"SELECT COUNT(*) AS n FROM tasks WHERE user_id=? AND status IN ({placeholders})",
                              [int(user_id), *self.ACTIVE_STATES]).fetchone()
            return int(row["n"])

    def count_user_tasks(self, user_id: int, status: str | None = None) -> int:
        if self._mongo is not None:
            query: dict[str, Any] = {"user_id": int(user_id)}
            if status == "active":
                query["status"] = {"$in": self.ACTIVE_STATES}
            elif status == "failed":
                query["status"] = "FAILED"
            elif status == "completed":
                query["status"] = "COMPLETED"
            return int(self.tasks.count_documents(query))
        where, params = "user_id=?", [int(user_id)]
        if status == "active":
            placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
            where += f" AND status IN ({placeholders})"
            params.extend(self.ACTIVE_STATES)
        elif status == "failed":
            where += " AND status='FAILED'"
        elif status == "completed":
            where += " AND status='COMPLETED'"
        with self._connection() as con:
            row = con.execute(f"SELECT COUNT(*) AS n FROM tasks WHERE {where}", params).fetchone()
            return int(row["n"])

    def mark_interrupted_tasks(self) -> int:
        """Unfinished runtime stages cannot be resumed by this single-process queue."""
        timestamp = now_iso()
        if self._mongo is not None:
            result = self.tasks.update_many(
                {"status": {"$in": self.ACTIVE_STATES}},
                {"$set": {"status": "FAILED", "stage": "Interrupted", "error": "Bot restarted; submit the URL again.", "updated_at": timestamp}},
            )
            return int(result.modified_count)
        placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
        with self._connection() as con:
            cur = con.execute(
                f"UPDATE tasks SET status='FAILED',stage='Interrupted',error=?,updated_at=? WHERE status IN ({placeholders})",
                ["Bot restarted; submit the URL again.", timestamp, *self.ACTIVE_STATES],
            )
            return int(cur.rowcount)

    def close(self) -> None:
        if self._mongo is not None:
            self._mongo.close()
