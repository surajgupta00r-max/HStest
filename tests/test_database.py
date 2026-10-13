import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bot.config import Config
from bot.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = Config(
            api_id=123, api_hash="hash", bot_token="token", mongo_uri="",
            mongo_database="test", sqlite_path=root / "data" / "state.sqlite3",
            destination_chat_id=None, cookie_file=None, work_dir=root / "work",
            max_workers=1, max_active_tasks_per_user=1, max_upload_size_bytes=1024,
            keep_failed_files=True, log_level="INFO",
            hotstar_domains=("hotstar.com",), sonyliv_domains=("sonyliv.com",), allowed_user_ids=(),
        )
        self.db = Database(self.config)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_register_and_update_settings(self):
        self.db.register_user(42, "qa_user")
        self.assertEqual(self.db.get_settings(42)["quality"], "best")
        updated = self.db.update_setting(42, "quality", "720")
        self.assertEqual(updated["quality"], "720")
        self.assertEqual(self.db.get_settings(42)["quality"], "720")

    def test_task_lifecycle_persistence(self):
        self.db.register_user(42, "qa_user")
        self.db.create_task({
            "task_id": "ABCD1234", "user_id": 42, "url": "https://www.hotstar.com/x",
            "source": "Hotstar", "title": "A title", "status": "WAITING_FOR_SELECTION",
            "progress": 0, "quality": "best", "audio_language": "best",
            "subtitles_enabled": False, "subtitle_language": "off", "error": None,
            "output_url": None, "workdir": None, "message_chat_id": 42, "message_id": 100,
        })
        self.assertEqual(self.db.count_active_tasks(42), 1)
        self.db.update_task("ABCD1234", status="QUEUED")
        self.assertEqual(self.db.get_task("ABCD1234")["status"], "QUEUED")
        self.assertEqual(self.db.list_user_tasks(42)[0]["task_id"], "ABCD1234")


    def test_paginated_and_filtered_task_queries(self):
        self.db.register_user(42, "qa_user")
        for index in range(6):
            status = "FAILED" if index == 0 else "COMPLETED" if index == 1 else "QUEUED"
            self.db.create_task({
                "task_id": f"PAGE{index:04d}", "user_id": 42, "url": f"https://www.hotstar.com/{index}",
                "source": "Hotstar", "title": f"Media {index}", "status": status,
                "progress": 0, "quality": "best", "audio_language": "best",
                "subtitles_enabled": False, "subtitle_language": "off", "error": None,
                "output_url": None, "workdir": None, "message_chat_id": 42, "message_id": 100 + index,
            })
        first = self.db.list_user_tasks(42, limit=2, offset=0)
        second = self.db.list_user_tasks(42, limit=2, offset=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertFalse(set(t["task_id"] for t in first) & set(t["task_id"] for t in second))
        self.assertEqual(self.db.count_user_tasks(42, "failed"), 1)
        self.assertEqual(self.db.count_user_tasks(42, "completed"), 1)
        self.assertEqual(self.db.count_user_tasks(42, "active"), 4)

    def test_media_metadata_fields_round_trip(self):
        self.db.register_user(42, "qa_user")
        self.db.create_task({
            "task_id": "META0001", "user_id": 42, "url": "https://www.hotstar.com/1",
            "source": "Hotstar", "title": "Media", "status": "PROCESSING",
            "progress": 35, "quality": "best", "audio_language": "best",
            "subtitles_enabled": False, "subtitle_language": "off", "error": None,
            "output_url": None, "workdir": "/tmp/task", "stage": "Probing",
            "original_filename": "Original.mkv", "physical_filename": "Renamed.mkv",
            "caption_filename": "Caption.mkv", "media_metadata": {"probe": {"resolution": 1080}},
        })
        row = self.db.get_task("META0001")
        self.assertEqual(row["stage"], "Probing")
        self.assertEqual(row["original_filename"], "Original.mkv")
        self.assertEqual(row["physical_filename"], "Renamed.mkv")
        self.assertEqual(row["media_metadata"]["probe"]["resolution"], 1080)


    def test_migrates_existing_sqlite_database_without_losing_tasks(self):
        legacy_path = self.config.sqlite_path.parent / "legacy.sqlite3"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(legacy_path) as con:
            con.executescript("""
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY, username TEXT, settings TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, url TEXT NOT NULL,
                    source TEXT NOT NULL, title TEXT, status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'best',
                    audio_language TEXT NOT NULL DEFAULT 'best', subtitles_enabled INTEGER NOT NULL DEFAULT 0,
                    subtitle_language TEXT NOT NULL DEFAULT 'en', error TEXT, output_url TEXT, workdir TEXT,
                    message_chat_id INTEGER, message_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO users VALUES (42, 'qa_user', '{"quality":"720"}', '2025-01-01', '2025-01-01');
                INSERT INTO tasks (
                    task_id,user_id,url,source,title,status,progress,quality,audio_language,
                    subtitles_enabled,subtitle_language,created_at,updated_at
                ) VALUES (
                    'OLDTASK1',42,'https://www.hotstar.com/old','Hotstar','Old task',
                    'COMPLETED',100,'720','best',0,'off','2025-01-01','2025-01-01'
                );
            """)
        legacy_db = Database(replace(self.config, sqlite_path=legacy_path))
        try:
            task = legacy_db.get_task("OLDTASK1")
            self.assertIsNotNone(task)
            self.assertEqual(task["title"], "Old task")
            self.assertEqual(task["stage"], "Completed")
            with legacy_db._connection() as con:
                columns = {row["name"] for row in con.execute("PRAGMA table_info(tasks)").fetchall()}
            self.assertIn("media_metadata", columns)
            self.assertIn("caption_filename", columns)
        finally:
            legacy_db.close()

    def test_interrupted_tasks_marked_failed(self):
        self.db.register_user(42, "qa_user")
        self.db.create_task({
            "task_id": "TASK0001", "user_id": 42, "url": "https://www.hotstar.com/x",
            "source": "Hotstar", "title": "A title", "status": "DOWNLOADING",
            "progress": 10, "quality": "best", "audio_language": "best",
            "subtitles_enabled": False, "subtitle_language": "off", "error": None,
            "output_url": None, "workdir": None, "message_chat_id": 42, "message_id": 100,
        })
        self.assertEqual(self.db.mark_interrupted_tasks(), 1)
        self.assertEqual(self.db.get_task("TASK0001")["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
