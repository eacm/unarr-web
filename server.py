#!/usr/bin/env python3
"""Local web interface for an installed unarr CLI."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = Path(os.environ.get("UNARR_DATA_DIR", Path.home() / "Library" / "Application Support" / "unarr"))
LIBRARY_CACHE = DATA_ROOT / "library.json"
HLS_ROOT = Path(os.environ.get("UNARR_WEB_HLS_DIR", ROOT / ".cache" / "hls"))
TRAKT_IMAGE_ROOT = ROOT / ".cache" / "trakt-images"
TRAKT_SETTINGS_FILE = DATA_ROOT / "web-trakt.json"
INFO_HASH = re.compile(r"^(?:[a-fA-F0-9]{40}|[A-Z2-7a-z2-7]{32})$")
STREAM_URL = re.compile(r"Open this URL in your player:\s*(https?://\S+)")
BUFFER_PROGRESS = re.compile(r"Buffering:\s*(\d+)%")
DOWNLOAD_PROGRESS = re.compile(r"(\d+)%\s*\|\s*([^|]+)\|\s*Peers:\s*(\d+)\s*\|\s*Seeds:\s*(\d+)")
METADATA_TIMEOUT_SECONDS = 60
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv"}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}
FILTERS = {
    "type": ("--type", {"movie", "show"}),
    "quality": ("--quality", {"480p", "720p", "1080p", "2160p"}),
    "sort": ("--sort", {"relevance", "seeders", "year", "rating", "added"}),
}


class UnarrHandler(SimpleHTTPRequestHandler):
    server_version = "unarr-web/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' https: data:; connect-src 'self'; media-src http: https:")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == "/api/health":
            return self.command_json(["version"], transform=lambda text: {"ok": True, "version": text.strip()})
        if request.path == "/api/status":
            return self.command_json(["status", "--no-color"], transform=lambda text: {"output": text.strip()})
        if request.path == "/api/search":
            return self.search(parse_qs(request.query))
        if request.path == "/api/library":
            return self.library()
        if request.path == "/api/trakt/dashboard":
            return self.trakt_dashboard()
        if request.path == "/api/trakt/settings":
            return self.send_json(self.server.get_trakt_settings())
        if request.path == "/api/trakt/auth":
            return self.send_json(self.server.get_trakt_auth())
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
        if path not in {"/api/download", "/api/stream", "/api/library/stream", "/api/trakt/settings", "/api/trakt/auth"}:
            return self.error_json(404, "Not found.")
        if not self.same_origin():
            return self.error_json(403, "Cross-origin requests are not allowed.")
        body = self.read_json()
        if body is None:
            return self.error_json(400, "A valid JSON request is required.")
        if path == "/api/trakt/settings":
            try:
                return self.send_json(self.server.save_trakt_settings(body))
            except ValueError as error:
                return self.error_json(400, str(error))
            except OSError as error:
                return self.error_json(500, f"Could not save Trakt settings: {error}")
        if path == "/api/trakt/auth":
            try:
                return self.send_json(self.server.start_trakt_auth(), 202)
            except urllib.error.HTTPError as error:
                return self.error_json(502, getattr(error, "trakt_message", str(error)))
            except (RuntimeError, urllib.error.URLError) as error:
                return self.error_json(502, str(error))
        if path == "/api/library/stream":
            return self.start_library_stream(body)
        info_hash = body.get("infoHash", "")
        if not isinstance(info_hash, str) or not INFO_HASH.fullmatch(info_hash):
            return self.error_json(400, "A valid torrent info hash is required.")
        if path == "/api/stream":
            return self.start_stream(info_hash)
        return self.start_download(info_hash)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096:
                raise ValueError
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def library(self):
        try:
            cache = self.server.reconcile_library()
        except (OSError, json.JSONDecodeError) as error:
            return self.error_json(500, f"Could not read the unarr library: {error}")
        items = [self.server.public_library_item(item) for item in cache.get("items", [])]
        return self.send_json({
            "items": items, "scannedAt": cache.get("scannedAt"), "refreshedAt": cache.get("refreshedAt"),
            "transcode": {"available": bool(self.server.ffmpeg and self.server.ffprobe), "ffmpeg": self.server.ffmpeg},
            "scan": self.server.get_scan_state(),
        })

    def trakt_dashboard(self):
        try:
            return self.send_json(self.server.get_trakt_dashboard())
        except Exception as error:
            return self.error_json(502, f"Trakt dashboard unavailable: {error}")

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

    def start_download(self, info_hash):
        """Start a long-running download without tying it to the HTTP request."""
        try:
            process = subprocess.Popen(
                [self.server.unarr_bin, "download", info_hash, "--no-color"],
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
            args=(process, info_hash),
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, message, *args):
        print(f"[{self.log_date_time_string()}] {message % args}")


class UnarrServer(ThreadingHTTPServer):
    def __init__(self, address, handler, unarr_bin, command_timeout=20):
        self.unarr_bin = unarr_bin
        self.command_timeout = command_timeout
        self.streams = {}
        self.stream_lock = threading.Lock()
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
        saved_trakt = self.load_trakt_settings()
        self.trakt_client_id = os.environ.get("TRAKT_CLIENT_ID", saved_trakt.get("client_id", ""))
        self.trakt_client_secret = os.environ.get("TRAKT_CLIENT_SECRET", saved_trakt.get("client_secret", ""))
        self.trakt_access_token = os.environ.get("TRAKT_ACCESS_TOKEN", saved_trakt.get("access_token", ""))
        self.trakt_refresh_token = saved_trakt.get("refresh_token", "")
        self.trakt_user = saved_trakt.get("user")
        self.trakt_auth = {"status": "idle"}
        self.trakt_auth_id = None
        self.trakt_cache = None
        self.trakt_cache_time = 0
        self.trakt_lock = threading.Lock()
        self.trakt_images = {}
        super().__init__(address, handler)

    @staticmethod
    def load_trakt_settings():
        try:
            value = json.loads(TRAKT_SETTINGS_FILE.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def write_trakt_settings(self):
        TRAKT_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "client_id": self.trakt_client_id, "client_secret": self.trakt_client_secret,
            "access_token": self.trakt_access_token, "refresh_token": self.trakt_refresh_token,
            "user": self.trakt_user,
        }
        temporary = TRAKT_SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2))
        temporary.chmod(0o600)
        temporary.replace(TRAKT_SETTINGS_FILE)

    def get_trakt_settings(self):
        return {
            "configured": bool(self.trakt_client_id and self.trakt_client_secret),
            "clientId": self.trakt_client_id,
            "hasClientSecret": bool(self.trakt_client_secret),
            "authenticated": bool(self.trakt_access_token),
            "user": self.trakt_user,
        }

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
        url = f"https://api.trakt.tv{path}{separator}extended=full"
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

    def get_trakt_dashboard(self):
        with self.trakt_lock:
            if self.trakt_cache and time.monotonic() - self.trakt_cache_time < 300:
                return self.trakt_cache
        sections = [
            ("continue", "Continue watching", "/sync/playback/movies?limit=20", True),
            ("watchlist", "Watchlist", "/sync/watchlist/movies?limit=20", True),
            ("history", "History", "/sync/history/movies?limit=20", True),
            ("collection", "Collection", "/sync/collection/movies?limit=20", True),
            ("ratings", "Ratings", "/users/me/ratings/movies?limit=20", True),
            ("recommendations", "Recommendations", "/recommendations/movies?limit=20", True),
            ("trending", "Trending", "/movies/trending?limit=20", False),
            ("popular", "Popular", "/movies/popular?limit=20", False),
            ("anticipated", "Anticipated", "/movies/anticipated?limit=20", False),
            ("lists", "Custom lists", "/users/me/lists?limit=20", True),
        ]
        if not self.trakt_client_id:
            rows = [{"id": key, "title": title, "items": [], "locked": True} for key, title, _, _ in sections]
            return {"configured": False, "authenticated": False, "sections": rows, "message": "Open Settings to configure and connect Trakt."}

        def load(section):
            key, title, path, auth = section
            if auth and not self.trakt_access_token:
                return {"id": key, "title": title, "items": [], "locked": True}
            try:
                values = self.trakt_request(path, authenticated=auth)
                return {"id": key, "title": title, "items": [self.normalize_trakt_item(value, key) for value in values[:20]]}
            except Exception as error:
                return {"id": key, "title": title, "items": [], "error": str(error), "locked": auth}

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(load, sections))
        dashboard = {"configured": True, "authenticated": bool(self.trakt_access_token), "sections": rows}
        with self.trakt_lock:
            self.trakt_cache = dashboard
            self.trakt_cache_time = time.monotonic()
        return dashboard

    def normalize_trakt_item(self, value, section):
        media = value.get("movie") or value.get("show") or value.get("episode") or value
        title = media.get("title") or value.get("name") or "Untitled"
        images = media.get("images") or value.get("images") or {}
        image_source = self.pick_trakt_image(images, "fanart") or self.pick_trakt_image(images, "poster") or self.pick_trakt_image(images, "thumb")
        image_url = self.register_trakt_image(image_source) if image_source else None
        progress = value.get("progress")
        rating = value.get("rating") or media.get("rating")
        return {
            "title": title, "year": media.get("year"), "overview": media.get("overview"),
            "ids": media.get("ids", {}), "image": image_url, "progress": progress,
            "rating": rating, "listedAt": value.get("listed_at"), "watchedAt": value.get("watched_at"),
            "plays": value.get("plays"), "section": section,
        }

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
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
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
            if not ready and playlist.is_file() and segments:
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
                    item["message"] = f"Encoding first HLS segment… {elapsed}s"
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

    @staticmethod
    def watch_download(process, info_hash):
        output, _ = process.communicate()
        short_hash = info_hash[:8]
        if process.returncode:
            message = output.strip() or "no command output"
            print(f"[download {short_hash}] failed ({process.returncode}): {message}")
        else:
            print(f"[download {short_hash}] completed")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("UNARR_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNARR_WEB_PORT", "8787")))
    parser.add_argument("--unarr", default=os.environ.get("UNARR_BIN", "unarr"), help="path to the unarr executable")
    return parser.parse_args()


def main():
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
