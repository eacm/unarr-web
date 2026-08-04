import unittest
from pathlib import Path
from types import SimpleNamespace
from server import BUFFER_PROGRESS, DOWNLOAD_PROGRESS, FILTERS, INFO_HASH, STREAM_URL, UnarrServer


class ValidationTests(unittest.TestCase):
    def test_valid_info_hashes(self):
        self.assertTrue(INFO_HASH.fullmatch("0123456789abcdef0123456789abcdef01234567"))
        self.assertTrue(INFO_HASH.fullmatch("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))

    def test_rejects_command_injection(self):
        for value in ("", "../../bin/sh", "magnet:?xt=urn:btih:nope", "a" * 41):
            self.assertIsNone(INFO_HASH.fullmatch(value))

    def test_filter_allowlists(self):
        self.assertIn("movie", FILTERS["type"][1])
        self.assertNotIn("anything", FILTERS["type"][1])

    def test_favicon_exists(self):
        self.assertTrue((Path(__file__).parent / "web" / "favicon.svg").is_file())

    def test_stream_ready_url(self):
        line = "Buffering: 100%  Open this URL in your player: http://192.168.1.4:9000/stream?t=secret"
        self.assertEqual(STREAM_URL.search(line).group(1), "http://192.168.1.4:9000/stream?t=secret")

    def test_stream_progress_patterns(self):
        self.assertEqual(BUFFER_PROGRESS.search("Buffering: 42% (4 MB / 10 MB)").group(1), "42")
        progress = DOWNLOAD_PROGRESS.search("18% | 2.4 MB/s | Peers: 7 | Seeds: 3")
        self.assertEqual(progress.groups(), ("18", "2.4 MB/s ", "7", "3"))

    def test_public_library_item_hides_path(self):
        item = {"fingerprint": "a" * 64, "filePath": "/private/movie.mkv", "fileName": "movie.mkv", "title": "Movie"}
        public = UnarrServer.public_library_item(item)
        self.assertEqual(public["id"], "a" * 64)
        self.assertNotIn("filePath", public)

    def test_live_item_metadata_from_filename(self):
        item = UnarrServer.basic_library_item(Path("Example.Show.S02E03.2026.1080p.mkv"), SimpleNamespace(st_size=42, st_mtime=0))
        self.assertEqual(item["title"], "Example Show")
        self.assertEqual((item["season"], item["episode"]), (2, 3))
        self.assertEqual((item["year"], item["quality"]), ("2026", "1080p"))


if __name__ == "__main__":
    unittest.main()
