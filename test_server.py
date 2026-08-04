import json
import threading
import tempfile
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

    def test_discover_starts_with_search_and_version_opens_status(self):
        web = Path(__file__).parent / "web"
        markup = (web / "index.html").read_text()
        self.assertLess(markup.index('id="search-form"'), markup.index('id="trakt-dashboard"'))
        self.assertIn('id="connection" class="pill connection-button"', markup)
        self.assertNotIn("Ready to discover", markup)
        self.assertNotIn("Search the catalog to begin", markup)

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
            ["continue", "start", "calendar", "watchlist", "history", "collection", "ratings", "recommendations", "trending", "popular", "anticipated", "lists"],
        )
        self.assertTrue(all(section["locked"] for section in dashboard["sections"]))

    def test_trakt_calendar_combines_movies_and_shows_by_date(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = "client"
        server.trakt_access_token = "token"
        server.trakt_cache = None
        server.trakt_cache_time = 0
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}

        def request(path, authenticated=False):
            if "/calendars/my/shows/" in path:
                return [{"first_aired": "2026-08-06T01:00:00.000Z", "show": {"title": "A Show", "ids": {"trakt": 2}}, "episode": {"season": 1, "number": 3}}]
            if "/calendars/my/movies/" in path:
                return [{"released": "2026-08-05", "movie": {"title": "A Movie", "ids": {"trakt": 1}}}]
            return []

        server.trakt_request = request
        calendar = next(section for section in server.get_trakt_dashboard()["sections"] if section["id"] == "calendar")
        self.assertEqual([item["title"] for item in calendar["items"]], ["A Movie", "A Show"])
        self.assertEqual([item["mediaType"] for item in calendar["items"]], ["movie", "show"])

    def test_watchlist_update_rejects_invalid_title(self):
        server = object.__new__(UnarrServer)
        server.trakt_access_token = "token"
        with self.assertRaises(ValueError):
            server.update_trakt_watchlist({"action": "add", "type": "movie", "traktId": "1"})

    def test_poster_states_and_long_press_actions_exist(self):
        app = (Path(__file__).parent / "web" / "app.js").read_text()
        markup = (Path(__file__).parent / "web" / "index.html").read_text()
        self.assertIn("poster-identifier watched", app)
        self.assertIn("pointerdown", app)
        self.assertIn("/api/trakt/watchlist", app)
        self.assertIn('id="poster-actions"', markup)

    def test_library_uses_title_and_source_filters(self):
        markup = (Path(__file__).parent / "web" / "index.html").read_text()
        app = (Path(__file__).parent / "web" / "app.js").read_text()
        self.assertNotIn('id="library-quality"', markup)
        self.assertNotIn('id="transcode-state"', markup)
        for source in ("favorites", "cloud", "local", "all"):
            self.assertIn(f'data-library-filter="{source}"', markup)
        self.assertIn("data-library-action", app)
        self.assertIn("Match with Trakt", app)
        for sort in ("az", "za", "added", "released"):
            self.assertIn(f'value="{sort}"', markup)
        self.assertIn("groupedLibraryTitle", app)
        self.assertIn("library-poster", app)
        self.assertIn("openLibraryGroup", app)
        self.assertIn("rankedTraktMatches", app)
        self.assertIn("autoMatchLibrary", app)
        self.assertIn("folderTitle", (Path(__file__).parent / "server.py").read_text())

    def test_trakt_artwork_uses_same_origin_proxy(self):
        server = object.__new__(UnarrServer)
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}
        proxy = server.register_trakt_image("https://example.test/art.webp")
        self.assertRegex(proxy, r"^/api/trakt/image/[a-f0-9]{32}$")
        self.assertEqual(server.trakt_images[proxy.rsplit("/", 1)[-1]], "https://example.test/art.webp")

    def test_trakt_cards_prefer_posters(self):
        server = object.__new__(UnarrServer)
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}
        item = server.normalize_trakt_item({"movie": {"title": "Example", "images": {"fanart": ["example.test/fanart.webp"], "poster": ["example.test/poster.webp"]}}}, "popular")
        image_id = item["image"].rsplit("/", 1)[-1]
        self.assertEqual(server.trakt_images[image_id], "https://example.test/poster.webp")

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

    def test_trakt_settings_are_written_owner_only(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = "client-id"
        server.trakt_client_secret = "client-secret"
        server.trakt_access_token = ""
        server.trakt_refresh_token = ""
        server.trakt_user = None
        with tempfile.TemporaryDirectory() as directory, patch("server.TRAKT_SETTINGS_FILE", Path(directory) / "trakt.json") as settings_file:
            server.write_trakt_settings()
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(settings_file.read_text())["client_id"], "client-id")

    def test_settings_backup_is_scoped_to_connected_user(self):
        server = object.__new__(UnarrServer)
        server.trakt_client_id = "client-id"
        server.trakt_client_secret = "client-secret"
        server.trakt_access_token = "access-token"
        server.trakt_refresh_token = "refresh-token"
        server.trakt_user = {"username": "viewer", "name": "Viewer"}
        backup = server.get_settings_backup()
        self.assertEqual(backup["profile"], "viewer")
        self.assertEqual(backup["format"], "unarr-web-settings")
        self.assertEqual(backup["trakt"]["accessToken"], "access-token")

    def test_restore_rejects_unknown_backup_format(self):
        server = object.__new__(UnarrServer)
        with self.assertRaisesRegex(ValueError, "supported"):
            server.restore_settings_backup({"format": "something-else", "version": 1})

    def test_trakt_cards_use_image_elements_under_csp(self):
        script = (Path(__file__).parent / "web" / "app.js").read_text()
        self.assertIn('<img src="${escapeHTML(item.image)}"', script)
        self.assertNotIn("--art:url", script)

    def test_trakt_items_include_detail_identity(self):
        server = object.__new__(UnarrServer)
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}
        item = server.normalize_trakt_item({"show": {"title": "Example", "ids": {"trakt": 42}}}, "start")
        self.assertEqual(item["mediaType"], "show")
        self.assertEqual(item["ids"]["trakt"], 42)

    def test_trakt_details_reject_invalid_identifiers(self):
        server = object.__new__(UnarrServer)
        for media_type, trakt_id in (("episode", "1"), ("movie", "../../1"), ("show", "")):
            with self.assertRaises(ValueError):
                server.get_trakt_details(media_type, trakt_id)

    def test_discover_uses_live_trakt_search(self):
        script = (Path(__file__).parent / "web" / "app.js").read_text()
        self.assertIn("/api/trakt/search?q=", script)
        self.assertIn("loadTraktMatches", script)
        self.assertNotIn("fetch(`/api/search?${params}`)", script)
        styles = (Path(__file__).parent / "web" / "player.css").read_text()
        self.assertRegex(styles, r"\.search \{[^}]*z-index: 12")

    def test_trakt_search_sorting(self):
        server = object.__new__(UnarrServer)
        server.trakt_request = Mock(return_value=[
            {"score": 10, "movie": {"title": "Relevant", "votes": 2, "ids": {"trakt": 1}}},
            {"score": 2, "movie": {"title": "Popular", "votes": 500, "ids": {"trakt": 2}}},
        ])
        server.trakt_lock = threading.Lock()
        server.trakt_images = {}
        self.assertEqual(server.search_trakt("test", "recommended")[0]["title"], "Relevant")
        self.assertEqual(server.search_trakt("test", "popular")[0]["title"], "Popular")

    def test_live_search_has_no_sort_control(self):
        web = Path(__file__).parent / "web"
        self.assertNotIn("trakt-search-sort", (web / "index.html").read_text())
        self.assertIn("sort=popular", (web / "app.js").read_text())

    def test_torrentclaw_selects_best_valid_release(self):
        server = object.__new__(UnarrServer)
        server.torrentclaw_api_key = "test-key"
        server.torrentclaw_request = Mock(return_value={"results": [{"torrents": [
            {"infoHash": "0" * 40, "qualityScore": 60, "seeders": 100, "rawTitle": "Lower"},
            {"infoHash": "1" * 40, "qualityScore": 95, "seeders": 20, "rawTitle": "Best"},
            {"infoHash": "invalid", "qualityScore": 100, "seeders": 999, "rawTitle": "Invalid"},
        ]}]})
        release = server.find_torrentclaw_release({"type": "movie", "title": "Example", "imdbId": "tt1234567"})
        self.assertEqual(release["rawTitle"], "Best")
        params = server.torrentclaw_request.call_args.args[0]
        self.assertEqual(params["imdbid"], "tt1234567")

    def test_torrentclaw_show_requires_episode(self):
        server = object.__new__(UnarrServer)
        server.torrentclaw_api_key = "test-key"
        with self.assertRaisesRegex(ValueError, "episode"):
            server.find_torrentclaw_release({"type": "show", "title": "Example", "season": 1})


if __name__ == "__main__":
    unittest.main()
