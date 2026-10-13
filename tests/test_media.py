import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import media


class MediaTests(unittest.TestCase):
    def test_unknown_audio_is_not_assumed_hindi_and_bps_is_bits_per_second(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.mkv"
            path.write_bytes(b"test")
            ffprobe = {
                "format": {"duration": "120.5", "size": "12345"},
                "streams": [
                    {
                        "codec_type": "video", "height": 1080, "codec_name": "hevc",
                        "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001",
                        "tags": {"BPS": "512000"}, "disposition": {"attached_pic": 0},
                    },
                    {
                        "codec_type": "audio", "codec_name": "aac", "channels": 2,
                        "tags": {"language": "eng", "BPS": "128000"},
                        "disposition": {"default": 1},
                    },
                    {
                        "codec_type": "audio", "codec_name": "eac3", "channels": 8,
                        "tags": {"language": "und"}, "disposition": {"default": 0},
                    },
                    {"codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}},
                ],
            }

            def fake_command(command, timeout=45):
                return ffprobe if command[0] == "ffprobe" else None

            with patch.object(media, "_run_json_command", side_effect=fake_command):
                result = media.probe_media_sync(path)

        self.assertEqual(result["video_codec"], "HEVC")
        self.assertEqual(result["bit_depth"], "10bit")
        self.assertEqual(result["video_bitrate_kbps"], 512)
        self.assertEqual(result["audio_streams"][0]["bitrate_kbps"], 128)
        self.assertEqual(result["audio_streams"][1]["language"], "Unknown")
        self.assertEqual(result["audio_streams"][1]["channels"], "7.1")
        self.assertEqual(result["subtitle_languages"], ["English"])
        self.assertNotIn("Hindi", result["audio_languages"])

    def test_smart_caption_v1_and_v2_are_distinct(self):
        info = {
            "title": "Example Episode", "series": "Example Show", "season_number": 2,
            "episode_number": 3, "episode_title": "Example Episode", "webpage_url": "https://www.hotstar.com/shows/1",
            "subtitle_languages": ["en", "hi"],
        }
        probe = {
            "resolution": 1080, "bit_depth": "10bit", "video_codec": "HEVC", "fps": 24,
            "audio_streams": [
                {"language": "Hindi", "codec": "AAC", "channels": "2.0", "bitrate_kbps": 128},
                {"language": "English", "codec": "AAC", "channels": "2.0", "bitrate_kbps": 128},
            ],
            "subtitle_languages": ["English", "Hindi"], "duration": 600, "size_bytes": 1000000,
        }
        tmdb = {"title": "Example Show", "year": "2020", "genres": ["Drama"]}
        v1 = media.build_smart_caption_filename(info, probe, tmdb, {"smart_caption_version": "v1"}, "original-file.mkv", "Hotstar WEB-DL")
        v2 = media.build_smart_caption_filename(info, probe, tmdb, {"smart_caption_version": "v2"}, "original-file.mkv", "Hotstar WEB-DL")
        self.assertIn("x265.HEVC", v1)
        self.assertIn("Hindi.AAC.2.0.128kbps+English.AAC.2.0.128kbps", v1)
        self.assertIn("MultiAudio", v2)
        self.assertNotIn("Hindi.AAC.2.0.128kbps+English.AAC.2.0.128kbps", v2)

    def test_caption_preserves_original_and_physical_filenames_separately(self):
        task = {
            "source": "Hotstar", "settings": {"smart_caption_version": "v2"},
            "original_filename": "Example.Source.Name.mkv", "physical_filename": "Example - 1080p.mkv",
        }
        info = {"title": "Example", "webpage_url": "https://www.hotstar.com/movies/example"}
        probe = {"resolution": 1080, "fps": 24, "bit_depth": "8bit", "video_codec": "AVC",
                 "audio_streams": [], "subtitle_languages": [], "duration": 60, "size_bytes": 1000}
        caption_filename, caption = media.build_caption(info, probe, {}, task, "Hotstar WEB-DL")
        self.assertTrue(caption_filename.endswith(".mkv"))
        self.assertIn("Original filename: Example.Source.Name.mkv", caption)
        self.assertIn("Saved as: Example - 1080p.mkv", caption)

    def test_safe_filename_removes_path_and_reserved_characters(self):
        value = media.safe_filename("../../Law & Order: SVU? <Pilot>.mkv")
        self.assertNotIn("/", value)
        self.assertNotIn(":", value)
        self.assertNotIn("?", value)


if __name__ == "__main__":
    unittest.main()
