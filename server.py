#!/usr/bin/env python3
"""Local web interface for an installed unarr CLI."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import html
import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from database import AppDatabase

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = Path(os.environ.get("UNARR_DATA_DIR", Path.home() / "Library" / "Application Support" / "unarr"))
LIBRARY_CACHE = DATA_ROOT / "library.json"
HLS_ROOT = Path(os.environ.get("UNARR_WEB_HLS_DIR", ROOT / ".cache" / "hls"))
TRAKT_IMAGE_ROOT = ROOT / ".cache" / "trakt-images"
TORBOX_MEDIA_ROOT = ROOT / ".cache" / "torbox-media"
PRIVATE_TORRENT_ROOT = ROOT / ".data" / "private-torrents"
LEGACY_TRAKT_SETTINGS_FILE = ROOT / ".cache" / "trakt-settings.json"
TRAKT_SETTINGS_FILE = Path(os.environ.get("UNARR_WEB_TRAKT_SETTINGS", ROOT / ".data" / "user-settings.json"))
DATABASE_FILE = Path(os.environ.get("UNARR_WEB_DATABASE", ROOT / ".data" / "unarr-web.sqlite3"))
INFO_HASH = re.compile(r"^(?:[a-fA-F0-9]{40}|[A-Z2-7a-z2-7]{32})$")
STREAM_URL = re.compile(r"Open this URL in your player:\s*(https?://\S+)")
BUFFER_PROGRESS = re.compile(r"Buffering:\s*(\d+)%")
DOWNLOAD_PROGRESS = re.compile(r"(\d+)%\s*\|\s*([^|]+)\|\s*Peers:\s*(\d+)\s*\|\s*Seeds:\s*(\d+)")
METADATA_TIMEOUT_SECONDS = 60
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv"}
MIN_LIBRARY_VIDEO_BYTES = 20 * 1024 * 1024
MIN_LIBRARY_VIDEO_SECONDS = 5 * 60
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}
FILTERS = {
    "type": ("--type", {"movie", "show"}),
    "quality": ("--quality", {"480p", "720p", "1080p", "2160p"}),
    "sort": ("--sort", {"relevance", "seeders", "year", "rating", "added"}),
}


class TraktRateLimitError(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Trakt rate limit reached. Try again in {retry_after} seconds.")


TORRENTDAY_URLS = (
    "https://tday.love/", "https://torrentday.cool/", "https://secure.torrentday.com/",
    "https://classic.torrentday.com/", "https://www.torrentday.com/", "https://www.torrentday.me/",
    "https://torrentday.it/", "https://td.findnemo.net/", "https://td.getcrazy.me/", "https://td.venom.global/",
    "https://td.workisboring.net/", "https://tday.findnemo.net/", "https://tday.getcrazy.me/", "https://tday.venom.global/",
    "https://tday.workisboring.net/",
)
TORRENTDAY_CATEGORIES = {
    "movie": (25, 96, 11, 5, 103, 3, 21, 22, 13, 44, 48, 1),
    "show": (24, 104, 32, 31, 33, 46, 82, 14, 26, 7, 34, 2),
}


class UnarrHandler(SimpleHTTPRequestHandler):
    server_version = "unarr-web/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self):
        request_path = urlparse(getattr(self, "path", "")).path
        if not request_path.startswith("/api/") and (request_path == "/" or Path(request_path).suffix in {".html", ".js", ".css"}):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' https: data:; connect-src 'self'; media-src http: https:")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self):
        request = urlparse(self.path)
        if request.path in {"/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
            self.send_response(302)
            self.send_header("Location", "/favicon.svg")
            self.end_headers()
            return
        if request.path == "/api/health":
            return self.command_json(["version"], transform=lambda text: {"ok": True, "version": text.strip()})
        if request.path == "/api/status":
            return self.command_json(["status", "--no-color"], transform=lambda text: {"output": text.strip()})
        if request.path == "/api/search":
            return self.search(parse_qs(request.query))
        if request.path == "/api/library":
            return self.library(parse_qs(request.query))
        if request.path == "/api/trakt/dashboard":
            return self.trakt_dashboard()
        if request.path == "/api/trakt/calendar":
            try:
                return self.send_json({"items": self.server.get_trakt_calendar(parse_qs(request.query))})
            except (ValueError, RuntimeError, PermissionError, urllib.error.URLError) as error:
                return self.error_json(400 if isinstance(error, ValueError) else 502, str(error))
        if request.path == "/api/trakt/custom":
            try:
                return self.send_json(self.server.get_trakt_custom(parse_qs(request.query)))
            except (ValueError, RuntimeError, PermissionError, urllib.error.URLError) as error:
                return self.error_json(400 if isinstance(error, ValueError) else 502, str(error))
        if request.path == "/api/trakt/continue":
            try:
                return self.send_json({"id": "continue", "title": "Continue watching", "items": self.server.get_trakt_continue()})
            except (RuntimeError, PermissionError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if request.path == "/api/trakt/settings":
            return self.send_json(self.server.get_trakt_settings())
        if request.path == "/api/torrentclaw/settings":
            return self.send_json(self.server.get_torrentclaw_settings())
        if request.path == "/api/private-trackers/settings":
            return self.send_json(self.server.get_private_tracker_settings())
        if request.path == "/api/ai/settings":
            return self.send_json(self.server.get_ai_settings())
        if request.path == "/api/library/matches/export":
            return self.send_json(self.server.export_library_matches())
        if request.path == "/api/activity":
            return self.send_json({"items": self.server.get_activity()})
        if request.path == "/api/trakt/auth":
            return self.send_json(self.server.get_trakt_auth())
        if request.path == "/api/trakt/details":
            return self.trakt_details(parse_qs(request.query))
        if request.path == "/api/trakt/search":
            return self.trakt_search(parse_qs(request.query))
        if request.path == "/api/settings/backup":
            return self.database_backup()
        if request.path == "/api/database/backup":
            return self.database_backup()
        if request.path.startswith("/api/trakt/image/"):
            return self.trakt_image(request.path[len("/api/trakt/image/"):], head_only=False)
        if request.path.startswith("/api/stream/"):
            return self.stream_status(request.path[len("/api/stream/"):])
        if request.path.startswith("/media/"):
            return self.serve_media(request.path)
        return super().do_GET()

    def do_HEAD(self):
        request = urlparse(self.path)
        if request.path.startswith("/media/"):
            return self.serve_media(request.path, head_only=True)
        if request.path.startswith("/api/trakt/image/"):
            return self.trakt_image(request.path[len("/api/trakt/image/"):], head_only=True)
        return super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in {"/api/download", "/api/stream", "/api/stream/tracks", "/api/library/stream", "/api/library/action", "/api/library/ai-match", "/api/library/matches/import", "/api/trakt/settings", "/api/trakt/auth", "/api/trakt/scrobble", "/api/trakt/watchlist", "/api/trakt/history", "/api/trakt/favorites", "/api/trakt/playback/remove", "/api/torrentclaw/settings", "/api/private-trackers/settings", "/api/ai/settings", "/api/torrentclaw/releases", "/api/torrentclaw/debrid/play", "/api/torrentclaw/debrid/download", "/api/settings/restore", "/api/database/restore"}:
            return self.error_json(404, "Not found.")
        if not self.same_origin():
            return self.error_json(403, "Cross-origin requests are not allowed.")
        if path == "/api/database/restore":
            return self.database_restore()
        body = self.read_json()
        if body is None:
            return self.error_json(400, "A valid JSON request is required.")
        if path == "/api/settings/restore":
            try:
                return self.send_json(self.server.restore_settings_backup(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not restore settings: {error}")
        if path == "/api/trakt/settings":
            try:
                return self.send_json(self.server.save_trakt_settings(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not save Trakt settings: {error}")
        if path == "/api/torrentclaw/settings":
            try:
                return self.send_json(self.server.save_torrentclaw_settings(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not save TorrentClaw settings: {error}")
        if path == "/api/private-trackers/settings":
            try:
                return self.send_json(self.server.save_private_tracker_settings(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not save private tracker settings: {error}")
        if path == "/api/ai/settings":
            try:
                return self.send_json(self.server.save_ai_settings(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not save AI settings: {error}")
        if path == "/api/library/ai-match":
            try:
                return self.send_json(self.server.ai_match_library(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, OSError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/library/matches/import":
            try:
                return self.send_json(self.server.import_library_matches(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, str(error))
        if path == "/api/torrentclaw/releases":
            try:
                return self.send_json(self.server.find_torrentclaw_releases(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except LookupError as error:
                return self.error_json(404, str(error))
            except RuntimeError as error:
                return self.error_json(503, str(error))
            except (OSError, urllib.error.URLError) as error:
                return self.error_json(502, f"Could not load TorrentClaw releases: {error}")
        if path in {"/api/torrentclaw/debrid/play", "/api/torrentclaw/debrid/download"}:
            info_hash = body.get("infoHash", "")
            if not isinstance(info_hash, str) or not INFO_HASH.fullmatch(info_hash):
                return self.error_json(400, "A valid torrent info hash is required.")
            try:
                torrent_id = body.get("torrentId")
                file_id = body.get("fileId")
                file_name = body.get("fileName") or body.get("title", "")
                result = self.server.start_torbox(info_hash, play=path.endswith("/play"), torrent_id=torrent_id, file_id=file_id, file_name=file_name)
                return self.send_json(result, 200 if result.get("url") else 202)
            except (RuntimeError, LookupError, PermissionError) as error:
                return self.error_json(502, str(error))
            except (OSError, urllib.error.URLError) as error:
                return self.error_json(502, f"TorBox request failed: {error}")
        if path == "/api/trakt/auth":
            try:
                return self.send_json(self.server.start_trakt_auth(), 202)
            except urllib.error.HTTPError as error:
                return self.error_json(502, getattr(error, "trakt_message", str(error)))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/trakt/scrobble":
            try:
                return self.send_json(self.server.scrobble_trakt(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/trakt/watchlist":
            try:
                return self.send_json(self.server.update_trakt_watchlist(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/trakt/history":
            try:
                return self.send_json(self.server.update_trakt_history(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/trakt/favorites":
            try:
                return self.send_json(self.server.update_trakt_favorite(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except TraktRateLimitError as error:
                return self.send_json({"error": str(error), "retryAfter": error.retry_after}, 429)
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/trakt/playback/remove":
            try:
                return self.send_json(self.server.remove_trakt_playback(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/library/stream":
            return self.start_library_stream(body)
        if path == "/api/library/action":
            try:
                return self.send_json(self.server.library_action(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except LookupError as error:
                return self.error_json(404, str(error))
            except PermissionError as error:
                return self.error_json(401, str(error))
            except (RuntimeError, OSError) as error:
                print(f"[library-action] {body.get('action')} failed for {body.get('itemId')}: {error}")
                return self.error_json(502, str(error))
        if path == "/api/stream/tracks":
            try:
                session_id = body.get("sessionId", "")
                audio_index = body.get("audioIndex", 0)
                subtitle_index = body.get("subtitleIndex", -1)
                self.server.select_remote_tracks(session_id, audio_index, subtitle_index)
                return self.send_json({"id": session_id, "status": "buffering"}, 202)
            except ValueError as error:
                return self.error_json(400, str(error))
            except (LookupError, RuntimeError, OSError) as error:
                return self.error_json(502, str(error))
        info_hash = body.get("infoHash", "")
        if not isinstance(info_hash, str) or not INFO_HASH.fullmatch(info_hash):
            return self.error_json(400, "A valid torrent info hash is required.")
        if path == "/api/stream":
            return self.start_stream(info_hash)
        return self.start_download(info_hash, str(body.get("title") or "")[:300])

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                raise ValueError
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def library(self, query=None):
        source = ((query or {}).get("source") or ["all"])[0]
        if source not in {"all", "local", "cloud", "favorites"}:
            return self.error_json(400, "Invalid library source.")
        try:
            cache = self.server.reconcile_library()
        except (OSError, json.JSONDecodeError) as error:
            return self.error_json(500, f"Could not read the unarr library: {error}")
        items = []
        for value in cache.get("items", []):
            item = dict(self.server.public_library_item(value), source="local", dateAdded=value.get("modTime"), folderTitle=Path(value.get("filePath", "")).parent.name)
            link = self.server.library_links.get(item["id"]) or {}
            if link:
                item.update(linked=True, trakt=link, image=link.get("image"), title=link.get("title") or item["title"], released=link.get("released"))
            items.append(item)
        cloud, favorites = [], []
        if source in {"all", "cloud", "favorites"}:
            cloud = self.server.database.get_library_items("cloud")
            if not cloud:
                try:
                    cloud = self.server.get_cloud_library()
                except RuntimeError:
                    cloud = []
        favorites = self.server.database.get_library_items("favorites")
        if not favorites and self.server.trakt_access_token:
            try:
                favorites = self.server.get_trakt_favorites()
            except (RuntimeError, PermissionError, urllib.error.URLError):
                favorites = []
        if favorites:
            local_ids = {(str((item.get("trakt") or {}).get("type")), str((item.get("trakt") or {}).get("traktId"))) for item in items if (item.get("trakt") or {}).get("traktId")}
            cloud_ids = {(str((item.get("trakt") or {}).get("type")), str((item.get("trakt") or {}).get("traktId"))) for item in cloud if (item.get("trakt") or {}).get("traktId")}
            favorites = [dict(item, availableLocal=(str(item.get("mediaType")), str((item.get("ids") or {}).get("trakt"))) in local_ids, availableCloud=(str(item.get("mediaType")), str((item.get("ids") or {}).get("trakt"))) in cloud_ids) for item in favorites]
            favorite_ids = {(str(item.get("mediaType")), str((item.get("ids") or {}).get("trakt"))) for item in favorites}
            for item in items + cloud:
                identity = item.get("trakt") or {}
                item["favorite"] = (str(identity.get("type")), str(identity.get("traktId"))) in favorite_ids
        selected = {"local": items, "cloud": cloud, "favorites": favorites}.get(source, items + cloud + favorites)
        media_filter = (((query or {}).get("media") or ["all"])[0])
        if media_filter not in {"all", "movie", "show", "unmatched", "duplicate"}:
            return self.error_json(400, "Invalid library media filter.")
        duplicates = self.server.library_duplicates(selected)
        selected, cleanup = self.server.clean_library_items(selected)
        if media_filter == "duplicate":
            selected = duplicates
        elif media_filter == "unmatched":
            selected = [item for item in selected if not (item.get("linked") or item.get("trakt") or item.get("ids", {}).get("trakt"))]
        elif media_filter in {"movie", "show"}:
            selected = [item for item in selected if self.server.library_media_type(item) == media_filter]
        date_from = (((query or {}).get("from") or [""])[0])
        date_to = (((query or {}).get("to") or [""])[0])
        if date_from or date_to:
            for value in (date_from, date_to):
                if value:
                    try:
                        datetime.date.fromisoformat(value)
                    except ValueError:
                        return self.error_json(400, "Invalid library date range.")
            def release_date(item):
                value = str(item.get("released") or item.get("year") or "")
                return f"{value}-01-01" if re.fullmatch(r"\d{4}", value) else value[:10]
            selected = [item for item in selected if (not date_from or release_date(item) >= date_from) and (not date_to or release_date(item) <= date_to)]
        title_query = (((query or {}).get("q") or [""])[0]).strip().casefold()
        if title_query:
            selected = [item for item in selected if title_query in " ".join(str(item.get(key) or "") for key in ("title", "folderTitle", "fileName")).casefold()]
        sort = (((query or {}).get("sort") or ["az"])[0])
        if sort not in {"az", "za", "added", "released"}:
            return self.error_json(400, "Invalid library sort.")
        if sort in {"az", "za"}:
            selected.sort(key=lambda item: str(item.get("title") or "").casefold(), reverse=sort == "za")
        else:
            selected.sort(key=lambda item: str(item.get("dateAdded") if sort == "added" else item.get("released") or item.get("year") or ""), reverse=True)
        total = len(selected)
        limit_value = ((query or {}).get("limit") or [None])[0]
        if limit_value is not None:
            try:
                limit = int(limit_value)
            except (TypeError, ValueError):
                return self.error_json(400, "Invalid library limit.")
            if limit < 1 or limit > 25000:
                return self.error_json(400, "Library limit must be between 1 and 25000.")
            selected = selected[:limit]
        return self.send_json({
            "items": selected, "total": total, "source": source, "counts": {"local": len(items), "cloud": len(cloud), "favorites": len(favorites)}, "scannedAt": cache.get("scannedAt"), "refreshedAt": cache.get("refreshedAt"),
            "transcode": {"available": bool(self.server.ffmpeg and self.server.ffprobe), "ffmpeg": self.server.ffmpeg},
            "scan": self.server.get_scan_state(), "cleanup": cleanup,
        })

    def trakt_dashboard(self):
        try:
            return self.send_json(self.server.get_trakt_dashboard())
        except Exception as error:
            return self.error_json(502, f"Trakt dashboard unavailable: {error}")

    def trakt_details(self, params):
        media_type = params.get("type", [""])[0]
        trakt_id = params.get("id", [""])[0]
        season_value = params.get("season", [None])[0]
        try:
            season = int(season_value) if season_value is not None else None
        except ValueError:
            return self.error_json(400, "Invalid season number.")
        try:
            return self.send_json(self.server.get_trakt_details(media_type, trakt_id, season))
        except ValueError as error:
            return self.error_json(400, str(error))
        except (RuntimeError, PermissionError, urllib.error.URLError) as error:
            return self.error_json(502, f"Could not load Trakt metadata: {error}")

    def trakt_search(self, params):
        query = params.get("q", [""])[0].strip()
        sort = params.get("sort", ["popular"])[0]
        if not 2 <= len(query) <= 120:
            return self.error_json(400, "Search must be between 2 and 120 characters.")
        if sort not in {"recommended", "popular"}:
            return self.error_json(400, "Invalid Trakt search sort.")
        try:
            return self.send_json({"results": self.server.search_trakt(query, sort), "limit": 20, "sort": sort})
        except (RuntimeError, PermissionError, urllib.error.URLError) as error:
            return self.error_json(502, f"Could not search Trakt: {error}")

    def settings_backup(self):
        payload = json.dumps(self.server.get_settings_backup(), indent=2).encode()
        username = re.sub(r"[^A-Za-z0-9_-]", "-", self.server.trakt_user_name() or "local")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="unarr-web-{username}-backup.json"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def database_backup(self):
        username = re.sub(r"[^A-Za-z0-9_-]", "-", self.server.trakt_user_name() or "local")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"unarr-web-{username}-backup.sqlite3"
            self.server.database.backup_to(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as backup:
                shutil.copyfileobj(backup, self.wfile)

    def database_restore(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 100 or length > 256 * 1024 * 1024:
            return self.error_json(400, "A SQLite backup smaller than 256 MB is required.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restore.sqlite3"
            remaining = length
            with path.open("wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return self.error_json(400, "The SQLite upload was incomplete.")
                    output.write(chunk)
                    remaining -= len(chunk)
            try:
                self.server.restore_database(path)
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not restore database: {error}")
        return self.send_json({"ok": True, "restored": True, "settings": self.server.get_trakt_settings()})

    def trakt_image(self, image_id, head_only=False):
        if not re.fullmatch(r"[a-f0-9]{32}", image_id):
            return self.error_json(404, "Artwork not found.")
        try:
            path = self.server.get_trakt_image(image_id)
        except (LookupError, OSError, urllib.error.URLError) as error:
            return self.error_json(404, f"Artwork unavailable: {error}")
        self.send_response(200)
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        if not head_only:
            with path.open("rb") as image:
                shutil.copyfileobj(image, self.wfile)

    def start_library_stream(self, body):
        item_id = body.get("itemId", "")
        quality = body.get("quality", "original")
        audio_index = body.get("audioIndex", 0)
        subtitle_index = body.get("subtitleIndex", -1)
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-f0-9]{64}", item_id):
            return self.error_json(400, "A valid library item ID is required.")
        if quality not in {"original", "1080p", "720p", "480p"}:
            return self.error_json(400, "Invalid playback quality.")
        if isinstance(audio_index, bool) or not isinstance(audio_index, int) or audio_index < 0 or audio_index > 99:
            return self.error_json(400, "Invalid audio track.")
        if isinstance(subtitle_index, bool) or not isinstance(subtitle_index, int) or subtitle_index < -1 or subtitle_index > 99:
            return self.error_json(400, "Invalid subtitle track.")
        try:
            session_id = self.server.create_library_stream(item_id, quality, audio_index, subtitle_index)
        except LookupError as error:
            return self.error_json(404, str(error))
        except RuntimeError as error:
            return self.error_json(503, str(error))
        except OSError as error:
            return self.error_json(502, f"Could not start HLS: {error}")
        return self.send_json({"id": session_id, "status": "buffering"}, 202)

    def serve_media(self, request_path, head_only=False):
        parts = request_path.split("/")
        if len(parts) != 4 or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", parts[2]) or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[3]):
            return self.error_json(404, "Media segment not found.")
        path = (HLS_ROOT / parts[2] / parts[3]).resolve()
        if HLS_ROOT.resolve() not in path.parents or not path.is_file():
            return self.error_json(404, "Media segment not found.")
        content_types = {".m3u8": "application/vnd.apple.mpegurl", ".m4s": "video/iso.segment", ".mp4": "video/mp4", ".vtt": "text/vtt; charset=utf-8"}
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-cache" if path.suffix == ".m3u8" else "public, max-age=31536000, immutable")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as media:
            shutil.copyfileobj(media, self.wfile)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/trakt/auth":
            if not self.same_origin():
                return self.error_json(403, "Cross-origin requests are not allowed.")
            try:
                self.server.disconnect_trakt()
            except OSError as error:
                return self.error_json(500, f"Could not disconnect Trakt: {error}")
            return self.send_json({"authenticated": False})
        if not path.startswith("/api/stream/"):
            return self.error_json(404, "Not found.")
        if not self.same_origin():
            return self.error_json(403, "Cross-origin requests are not allowed.")
        if self.server.stop_stream(path[len("/api/stream/"):]):
            return self.send_json({"status": "stopped"})
        return self.error_json(404, "Stream session not found.")

    def start_download(self, info_hash, title=""):
        """Start a long-running download without tying it to the HTTP request."""
        try:
            process = subprocess.Popen(
                [self.server.unarr_bin, "download", self.server.private_magnet(info_hash), "--no-color"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            return self.error_json(503, f"unarr executable not found: {self.server.unarr_bin}")
        except OSError as error:
            return self.error_json(502, f"Could not start unarr: {error}")

        threading.Thread(
            target=self.server.watch_download,
            args=(process, info_hash, self.server.add_activity("local", info_hash, "Downloading with Unarr", title=title, media_type="download")),
            name=f"unarr-download-{process.pid}",
            daemon=True,
        ).start()
        return self.send_json({"output": "Download started.", "pid": process.pid}, 202)

    def start_stream(self, info_hash):
        try:
            session_id = self.server.create_stream(info_hash)
        except RuntimeError as error:
            return self.error_json(429, str(error))
        except FileNotFoundError:
            return self.error_json(503, f"unarr executable not found: {self.server.unarr_bin}")
        except OSError as error:
            return self.error_json(502, f"Could not start unarr: {error}")
        return self.send_json({"id": session_id, "status": "buffering"}, 202)

    def stream_status(self, session_id):
        session = self.server.get_stream(session_id)
        if session is None:
            return self.error_json(404, "Stream session not found.")
        return self.send_json(session)

    def search(self, params):
        query = params.get("q", [""])[0].strip()
        if not 2 <= len(query) <= 120:
            return self.error_json(400, "Search must be between 2 and 120 characters.")
        args = ["search", query, "--json", "--limit", "20"]
        for key, (flag, allowed) in FILTERS.items():
            value = params.get(key, [""])[0]
            if value:
                if value not in allowed:
                    return self.error_json(400, f"Invalid {key} filter.")
                args.extend([flag, value])
        return self.command_json(args, raw_json=True)

    def command_json(self, args, *, status=200, transform=None, raw_json=False):
        try:
            result = subprocess.run(
                [self.server.unarr_bin, *args], capture_output=True, text=True,
                timeout=self.server.command_timeout, check=False,
            )
        except FileNotFoundError:
            return self.error_json(503, f"unarr executable not found: {self.server.unarr_bin}")
        except subprocess.TimeoutExpired:
            return self.error_json(504, "The unarr command timed out.")
        output = result.stdout or result.stderr
        if result.returncode:
            return self.error_json(502, output.strip() or f"unarr exited with code {result.returncode}")
        if raw_json:
            try:
                return self.send_json(json.loads(output), status)
            except json.JSONDecodeError:
                return self.error_json(502, "unarr returned invalid JSON.")
        return self.send_json(transform(output) if transform else {"output": output}, status)

    def same_origin(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        return not origin or origin in {f"http://{host}", f"https://{host}"}

    def error_json(self, status, message):
        return self.send_json({"error": message}, status)

    def send_json(self, value, status=200):
        payload = json.dumps(value).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, message, *args):
        print(f"[{self.log_date_time_string()}] {message % args}")


class UnarrServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, address, handler, unarr_bin, command_timeout=20):
        self.unarr_bin = unarr_bin
        self.command_timeout = command_timeout
        self.streams = {}
        self.stream_lock = threading.Lock()
        self.activity = []
        self.activity_lock = threading.Lock()
        self.torbox_index = []
        self.torbox_index_time = 0
        self.library_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.library_snapshot = None
        self.scan_timer = None
        self.scan_again_roots = None
        self.scan_state = {"status": "idle", "message": "Watching for library changes"}
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        HLS_ROOT.mkdir(parents=True, exist_ok=True)
        TRAKT_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
        TORBOX_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        PRIVATE_TORRENT_ROOT.mkdir(parents=True, exist_ok=True)
        PRIVATE_TORRENT_ROOT.chmod(0o700)
        self.clean_private_torrent_cache()
        self.database = AppDatabase(DATABASE_FILE)
        saved_trakt = self.load_trakt_settings()
        if self.database.is_empty() and saved_trakt:
            legacy_links = saved_trakt.pop("library_links", {})
            self.database.save_settings(saved_trakt)
            if isinstance(legacy_links, dict):
                self.database.save_matches(legacy_links)
        saved_trakt = self.database.load_settings()
        self.trakt_client_id = os.environ.get("TRAKT_CLIENT_ID", saved_trakt.get("client_id", ""))
        self.trakt_client_secret = os.environ.get("TRAKT_CLIENT_SECRET", saved_trakt.get("client_secret", ""))
        self.trakt_access_token = os.environ.get("TRAKT_ACCESS_TOKEN", saved_trakt.get("access_token", ""))
        self.trakt_refresh_token = saved_trakt.get("refresh_token", "")
        self.trakt_user = saved_trakt.get("user")
        self.torrentclaw_api_key = os.environ.get("TORRENTCLAW_API_KEY", saved_trakt.get("torrentclaw_api_key", ""))
        self.torbox_api_key = os.environ.get("TORBOX_API_KEY", saved_trakt.get("torbox_api_key", ""))
        self.torrentday_cookie = os.environ.get("TORRENTDAY_COOKIE", saved_trakt.get("torrentday_cookie", ""))
        self.torrentday_base_url = saved_trakt.get("torrentday_base_url", TORRENTDAY_URLS[0])
        self.torrentday_freeleech = bool(saved_trakt.get("torrentday_freeleech", False))
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", saved_trakt.get("openai_api_key", ""))
        self.openai_model = saved_trakt.get("openai_model", "gpt-5.6-luna")
        self.library_links = self.database.load_matches()
        self.trakt_auth = {"status": "idle"}
        self.trakt_auth_id = None
        self.trakt_cache = None
        self.trakt_cache_time = 0
        self.trakt_favorites_cache = None
        self.trakt_favorites_cache_time = 0
        self.trakt_lock = threading.Lock()
        self.trakt_library_lock = threading.Lock()
        self.trakt_library_list_id = saved_trakt.get("trakt_library_list_id")
        self.trakt_library_migrated = saved_trakt.get("trakt_library_migrated", "")
        saved_images, _ = self.database.get_provider_cache("trakt_artwork_sources")
        self.trakt_images = saved_images if isinstance(saved_images, dict) else {}
        self.provider_sync_stop = threading.Event()
        super().__init__(address, handler)
        threading.Thread(target=self.sync_library_providers, name="library-provider-sync", daemon=True).start()
        threading.Thread(target=self.repair_library_artwork, name="library-artwork-repair", daemon=True).start()

    def repair_library_artwork(self):
        repaired = {}
        for link in list(self.library_links.values()):
            if self.provider_sync_stop.is_set():
                return
            image = str(link.get("image") or "")
            image_id = image.rsplit("/", 1)[-1] if image.startswith("/api/trakt/image/") else ""
            if image_id and ((TRAKT_IMAGE_ROOT / f"{image_id}.webp").is_file() or image_id in self.trakt_images):
                continue
            key = (link.get("type"), link.get("traktId"))
            if key not in repaired:
                try:
                    metadata = self.get_trakt_details(str(key[0]), str(key[1]))
                    repaired[key] = metadata.get("poster") or metadata.get("fanart") or ""
                except Exception as error:
                    print(f"[artwork] Could not repair {key[0]} {key[1]}: {error}")
                    repaired[key] = ""
            if repaired[key]:
                link["image"] = repaired[key]
        if repaired:
            self.database.save_matches(self.library_links)

    def sync_library_providers(self):
        while not self.provider_sync_stop.is_set():
            try:
                if self.torbox_api_key:
                    self.get_cloud_library()
                if self.trakt_access_token:
                    self.get_trakt_favorites()
            except Exception as error:
                print(f"[provider-sync] {error}")
            self.provider_sync_stop.wait(60)

    @staticmethod
    def load_trakt_settings():
        for path in (TRAKT_SETTINGS_FILE, LEGACY_TRAKT_SETTINGS_FILE):
            try:
                value = json.loads(path.read_text())
                if isinstance(value, dict):
                    if path == LEGACY_TRAKT_SETTINGS_FILE and not TRAKT_SETTINGS_FILE.exists():
                        TRAKT_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
                        TRAKT_SETTINGS_FILE.write_text(json.dumps(value, indent=2))
                        TRAKT_SETTINGS_FILE.chmod(0o600)
                    return value
            except (OSError, json.JSONDecodeError):
                continue
        return {}

    def write_trakt_settings(self):
        payload = {
            "client_id": self.trakt_client_id, "client_secret": self.trakt_client_secret,
            "access_token": self.trakt_access_token, "refresh_token": self.trakt_refresh_token,
            "user": self.trakt_user,
            "trakt_library_list_id": getattr(self, "trakt_library_list_id", None),
            "trakt_library_migrated": getattr(self, "trakt_library_migrated", ""),
            "torrentclaw_api_key": getattr(self, "torrentclaw_api_key", ""),
            "torbox_api_key": getattr(self, "torbox_api_key", ""),
            "torrentday_cookie": getattr(self, "torrentday_cookie", ""),
            "torrentday_base_url": getattr(self, "torrentday_base_url", TORRENTDAY_URLS[0]),
            "torrentday_freeleech": bool(getattr(self, "torrentday_freeleech", False)),
            "openai_api_key": getattr(self, "openai_api_key", ""), "openai_model": getattr(self, "openai_model", "gpt-5.6-luna"),
        }
        self.database.save_settings(payload)

    def restore_database(self, path):
        self.database.restore_from(path)
        saved = self.database.load_settings()
        with self.trakt_lock:
            self.trakt_client_id = saved.get("client_id", "")
            self.trakt_client_secret = saved.get("client_secret", "")
            self.trakt_access_token = saved.get("access_token", "")
            self.trakt_refresh_token = saved.get("refresh_token", "")
            self.trakt_user = saved.get("user")
            self.trakt_library_list_id = saved.get("trakt_library_list_id")
            self.trakt_library_migrated = saved.get("trakt_library_migrated", "")
            self.torrentclaw_api_key = saved.get("torrentclaw_api_key", "")
            self.torbox_api_key = saved.get("torbox_api_key", "")
            self.torrentday_cookie = saved.get("torrentday_cookie", "")
            self.torrentday_base_url = saved.get("torrentday_base_url", TORRENTDAY_URLS[0])
            self.torrentday_freeleech = bool(saved.get("torrentday_freeleech", False))
            self.openai_api_key = saved.get("openai_api_key", "")
            self.openai_model = saved.get("openai_model", "gpt-5.6-luna")
            self.library_links = self.database.load_matches()
            self.torbox_index, self.torbox_index_time = [], 0
            self.trakt_cache = self.trakt_favorites_cache = None

    def get_trakt_settings(self):
        return {
            "configured": bool(self.trakt_client_id and self.trakt_client_secret),
            "clientId": self.trakt_client_id,
            "hasClientSecret": bool(self.trakt_client_secret),
            "authenticated": bool(self.trakt_access_token),
            "user": self.trakt_user,
            "storage": "server",
        }

    def get_torrentclaw_settings(self):
        return {"configured": bool(self.torrentclaw_api_key), "hasApiKey": bool(self.torrentclaw_api_key), "hasTorBoxKey": bool(self.torbox_api_key), "debridProvider": "torbox" if self.torbox_api_key else "", "baseUrl": "https://torrentclaw.com"}

    def get_private_tracker_settings(self):
        cookie = getattr(self, "torrentday_cookie", "")
        return {"configured": bool(cookie), "hasCookie": bool(cookie),
                "baseUrl": getattr(self, "torrentday_base_url", TORRENTDAY_URLS[0]), "baseUrls": list(TORRENTDAY_URLS),
                "freeleechOnly": bool(getattr(self, "torrentday_freeleech", False))}

    def save_private_tracker_settings(self, body):
        cookie, base_url = body.get("cookie", ""), body.get("baseUrl", TORRENTDAY_URLS[0])
        freeleech = body.get("freeleechOnly", False)
        if not isinstance(cookie, str) or len(cookie) > 8192 or "\r" in cookie or "\n" in cookie:
            raise ValueError("Enter a valid TorrentDay cookie header.")
        if base_url not in TORRENTDAY_URLS:
            raise ValueError("Choose a supported TorrentDay address.")
        if not isinstance(freeleech, bool):
            raise ValueError("Freeleech only must be on or off.")
        if cookie.strip():
            if "=" not in cookie:
                raise ValueError("Paste the full Cookie request header (name=value; name=value).")
            self.torrentday_cookie = cookie.strip()
        self.torrentday_base_url, self.torrentday_freeleech = base_url, freeleech
        self.write_trakt_settings()
        return self.get_private_tracker_settings()

    def get_ai_settings(self):
        return {"configured": bool(self.openai_api_key), "hasApiKey": bool(self.openai_api_key), "model": self.openai_model}

    def save_ai_settings(self, body):
        api_key, model = body.get("apiKey", ""), body.get("model", "gpt-5.6-luna")
        if not isinstance(api_key, str) or len(api_key.strip()) > 1000:
            raise ValueError("Enter a valid OpenAI API key.")
        if model not in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}:
            raise ValueError("Choose a supported GPT-5.6 model.")
        if api_key.strip():
            self.openai_api_key = api_key.strip()
        self.openai_model = model
        self.write_trakt_settings()
        return self.get_ai_settings()

    def ai_match_library(self, body):
        if not self.openai_api_key:
            raise PermissionError("Add an OpenAI API key in Settings first.")
        title, media_type, candidates = body.get("title"), body.get("type"), body.get("candidates")
        files = body.get("files")
        if not isinstance(title, str) or media_type not in {"movie", "show"} or not isinstance(files, list) or not isinstance(candidates, list):
            raise ValueError("A valid ambiguous library title is required.")
        safe_files = [{key: str(item.get(key) or "")[:500] for key in ("fileName", "folderTitle")} for item in files[:50] if isinstance(item, dict)]
        safe_candidates = [{"traktId": item.get("traktId"), "title": str(item.get("title") or "")[:300], "year": item.get("year"), "type": item.get("type"), "score": item.get("score")} for item in candidates[:12] if isinstance(item, dict)]
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "classification": {"type": "string", "enum": ["movie", "show", "episode", "extra", "unknown"]},
                "traktId": {"type": ["integer", "null"]}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            }, "required": ["classification", "traktId", "confidence", "reason"],
        }
        prompt = {
            "claimedType": media_type, "groupTitle": title[:300], "files": safe_files, "traktCandidates": safe_candidates,
            "rules": "Use folder hierarchy over episode filenames. Mark bonus features, interviews, deleted scenes, trailers, samples, and featurettes as extra. Select only a supplied traktId. Return null if uncertain.",
        }
        payload = {
            "model": self.openai_model, "store": False, "reasoning": {"effort": "low"},
            "input": [{"role": "system", "content": "Classify media-library files and choose the correct supplied Trakt candidate conservatively."}, {"role": "user", "content": json.dumps(prompt)}],
            "text": {"format": {"type": "json_schema", "name": "library_match", "strict": True, "schema": schema}},
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(8192).decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message") or detail
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"OpenAI returned HTTP {error.code}: {detail}") from error
        text_output = next((content.get("text") for item in result.get("output", []) if item.get("type") == "message" for content in item.get("content", []) if content.get("type") == "output_text"), None)
        try:
            decision = json.loads(text_output or "")
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenAI returned an invalid match decision.") from error
        valid_ids = {item.get("traktId") for item in safe_candidates}
        if decision.get("traktId") not in valid_ids:
            decision["traktId"] = None
            decision["confidence"] = 0
        return {"decision": decision, "model": result.get("model", self.openai_model), "usage": result.get("usage")}

    def export_library_matches(self):
        items = []
        try:
            cloud = self.get_cloud_library()
        except RuntimeError:
            cloud = []
        local_cache = self.reconcile_library().get("items", [])
        local = [dict(self.public_library_item(item), source="local", folderTitle=Path(item.get("filePath", "")).parent.name) for item in local_cache]
        for item in cloud + local:
            if item.get("linked") or item.get("id") in self.library_links:
                continue
            items.append({key: item.get(key) for key in ("id", "source", "title", "fileName", "folderTitle", "year", "season", "episode")})
        return {"format": "unarr-web-library-matches", "version": 1, "instructions": "Add a mappings array containing itemId, type, traktId, and title. Only include confident movie/show matches; omit extras and uncertain files.", "items": items, "mappings": []}

    def import_library_matches(self, body):
        if body.get("format") != "unarr-web-library-matches" or body.get("version") != 1 or not isinstance(body.get("mappings"), list):
            raise ValueError("This is not a supported unarr-web match manifest.")
        imported = 0
        for mapping in body["mappings"][:10000]:
            if not isinstance(mapping, dict):
                continue
            item_id, media_type, trakt_id = mapping.get("itemId"), mapping.get("type"), mapping.get("traktId")
            if not isinstance(item_id, str) or media_type not in {"movie", "show"} or isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
                continue
            self.library_links[item_id] = {"type": media_type, "traktId": trakt_id, "title": str(mapping.get("title") or "Matched title")[:300], "image": str(mapping.get("image") or "")[:1000], "released": str(mapping.get("released") or "")[:40]}
            imported += 1
        self.database.save_matches(self.library_links)
        return {"ok": True, "imported": imported}

    def save_torrentclaw_settings(self, body):
        api_key = body.get("apiKey", "")
        torbox_key = body.get("torboxApiKey", "")
        if not isinstance(api_key, str) or len(api_key.strip()) > 1000:
            raise ValueError("Enter a valid TorrentClaw API key.")
        if not isinstance(torbox_key, str) or len(torbox_key.strip()) > 1000:
            raise ValueError("Enter a valid TorBox API key.")
        if api_key.strip():
            self.torrentclaw_api_key = api_key.strip()
        if torbox_key.strip():
            self.torbox_api_key = torbox_key.strip()
            self.torbox_index = []
            self.torbox_index_time = 0
        self.write_trakt_settings()
        return self.get_torrentclaw_settings()

    def trakt_user_name(self):
        return (self.trakt_user or {}).get("username") or (self.trakt_user or {}).get("name")

    def get_settings_backup(self):
        return {
            "format": "unarr-web-settings", "version": 1, "exportedAt": int(time.time()),
            "profile": self.trakt_user_name() or "local",
            "trakt": {
                "clientId": self.trakt_client_id, "clientSecret": self.trakt_client_secret,
                "accessToken": self.trakt_access_token, "refreshToken": self.trakt_refresh_token,
                "user": self.trakt_user,
            },
            "torrentclaw": {"apiKey": getattr(self, "torrentclaw_api_key", ""), "torboxApiKey": getattr(self, "torbox_api_key", "")},
            "privateTrackers": {"torrentDay": {"cookie": getattr(self, "torrentday_cookie", ""), "baseUrl": getattr(self, "torrentday_base_url", TORRENTDAY_URLS[0]), "freeleechOnly": bool(getattr(self, "torrentday_freeleech", False))}},
            "ai": {"apiKey": getattr(self, "openai_api_key", ""), "model": getattr(self, "openai_model", "gpt-5.6-luna")},
            "libraryLinks": getattr(self, "library_links", {}),
        }

    def restore_settings_backup(self, backup):
        if backup.get("format") != "unarr-web-settings" or backup.get("version") != 1 or not isinstance(backup.get("trakt"), dict):
            raise ValueError("This is not a supported unarr-web settings backup.")
        trakt = backup["trakt"]
        fields = ("clientId", "clientSecret", "accessToken", "refreshToken")
        if any(not isinstance(trakt.get(field, ""), str) or len(trakt.get(field, "")) > 1000 for field in fields):
            raise ValueError("The backup contains invalid Trakt credentials.")
        user = trakt.get("user")
        if user is not None and not isinstance(user, dict):
            raise ValueError("The backup contains an invalid user profile.")
        torrentclaw = backup.get("torrentclaw") or {}
        api_key = torrentclaw.get("apiKey", "")
        torbox_key = torrentclaw.get("torboxApiKey", "")
        if not isinstance(torrentclaw, dict) or not isinstance(api_key, str) or len(api_key) > 1000 or not isinstance(torbox_key, str) or len(torbox_key) > 1000:
            raise ValueError("The backup contains invalid TorrentClaw settings.")
        ai = backup.get("ai") or {}
        torrentday = (backup.get("privateTrackers") or {}).get("torrentDay") or {}
        td_cookie, td_url, td_free = torrentday.get("cookie", ""), torrentday.get("baseUrl", TORRENTDAY_URLS[0]), torrentday.get("freeleechOnly", False)
        if not isinstance(torrentday, dict) or not isinstance(td_cookie, str) or len(td_cookie) > 8192 or td_url not in TORRENTDAY_URLS or not isinstance(td_free, bool):
            raise ValueError("The backup contains invalid private tracker settings.")
        openai_key, openai_model = ai.get("apiKey", ""), ai.get("model", "gpt-5.6-luna")
        library_links = backup.get("libraryLinks") or {}
        if not isinstance(ai, dict) or not isinstance(openai_key, str) or len(openai_key) > 1000 or openai_model not in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"} or not isinstance(library_links, dict):
            raise ValueError("The backup contains invalid AI or library-match settings.")
        with self.trakt_lock:
            self.trakt_client_id = trakt.get("clientId", "")
            self.trakt_client_secret = trakt.get("clientSecret", "")
            self.trakt_access_token = trakt.get("accessToken", "")
            self.trakt_refresh_token = trakt.get("refreshToken", "")
            self.trakt_user = user
            self.trakt_auth = {"status": "idle"}
            self.trakt_auth_id = None
            self.trakt_cache = None
            self.torrentclaw_api_key = api_key
            self.torbox_api_key = torbox_key
            self.torrentday_cookie, self.torrentday_base_url, self.torrentday_freeleech = td_cookie, td_url, td_free
            self.openai_api_key = openai_key
            self.openai_model = openai_model
            self.library_links = library_links
        self.write_trakt_settings()
        return self.get_trakt_settings()

    def torrentclaw_request(self, params):
        url = "https://torrentclaw.com/api/v1/search?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "unarr-web/0.1"}
        if self.torrentclaw_api_key:
            headers["Authorization"] = f"Bearer {self.torrentclaw_api_key}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("TorrentClaw API key was rejected") from error
            if error.code == 429:
                raise RuntimeError("TorrentClaw rate limit reached; try again shortly") from error
            raise RuntimeError(f"TorrentClaw returned HTTP {error.code}") from error

    @staticmethod
    def _bencode_end(data, offset=0):
        if offset >= len(data):
            raise ValueError("Truncated torrent metadata.")
        token = data[offset:offset + 1]
        if token == b"i":
            end = data.find(b"e", offset + 1)
            if end < 0:
                raise ValueError("Invalid torrent integer.")
            int(data[offset + 1:end])
            return end + 1
        if token in {b"l", b"d"}:
            cursor = offset + 1
            while cursor < len(data) and data[cursor:cursor + 1] != b"e":
                cursor = UnarrServer._bencode_end(data, cursor)
                if token == b"d":
                    cursor = UnarrServer._bencode_end(data, cursor)
            if cursor >= len(data):
                raise ValueError("Unterminated torrent container.")
            return cursor + 1
        colon = data.find(b":", offset, min(len(data), offset + 24))
        if colon < 0:
            raise ValueError("Invalid torrent string.")
        length = int(data[offset:colon])
        end = colon + 1 + length
        if length < 0 or end > len(data):
            raise ValueError("Truncated torrent string.")
        return end

    @staticmethod
    def torrent_metadata(data):
        if not isinstance(data, bytes) or not data.startswith(b"d") or len(data) > 10 * 1024 * 1024:
            raise ValueError("TorrentDay returned invalid torrent metadata.")
        cursor, info_slice, trackers = 1, None, []
        while cursor < len(data) and data[cursor:cursor + 1] != b"e":
            key_end = UnarrServer._bencode_end(data, cursor)
            colon = data.find(b":", cursor, key_end)
            key = data[colon + 1:key_end]
            value_start = key_end
            value_end = UnarrServer._bencode_end(data, value_start)
            if key == b"info":
                info_slice = data[value_start:value_end]
            elif key == b"announce":
                value_colon = data.find(b":", value_start, value_end)
                trackers.append(data[value_colon + 1:value_end].decode("utf-8", "ignore"))
            cursor = value_end
        if not info_slice:
            raise ValueError("Torrent metadata has no info dictionary.")
        return hashlib.sha1(info_slice).hexdigest(), [value for value in trackers if value.startswith(("http://", "https://", "udp://"))]

    def _torrentday_fetch(self, url):
        parsed = urllib.parse.urlparse(url)
        allowed = {urllib.parse.urlparse(value).hostname for value in TORRENTDAY_URLS}
        if parsed.scheme != "https" or parsed.hostname not in allowed:
            raise RuntimeError("TorrentDay returned an unsafe download address.")
        request = urllib.request.Request(url, headers={"Accept": "application/json, application/x-bittorrent", "Cookie": self.torrentday_cookie, "User-Agent": "unarr-web/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if urllib.parse.urlparse(response.geturl()).hostname not in allowed:
                    raise RuntimeError("TorrentDay redirected outside its supported addresses.")
                data = response.read(10 * 1024 * 1024 + 1)
                if len(data) > 10 * 1024 * 1024:
                    raise RuntimeError("TorrentDay response was too large.")
                if data.startswith(b"\x1f\x8b"):
                    try:
                        data = gzip.decompress(data)
                    except (OSError, EOFError) as error:
                        raise RuntimeError("TorrentDay returned invalid compressed data.") from error
                    if len(data) > 10 * 1024 * 1024:
                        raise RuntimeError("TorrentDay decompressed response was too large.")
                return data
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("TorrentDay cookie was rejected; copy a fresh Cookie header from your browser.") from error
            raise RuntimeError(f"TorrentDay returned HTTP {error.code}.") from error

    @staticmethod
    def release_traits(title):
        quality = next((value for value in ("2160p", "1080p", "720p", "480p") if value.lower() in title.lower()), "Unknown")
        codec = "HEVC" if re.search(r"\b(?:x265|h.?265|hevc)\b", title, re.I) else "AVC" if re.search(r"\b(?:x264|h.?264|avc)\b", title, re.I) else ""
        score = {"2160p": 400, "1080p": 300, "720p": 200, "480p": 100}.get(quality, 0)
        return quality, codec, score

    @staticmethod
    def clean_private_torrent_cache(max_age=7 * 24 * 60 * 60, max_files=400):
        """Keep passkey-bearing torrent metadata private and bounded."""
        now = time.time()
        try:
            files = sorted(
                (path for path in PRIVATE_TORRENT_ROOT.iterdir() if path.is_file() and path.suffix in {".torrent", ".json"}),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for index, path in enumerate(files):
            try:
                path.chmod(0o600)
                if index >= max_files or now - path.stat().st_mtime > max_age:
                    path.unlink()
            except OSError:
                continue

    @staticmethod
    def parse_torrentday_html(data, base_url, limit=12):
        markup = data.decode("utf-8", "replace")
        results = []
        for match in re.finditer(r"<tr\b[^>]*>([\s\S]*?)</tr>", markup, re.I):
            row = match.group(1)
            name_cell = re.search(r'<td\b[^>]*class\s*=\s*["\'][^"\']*\btorrentNameInfo\b[^"\']*["\'][^>]*>([\s\S]*?)</td>', row, re.I)
            if not name_cell:
                continue
            title = ""
            for anchor in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", name_cell.group(1), re.I):
                href = re.search(r'\bhref\s*=\s*["\']([^"\']*)["\']', anchor.group(1), re.I)
                if href and re.match(r"^/t/\d+", html.unescape(href.group(1))):
                    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", anchor.group(2)))).strip()
                    break
            download_url = ""
            for href in re.findall(r'\bhref\s*=\s*["\']([^"\']*)["\']', row, re.I):
                href = html.unescape(href)
                if re.match(r"^/download\.php/\d+/.+\.torrent(?:\?.*)?$", href, re.I):
                    download_url = urllib.parse.urljoin(base_url, href)
                    break
            if not download_url:
                continue
            seed_cell = re.search(r'<td\b[^>]*class\s*=\s*["\'][^"\']*\bseedersInfo\b[^"\']*["\'][^>]*>([\s\S]*?)</td>', row, re.I)
            seed_text = re.sub(r"<[^>]*>", " ", seed_cell.group(1)) if seed_cell else ""
            size_match = re.search(r"([\d.]+)\s*(KB|MB|GB|TB)\b", re.sub(r"<[^>]*>", " ", row), re.I)
            size = round(float(size_match.group(1)) * 1024 ** {"KB": 1, "MB": 2, "GB": 3, "TB": 4}[size_match.group(2).upper()]) if size_match else 0
            torrent_id = re.search(r"/download\.php/(\d+)/", download_url, re.I).group(1)
            results.append({"t": torrent_id, "name": title or "TorrentDay result", "downloadUrl": download_url,
                            "seeders": int(re.search(r"\d+", seed_text).group()) if re.search(r"\d+", seed_text) else 0, "size": size})
            if len(results) >= limit:
                break
        return results

    def find_torrentday_releases(self, body):
        if not getattr(self, "torrentday_cookie", ""):
            return []
        media_type, title = body.get("type"), body.get("title", "")
        categories = ";".join(str(value) for value in TORRENTDAY_CATEGORIES[media_type])
        terms = []
        imdb_id = body.get("imdbId")
        if isinstance(imdb_id, str) and re.fullmatch(r"tt\d{5,12}", imdb_id):
            terms.append(imdb_id)
        terms.append(title)
        if media_type == "show":
            terms.append(f"S{int(body['season']):02d}E{int(body['episode']):02d}")
        url = f"{self.torrentday_base_url}t.json?{categories}{';free' if self.torrentday_freeleech else ''};q={urllib.parse.quote_plus(' '.join(terms))}"
        json_data = self._torrentday_fetch(url)
        try:
            payload = json.loads(json_data)
            rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
        except (json.JSONDecodeError, UnicodeDecodeError):
            rows = []
        if not rows:
            if re.search(br"(?:/login\.php|name=[\"']login)", json_data, re.I):
                raise PermissionError("TorrentDay session cookie is missing or expired.")
            query = title + (f" S{int(body['season']):02d}E{int(body['episode']):02d}" if media_type == "show" else "")
            html_url = urllib.parse.urljoin(self.torrentday_base_url, "/t") + "?" + urllib.parse.urlencode({"q": query, "qf": ""})
            html_data = self._torrentday_fetch(html_url)
            if re.search(br"(?:/login\.php|name=[\"']login)", html_data, re.I):
                raise PermissionError("TorrentDay session cookie is missing or expired.")
            rows = self.parse_torrentday_html(html_data, self.torrentday_base_url)

        def convert(row):
            torrent_id = str(row.get("t") or "")
            if not torrent_id.isdigit():
                return None
            download_url = row.get("downloadUrl") or f"{self.torrentday_base_url}download.php/{torrent_id}/{torrent_id}.torrent"
            data = self._torrentday_fetch(download_url)
            info_hash, trackers = self.torrent_metadata(data)
            path = PRIVATE_TORRENT_ROOT / f"{info_hash}.torrent"
            path.write_bytes(data)
            path.chmod(0o600)
            metadata_path = PRIVATE_TORRENT_ROOT / f"{info_hash}.json"
            metadata_path.write_text(json.dumps({"trackers": trackers}))
            metadata_path.chmod(0o600)
            raw_title = str(row.get("name") or title)[:500]
            quality, codec, score = self.release_traits(raw_title)
            return {"infoHash": info_hash, "rawTitle": raw_title, "quality": quality, "codec": codec,
                    "sourceType": "Private tracker", "sizeBytes": int(row.get("size") or 0),
                    "seeders": int(row.get("seeders") or 0), "qualityScore": score, "hdrType": "HDR" if "HDR" in raw_title.upper() else "",
                    "audioCodec": "", "verified": True, "sourceGroup": "torrentday", "tracker": "TorrentDay", "private": True}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            releases = [value for value in executor.map(convert, [row for row in rows[:12] if isinstance(row, dict)]) if value]
        self.clean_private_torrent_cache()
        return releases

    def private_magnet(self, info_hash):
        metadata = PRIVATE_TORRENT_ROOT / f"{info_hash.lower()}.json"
        try:
            trackers = json.loads(metadata.read_text()).get("trackers", [])
        except (OSError, json.JSONDecodeError):
            trackers = []
        return "magnet:?" + urllib.parse.urlencode([("xt", f"urn:btih:{info_hash}"), *(("tr", value) for value in trackers)]) if trackers else info_hash

    def find_torrentclaw_releases(self, body):
        if not self.torrentclaw_api_key and not getattr(self, "torrentday_cookie", ""):
            raise RuntimeError("Add a TorrentClaw API key or private tracker cookie in Settings before playing.")
        media_type = body.get("type")
        title = body.get("title", "")
        imdb_id = body.get("imdbId")
        tmdb_id = body.get("tmdbId")
        season = body.get("season")
        episode = body.get("episode")
        if media_type not in {"movie", "show"} or not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise ValueError("Invalid title for TorrentClaw playback.")
        params = {"type": media_type, "availability": "available", "sort": "seeders", "limit": 5}
        if isinstance(imdb_id, str) and re.fullmatch(r"tt\d{5,12}", imdb_id):
            params["imdbid"] = imdb_id
        elif str(tmdb_id or "").isdigit():
            params["tmdbid"] = str(tmdb_id)
        else:
            params["q"] = title
        if media_type == "show":
            if isinstance(season, bool) or not isinstance(season, int) or season < 0 or season > 999:
                raise ValueError("Select a valid season.")
            if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0 or episode > 9999:
                raise ValueError("Select a valid episode.")
            params.update({"season": season, "episode": episode})
        private_future = None
        if getattr(self, "torbox_api_key", ""):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                library_future = executor.submit(self.find_torbox_library, body)
                search_future = executor.submit(self.search_torbox, body)
                torrentclaw_future = executor.submit(self.torrentclaw_request, params) if self.torrentclaw_api_key else None
                private_future = executor.submit(self.find_torrentday_releases, body)
                torbox_library = library_future.result()
                torbox_search = search_future.result()
                response = torrentclaw_future.result() if torrentclaw_future else {"results": []}
                private_releases = private_future.result()
        else:
            torbox_library, torbox_search = [], []
            response = self.torrentclaw_request(params) if self.torrentclaw_api_key else {"results": []}
            private_releases = self.find_torrentday_releases(body)
        torrents = [torrent for item in response.get("results", []) for torrent in item.get("torrents", []) if INFO_HASH.fullmatch(str(torrent.get("infoHash", "")))]
        if not torrents and not private_releases:
            raise LookupError("No configured release provider found a playable torrent for this title.")
        fields = ("infoHash", "rawTitle", "quality", "codec", "sourceType", "sizeBytes", "seeders", "qualityScore", "hdrType", "audioCodec", "verified")
        releases = [{key: torrent.get(key) for key in fields} for torrent in torrents]
        cached = self.torbox_cached([item["infoHash"] for item in releases]) if getattr(self, "torbox_api_key", "") else set()
        for release in releases:
            release["instant"] = release["infoHash"].lower() in cached
            release["debridProvider"] = "TorBox" if getattr(self, "torbox_api_key", "") else None
            release["sourceGroup"] = "torrentclaw"
        private_cached = self.torbox_cached([item["infoHash"] for item in private_releases]) if getattr(self, "torbox_api_key", "") and private_releases else set()
        for release in private_releases:
            release["instant"] = release["infoHash"].lower() in private_cached
            release["debridProvider"] = "TorBox" if getattr(self, "torbox_api_key", "") else None
        releases.sort(key=lambda item: (item["instant"], item.get("qualityScore") or 0, item.get("seeders") or 0), reverse=True)
        known = {item["infoHash"].lower() for item in torbox_library + torbox_search}
        releases = [item for item in releases if item["infoHash"].lower() not in known]
        return {"groups": [
            {"id": "torbox-library", "title": "Cached in your TorBox", "releases": torbox_library},
            {"id": "torbox-search", "title": "TorBox search", "releases": torbox_search},
            {"id": "torrentclaw", "title": "TorrentClaw results", "releases": releases},
            {"id": "torrentday", "title": "TorrentDay (private)", "releases": private_releases},
        ], "releases": torbox_library + torbox_search + releases + private_releases, "debridConfigured": bool(getattr(self, "torbox_api_key", "")), "debridProvider": "TorBox" if getattr(self, "torbox_api_key", "") else None}

    def find_torrentclaw_release(self, body):
        """Compatibility helper for callers that still request one release."""
        return self.find_torrentclaw_releases(body)["releases"][0]

    def torbox_request(self, path, *, form=None, json_body=None, files=None):
        if not self.torbox_api_key:
            raise RuntimeError("Add a TorBox API key in Settings first.")
        command = ["curl", "-sS", "--max-time", "45", "-H", f"Authorization: Bearer {self.torbox_api_key}", "-H", "Accept: application/json", "-H", "User-Agent: unarr-web/0.1"]
        for key, value in (form or {}).items():
            command.extend(["-F", f"{key}={value}"])
        for key, value in (files or {}).items():
            command.extend(["-F", f"{key}=@{value}"])
        if json_body is not None:
            command.extend(["-H", "Content-Type: application/json", "--data", json.dumps(json_body, separators=(",", ":"))])
        command.append(f"https://api.torbox.app/v1/api/torrents/{path}")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=50, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("TorBox did not respond before the request timed out.") from error
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"TorBox transport failed with curl code {result.returncode}.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("TorBox returned an invalid response.") from error
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(payload.get("detail") or payload.get("error") or "TorBox rejected the request.")
        if isinstance(payload, dict) and "detail" in payload and "data" not in payload:
            raise RuntimeError(str(payload["detail"]))
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def torbox_cached(self, hashes):
        query = urllib.parse.urlencode({"hash": ",".join(hashes), "format": "object", "list_files": "false"})
        data = self.torbox_request(f"checkcached?{query}")
        if isinstance(data, dict):
            return {str(key).lower() for key, value in data.items() if value}
        return {str(item.get("hash", "")).lower() for item in (data or []) if isinstance(item, dict)}

    def refresh_torbox_index(self, force=False):
        if not force and self.torbox_index and time.time() - self.torbox_index_time < 60:
            return self.torbox_index
        if not force and not self.torbox_index:
            cached, synced_at = self.database.get_provider_cache("torbox")
            if isinstance(cached, list):
                self.torbox_index, self.torbox_index_time = cached, synced_at
                if time.time() - synced_at < 60:
                    return self.torbox_index
        try:
            data = self.torbox_request("mylist?" + urllib.parse.urlencode({"bypass_cache": "true", "limit": 1000}))
        except RuntimeError:
            if self.torbox_index:
                return self.torbox_index
            raise
        self.torbox_index = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        self.torbox_index_time = time.time()
        self.database.cache_provider("torbox", self.torbox_index)
        return self.torbox_index

    def get_cloud_library(self):
        if not self.torbox_api_key:
            return []
        values = []
        for torrent in self.refresh_torbox_index():
            state = str(torrent.get("download_state") or torrent.get("status") or "").lower()
            if state not in {"cached", "completed", "uploading", "seeding"}:
                continue
            info_hash = str(torrent.get("hash") or torrent.get("info_hash") or "")
            torrent_id = torrent.get("id") or torrent.get("torrent_id")
            for file in torrent.get("files") or []:
                full_name = str(file.get("name") or file.get("short_name") or "")
                name = str(file.get("short_name") or Path(full_name).name or "")
                if Path(name).suffix.lower() not in VIDEO_EXTENSIONS and not str(file.get("mimetype", "")).startswith("video/"):
                    continue
                file_id = file.get("id")
                item_id = f"cloud:{torrent_id}:{file_id}"
                link = self.library_links.get(item_id) or {}
                values.append({
                    "id": item_id, "source": "cloud", "title": link.get("title") or name, "fileName": name,
                    "fileSize": file.get("size") or 0, "infoHash": info_hash, "torrentId": torrent_id,
                    "fileId": file_id, "linked": bool(link), "trakt": link, "image": link.get("image"),
                    "released": link.get("released"), "dateAdded": torrent.get("created_at") or torrent.get("updated_at") or torrent.get("download_finished"),
                    "season": (self.library_episode({"fileName": name}) or (None, None))[0],
                    "episode": (self.library_episode({"fileName": name}) or (None, None))[1],
                    "upstreamFolder": str(torrent.get("name") or Path(full_name).parts[0] if Path(full_name).parts else "TorBox download"),
                    "folderTitle": Path(full_name).parent.name if Path(full_name).parent.name not in {"", "."} else str(torrent.get("name") or ""),
                })
        self.database.replace_library_items("cloud", values)
        return values

    @staticmethod
    def library_episode(item):
        if item.get("season") is not None and item.get("episode") is not None:
            return int(item["season"]), int(item["episode"])
        name = str(item.get("fileName") or "")
        match = re.search(r"(?i)(?:\bS(\d{1,3})[ ._-]*E(\d{1,4})\b|\b(\d{1,3})x(\d{1,4})\b)", name)
        return (int(match.group(1) or match.group(3)), int(match.group(2) or match.group(4))) if match else None

    @staticmethod
    def library_quality_score(item):
        text = " ".join(str(item.get(key) or "") for key in ("quality", "fileName", "folderTitle", "codec")).lower()
        resolution = next((value for token, value in (("4320", 5), ("2160", 4), ("1080", 3), ("720", 2), ("480", 1)) if token in text), 0)
        source = 3 if re.search(r"(?:remux|blu[ ._-]?ray)", text) else 2 if re.search(r"web[ ._-]?(?:dl|rip)", text) else 1
        codec = 2 if re.search(r"(?:hevc|h[ ._-]?265|x265|av1)", text) else 1 if re.search(r"(?:h[ ._-]?264|x264)", text) else 0
        hdr = 1 if re.search(r"(?:dolby[ ._-]?vision|\bdv\b|hdr10|\bhdr\b)", text) else 0
        return resolution, source, hdr, codec, int(item.get("fileSize") or 0)

    @classmethod
    def library_media_type(cls, item):
        linked = item.get("trakt") or {}
        if linked.get("type") in {"movie", "show"}:
            return linked["type"]
        if item.get("mediaType") in {"movie", "show"}:
            return item["mediaType"]
        return "show" if cls.library_episode(item) else "movie"

    @classmethod
    def library_duplicates(cls, items):
        best, duplicates = {}, []
        for item in items:
            key = cls.library_duplicate_key(item)
            if key is None:
                continue
            current = best.get(key)
            if current is None:
                best[key] = item
            elif cls.library_quality_score(item) > cls.library_quality_score(current):
                duplicates.append(dict(current, duplicate=True))
                best[key] = item
            else:
                duplicates.append(dict(item, duplicate=True))
        return duplicates

    @classmethod
    def library_duplicate_key(cls, item):
        if item.get("source") == "favorites":
            return None
        link = item.get("trakt") or {}
        media_type, trakt_id = link.get("type"), link.get("traktId")
        episode = cls.library_episode(item)
        if media_type == "show" and trakt_id and episode:
            return "show", str(trakt_id), episode[0], episode[1]
        if media_type == "movie" and trakt_id and not re.search(r"(?i)\b(?:cd|disc|part)[ ._-]*\d+\b", str(item.get("fileName") or "")):
            return "movie", str(trakt_id)
        if episode:
            value = str(item.get("folderTitle") or item.get("fileName") or "")
            title = re.split(r"(?i)\b(?:S\d{1,3}[ ._-]*E\d{1,4}|\d{1,3}x\d{1,4}|season[ ._-]*\d+)\b", value, maxsplit=1)[0]
            title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            return ("episode", title, episode[0], episode[1]) if title else None
        return None

    @classmethod
    def clean_library_items(cls, items):
        kept, positions, hidden = [], {}, {"nonVideo": 0, "tooSmall": 0, "tooShort": 0, "duplicates": 0}
        for item in items:
            if item.get("source") != "favorites":
                extension = Path(str(item.get("fileName") or "")).suffix.lower()
                if extension not in VIDEO_EXTENSIONS:
                    hidden["nonVideo"] += 1
                    continue
                size = int(item.get("fileSize") or 0)
                if 0 < size < MIN_LIBRARY_VIDEO_BYTES:
                    hidden["tooSmall"] += 1
                    continue
                duration = float((((item.get("mediaInfo") or {}).get("video") or {}).get("duration") or item.get("durationSeconds") or 0))
                if 0 < duration < MIN_LIBRARY_VIDEO_SECONDS:
                    hidden["tooShort"] += 1
                    continue
            key = cls.library_duplicate_key(item)
            if key is None or key not in positions:
                if key is not None:
                    positions[key] = len(kept)
                kept.append(item)
                continue
            hidden["duplicates"] += 1
            index = positions[key]
            if cls.library_quality_score(item) > cls.library_quality_score(kept[index]):
                kept[index] = item
        hidden["total"] = sum(hidden.values())
        return kept, hidden

    def get_trakt_favorites(self):
        if not self.trakt_access_token:
            return []
        migration_key = str((getattr(self, "trakt_user", None) or {}).get("username") or "connected")
        list_id = getattr(self, "trakt_library_list_id", None)
        if not list_id or getattr(self, "trakt_library_migrated", "") != migration_key:
            list_id = self.ensure_trakt_library_list()
        if self.trakt_favorites_cache is None:
            cached, synced_at = self.database.get_provider_cache("trakt_library_list")
            if isinstance(cached, list):
                self.trakt_favorites_cache = cached
                self.trakt_favorites_cache_time = time.monotonic() - max(0, time.time() - synced_at)
        if self.trakt_favorites_cache is not None and time.monotonic() - self.trakt_favorites_cache_time < 300:
            return self.trakt_favorites_cache
        values = []
        for value in self.trakt_request(f"/users/me/lists/{list_id}/items?limit=1000", authenticated=True):
            item = self.normalize_trakt_item(value, "favorites")
            if item.get("mediaType") not in {"movie", "show"}:
                continue
            item.update(id=f"favorite:{item['mediaType']}:{item.get('ids', {}).get('trakt')}", source="favorites", favorite=True, fileName="Trakt library list", fileSize=0, dateAdded=item.get("listedAt"), released=item.get("calendarAt") or item.get("year"))
            values.append(item)
        self.trakt_favorites_cache = values
        self.trakt_favorites_cache_time = time.monotonic()
        self.database.cache_provider("trakt_library_list", values)
        self.database.replace_library_items("favorites", values)
        return values

    def library_action(self, body):
        action, item_id = body.get("action"), body.get("itemId")
        if action not in {"link", "delete", "unfavorite", "download-local"} or not isinstance(item_id, str):
            raise ValueError("A valid library action and item are required.")
        if action == "link":
            media_type, trakt_id, title = body.get("type"), body.get("traktId"), body.get("title")
            if media_type not in {"movie", "show"} or isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
                raise ValueError("Choose a valid Trakt movie or show.")
            self.library_links[item_id] = {"type": media_type, "traktId": trakt_id, "title": str(title or "Matched title")[:300], "image": str(body.get("image") or "")[:1000], "released": str(body.get("released") or "")[:40]}
            self.database.save_match(item_id, self.library_links[item_id])
            return {"ok": True, "linked": self.library_links[item_id]}
        if action == "unfavorite":
            match = re.fullmatch(r"favorite:(movie|show):(\d+)", item_id)
            if not match or not self.trakt_access_token:
                raise PermissionError("Connect Trakt before changing favorites.")
            media_type, trakt_id = match.group(1), int(match.group(2))
            self.update_trakt_favorite({"action": "remove", "type": media_type, "traktId": trakt_id})
            return {"ok": True}
        if item_id.startswith("cloud:"):
            item = next((value for value in self.get_cloud_library() if value["id"] == item_id), None)
            if not item:
                raise LookupError("Cloud item no longer exists.")
            if action == "download-local":
                if not INFO_HASH.fullmatch(item["infoHash"]):
                    raise ValueError("Cloud item has no valid torrent hash.")
                return {"ok": True, "infoHash": item["infoHash"]}
            if action == "delete":
                try:
                    self.torbox_request("controltorrent", json_body={"torrent_id": item["torrentId"], "operation": "delete"})
                except RuntimeError as error:
                    if "DATABASE_ERROR" not in str(error):
                        raise
                    remaining = self.refresh_torbox_index(force=True)
                    if any(str(value.get("id") or value.get("torrent_id")) == str(item["torrentId"]) for value in remaining):
                        raise
                self.torbox_index = []
                self.torbox_index_time = 0
                prefix = f"cloud:{item['torrentId']}:"
                removed = [value for value in self.library_links if value.startswith(prefix)]
                for value in removed:
                    self.library_links.pop(value, None)
                    self.database.delete_match(value)
                return {"ok": True}
        if action == "delete":
            item = next((value for value in self.reconcile_library().get("items", []) if self.library_item_id(value) == item_id), None)
            if not item:
                raise LookupError("Local library item no longer exists.")
            path = Path(item["filePath"])
            if not path.is_file():
                raise LookupError("Local file no longer exists.")
            path.unlink()
            self.library_snapshot = None
            return {"ok": True}
        raise ValueError("That action is not available for this item.")

    def trakt_sync_remove(self, collection, plural, trakt_id):
        payload = {plural: [{"ids": {"trakt": trakt_id}}]}
        request = urllib.request.Request(
            f"https://api.trakt.tv/sync/{collection}/remove", data=json.dumps(payload).encode(), method="POST",
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            json.load(response)
        with self.trakt_lock:
            self.trakt_cache = None
            self.trakt_favorites_cache = None

    @staticmethod
    def torbox_release(item, source_group, *, instant=True, file=None):
        info_hash = str(item.get("hash") or item.get("info_hash") or item.get("infoHash") or "")
        if not INFO_HASH.fullmatch(info_hash):
            return None
        return {
            "infoHash": info_hash, "rawTitle": (file or {}).get("short_name") or (file or {}).get("name") or item.get("name") or item.get("title") or item.get("raw_title") or info_hash,
            "quality": item.get("quality"), "codec": item.get("codec"), "sourceType": item.get("source") or "TorBox",
            "sizeBytes": (file or {}).get("size") or item.get("size") or item.get("size_bytes"), "seeders": item.get("seeders") or item.get("seeds") or 0,
            "qualityScore": item.get("quality_score") or item.get("qualityScore") or 0, "instant": instant,
            "debridProvider": "TorBox", "sourceGroup": source_group,
            "torrentId": item.get("id") or item.get("torrent_id"), "fileId": (file or {}).get("id"),
        }

    def find_torbox_library(self, body):
        clean_title = re.sub(r"\s+s\d{1,3}e\d{1,4}$", "", body["title"], flags=re.IGNORECASE)
        title = re.sub(r"[^a-z0-9]+", " ", clean_title.lower()).strip()
        terms = [term for term in title.split() if len(term) > 1]
        matches = []
        season = body.get("season")
        episode = body.get("episode")
        for item in self.refresh_torbox_index():
            state = str(item.get("download_state") or item.get("status") or "").lower()
            if state not in {"cached", "completed", "uploading", "seeding"}:
                continue
            videos = [file for file in (item.get("files") or []) if Path(str(file.get("short_name") or file.get("name") or "")).suffix.lower() in VIDEO_EXTENSIONS or str(file.get("mimetype", "")).startswith("video/")]
            for file in videos:
                file_name = str(file.get("short_name") or file.get("name") or item.get("name") or "")
                name = re.sub(r"[^a-z0-9]+", " ", file_name.lower())
                if not terms or not all(term in name for term in terms):
                    continue
                if body["type"] == "show":
                    episode_match = re.search(r"(?:s|season[ ._-]*)(\d{1,3})[ ._-]*(?:e|x)(\d{1,4})", file_name, re.IGNORECASE)
                    if not episode_match or (int(episode_match.group(1)), int(episode_match.group(2))) != (season, episode):
                        continue
                release = self.torbox_release(item, "torbox-library", file=file)
                if release:
                    matches.append(release)
        matches.sort(key=lambda release: (Path(release["rawTitle"]).suffix.lower() in {".mp4", ".m4v", ".webm"}, release.get("sizeBytes") or 0), reverse=True)
        return matches[:20]

    def search_torbox(self, body):
        imdb_id = body.get("imdbId")
        tmdb_id = body.get("tmdbId")
        if isinstance(imdb_id, str) and re.fullmatch(r"tt\d{5,12}", imdb_id):
            identity = f"imdb:{imdb_id}"
        elif str(tmdb_id or "").isdigit():
            identity = f"tmdb:{'movie' if body['type'] == 'movie' else 'tv'}:{tmdb_id}"
        else:
            return []
        try:
            result = subprocess.run(["curl", "-sS", "--max-time", "8", "-H", f"Authorization: Bearer {self.torbox_api_key}", "-H", "Accept: application/json", "https://search-api.torbox.app/torrents/" + urllib.parse.quote(identity, safe=":")], capture_output=True, text=True, timeout=12, check=False)
            payload = json.loads(result.stdout) if result.returncode == 0 else {}
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []
        values = payload.get("data") or payload.get("results") or payload.get("torrents") or payload if isinstance(payload, dict) else payload
        if isinstance(values, dict):
            values = values.get("torrents") or list(values.values())
        releases = [self.torbox_release(item, "torbox-search", instant=bool(item.get("cached", True))) for item in (values or []) if isinstance(item, dict)]
        return [item for item in releases if item][:20]

    def start_torbox(self, info_hash, *, play, torrent_id=None, file_id=None, file_name=""):
        job_id = self.add_activity("torbox", info_hash, "Opening instant TorBox stream" if play else "Adding download to TorBox", title=file_name, media_type="stream" if play else "download")
        try:
            if not torrent_id:
                private_torrent = PRIVATE_TORRENT_ROOT / f"{info_hash.lower()}.torrent"
                form = {} if private_torrent.is_file() else {"magnet": f"magnet:?xt=urn:btih:{info_hash}"}
                if private_torrent.is_file():
                    form["seed"] = "1"
                if play:
                    form["add_only_if_cached"] = "true"
                created = self.torbox_request("createtorrent", form=form, files={"file": private_torrent} if private_torrent.is_file() else None)
                torrent_id = created.get("torrent_id") or created.get("id") if isinstance(created, dict) else None
            if not torrent_id:
                raise RuntimeError("TorBox did not return a torrent ID.")
            if not play:
                self.update_activity(job_id, "active", "Queued in TorBox")
                return {"status": "queued", "activityId": job_id}
            if file_id is None:
                listing = self.torbox_request("mylist?" + urllib.parse.urlencode({"id": torrent_id, "bypass_cache": "true"}))
                item = listing[0] if isinstance(listing, list) and listing else listing
                files = item.get("files", []) if isinstance(item, dict) else []
                videos = [file for file in files if Path(str(file.get("short_name") or file.get("name", ""))).suffix.lower() in VIDEO_EXTENSIONS]
                chosen = max(videos or files, key=lambda file: file.get("size") or file.get("size_bytes") or 0, default=None)
                if not chosen:
                    raise LookupError("TorBox has no playable video file for this release.")
                file_id = chosen.get("id") or chosen.get("file_id")
            link = self.torbox_request("requestdl?" + urllib.parse.urlencode({"token": self.torbox_api_key, "torrent_id": torrent_id, "file_id": file_id, "redirect": "false"}))
            url = link if isinstance(link, str) else (link or {}).get("url") or (link or {}).get("link")
            if not url:
                raise RuntimeError("TorBox did not return a playable CDN URL.")
            self.update_activity(job_id, "complete", "Instant stream ready")
            session_id = self.create_remote_selection(url, info_hash, file_id, file_name)
            return {"status": "selecting", "id": session_id, "activityId": job_id}
        except Exception as error:
            self.update_activity(job_id, "error", str(error))
            raise

    def create_remote_selection(self, source_url, info_hash, file_id, file_name):
        if not self.ffprobe:
            raise RuntimeError("ffprobe is required to inspect TorBox audio and subtitle tracks.")
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,channels:stream_tags=language,title:stream_disposition=default", "-of", "json", source_url],
            capture_output=True, text=True, timeout=45, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or "Could not inspect TorBox media tracks.").strip())
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("ffprobe returned invalid TorBox media metadata.") from error
        audio, subtitles = [], []
        for stream in probe.get("streams", []):
            tags, disposition = stream.get("tags") or {}, stream.get("disposition") or {}
            track = {"codec": stream.get("codec_name"), "lang": tags.get("language"), "title": tags.get("title"), "default": bool(disposition.get("default"))}
            if stream.get("codec_type") == "audio":
                track["channels"] = stream.get("channels")
                audio.append(track)
            elif stream.get("codec_type") == "subtitle":
                subtitles.append(track)
        session_id = secrets.token_urlsafe(18)
        with self.stream_lock:
            self.streams[session_id] = {
                "kind": "torbox", "status": "selecting", "phase": "tracks", "message": "Choose audio and subtitles",
                "progress": 0, "url": None, "error": None, "process": None, "started_at": time.monotonic(),
                "source_url": source_url, "info_hash": info_hash, "file_id": file_id, "file_name": file_name,
                "availableAudio": audio, "availableSubtitles": subtitles,
                "durationSeconds": float((probe.get("format") or {}).get("duration") or 0),
            }
        return session_id

    def select_remote_tracks(self, session_id, audio_index, subtitle_index):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A valid stream session is required.")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (audio_index, subtitle_index)):
            raise ValueError("Track selections must be integers.")
        with self.stream_lock:
            item = self.streams.get(session_id)
            if not item or item.get("status") != "selecting":
                raise LookupError("The TorBox track-selection session is no longer available.")
            audio = item.get("availableAudio") or []
            subtitles = item.get("availableSubtitles") or []
            if audio_index < 0 or (audio and audio_index >= len(audio)) or subtitle_index < -1 or subtitle_index >= len(subtitles):
                raise ValueError("The selected audio or subtitle track does not exist.")
            source_url, info_hash, file_id = item["source_url"], item["info_hash"], item["file_id"]
            duration = item.get("durationSeconds")
        with self.stream_lock:
            item["status"] = "buffering"
            item["phase"] = "downloading"
            item["message"] = "Caching TorBox media on this server…"
            item["selected_audio_index"] = audio_index
            item["selected_subtitle_index"] = subtitle_index
        threading.Thread(
            target=self.cache_and_start_remote_hls,
            args=(session_id, source_url, info_hash, file_id, audio_index, subtitle_index, duration, audio, subtitles),
            name=f"torbox-cache-{session_id[:8]}", daemon=True,
        ).start()

    def cache_and_start_remote_hls(self, session_id, source_url, info_hash, file_id, audio_index, subtitle_index, duration, audio, subtitles):
        source_path = TORBOX_MEDIA_ROOT / f"{info_hash.lower()}-{file_id}.media"
        partial_path = source_path.with_suffix(".part")
        try:
            if not source_path.is_file():
                command = ["curl", "-sS", "--location", "--fail", "--retry", "30", "--retry-delay", "2", "--retry-all-errors", "--continue-at", "-", "--output", str(partial_path), source_url]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
                with self.stream_lock:
                    item = self.streams.get(session_id)
                    if not item or item.get("status") == "stopped":
                        process.terminate()
                        return
                    item["process"] = process
                _, error = process.communicate()
                if process.returncode:
                    raise RuntimeError((error or "TorBox cache download failed.").strip())
                partial_path.replace(source_path)
            with self.stream_lock:
                item = self.streams.get(session_id)
                if not item or item.get("status") == "stopped":
                    return
                item["process"] = None
                item["message"] = "Preparing cached media for playback…"
            self.create_remote_hls(str(source_path), info_hash, file_id, audio_index, subtitle_index, session_id=session_id, duration=duration, audio=audio, subtitles=subtitles)
        except Exception as error:
            with self.stream_lock:
                item = self.streams.get(session_id)
                if item and item.get("status") != "stopped":
                    item.update(status="error", phase="error", error=str(error), message="Could not cache TorBox media")

    def create_remote_hls(self, source_url, info_hash, file_id, audio_index=0, subtitle_index=-1, *, session_id=None, duration=None, audio=None, subtitles=None):
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is required to play this TorBox container in a browser.")
        media_id = f"tb{info_hash[:16]}f{file_id}a{audio_index}s{subtitle_index + 1}"
        output_dir = HLS_ROOT / media_id
        playlist = output_dir / "master.m3u8"
        session_id = session_id or secrets.token_urlsafe(18)
        selected_subtitle = (subtitles or [])[subtitle_index] if 0 <= subtitle_index < len(subtitles or []) else {}
        cached_tracks = []
        if subtitle_index >= 0 and (output_dir / "subtitle.vtt").is_file():
            cached_tracks.append({"kind": "subtitles", "src": f"/media/{media_id}/subtitle.vtt", "srclang": selected_subtitle.get("lang") or "und", "label": selected_subtitle.get("title") or selected_subtitle.get("lang") or "Subtitles", "default": True})
        cache_complete = playlist.is_file() and "#EXT-X-ENDLIST" in playlist.read_text(errors="ignore")
        if cache_complete:
            with self.stream_lock:
                self.streams[session_id].update(status="ready", phase="ready", message="Loaded from persistent HLS cache", progress=100, url=f"/media/{media_id}/master.m3u8", process=None, tracks=cached_tracks, audio=(audio or [{}])[audio_index] if audio_index < len(audio or []) else {}, durationSeconds=duration)
            return session_id
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)
        video_mapping = ["-map", "0:v:0"]
        subtitle_tracks = []
        if subtitle_index >= 0 and str(selected_subtitle.get("codec", "")).lower() in TEXT_SUBTITLE_CODECS:
            subtitle_path = output_dir / "subtitle.vtt"
            extraction = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source_url, "-map", f"0:s:{subtitle_index}", "-c:s", "webvtt", str(subtitle_path)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if extraction.returncode or not subtitle_path.is_file():
                raise RuntimeError((extraction.stderr or "Could not prepare the selected subtitle track.").strip())
            subtitle_tracks.append({"kind": "subtitles", "src": f"/media/{media_id}/subtitle.vtt", "srclang": selected_subtitle.get("lang") or "und", "label": selected_subtitle.get("title") or selected_subtitle.get("lang") or "Subtitles", "default": True})
        elif subtitle_index >= 0:
            escaped = source_url.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            video_mapping = ["-vf", f"subtitles='{escaped}':si={subtitle_index}", "-map", "0:v:0"]
        remote_input = source_url.startswith(("http://", "https://"))
        connection_args = ["-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1", "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "4xx,5xx", "-reconnect_delay_max", "3"] if remote_input else []
        args = [
            self.ffmpeg, "-hide_banner", "-y", *connection_args, "-i", source_url, *(["-t", str(duration)] if duration else []), *video_mapping, "-map", f"0:a:{audio_index}?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "5M", "-bufsize", "10M",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-f", "hls", "-hls_time", "4",
            "-hls_playlist_type", "event", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(output_dir / "seg-%06d.m4s"), str(playlist),
        ]
        # ffmpeg continuously writes progress to stderr. Leaving it attached to an
        # unread pipe eventually fills the OS pipe buffer and freezes the encoder.
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        with self.stream_lock:
            self.streams[session_id] = {
                "kind": "torbox", "status": "buffering", "phase": "transcoding", "message": "Preparing TorBox HLS…",
                "progress": 0, "url": None, "error": None, "process": process, "started_at": time.monotonic(),
                "playlist": playlist, "media_url": f"/media/{media_id}/master.m3u8", "tracks": subtitle_tracks,
                "audio": (audio or [{}])[audio_index] if audio_index < len(audio or []) else {}, "durationSeconds": duration,
                "min_ready_segments": 8,
            }
        threading.Thread(target=self.watch_library_stream, args=(session_id,), name=f"torbox-hls-{process.pid}", daemon=True).start()
        return session_id

    def create_direct_stream(self, source_url):
        session_id = secrets.token_urlsafe(18)
        with self.stream_lock:
            self.streams[session_id] = {
                "kind": "torbox", "status": "ready", "phase": "ready", "message": "TorBox stream ready",
                "progress": 100, "url": source_url, "error": None, "process": None,
                "started_at": time.monotonic(), "tracks": [], "audio": {},
            }
        return session_id

    def add_activity(self, provider, info_hash, message, *, title="", media_type=""):
        item = {"id": secrets.token_urlsafe(8), "provider": provider, "infoHash": info_hash, "status": "active", "message": message, "title": title, "mediaType": media_type, "createdAt": int(time.time() * 1000)}
        with self.activity_lock:
            self.activity.insert(0, item)
            del self.activity[50:]
        return item["id"]

    def update_activity(self, job_id, status, message):
        with self.activity_lock:
            for item in self.activity:
                if item["id"] == job_id:
                    item.update(status=status, message=message, updatedAt=int(time.time() * 1000))
                    break

    def get_activity(self):
        with self.activity_lock:
            return [dict(item) for item in self.activity]

    def save_trakt_settings(self, body):
        client_id = body.get("clientId", "").strip() if isinstance(body.get("clientId", ""), str) else ""
        client_secret = body.get("clientSecret", "").strip() if isinstance(body.get("clientSecret", ""), str) else ""
        if not 8 <= len(client_id) <= 200:
            raise ValueError("Enter a valid Trakt client ID.")
        if not client_secret and not self.trakt_client_secret:
            raise ValueError("Enter the Trakt client secret.")
        if len(client_secret) > 300:
            raise ValueError("The Trakt client secret is too long.")
        with self.trakt_lock:
            changed = client_id != self.trakt_client_id
            self.trakt_client_id = client_id
            if client_secret:
                self.trakt_client_secret = client_secret
            if changed:
                self.trakt_access_token = ""
                self.trakt_refresh_token = ""
                self.trakt_user = None
                self.trakt_auth_id = None
                self.trakt_auth = {"status": "idle"}
            self.trakt_cache = None
        self.write_trakt_settings()
        return self.get_trakt_settings()

    def trakt_oauth_post(self, path, payload):
        request = urllib.request.Request(
            f"https://api.trakt.tv{path}", data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json", "User-Agent": "unarr-web/0.1",
                "trakt-api-key": self.trakt_client_id, "trakt-api-version": "2",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode()).get("error")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            error.trakt_message = detail or f"Trakt rejected the OAuth request (HTTP {error.code}). Check the client ID and client secret."
            raise

    def start_trakt_auth(self):
        if not self.trakt_client_id or not self.trakt_client_secret:
            raise RuntimeError("Save a Trakt client ID and client secret first.")
        device = self.trakt_oauth_post("/oauth/device/code", {"client_id": self.trakt_client_id})
        state = {
            "status": "pending", "userCode": device["user_code"],
            "verificationUrl": device.get("verification_url", "https://trakt.tv/activate"),
            "expiresAt": int(time.time()) + int(device.get("expires_in", 600)),
        }
        auth_id = secrets.token_urlsafe(12)
        with self.trakt_lock:
            self.trakt_auth = state
            self.trakt_auth_id = auth_id
        threading.Thread(
            target=self.poll_trakt_auth,
            args=(auth_id, device["device_code"], max(5, int(device.get("interval", 5))), state["expiresAt"]),
            name="trakt-device-auth", daemon=True,
        ).start()
        return dict(state)

    def poll_trakt_auth(self, auth_id, device_code, interval, expires_at):
        while time.time() < expires_at:
            time.sleep(interval)
            with self.trakt_lock:
                if auth_id != self.trakt_auth_id:
                    return
            try:
                token = self.trakt_oauth_post("/oauth/device/token", {
                    "code": device_code, "client_id": self.trakt_client_id,
                    "client_secret": self.trakt_client_secret,
                })
            except urllib.error.HTTPError as error:
                if error.code == 400:
                    continue
                if error.code == 429:
                    interval += 5
                    continue
                message = {404: "The device code is invalid.", 409: "This code was already used.", 410: "The code expired.", 418: "Authorization was denied."}.get(error.code, getattr(error, "trakt_message", f"Trakt returned HTTP {error.code}."))
                with self.trakt_lock:
                    self.trakt_auth = {"status": "error", "error": message}
                return
            except (OSError, urllib.error.URLError) as error:
                with self.trakt_lock:
                    self.trakt_auth = {"status": "error", "error": str(error)}
                return
            with self.trakt_lock:
                if auth_id != self.trakt_auth_id:
                    return
                self.trakt_access_token = token.get("access_token", "")
                self.trakt_refresh_token = token.get("refresh_token", "")
                self.trakt_cache = None
            try:
                settings = self.trakt_request("/users/settings", authenticated=True)
                user = settings.get("user") or {}
                self.trakt_user = {"username": user.get("username"), "name": user.get("name")}
            except Exception:
                self.trakt_user = None
            self.write_trakt_settings()
            with self.trakt_lock:
                self.trakt_auth = {"status": "complete", "user": self.trakt_user}
            return
        with self.trakt_lock:
            self.trakt_auth = {"status": "error", "error": "The authorization code expired."}

    def get_trakt_auth(self):
        with self.trakt_lock:
            return dict(self.trakt_auth)

    def disconnect_trakt(self):
        with self.trakt_lock:
            self.trakt_access_token = ""
            self.trakt_refresh_token = ""
            self.trakt_user = None
            self.trakt_auth = {"status": "idle"}
            self.trakt_auth_id = None
            self.trakt_cache = None
        self.write_trakt_settings()

    def trakt_request(self, path, authenticated=False):
        if not self.trakt_client_id:
            raise RuntimeError("TRAKT_CLIENT_ID is not configured")
        if authenticated and not self.trakt_access_token:
            raise PermissionError("Connect a Trakt account to load personal sections")
        separator = "&" if "?" in path else "?"
        url = f"https://api.trakt.tv{path}{separator}extended=full,images"
        headers = {"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"}
        if self.trakt_access_token:
            headers["Authorization"] = f"Bearer {self.trakt_access_token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Trakt authorization is missing or expired") from error
            raise RuntimeError(f"Trakt returned HTTP {error.code}") from error

    def trakt_write(self, path, payload, method="POST"):
        if not self.trakt_client_id or not self.trakt_access_token:
            raise PermissionError("Connect Trakt before changing your library list.")
        request = urllib.request.Request(
            f"https://api.trakt.tv{path}", data=json.dumps(payload).encode(), method=method,
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    detail_value = json.loads(error.read(2048).decode("utf-8", "replace"))
                    detail = str(detail_value.get("error_description") or detail_value.get("error") or detail_value.get("message") or "")[:300]
                except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                    detail = ""
                retry_header = error.headers.get("Retry-After", "") if error.headers else ""
                retry_after = int(retry_header) if str(retry_header).isdigit() else attempt + 1
                print(f"[trakt] Library list {method} {path} returned HTTP {error.code}{f': {detail}' if detail else ''}")
                if error.code in {401, 403}:
                    raise PermissionError("Trakt authorization is missing or expired.") from error
                if attempt < 2 and (error.code in {429, 500, 502, 503, 504}) and retry_after <= 10:
                    time.sleep(max(1, retry_after))
                    continue
                raise RuntimeError(f"Trakt library list returned HTTP {error.code}{f': {detail}' if detail else ''}.") from error
            except urllib.error.URLError as error:
                print(f"[trakt] Library list {method} {path} network error: {error.reason}")
                if attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise RuntimeError(f"Trakt library list network error: {error.reason}.") from error
        raise RuntimeError("Trakt library list update failed after retries.")

    def ensure_trakt_library_list(self):
        lock = getattr(self, "trakt_library_lock", None)
        if lock is None:
            lock = self.trakt_library_lock = threading.Lock()
        with lock:
            lists = self.trakt_request("/users/me/lists?limit=100", authenticated=True)
            selected = next((value for value in lists if str(value.get("name") or "").casefold() == "library"), None)
            if selected is None:
                selected = self.trakt_write("/users/me/lists", {
                    "name": "library", "description": "Titles saved from Unarr Web",
                    "privacy": "private", "display_numbers": False, "allow_comments": False,
                    "sort_by": "rank", "sort_how": "asc",
                })
            list_id = (selected.get("ids") or {}).get("trakt") or (selected.get("ids") or {}).get("slug")
            if not list_id:
                raise RuntimeError("Trakt did not return an identifier for the library list.")
            self.trakt_library_list_id = list_id
            migration_key = str((getattr(self, "trakt_user", None) or {}).get("username") or "connected")
            if getattr(self, "trakt_library_migrated", "") != migration_key:
                legacy_movies = self.trakt_request("/sync/favorites/movies?limit=1000", authenticated=True)
                legacy_shows = self.trakt_request("/sync/favorites/shows?limit=1000", authenticated=True)
                payload = {
                    "movies": [{"ids": {"trakt": value.get("movie", {}).get("ids", {}).get("trakt")}} for value in legacy_movies if value.get("movie", {}).get("ids", {}).get("trakt")],
                    "shows": [{"ids": {"trakt": value.get("show", {}).get("ids", {}).get("trakt")}} for value in legacy_shows if value.get("show", {}).get("ids", {}).get("trakt")],
                }
                if payload["movies"] or payload["shows"]:
                    self.trakt_write(f"/users/me/lists/{list_id}/items", payload)
                    self.trakt_write("/sync/favorites/remove", payload)
                self.trakt_library_migrated = migration_key
                self.write_trakt_settings()
                print(f"[trakt] Migrated {len(payload['movies']) + len(payload['shows'])} favorites to private library list {list_id}")
            return list_id

    def scrobble_trakt(self, body):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt before enabling playback tracking.")
        action = body.get("action")
        media_type = body.get("type")
        trakt_id = body.get("traktId")
        progress = body.get("progress")
        if action not in {"start", "pause", "stop"} or media_type not in {"movie", "show"}:
            raise ValueError("Invalid Trakt scrobble event.")
        if isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
            raise ValueError("A valid Trakt media ID is required.")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 100:
            raise ValueError("Scrobble progress must be between 0 and 100.")
        payload = {"progress": round(float(progress), 2), "app_version": "0.1", "app_date": "2026-08-04"}
        if media_type == "movie":
            payload["movie"] = {"ids": {"trakt": trakt_id}}
        else:
            season, episode = body.get("season"), body.get("episode")
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (season, episode)):
                raise ValueError("A valid season and episode are required.")
            payload["show"] = {"ids": {"trakt": trakt_id}}
            payload["episode"] = {"season": season, "number": episode}
        request = urllib.request.Request(
            f"https://api.trakt.tv/scrobble/{action}", data=json.dumps(payload).encode(), method="POST",
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Trakt authorization is missing or expired.") from error
            raise RuntimeError(f"Trakt scrobble returned HTTP {error.code}.") from error
        if action == "stop":
            with self.trakt_lock:
                self.trakt_cache = None
        return {"ok": True, "action": result.get("action", action), "progress": result.get("progress", progress)}

    def update_trakt_watchlist(self, body):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt before changing your watchlist.")
        action, media_type, trakt_id = body.get("action"), body.get("type"), body.get("traktId")
        if action not in {"add", "remove"} or media_type not in {"movie", "show"}:
            raise ValueError("A valid watchlist action and media type are required.")
        if isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
            raise ValueError("A valid Trakt title ID is required.")
        plural = "movies" if media_type == "movie" else "shows"
        payload = {plural: [{"ids": {"trakt": trakt_id}}]}
        endpoint = "sync/watchlist" if action == "add" else "sync/watchlist/remove"
        request = urllib.request.Request(
            f"https://api.trakt.tv/{endpoint}", data=json.dumps(payload).encode(), method="POST",
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Trakt authorization is missing or expired.") from error
            raise RuntimeError(f"Trakt watchlist returned HTTP {error.code}.") from error
        with self.trakt_lock:
            self.trakt_cache = None
            self.database.invalidate_provider_cache("trakt_poster_states")
        return {"ok": True, "action": action, "type": media_type, "traktId": trakt_id}

    def update_trakt_favorite(self, body):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt before changing favorites.")
        action, media_type, trakt_id = body.get("action"), body.get("type"), body.get("traktId")
        if action not in {"add", "remove"} or media_type not in {"movie", "show"}:
            raise ValueError("A valid favorite action and media type are required.")
        if isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
            raise ValueError("A valid Trakt title ID is required.")
        plural = "movies" if media_type == "movie" else "shows"
        payload = {plural: [{"ids": {"trakt": trakt_id}}]}
        list_id = self.ensure_trakt_library_list()
        result = self.trakt_write(f"/users/me/lists/{list_id}/items", payload, "POST" if action == "add" else "DELETE")
        with self.trakt_lock:
            self.trakt_cache = self.trakt_favorites_cache = None
            self.database.invalidate_provider_cache("trakt_library_list")
        try:
            self.get_trakt_favorites()
        except Exception as error:
            print(f"[trakt] Library list updated but its local cache could not be refreshed: {error}")
        return {"ok": True, "action": action, "type": media_type, "traktId": trakt_id, "trakt": result}

    def remove_trakt_playback(self, body):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt before changing playback history.")
        playback_id = body.get("playbackId")
        if isinstance(playback_id, bool) or not isinstance(playback_id, int) or playback_id < 1:
            raise ValueError("A valid Continue Watching item is required.")
        request = urllib.request.Request(
            f"https://api.trakt.tv/sync/playback/{playback_id}", method="DELETE",
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Trakt authorization is missing or expired.") from error
            raise RuntimeError(f"Trakt playback removal returned HTTP {error.code}.") from error
        with self.trakt_lock:
            self.trakt_cache = None
        return {"ok": True, "playbackId": playback_id}

    def update_trakt_history(self, body):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt before changing watched status.")
        action, media_type, trakt_id = body.get("action"), body.get("type"), body.get("traktId")
        if action not in {"add", "remove"} or media_type not in {"movie", "show"}:
            raise ValueError("A valid watched action and media type are required.")
        if isinstance(trakt_id, bool) or not isinstance(trakt_id, int) or trakt_id < 1:
            raise ValueError("A valid Trakt title ID is required.")
        plural = "movies" if media_type == "movie" else "shows"
        payload = {plural: [{"ids": {"trakt": trakt_id}}]}
        endpoint = "sync/history" if action == "add" else "sync/history/remove"
        request = urllib.request.Request(
            f"https://api.trakt.tv/{endpoint}", data=json.dumps(payload).encode(), method="POST",
            headers={"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id, "Authorization": f"Bearer {self.trakt_access_token}", "Content-Type": "application/json", "User-Agent": "unarr-web/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Trakt authorization is missing or expired.") from error
            raise RuntimeError(f"Trakt watched update returned HTTP {error.code}.") from error
        with self.trakt_lock:
            self.trakt_cache = None
            self.database.invalidate_provider_cache("trakt_poster_states")
        return {"ok": True, "action": action, "type": media_type, "traktId": trakt_id}

    def get_trakt_calendar(self, query):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt to load your calendar.")
        start = ((query or {}).get("from") or [datetime.date.today().isoformat()])[0]
        end = ((query or {}).get("to") or [(datetime.date.today() + datetime.timedelta(days=13)).isoformat()])[0]
        try:
            start_date, end_date = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
        except ValueError as error:
            raise ValueError("Choose a valid calendar date range.") from error
        days = (end_date - start_date).days + 1
        if days < 1 or days > 366:
            raise ValueError("Calendar range must be between 1 and 366 days.")
        values = []
        for kind in ("shows", "movies"):
            values.extend(self.trakt_request(f"/calendars/my/{kind}/{start}/{days}?limit=1000", authenticated=True))
        values.sort(key=lambda value: value.get("first_aired") or value.get("released") or "")
        return [self.normalize_trakt_item(value, "calendar") for value in values]

    def get_trakt_custom(self, query):
        if not self.trakt_client_id:
            raise PermissionError("Configure Trakt to load a custom rail.")
        today = datetime.date.today()
        first = today.replace(day=1)
        next_month = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        start, end = ((query or {}).get("from") or [first.isoformat()])[0], ((query or {}).get("to") or [(next_month - datetime.timedelta(days=1)).isoformat()])[0]
        media = ((query or {}).get("media") or ["all"])[0]
        genre = ((query or {}).get("genre") or ["all"])[0].strip().lower()
        try:
            start_date, end_date = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
        except ValueError as error:
            raise ValueError("Choose a valid custom-rail date range.") from error
        days = (end_date - start_date).days + 1
        if days < 1 or days > 36525 or media not in {"all", "movie", "show"} or not re.fullmatch(r"all|[a-z0-9-]{2,40}", genre):
            raise ValueError("Choose valid custom-rail filters (maximum range: 100 years).")
        values = []
        kinds = ("movies", "shows") if media == "all" else (("movies",) if media == "movie" else ("shows",))
        def fetch_window(path):
            cache_key = "trakt_custom:" + hashlib.sha256(path.encode()).hexdigest()
            cached, synced_at = self.database.get_provider_cache(cache_key)
            if isinstance(cached, list) and time.time() - synced_at < 30 * 24 * 60 * 60:
                return cached
            result = self.trakt_request(path)
            self.database.cache_provider(cache_key, result)
            return result
        chunk_end = end_date
        while chunk_end >= start_date:
            chunks, cursor = [], chunk_end
            for _ in range(6):
                if cursor < start_date:
                    break
                chunk_start = max(start_date, cursor - datetime.timedelta(days=30))
                chunks.append((chunk_start, (cursor - chunk_start).days + 1))
                cursor = chunk_start - datetime.timedelta(days=1)
            paths = [f"/calendars/all/{kind}/{chunk_start.isoformat()}/{chunk_days}?limit=1000" for chunk_start, chunk_days in chunks for kind in kinds]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(paths))) as executor:
                chunk = [value for result in executor.map(fetch_window, paths) for value in result]
            if genre != "all":
                chunk = [value for value in chunk if genre in {str(item).lower().replace(" ", "-") for item in ((value.get("movie") or value.get("show") or {}).get("genres") or [])}]
            values.extend(chunk)
            chunk_end = cursor
        seen, items = set(), []
        for value in sorted(values, key=lambda item: item.get("first_aired") or item.get("released") or "", reverse=True):
            item = self.normalize_trakt_item(value, "custom")
            release_date = str(item.get("calendarAt") or "")[:10]
            if not release_date or release_date < start or release_date > end:
                continue
            key = (item.get("mediaType"), item.get("ids", {}).get("trakt"))
            if key in seen:
                continue
            seen.add(key); items.append(item)
        favorites = {(item.get("mediaType"), item.get("ids", {}).get("trakt")) for item in self.get_trakt_favorites()} if self.trakt_access_token else set()
        states = self.get_trakt_poster_states() if self.trakt_access_token else {"watchedMovies": [], "watchedShows": {}, "watchlistMovies": [], "watchlistShows": []}
        local_ids, cloud_ids = set(), set()
        for source, target in (("local", local_ids), ("cloud", cloud_ids)):
            for library_item in self.database.get_library_items(source):
                identity = library_item.get("trakt") or self.library_links.get(str(library_item.get("id") or "")) or {}
                if identity.get("traktId"):
                    target.add((str(identity.get("type")), str(identity.get("traktId"))))
        for item in items:
            media_type, trakt_id = item.get("mediaType"), item.get("ids", {}).get("trakt")
            item["favorite"] = (media_type, trakt_id) in favorites
            item["watchlisted"] = trakt_id in states["watchlistMovies" if media_type == "movie" else "watchlistShows"]
            if media_type == "movie" and trakt_id in states["watchedMovies"]:
                item["watchState"] = "watched"
            elif media_type == "show" and str(trakt_id) in states["watchedShows"]:
                item["watchState"] = states["watchedShows"][str(trakt_id)]
            identity = (str(media_type), str(trakt_id))
            item["availableLocal"] = identity in local_ids
            item["availableCloud"] = identity in cloud_ids
        genres = sorted({str(value) for item in items for value in (item.get("genres") or [])})
        return {"id": "custom", "title": "Custom", "items": items, "genres": genres, "from": start, "to": end, "media": media, "genre": genre}

    def get_trakt_poster_states(self):
        cached, synced_at = self.database.get_provider_cache("trakt_poster_states")
        if isinstance(cached, dict) and time.time() - synced_at < 300:
            return cached
        paths = ("/sync/watched/movies?limit=1000", "/sync/watched/shows?limit=1000", "/sync/watchlist/movies?limit=1000", "/sync/watchlist/shows?limit=1000")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            watched_movies, watched_shows, watchlist_movies, watchlist_shows = list(pool.map(lambda path: self.trakt_request(path, authenticated=True), paths))
        show_states = {}
        for value in watched_shows:
            show = value.get("show") or {}
            watched_count = sum(len(season.get("episodes") or []) for season in value.get("seasons") or [])
            aired_count = int(show.get("aired_episodes") or 0)
            show_states[str(show.get("ids", {}).get("trakt"))] = "watched" if aired_count and watched_count >= aired_count else "in-progress"
        result = {
            "watchedMovies": [item.get("movie", {}).get("ids", {}).get("trakt") for item in watched_movies],
            "watchedShows": show_states,
            "watchlistMovies": [item.get("movie", {}).get("ids", {}).get("trakt") for item in watchlist_movies],
            "watchlistShows": [item.get("show", {}).get("ids", {}).get("trakt") for item in watchlist_shows],
        }
        self.database.cache_provider("trakt_poster_states", result)
        return result

    def get_trakt_continue(self):
        if not self.trakt_access_token:
            raise PermissionError("Connect Trakt to load Continue Watching.")
        values = self.trakt_request("/sync/playback/movies?limit=50", authenticated=True)
        return [self.normalize_trakt_item(value, "continue") for value in values[:50]]

    def get_trakt_dashboard(self):
        with self.trakt_lock:
            if self.trakt_cache and time.monotonic() - self.trakt_cache_time < 300:
                return self.trakt_cache
        calendar_start = datetime.date.today().isoformat()
        custom_first = datetime.date.today().replace(day=1)
        custom_next = (custom_first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        custom_days = (custom_next - custom_first).days
        sections = [
            ("custom", "Custom", (f"/calendars/all/movies/{custom_first.isoformat()}/{custom_days}?limit=250", f"/calendars/all/shows/{custom_first.isoformat()}/{custom_days}?limit=250"), False),
            ("continue", "Continue watching", "/sync/playback/movies?limit=50", True),
            ("start", "Start watching", "/sync/watchlist/shows?limit=50", True),
            ("calendar", "Calendar", (
                f"/calendars/my/shows/{calendar_start}/14?limit=50",
                f"/calendars/my/movies/{calendar_start}/14?limit=50",
            ), True),
            ("watchlist", "Watchlist", "/sync/watchlist/movies?limit=50", True),
            ("history", "History", "/sync/history/movies?limit=50", True),
            ("collection", "Collection", "/sync/collection/movies?limit=50", True),
            ("ratings", "Ratings", "/users/me/ratings/movies?limit=50", True),
            ("recommendations", "Recommendations", "/recommendations/movies?limit=50", True),
            ("trending", "Trending", "/movies/trending?limit=50", False),
            ("popular", "Popular", "/movies/popular?limit=50", False),
            ("anticipated", "Anticipated", "/movies/anticipated?limit=50", False),
            ("lists", "Custom lists", "/users/me/lists?limit=50", True),
        ]
        if not self.trakt_client_id:
            rows = [{"id": key, "title": title, "items": [], "locked": True} for key, title, _, _ in sections]
            return {"configured": False, "authenticated": False, "sections": rows, "message": "Open Settings to configure and connect Trakt."}

        def load(section):
            key, title, path, auth = section
            if auth and not self.trakt_access_token:
                return {"id": key, "title": title, "items": [], "locked": True}
            try:
                if isinstance(path, tuple):
                    values = []
                    for calendar_path in path:
                        values.extend(self.trakt_request(calendar_path, authenticated=auth))
                    values.sort(key=lambda value: value.get("first_aired") or value.get("released") or "")
                else:
                    values = self.trakt_request(path, authenticated=auth)
                return {"id": key, "title": title, "items": [self.normalize_trakt_item(value, key) for value in values[:50]]}
            except Exception as error:
                return {"id": key, "title": title, "items": [], "error": str(error), "locked": auth}

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(load, sections))
        if self.trakt_access_token:
            status_paths = (
                "/sync/watched/movies?limit=1000", "/sync/watched/shows?limit=1000",
                "/sync/watchlist/movies?limit=1000", "/sync/watchlist/shows?limit=1000",
            )
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    watched_movies, watched_shows, watchlist_movies, watchlist_shows = list(pool.map(lambda path: self.trakt_request(path, authenticated=True), status_paths))
                library_items = self.get_trakt_favorites()
                movie_watched = {item.get("movie", {}).get("ids", {}).get("trakt") for item in watched_movies}
                movie_watchlist = {item.get("movie", {}).get("ids", {}).get("trakt") for item in watchlist_movies}
                show_watchlist = {item.get("show", {}).get("ids", {}).get("trakt") for item in watchlist_shows}
                movie_favorites = {item.get("ids", {}).get("trakt") for item in library_items if item.get("mediaType") == "movie"}
                show_favorites = {item.get("ids", {}).get("trakt") for item in library_items if item.get("mediaType") == "show"}
                show_progress = {}
                for value in watched_shows:
                    show = value.get("show") or {}
                    watched_count = sum(len(season.get("episodes") or []) for season in value.get("seasons") or [])
                    aired_count = int(show.get("aired_episodes") or 0)
                    show_progress[show.get("ids", {}).get("trakt")] = "watched" if aired_count and watched_count >= aired_count else "in-progress"
                for row in rows:
                    for item in row.get("items") or []:
                        trakt_id, media_type = item.get("ids", {}).get("trakt"), item.get("mediaType")
                        item["watchlisted"] = trakt_id in (movie_watchlist if media_type == "movie" else show_watchlist)
                        item["favorite"] = trakt_id in (movie_favorites if media_type == "movie" else show_favorites)
                        if media_type == "movie" and trakt_id in movie_watched:
                            item["watchState"] = "watched"
                        elif media_type == "show" and trakt_id in show_progress:
                            item["watchState"] = show_progress[trakt_id]
            except Exception as error:
                print(f"[trakt] Could not load poster states: {error}")
        dashboard = {"configured": True, "authenticated": bool(self.trakt_access_token), "sections": rows}
        with self.trakt_lock:
            self.trakt_cache = dashboard
            self.trakt_cache_time = time.monotonic()
        return dashboard

    def normalize_trakt_item(self, value, section):
        if value.get("movie"):
            media_type, media = "movie", value["movie"]
        elif value.get("show"):
            media_type, media = "show", value["show"]
        elif value.get("episode"):
            media_type, media = "episode", value["episode"]
        elif section in {"recommendations", "trending", "popular", "anticipated", "search"} and value.get("ids"):
            media_type, media = "movie", value
        else:
            media_type, media = "list", value
        title = media.get("title") or value.get("name") or "Untitled"
        images = media.get("images") or value.get("images") or {}
        image_source = self.pick_trakt_image(images, "poster") or self.pick_trakt_image(images, "fanart") or self.pick_trakt_image(images, "thumb")
        image_url = self.register_trakt_image(image_source) if image_source else None
        progress = value.get("progress")
        rating = value.get("rating") or media.get("rating")
        episode_context = value.get("episode") or (media if media_type == "episode" else {})
        return {
            "title": title, "year": media.get("year"), "overview": media.get("overview"),
            "ids": media.get("ids", {}), "image": image_url, "progress": progress,
            "rating": rating, "listedAt": value.get("listed_at"), "watchedAt": value.get("watched_at"),
            "calendarAt": value.get("first_aired") or value.get("released"),
            "plays": value.get("plays"), "section": section, "mediaType": media_type,
            "playbackId": value.get("id") if section == "continue" else None,
            "score": value.get("score"), "votes": media.get("votes"),
            "season": episode_context.get("season"), "episode": episode_context.get("number"),
            "genres": media.get("genres") or [],
        }

    def get_trakt_details(self, media_type, trakt_id, season=None):
        if media_type not in {"movie", "show"} or not re.fullmatch(r"\d{1,12}", trakt_id):
            raise ValueError("Invalid Trakt title identifier.")
        plural = "movies" if media_type == "movie" else "shows"
        media = self.trakt_request(f"/{plural}/{trakt_id}")
        result = self.public_trakt_metadata(media, media_type)
        if self.trakt_access_token:
            result["favorite"] = any(item.get("mediaType") == media_type and str(item.get("ids", {}).get("trakt")) == trakt_id for item in self.get_trakt_favorites())
        if media_type == "movie":
            return result
        seasons = self.trakt_request(f"/shows/{trakt_id}/seasons")
        result["seasons"] = [
            {
                "number": item.get("number"), "title": item.get("title") or ("Specials" if item.get("number") == 0 else f"Season {item.get('number')}"),
                "episodeCount": item.get("episode_count"), "airedEpisodes": item.get("aired_episodes"),
                "image": self.proxy_trakt_artwork(item.get("images") or {}, "poster"),
            }
            for item in seasons if isinstance(item.get("number"), int)
        ]
        if season is not None:
            if season < 0 or season > 999:
                raise ValueError("Invalid season number.")
            episodes = self.trakt_request(f"/shows/{trakt_id}/seasons/{season}")
            result["episodes"] = [
                {
                    "number": item.get("number"), "season": item.get("season"), "title": item.get("title") or f"Episode {item.get('number')}",
                    "overview": item.get("overview"), "runtime": item.get("runtime"), "firstAired": item.get("first_aired"),
                    "rating": item.get("rating"), "image": self.proxy_trakt_artwork(item.get("images") or {}, "screenshot"),
                }
                for item in episodes
            ]
        return result

    def search_trakt(self, query, sort="recommended"):
        cache_key = "trakt_search:" + hashlib.sha256(query.strip().casefold().encode()).hexdigest()
        values, synced_at = self.database.get_provider_cache(cache_key)
        if not isinstance(values, list) or time.time() - synced_at > 30 * 24 * 60 * 60:
            encoded = urllib.parse.urlencode({"query": query, "limit": 20})
            try:
                values = self.trakt_request(f"/search/movie,show?{encoded}")
                self.database.cache_provider(cache_key, values)
            except (RuntimeError, PermissionError, urllib.error.URLError):
                if not isinstance(values, list):
                    raise
        results = [self.normalize_trakt_item(value, "search") for value in values[:20]]
        key = (lambda item: item.get("votes") or 0) if sort == "popular" else (lambda item: item.get("score") or 0)
        return sorted(results, key=key, reverse=True)

    def public_trakt_metadata(self, media, media_type):
        return {
            "mediaType": media_type, "title": media.get("title"), "year": media.get("year"),
            "overview": media.get("overview"), "tagline": media.get("tagline"), "runtime": media.get("runtime"),
            "rating": media.get("rating"), "votes": media.get("votes"), "certification": media.get("certification"),
            "genres": media.get("genres") or [], "status": media.get("status"), "network": media.get("network"),
            "released": media.get("released") or media.get("first_aired"), "trailer": media.get("trailer"),
            "ids": media.get("ids") or {}, "poster": self.proxy_trakt_artwork(media.get("images") or {}, "poster"),
            "fanart": self.proxy_trakt_artwork(media.get("images") or {}, "fanart"),
        }

    def proxy_trakt_artwork(self, images, kind):
        source = self.pick_trakt_image(images, kind)
        return self.register_trakt_image(source) if source else None

    @staticmethod
    def pick_trakt_image(images, kind):
        values = images.get(kind) or []
        if isinstance(values, list) and values:
            return values[0]
        return values if isinstance(values, str) else None

    def register_trakt_image(self, source):
        source = source if source.startswith("https://") else "https://" + source.lstrip("/")
        image_id = hashlib.sha256(source.encode()).hexdigest()[:32]
        with self.trakt_lock:
            self.trakt_images[image_id] = source
            sources = dict(self.trakt_images)
        if hasattr(self, "database"):
            self.database.cache_provider("trakt_artwork_sources", sources)
        return f"/api/trakt/image/{image_id}"

    def get_trakt_image(self, image_id):
        path = TRAKT_IMAGE_ROOT / f"{image_id}.webp"
        if path.is_file():
            return path
        with self.trakt_lock:
            source = self.trakt_images.get(image_id)
        if not source:
            raise LookupError("unknown artwork")
        request = urllib.request.Request(source, headers={"User-Agent": "unarr-web/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise OSError("artwork exceeds 8 MB")
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        return path

    def server_close(self):
        self.provider_sync_stop.set()
        with self.stream_lock:
            processes = [item.get("process") for item in self.streams.values()]
        for process in processes:
            if process is not None and process.poll() is None:
                process.terminate()
        with self.scan_lock:
            if self.scan_timer is not None:
                self.scan_timer.cancel()
        super().server_close()

    @staticmethod
    def load_library():
        if not LIBRARY_CACHE.is_file():
            return {"items": [], "scannedAt": None}
        with LIBRARY_CACHE.open(encoding="utf-8") as handle:
            return json.load(handle)

    def reconcile_library(self):
        """Overlay Unarr's rich scan metadata onto the files present right now."""
        with self.library_lock:
            cache = self.load_library()
            roots = []
            if cache.get("path"):
                roots.append(Path(cache["path"]))
            roots.extend(Path(value) for value in os.environ.get("UNARR_LIBRARY_PATHS", "").split(os.pathsep) if value)
            roots = list(dict.fromkeys(path for path in roots if path.is_dir()))
            cached = {str(Path(item.get("filePath", ""))).casefold(): item for item in cache.get("items", [])}
            live_items = []
            for root in roots:
                for current, directories, files in os.walk(root):
                    directories[:] = [name for name in directories if not name.startswith(".")]
                    for name in files:
                        path = Path(current) / name
                        if path.suffix.lower() not in VIDEO_EXTENSIONS:
                            continue
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        previous = cached.get(str(path).casefold())
                        item = dict(previous) if previous else self.basic_library_item(path, stat)
                        if previous and previous.get("fileSize") != stat.st_size:
                            item.pop("mediaInfo", None)
                            item.pop("scanError", None)
                        item.update({
                            "filePath": str(path), "fileName": name, "fileSize": stat.st_size,
                            "modTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                            "live": True, "indexed": previous is not None,
                        })
                        live_items.append(item)
            live_items.sort(key=lambda item: (item.get("title", "").casefold(), item.get("season", 0), item.get("episode", 0)))
            snapshot = self.library_signature(live_items)
            if self.library_snapshot is None:
                self.library_snapshot = snapshot
                if any(not item.get("indexed") for item in live_items):
                    self.schedule_library_scan(roots)
            elif snapshot != self.library_snapshot:
                self.library_snapshot = snapshot
                self.schedule_library_scan(roots)
            self.database.replace_library_items("local", live_items)
            return {**cache, "items": live_items, "refreshedAt": time.time(), "roots": [str(root) for root in roots]}

    @staticmethod
    def library_signature(items):
        return tuple(sorted((item.get("filePath"), item.get("fileSize"), item.get("modTime")) for item in items))

    def schedule_library_scan(self, roots):
        with self.scan_lock:
            if self.scan_state.get("status") == "running":
                self.scan_again_roots = list(roots)
                self.scan_state["message"] = "Scanning metadata; another change will be scanned next"
                return
            if self.scan_timer is not None:
                self.scan_timer.cancel()
            self.scan_state = {"status": "scheduled", "message": "Library changed; scan starts after files settle"}
            self.scan_timer = threading.Timer(5, self.run_library_scan, args=(list(roots),))
            self.scan_timer.daemon = True
            self.scan_timer.start()

    def run_library_scan(self, roots):
        with self.scan_lock:
            self.scan_timer = None
            if self.scan_state.get("status") == "running":
                return
            self.scan_state = {"status": "running", "message": "Unarr is scanning library metadata…", "startedAt": time.time()}
        failures = []
        for root in roots:
            try:
                result = subprocess.run(
                    [self.unarr_bin, "scan", str(root), "--no-color"], capture_output=True, text=True,
                    timeout=2 * 60 * 60, check=False,
                )
                if result.returncode:
                    failures.append((result.stderr or result.stdout).strip() or f"exit code {result.returncode}")
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(str(error))
        rerun_roots = None
        with self.scan_lock:
            if failures:
                self.scan_state = {"status": "error", "message": failures[-1], "finishedAt": time.time()}
                print(f"[library-scan] failed: {failures[-1]}")
            else:
                self.scan_state = {"status": "complete", "message": "Library metadata is up to date", "finishedAt": time.time()}
                print("[library-scan] complete")
            rerun_roots = self.scan_again_roots
            self.scan_again_roots = None
        if rerun_roots:
            self.schedule_library_scan(rerun_roots)

    def get_scan_state(self):
        with self.scan_lock:
            return dict(self.scan_state)

    @staticmethod
    def basic_library_item(path, stat):
        stem = re.sub(r"[._]+", " ", path.stem)
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", stem)
        quality_match = re.search(r"\b(2160p|1080p|720p|480p)\b", stem, re.I)
        year = year_match.group(1) if year_match else None
        quality = quality_match.group(1).lower() if quality_match else None
        episode = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", stem, re.I)
        title = re.split(r"\b(?:19\d{2}|20\d{2}|S\d{1,2}E\d{1,3}|2160p|1080p|720p|480p)\b", stem, maxsplit=1, flags=re.I)[0].strip(" -_([]{}") or path.stem
        return {
            "filePath": str(path), "fileName": path.name, "fileSize": stat.st_size,
            "modTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "title": title, "year": year, "quality": quality,
            "season": int(episode.group(1)) if episode else None, "episode": int(episode.group(2)) if episode else None,
        }

    @staticmethod
    def library_item_id(item):
        return item.get("fingerprint") or hashlib.sha256(item.get("filePath", "").encode()).hexdigest()

    @staticmethod
    def public_library_item(item):
        return {
            "id": UnarrServer.library_item_id(item), "title": item.get("title") or item.get("fileName"),
            "fileName": item.get("fileName"), "fileSize": item.get("fileSize", 0),
            "year": item.get("year"), "season": item.get("season"), "episode": item.get("episode"),
            "quality": item.get("quality"), "codec": item.get("codec"), "mediaInfo": item.get("mediaInfo"),
            "scanError": item.get("scanError"), "indexed": item.get("indexed", True),
        }

    def create_library_stream(self, item_id, quality, audio_index=0, subtitle_index=-1):
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("ffmpeg and ffprobe are required for browser HLS playback.")
        item = next((value for value in self.reconcile_library().get("items", []) if self.library_item_id(value) == item_id), None)
        if item is None:
            raise LookupError("Library item not found. Run 'unarr scan' to refresh the library.")
        source = Path(item.get("filePath", ""))
        if not source.is_file():
            raise LookupError("The library file is no longer available. Run 'unarr scan' to refresh the library.")
        media_info = item.get("mediaInfo") or {}
        audio_tracks = media_info.get("audio") or []
        subtitle_tracks = media_info.get("subtitles") or []
        if audio_tracks and audio_index >= len(audio_tracks):
            raise LookupError("The selected audio track no longer exists. Refresh the library and try again.")
        if subtitle_index >= len(subtitle_tracks):
            raise LookupError("The selected subtitle track no longer exists. Refresh the library and try again.")
        burn_subtitle = subtitle_index >= 0 and subtitle_tracks[subtitle_index].get("codec", "").lower() not in TEXT_SUBTITLE_CODECS
        with self.stream_lock:
            active = sum(value["status"] in {"buffering", "ready"} and value.get("kind") == "library" and value.get("process") and value["process"].poll() is None for value in self.streams.values())
            if active >= 2:
                raise RuntimeError("Two library transcodes are already active. Stop one before starting another.")

        media_id = f"lib{item_id[:16]}{quality.replace('p', '')}a{audio_index}s{subtitle_index + 1}"
        output_dir = HLS_ROOT / media_id
        playlist = output_dir / "master.m3u8"
        session_id = secrets.token_urlsafe(18)
        cache_complete = playlist.is_file() and "#EXT-X-ENDLIST" in playlist.read_text(errors="ignore")
        if not cache_complete:
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True)
        subtitle_url = self.prepare_subtitle(source, subtitle_index, output_dir, media_id) if subtitle_index >= 0 and not burn_subtitle else None
        selected_audio = audio_tracks[audio_index] if audio_index < len(audio_tracks) else {}
        selected_subtitle = subtitle_tracks[subtitle_index] if subtitle_index >= 0 else {}
        duration_seconds = (media_info.get("video") or {}).get("duration")
        tracks = []
        if subtitle_url:
            tracks.append({
                "kind": "subtitles", "src": subtitle_url,
                "srclang": selected_subtitle.get("lang") or "und",
                "label": selected_subtitle.get("title") or selected_subtitle.get("lang") or "Subtitles",
                "default": True,
            })
        if cache_complete:
            with self.stream_lock:
                self.streams[session_id] = {
                    "kind": "library", "status": "ready", "phase": "ready", "message": "Loaded from HLS cache",
                    "progress": 100, "url": f"/media/{media_id}/master.m3u8", "error": None,
                    "process": None, "started_at": time.monotonic(), "tracks": tracks,
                    "audio": selected_audio, "durationSeconds": duration_seconds,
                }
            return session_id
        heights = {"1080p": 1080, "720p": 720, "480p": 480}
        # libx264 is the reliable cross-platform fallback. The daemon performs
        # deeper hardware probing; this standalone process must not assume that
        # an encoder listed by ffmpeg can actually create a session on the host.
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "5M", "-bufsize", "10M"]
        filters = []
        if burn_subtitle:
            filters.append(f"[0:v:0][0:s:{subtitle_index}]overlay")
        if quality in heights:
            filters.append(f"scale=-2:min({heights[quality]}\\,ih)")
        if filters:
            filter_chain = ",".join(filters) + "[hlsvideo]"
            video_mapping = ["-filter_complex", filter_chain, "-map", "[hlsvideo]"]
        else:
            video_mapping = ["-map", "0:v:0"]
        args = [
            self.ffmpeg, "-hide_banner", "-y", "-i", str(source), *video_mapping, "-map", f"0:a:{audio_index}?",
            *video_args, "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-f", "hls", "-hls_time", "4",
            "-hls_playlist_type", "event", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(output_dir / "seg-%06d.m4s"), str(playlist),
        ]
        # Do not pipe ffmpeg's progress output unless it is drained concurrently;
        # a full stderr pipe stalls HLS generation even though ffmpeg stays alive.
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        with self.stream_lock:
            self.streams[session_id] = {
                "kind": "library", "status": "buffering", "phase": "transcoding", "message": "Starting HLS transcode…",
                "progress": 0, "url": None, "error": None, "process": process, "started_at": time.monotonic(),
                "playlist": playlist, "media_url": f"/media/{media_id}/master.m3u8",
                "tracks": tracks, "audio": selected_audio,
                "durationSeconds": duration_seconds,
            }
        threading.Thread(target=self.watch_library_stream, args=(session_id,), name=f"library-hls-{process.pid}", daemon=True).start()
        return session_id

    def prepare_subtitle(self, source, subtitle_index, output_dir, media_id):
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"subtitle-{subtitle_index}.vtt"
        if destination.is_file():
            return f"/media/{media_id}/{destination.name}"
        cached = source.parent / ".unarr" / f"{source.name}.s{subtitle_index}.vtt"
        if cached.is_file():
            shutil.copy2(cached, destination)
            return f"/media/{media_id}/{destination.name}"
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-map", f"0:s:{subtitle_index}", "-c:s", "webvtt", str(destination)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode or not destination.is_file():
            raise RuntimeError((result.stderr or "Could not extract the selected subtitle track.").strip())
        return f"/media/{media_id}/{destination.name}"

    def watch_library_stream(self, session_id):
        with self.stream_lock:
            item = self.streams[session_id]
            process, playlist = item["process"], item["playlist"]
        ready = False
        while process.poll() is None:
            segments = list(playlist.parent.glob("seg-*.m4s"))
            minimum_segments = item.get("min_ready_segments", 1)
            if not ready and playlist.is_file() and len(segments) >= minimum_segments:
                ready = True
                with self.stream_lock:
                    item["status"] = "ready"
                    item["phase"] = "ready"
                    item["message"] = "HLS playback ready"
                    item["url"] = item["media_url"]
                print(f"[library {session_id[:8]}] HLS ready: {item['url']}")
            elif not ready:
                elapsed = round(time.monotonic() - item["started_at"])
                with self.stream_lock:
                    item["message"] = f"Building playback buffer… {len(segments)}/{minimum_segments} segments · {elapsed}s"
            time.sleep(0.25)
        error_output = process.stderr.read().strip() if process.stderr else ""
        with self.stream_lock:
            if item["status"] == "stopped":
                return
            if process.returncode and not ready:
                item["status"] = "error"
                item["phase"] = "error"
                item["error"] = error_output.splitlines()[-1] if error_output else f"ffmpeg exited with code {process.returncode}"
                print(f"[library {session_id[:8]}] HLS failed: {item['error']}")
            elif process.returncode and ready:
                item["message"] = "Playback available; background transcode ended early"
            else:
                item["status"] = "ready"
                item["progress"] = 100
                item["message"] = "HLS encode cached"

    def create_stream(self, info_hash):
        with self.stream_lock:
            active = sum(item["status"] in {"buffering", "ready"} for item in self.streams.values())
            if active >= 3:
                raise RuntimeError("Three streams are already active. Stop one before starting another.")
        process = subprocess.Popen(
            [self.unarr_bin, "stream", info_hash, "--no-open", "--no-color"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, bufsize=1,
        )
        session_id = secrets.token_urlsafe(18)
        with self.stream_lock:
            self.streams[session_id] = {
                "status": "buffering", "phase": "metadata", "message": "Waiting for torrent metadata…",
                "progress": None, "url": None, "error": None, "process": process,
                "started_at": time.monotonic(),
            }
        threading.Thread(target=self.watch_stream, args=(session_id,), name=f"unarr-stream-{process.pid}", daemon=True).start()
        return session_id

    def get_stream(self, session_id):
        with self.stream_lock:
            item = self.streams.get(session_id)
            if item is None:
                return None
            response = {key: item[key] for key in ("status", "phase", "message", "progress", "url", "error")}
            response["tracks"] = item.get("tracks", [])
            response["audio"] = item.get("audio")
            response["availableAudio"] = item.get("availableAudio", [])
            response["availableSubtitles"] = item.get("availableSubtitles", [])
            response["durationSeconds"] = item.get("durationSeconds")
            response["elapsedSeconds"] = round(time.monotonic() - item["started_at"])
            if item.get("kind") != "library":
                response["metadataTimeoutSeconds"] = METADATA_TIMEOUT_SECONDS
            return response

    def stop_stream(self, session_id):
        with self.stream_lock:
            item = self.streams.get(session_id)
            if item is None:
                return False
            process = item["process"]
            item["status"] = "stopped"
        if process is not None and process.poll() is None:
            process.terminate()
        return True

    def watch_stream(self, session_id):
        with self.stream_lock:
            item = self.streams[session_id]
            process = item["process"]
        recent, record = [], ""
        for character in iter(lambda: process.stdout.read(1), ""):
            if character not in "\r\n":
                record += character
                continue
            if record.strip():
                recent = (recent + [record.strip()])[-8:]
                self.update_stream_progress(item, record, session_id)
            record = ""
        if record.strip():
            recent = (recent + [record.strip()])[-8:]
            self.update_stream_progress(item, record, session_id)
        returncode = process.wait()
        with self.stream_lock:
            if item["status"] == "stopped":
                return
            if returncode:
                item["status"] = "error"
                item["error"] = recent[-1] if recent else f"unarr exited with code {returncode}"
                print(f"[stream {session_id[:8]}] failed ({returncode}): {item['error']}")
            else:
                item["status"] = "stopped"

    def update_stream_progress(self, item, output, session_id):
        url_match = STREAM_URL.search(output)
        buffer_match = BUFFER_PROGRESS.search(output)
        download_match = DOWNLOAD_PROGRESS.search(output)
        with self.stream_lock:
            if "Waiting for metadata" in output:
                item["phase"] = "metadata"
                item["message"] = "Waiting for torrent metadata…"
            if buffer_match:
                percent = min(100, int(buffer_match.group(1)))
                item["phase"] = "buffering"
                item["progress"] = percent
                item["message"] = f"Building playback buffer… {percent}%"
            if download_match:
                percent, speed, peers, seeds = download_match.groups()
                item["phase"] = "streaming"
                item["progress"] = min(100, int(percent))
                item["message"] = f"{speed.strip()} · {peers} peers · {seeds} seeds"
            if url_match:
                item["url"] = url_match.group(1)
                item["status"] = "ready"
                item["phase"] = "ready"
                item["message"] = "Ready to play"
        if url_match:
            print(f"[stream {session_id[:8]}] ready: {url_match.group(1)}")

    def watch_download(self, process, info_hash, job_id):
        lines = []
        for line in process.stdout or []:
            lines.append(line)
            match = DOWNLOAD_PROGRESS.search(line)
            if match:
                percent, speed, peers, seeds = match.groups()
                self.update_activity(job_id, "active", f"{percent}% · {speed.strip()} · {peers} peers · {seeds} seeds")
        process.wait()
        output = "".join(lines)
        short_hash = info_hash[:8]
        if process.returncode:
            message = output.strip() or "no command output"
            self.update_activity(job_id, "error", message[-500:])
            print(f"[download {short_hash}] failed ({process.returncode}): {message}")
        else:
            self.update_activity(job_id, "complete", "Local Unarr download completed")
            print(f"[download {short_hash}] completed")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("UNARR_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNARR_WEB_PORT", "8787")))
    parser.add_argument("--unarr", default=os.environ.get("UNARR_BIN", "unarr"), help="path to the unarr executable")
    return parser.parse_args()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    args = parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("WARNING: listening beyond localhost; use an authenticated TLS reverse proxy")
    server = UnarrServer((args.host, args.port), UnarrHandler, args.unarr)
    print(f"unarr web is ready at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
