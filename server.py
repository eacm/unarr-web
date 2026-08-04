#!/usr/bin/env python3
"""Local web interface for an installed unarr CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = Path(os.environ.get("UNARR_DATA_DIR", Path.home() / "Library" / "Application Support" / "unarr"))
LIBRARY_CACHE = DATA_ROOT / "library.json"
HLS_ROOT = Path(os.environ.get("UNARR_WEB_HLS_DIR", ROOT / ".cache" / "hls"))
INFO_HASH = re.compile(r"^(?:[a-fA-F0-9]{40}|[A-Z2-7a-z2-7]{32})$")
STREAM_URL = re.compile(r"Open this URL in your player:\s*(https?://\S+)")
BUFFER_PROGRESS = re.compile(r"Buffering:\s*(\d+)%")
DOWNLOAD_PROGRESS = re.compile(r"(\d+)%\s*\|\s*([^|]+)\|\s*Peers:\s*(\d+)\s*\|\s*Seeds:\s*(\d+)")
METADATA_TIMEOUT_SECONDS = 60
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
        if request.path.startswith("/api/stream/"):
            return self.stream_status(request.path[len("/api/stream/"):])
        if request.path.startswith("/media/"):
            return self.serve_media(request.path)
        return super().do_GET()

    def do_HEAD(self):
        request = urlparse(self.path)
        if request.path.startswith("/media/"):
            return self.serve_media(request.path, head_only=True)
        return super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in {"/api/download", "/api/stream", "/api/library/stream"}:
            return self.error_json(404, "Not found.")
        if not self.same_origin():
            return self.error_json(403, "Cross-origin requests are not allowed.")
        body = self.read_json()
        if body is None:
            return self.error_json(400, "A valid JSON request is required.")
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
            cache = self.server.load_library()
        except (OSError, json.JSONDecodeError) as error:
            return self.error_json(500, f"Could not read the unarr library: {error}")
        items = [self.server.public_library_item(item) for item in cache.get("items", [])]
        return self.send_json({
            "items": items, "scannedAt": cache.get("scannedAt"),
            "transcode": {"available": bool(self.server.ffmpeg and self.server.ffprobe), "ffmpeg": self.server.ffmpeg},
        })

    def start_library_stream(self, body):
        item_id = body.get("itemId", "")
        quality = body.get("quality", "original")
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-f0-9]{64}", item_id):
            return self.error_json(400, "A valid library item ID is required.")
        if quality not in {"original", "1080p", "720p", "480p"}:
            return self.error_json(400, "Invalid playback quality.")
        try:
            session_id = self.server.create_library_stream(item_id, quality)
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
        content_types = {".m3u8": "application/vnd.apple.mpegurl", ".m4s": "video/iso.segment", ".mp4": "video/mp4"}
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
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        HLS_ROOT.mkdir(parents=True, exist_ok=True)
        super().__init__(address, handler)

    def server_close(self):
        with self.stream_lock:
            processes = [item.get("process") for item in self.streams.values()]
        for process in processes:
            if process is not None and process.poll() is None:
                process.terminate()
        super().server_close()

    @staticmethod
    def load_library():
        if not LIBRARY_CACHE.is_file():
            return {"items": [], "scannedAt": None}
        with LIBRARY_CACHE.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def library_item_id(item):
        return item.get("fingerprint") or __import__("hashlib").sha256(item.get("filePath", "").encode()).hexdigest()

    @staticmethod
    def public_library_item(item):
        return {
            "id": UnarrServer.library_item_id(item), "title": item.get("title") or item.get("fileName"),
            "fileName": item.get("fileName"), "fileSize": item.get("fileSize", 0),
            "year": item.get("year"), "season": item.get("season"), "episode": item.get("episode"),
            "quality": item.get("quality"), "codec": item.get("codec"), "mediaInfo": item.get("mediaInfo"),
            "scanError": item.get("scanError"),
        }

    def create_library_stream(self, item_id, quality):
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("ffmpeg and ffprobe are required for browser HLS playback.")
        item = next((value for value in self.load_library().get("items", []) if self.library_item_id(value) == item_id), None)
        if item is None:
            raise LookupError("Library item not found. Run 'unarr scan' to refresh the library.")
        source = Path(item.get("filePath", ""))
        if not source.is_file():
            raise LookupError("The library file is no longer available. Run 'unarr scan' to refresh the library.")
        with self.stream_lock:
            active = sum(value["status"] in {"buffering", "ready"} and value.get("kind") == "library" and value.get("process") and value["process"].poll() is None for value in self.streams.values())
            if active >= 2:
                raise RuntimeError("Two library transcodes are already active. Stop one before starting another.")

        media_id = f"lib{item_id[:20]}{quality.replace('p', '')}"
        output_dir = HLS_ROOT / media_id
        playlist = output_dir / "master.m3u8"
        session_id = secrets.token_urlsafe(18)
        if playlist.is_file() and "#EXT-X-ENDLIST" in playlist.read_text(errors="ignore"):
            with self.stream_lock:
                self.streams[session_id] = {
                    "kind": "library", "status": "ready", "phase": "ready", "message": "Loaded from HLS cache",
                    "progress": 100, "url": f"/media/{media_id}/master.m3u8", "error": None,
                    "process": None, "started_at": time.monotonic(),
                }
            return session_id
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

        heights = {"1080p": 1080, "720p": 720, "480p": 480}
        # libx264 is the reliable cross-platform fallback. The daemon performs
        # deeper hardware probing; this standalone process must not assume that
        # an encoder listed by ffmpeg can actually create a session on the host.
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "5M", "-bufsize", "10M"]
        if quality in heights:
            video_args += ["-vf", f"scale=-2:min({heights[quality]}\\,ih)"]
        args = [
            self.ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
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
            }
        threading.Thread(target=self.watch_library_stream, args=(session_id,), name=f"library-hls-{process.pid}", daemon=True).start()
        return session_id

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
