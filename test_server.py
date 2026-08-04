import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from types import SimpleNamespace
from server import BUFFER_PROGRESS, DOWNLOAD_PROGRESS, FILTERS, INFO_HASH, STREAM_URL, TEXT_SUBTITLE_CODECS, UnarrServer


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

    def test_dedicated_player_assets_exist(self):
        web = Path(__file__).parent / "web"
        for name in ("watch.html", "watch.js", "watch.css"):
            self.assertTrue((web / name).is_file())

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

    def test_library_signature_tracks_content_changes(self):
        before = [{"filePath": "/media/a.mkv", "fileSize": 10, "modTime": "one"}]
        after = [{"filePath": "/media/a.mkv", "fileSize": 11, "modTime": "two"}]
        self.assertNotEqual(UnarrServer.library_signature(before), UnarrServer.library_signature(after))

    def test_browser_text_subtitle_codecs(self):
        self.assertTrue({"subrip", "ass", "webvtt"}.issubset(TEXT_SUBTITLE_CODECS))
        self.assertNotIn("hdmv_pgs_subtitle", TEXT_SUBTITLE_CODECS)

    def test_trakt_dashboard_exposes_all_shelves_without_credentials(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = ""
        server.trakt_access_token = ""
        server.trakt_cache = None
        server.trakt_cache_time = 0
        server.trakt_lock = threading.Lock()
        dashboard = server.get_trakt_dashboard()
        self.assertFalse(dashboard["configured"])
        self.assertEqual(
            [section["id"] for section in dashboard["sections"]],
            ["continue", "watchlist", "history", "collection", "ratings", "recommendations", "trending", "popular", "anticipated", "lists"],
        )
        self.assertTrue(all(section["locked"] for section in dashboard["sections"]))

    def test_trakt_artwork_uses_same_origin_proxy(self):
        server = object.__new__(UnarrServer)
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}
        proxy = server.register_trakt_image("https://example.test/art.webp")
        self.assertRegex(proxy, r"^/api/trakt/image/[a-f0-9]{32}$")
        self.assertEqual(server.trakt_images[proxy.rsplit("/", 1)[-1]], "https://example.test/art.webp")

    def test_trakt_settings_never_expose_secrets(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = "client-id"
        server.trakt_client_secret = "client-secret"
        server.trakt_access_token = "access-token"
        server.trakt_user = {"username": "viewer"}
        settings = server.get_trakt_settings()
        self.assertTrue(settings["configured"])
        self.assertTrue(settings["authenticated"])
        self.assertNotIn("clientSecret", settings)
        self.assertNotIn("accessToken", settings)

    def test_device_auth_returns_user_code_without_device_code(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = "client-id"
        server.trakt_client_secret = "client-secret"
        server.trakt_lock = threading.Lock()
        server.trakt_oauth_post = Mock(return_value={"device_code": "private", "user_code": "ABCD1234", "verification_url": "https://trakt.tv/activate", "expires_in": 600, "interval": 5})
        with patch("server.threading.Thread") as thread:
            response = server.start_trakt_auth()
        self.assertEqual(response["userCode"], "ABCD1234")
        self.assertNotIn("deviceCode", response)
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
