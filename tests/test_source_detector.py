import tempfile
import unittest
from pathlib import Path

from bot.config import Config
from bot.source import SourceError, detect_source, explain_analysis_error


def make_test_config(root: Path) -> Config:
    return Config(
        api_id=123, api_hash="hash", bot_token="token",
        mongo_uri="", mongo_database="test", sqlite_path=root / "db.sqlite3",
        destination_chat_id=None, cookie_file=None, work_dir=root / "work",
        max_workers=1, max_active_tasks_per_user=1, max_upload_size_bytes=1024,
        keep_failed_files=True, log_level="INFO",
        hotstar_domains=("hotstar.com", "jiohotstar.com"),
        sonyliv_domains=("sonyliv.com", "sonyliv.in"),
        allowed_user_ids=(),
    )


class SourceDetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_test_config(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_hotstar_base_and_subdomain(self):
        self.assertEqual(detect_source("https://www.hotstar.com/in/shows/example/1", self.config).name, "Hotstar")
        self.assertEqual(detect_source("https://jiohotstar.com/title/1", self.config).name, "Hotstar")

    def test_sonyliv_base_and_subdomain(self):
        self.assertEqual(detect_source("https://www.sonyliv.com/shows/example/1", self.config).name, "SonyLIV")

    def test_reject_lookalike_hosts(self):
        for url in (
            "https://hotstar.com.attacker.example/video",
            "https://not-hotstar.com/video",
            "https://sonyliv.com.attacker.example/video",
        ):
            with self.subTest(url=url), self.assertRaises(SourceError):
                detect_source(url, self.config)

    def test_reject_http_and_embedded_credentials(self):
        with self.assertRaises(SourceError):
            detect_source("http://www.hotstar.com/video", self.config)
        with self.assertRaises(SourceError):
            detect_source("https://user:password@www.hotstar.com/video", self.config)
        with self.assertRaises(SourceError):
            detect_source("https://www.hotstar.com:8443/video", self.config)


class AnalysisErrorMessageTests(unittest.TestCase):
    def test_generic_extractor_error_is_not_labeled_drm(self):
        message = explain_analysis_error(RuntimeError("HTTP Error 500: Internal Server Error"))
        self.assertIn("does not by itself mean", message)
        self.assertNotIn("explicitly reports DRM", message)

    def test_403_is_not_assumed_to_be_drm(self):
        message = explain_analysis_error(RuntimeError("HTTP Error 403: Forbidden"))
        self.assertIn("does not prove DRM", message)

    def test_explicit_drm_error_gets_specific_message(self):
        message = explain_analysis_error(RuntimeError("This video is DRM protected"))
        self.assertIn("explicitly reports DRM protection", message)

    def test_signed_urls_are_redacted(self):
        message = explain_analysis_error(RuntimeError("Failed URL https://example.invalid/video?token=secret"))
        self.assertNotIn("secret", message)
        self.assertIn("[URL redacted]", message)


if __name__ == "__main__":
    unittest.main()
