import tempfile
import unittest
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
