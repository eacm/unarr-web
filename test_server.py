import json
import gzip
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from types import SimpleNamespace
from database import AppDatabase
from server import BUFFER_PROGRESS, DOWNLOAD_PROGRESS, FILTERS, INFO_HASH, STREAM_URL, TEXT_SUBTITLE_CODECS, UnarrServer


class ValidationTests(unittest.TestCase):
    def test_valid_info_hashes(self):
        self.assertTrue(INFO_HASH.fullmatch("0123456789abcdef0123456789abcdef01234567"))
        self.assertTrue(INFO_HASH.fullmatch("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))

    def test_private_tracker_settings_never_expose_cookie(self):
        server = object.__new__(UnarrServer)
        server.torrentday_cookie = "uid=1; pass=secret"
        server.torrentday_base_url = "https://tday.love/"
        server.torrentday_freeleech = True
        settings = server.get_private_tracker_settings()
        self.assertTrue(settings["configured"])
        self.assertNotIn("cookie", settings)
        self.assertNotIn("secret", json.dumps(settings))

    def test_torrent_metadata_extracts_exact_info_hash_and_tracker(self):
        data = b"d8:announce24:https://tracker.test/ann4:infod4:name4:testee"
        info_hash, trackers = UnarrServer.torrent_metadata(data)
        self.assertEqual(info_hash, __import__("hashlib").sha1(b"d4:name4:teste").hexdigest())
        self.assertEqual(trackers, ["https://tracker.test/ann"])

    def test_torrentday_html_fallback_keeps_real_download_url(self):
        markup = b'''<table><tr><td class="torrentNameInfo"><a href="/t/100">Example S01E02 1080p</a></td>
            <td><a href="/download.php/100/Example.torrent">DL</a></td><td>1.5 GB</td>
            <td class="seedersInfo">17</td></tr></table>'''
        [result] = UnarrServer.parse_torrentday_html(markup, "https://www.torrentday.com/")
        self.assertEqual(result["downloadUrl"], "https://www.torrentday.com/download.php/100/Example.torrent")
        self.assertEqual(result["seeders"], 17)
        self.assertEqual(result["size"], round(1.5 * 1024 ** 3))

    def test_torrentday_html_parser_accepts_decompressed_gzip_body(self):
        markup = b'<tr><td class="torrentNameInfo"><a href="/t/1">Movie 1080p</a></td><td><a href="/download.php/1/Movie.torrent">DL</a></td></tr>'
        self.assertTrue(gzip.compress(markup).startswith(b"\x1f\x8b"))
        [result] = UnarrServer.parse_torrentday_html(gzip.decompress(gzip.compress(markup)), "https://www.torrentday.com/")
        self.assertEqual(result["t"], "1")

    def test_rejects_command_injection(self):
        for value in ("", "../../bin/sh", "magnet:?xt=urn:btih:nope", "a" * 41):
            self.assertIsNone(INFO_HASH.fullmatch(value))

    def test_filter_allowlists(self):
        self.assertIn("movie", FILTERS["type"][1])
        self.assertNotIn("anything", FILTERS["type"][1])

    def test_library_auto_clean_filters_and_keeps_best_episode(self):
        common = {"source": "cloud", "trakt": {"type": "show", "traktId": 42}}
        items = [
            {**common, "id": "low", "fileName": "Show.S01E02.720p.mkv", "fileSize": 800_000_000},
            {**common, "id": "best", "fileName": "Show.S01E02.2160p.mkv", "fileSize": 4_000_000_000},
            {**common, "id": "tiny", "fileName": "Show.S01E03.sample.mkv", "fileSize": 3_000_000},
            {**common, "id": "text", "fileName": "notes.txt", "fileSize": 30_000_000},
            {**common, "id": "short", "fileName": "Show.S01E04.mkv", "fileSize": 30_000_000, "mediaInfo": {"video": {"duration": 120}}},
        ]
        kept, hidden = UnarrServer.clean_library_items(items)
        self.assertEqual([item["id"] for item in kept], ["best"])
        self.assertEqual(hidden, {"nonVideo": 1, "tooSmall": 1, "tooShort": 1, "duplicates": 1, "total": 4})

    def test_library_auto_clean_keeps_distinct_show_episodes(self):
        link = {"type": "show", "traktId": 42}
        items = [
            {"source": "cloud", "id": "one", "fileName": "Show.S01E01.mkv", "fileSize": 500_000_000, "trakt": link},
            {"source": "cloud", "id": "two", "fileName": "Show.S01E02.mkv", "fileSize": 500_000_000, "trakt": link},
        ]
        kept, hidden = UnarrServer.clean_library_items(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(hidden["duplicates"], 0)

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
        self.assertIn("reflectLibraryMatch", app)
        self.assertIn("libraryVisibleLimit", app)
        self.assertIn("libraryAutoMatchTried", app)
        self.assertNotIn("limit=${encodeURIComponent(libraryVisibleLimit)}", app)
        self.assertNotIn("matching items", app)
        self.assertIn("$('#results-title').textContent='Library'", app)
        self.assertIn("['download-options','Download to local',favorite.id]", app)
        self.assertIn("folderTitle", (Path(__file__).parent / "server.py").read_text())
        self.assertIn("isGenericExtra", app)
        self.assertIn("reviewLibraryGroupWithAI", app)
        self.assertIn('id="export-matches"', markup)

    def test_dashboard_rail_controls_and_continue_removal_exist(self):
        app = (Path(__file__).parent / "web" / "app.js").read_text()
        markup = (Path(__file__).parent / "web" / "index.html").read_text()
        for control in ("rail-date-sort", "rail-watched-toggle", "calendar-from", "calendar-to"):
            self.assertIn(control, app)
        self.assertIn("/api/trakt/playback/remove", app)
        self.assertIn("contextmenu", app)
        self.assertIn('id="poster-continue-action"', markup)

    def test_continue_items_expose_playback_id(self):
        server = object.__new__(UnarrServer)
        server.trakt_image_cache = {}
        server.trakt_image_lock = threading.Lock()
        item = server.normalize_trakt_item(
            {"id": 77, "movie": {"title": "Example", "year": 2024, "ids": {"trakt": 10}}},
            "continue",
        )
        self.assertEqual(item["playbackId"], 77)

    def test_remove_trakt_playback_rejects_invalid_id(self):
        server = object.__new__(UnarrServer)
        server.trakt_access_token = "token"
        with self.assertRaises(ValueError):
            server.remove_trakt_playback({"playbackId": "invalid"})

    def test_library_duplicate_list_contains_only_lower_quality_copy(self):
        lower = {"id": "cloud:1:1", "source": "cloud", "fileName": "Example.1080p.WEB-DL.mkv", "fileSize": 1_000_000_000, "trakt": {"type": "movie", "traktId": 12}}
        higher = {"id": "cloud:2:2", "source": "cloud", "fileName": "Example.2160p.Remux.mkv", "fileSize": 4_000_000_000, "trakt": {"type": "movie", "traktId": 12}}
        duplicates = UnarrServer.library_duplicates([lower, higher])
        self.assertEqual([item["id"] for item in duplicates], [lower["id"]])
        self.assertTrue(duplicates[0]["duplicate"])

    def test_popular_bare_titles_are_movies_with_options(self):
        server = object.__new__(UnarrServer)
        server.trakt_image_cache = {}
        server.trakt_image_lock = threading.Lock()
        item = server.normalize_trakt_item({"title": "Popular Movie", "ids": {"trakt": 44}}, "popular")
        self.assertEqual(item["mediaType"], "movie")
        self.assertEqual(item["ids"]["trakt"], 44)

    def test_new_library_and_rail_preferences_are_present(self):
        app = (Path(__file__).parent / "web" / "app.js").read_text()
        markup = (Path(__file__).parent / "web" / "index.html").read_text()
        for identifier in ('id="library-media"', 'id="library-from"', 'id="library-to"', 'id="poster-watched-action"'):
            self.assertIn(identifier, markup)
        for behavior in ("unarrRailPreferences", "/api/trakt/calendar", "/api/trakt/history", "/api/trakt/continue", "rail-visibility-toggle", "unarrActivitySeen"):
            self.assertIn(behavior, app)

    def test_torbox_control_uses_json_not_multipart(self):
        server = object.__new__(UnarrServer)
        server.torbox_api_key = "token"
        completed = SimpleNamespace(returncode=0, stdout='{"success":true,"data":{}}', stderr="")
        with patch("server.subprocess.run", return_value=completed) as run:
            server.torbox_request("controltorrent", json_body={"torrent_id": 123, "operation": "delete"})
        command = run.call_args.args[0]
        self.assertIn("--data", command)
        self.assertIn('{"torrent_id":123,"operation":"delete"}', command)
        self.assertNotIn("-F", command)

    def test_torbox_database_error_is_success_when_package_is_gone(self):
        server = object.__new__(UnarrServer)
        item = {"id": "cloud:65737841:320", "source": "cloud", "torrentId": 65737841, "title": "Example"}
        server.get_cloud_library = Mock(return_value=[item])
        server.torbox_request = Mock(side_effect=RuntimeError("DATABASE_ERROR"))
        server.refresh_torbox_index = Mock(return_value=[])
        server.torbox_index = [item]
        server.torbox_index_time = 1
        server.library_links = {}
        server.database = Mock()
        self.assertTrue(server.library_action({"action": "delete", "itemId": item["id"]})["ok"])
        server.refresh_torbox_index.assert_called_once_with(force=True)

    def test_artwork_repair_includes_empty_matched_images(self):
        server = object.__new__(UnarrServer)
        server.library_links = {"cloud:1:2": {"type": "movie", "traktId": 5, "title": "Example", "image": ""}}
        server.provider_sync_stop = threading.Event()
        server.trakt_images = {}
        server.database = Mock()
        server.get_trakt_details = Mock(return_value={"poster": "/api/trakt/image/repaired"})
        server.repair_library_artwork()
        self.assertEqual(server.library_links["cloud:1:2"]["image"], "/api/trakt/image/repaired")
        server.database.save_matches.assert_called_once()

    def test_ai_settings_never_expose_api_key(self):
        server = object.__new__(UnarrServer)
        server.openai_api_key = "secret"
        server.openai_model = "gpt-5.6-luna"
        settings = server.get_ai_settings()
        self.assertTrue(settings["configured"])
        self.assertTrue(settings["hasApiKey"])
        self.assertNotIn("secret", json.dumps(settings))

    def test_match_import_rejects_unknown_format(self):
        server = object.__new__(UnarrServer)
        with self.assertRaises(ValueError):
            server.import_library_matches({"format": "unknown", "version": 1, "mappings": []})

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
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "unarr-web.sqlite3"
            server.database = AppDatabase(settings_file)
            server.write_trakt_settings()
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(server.database.load_settings()["client_id"], "client-id")

    def test_sqlite_backup_round_trip_includes_matches_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = AppDatabase(root / "source.sqlite3")
            database.save_settings({"client_id": "client-id"})
            database.save_match("local:one", {"type": "movie", "traktId": 42, "title": "Example"})
            database.cache_provider("torbox", [{"id": 7}])
            database.replace_library_items("local", [{"id": "local:one", "title": "Example"}])
            backup = root / "backup.sqlite3"
            database.backup_to(backup)
            restored = AppDatabase(root / "restored.sqlite3")
            restored.restore_from(backup)
            self.assertEqual(restored.load_settings()["client_id"], "client-id")
            self.assertEqual(restored.load_matches()["local:one"]["traktId"], 42)
            self.assertEqual(restored.get_provider_cache("torbox")[0][0]["id"], 7)
            self.assertEqual(restored.get_library_items("local")[0]["title"], "Example")

    def test_sqlite_restore_rejects_unrelated_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.sqlite3"
            invalid.write_bytes(b"not sqlite")
            database = AppDatabase(root / "current.sqlite3")
            with self.assertRaisesRegex(ValueError, "valid SQLite"):
                database.restore_from(invalid)

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
        with tempfile.TemporaryDirectory() as directory:
            server.database = AppDatabase(Path(directory) / "test.sqlite3")
            self.assertEqual(server.search_trakt("test", "recommended")[0]["title"], "Relevant")
            self.assertEqual(server.search_trakt("test", "popular")[0]["title"], "Popular")
            self.assertEqual(server.trakt_request.call_count, 1)

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
