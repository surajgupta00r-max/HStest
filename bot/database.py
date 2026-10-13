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
}


class Database:
    """MongoDB when MONGO_URI is configured; otherwise SQLite for easy local setup."""

    ACTIVE_STATES = ["ANALYZING", "WAITING_FOR_SELECTION", "QUEUED", "DOWNLOADING", "UPLOADING"]

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
            except Exception as exc:
                if self._mongo is not None:
                    self._mongo.close()
                self._mongo = None
                raise RuntimeError("MONGO_URI is configured but MongoDB could not be reached. Check dependency installation, network access, credentials, and IP access-list settings.") from None

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
        if self._mongo is not None:
            self.tasks.insert_one(task)
            return
        with self._connection() as con:
            con.execute("""INSERT INTO tasks(
                task_id,user_id,url,source,title,status,progress,quality,audio_language,
                subtitles_enabled,subtitle_language,error,output_url,workdir,message_chat_id,message_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                task["task_id"], task["user_id"], task["url"], task["source"], task.get("title"), task["status"],
                float(task.get("progress", 0)), task.get("quality", "best"), task.get("audio_language", "best"),
                int(bool(task.get("subtitles_enabled", False))), task.get("subtitle_language", "en"), task.get("error"),
                task.get("output_url"), task.get("workdir"), task.get("message_chat_id"), task.get("message_id"),
                task["created_at"], task["updated_at"]
            ))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if self._mongo is not None:
            doc = self.tasks.find_one({"task_id": task_id}, {"_id": 0})
            return doc
        with self._connection() as con:
            row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["subtitles_enabled"] = bool(result["subtitles_enabled"])
            return result

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        if self._mongo is not None:
            self.tasks.update_one({"task_id": task_id}, {"$set": fields})
            return
        allowed = {
            "username", "url", "source", "title", "status", "progress", "quality", "audio_language",
            "subtitles_enabled", "subtitle_language", "error", "output_url", "workdir", "message_chat_id", "message_id"
        }
        items = [(key, int(bool(value)) if key == "subtitles_enabled" else value) for key, value in fields.items() if key in allowed]
        items.append(("updated_at", fields["updated_at"]))
        with self._connection() as con:
            con.execute(f"UPDATE tasks SET {', '.join(f'{key}=?' for key, _ in items)} WHERE task_id=?",
                        [value for _, value in items] + [task_id])

    def list_user_tasks(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        if self._mongo is not None:
            return list(self.tasks.find({"user_id": int(user_id)}, {"_id": 0}).sort("created_at", -1).limit(limit))
        with self._connection() as con:
            rows = con.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (int(user_id), limit)).fetchall()
            result = [dict(row) for row in rows]
            for task in result:
                task["subtitles_enabled"] = bool(task["subtitles_enabled"])
            return result

    def count_active_tasks(self, user_id: int) -> int:
        if self._mongo is not None:
            return self.tasks.count_documents({"user_id": int(user_id), "status": {"$in": self.ACTIVE_STATES}})
        placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
        with self._connection() as con:
            row = con.execute(f"SELECT COUNT(*) AS n FROM tasks WHERE user_id=? AND status IN ({placeholders})",
                              [int(user_id), *self.ACTIVE_STATES]).fetchone()
            return int(row["n"])

    def mark_interrupted_tasks(self) -> int:
        """Mark unfinished tasks interrupted at process start; active runtime state isn't safely resumable."""
        timestamp = now_iso()
        if self._mongo is not None:
            result = self.tasks.update_many(
                {"status": {"$in": self.ACTIVE_STATES}},
                {"$set": {"status": "FAILED", "error": "Bot restarted; submit the URL again.", "updated_at": timestamp}},
            )
            return int(result.modified_count)
        placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
        with self._connection() as con:
            cur = con.execute(
                f"UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE status IN ({placeholders})",
                ["Bot restarted; submit the URL again.", timestamp, *self.ACTIVE_STATES],
            )
            return int(cur.rowcount)

    def close(self) -> None:
        if self._mongo is not None:
            self._mongo.close()
