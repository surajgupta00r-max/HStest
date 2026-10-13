import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.cookies import CookieStore, CookieValidationError, MAX_COOKIE_FILE_BYTES
from bot.source import SourceManager


SAMPLE_COOKIES = b"""# Netscape HTTP Cookie File
.hotstar.com\tTRUE\t/\tTRUE\t1893456000\tuser_session\thotstar-secret
#HttpOnly_.sonyliv.com\tTRUE\t/\tTRUE\t1893456000\tsony_session\tsonyliv-secret
.other.example\tTRUE\t/\tTRUE\t1893456000\tother_session\tshould-not-be-kept
"""


class CookieStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CookieStore(
            self.root / "data" / "cookies",
            None,
            ("hotstar.com", "jiohotstar.com", "sonyliv.com", "sonyliv.in"),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_saves_only_supported_domains_and_owner_only_file(self):
        count = self.store.save_bytes(1001, SAMPLE_COOKIES)
        path = self.store.path_for_user(1001)
        content = path.read_text()
        self.assertEqual(count, 2)
        self.assertIn("user_session", content)
        self.assertIn("sony_session", content)
        self.assertNotIn("other_session", content)
        self.assertTrue(self.store.has_user_cookie(1001))
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)

    def test_cookies_are_isolated_per_user(self):
        self.store.save_bytes(1001, SAMPLE_COOKIES)
        self.assertIsNone(self.store.get_for_user(1002))
        self.assertEqual(self.store.get_for_user(1001), self.store.path_for_user(1001))

    def test_server_cookie_file_is_fallback(self):
        global_file = self.root / "global-cookies.txt"
        global_file.write_text("server side config")
        store = CookieStore(self.root / "cookies", global_file, ("hotstar.com",))
        self.assertEqual(store.get_for_user(1001), global_file)
        store.save_bytes(1001, SAMPLE_COOKIES)
        self.assertEqual(store.get_for_user(1001), store.path_for_user(1001))

    def test_remove_uploaded_cookie(self):
        self.store.save_bytes(1001, SAMPLE_COOKIES)
        self.assertTrue(self.store.remove_user_cookie(1001))
        self.assertFalse(self.store.has_user_cookie(1001))
        self.assertFalse(self.store.remove_user_cookie(1001))

    def test_source_analysis_uses_requesting_users_cookie_file(self):
        config = SimpleNamespace(
            sqlite_path=self.root / "data" / "bot.sqlite3",
            cookie_file=None,
            hotstar_domains=("hotstar.com",),
            sonyliv_domains=("sonyliv.com",),
        )
        manager = SourceManager(config)
        manager.cookie_store.save_bytes(1001, SAMPLE_COOKIES)
        url = "https://www.hotstar.com/title/example"
        with patch("bot.source._extract_info_sync", return_value={"title": "Example", "formats": []}) as extract:
            result = asyncio.run(manager.analyze(url, user_id=1001))
        extract.assert_called_once_with(url, config, manager.cookie_store.path_for_user(1001))
        self.assertEqual(result["title"], "Example")

    def test_rejects_unrelated_or_invalid_files(self):
        with self.assertRaises(CookieValidationError):
            self.store.save_bytes(1001, b"this is not a cookies file")
        unrelated = b".other.example\tTRUE\t/\tTRUE\t1893456000\tname\tvalue\n"
        with self.assertRaises(CookieValidationError):
            self.store.save_bytes(1001, unrelated)
        with self.assertRaises(CookieValidationError):
            self.store.save_bytes(1001, b"\xff\xfe\x00")

    def test_rejects_oversized_file(self):
        with self.assertRaises(CookieValidationError):
            self.store.save_bytes(1001, b"x" * (MAX_COOKIE_FILE_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
