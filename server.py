#!/usr/bin/env python3
"""Local web interface for an installed unarr CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
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
        if request.path.startswith("/api/stream/"):
            return self.stream_status(request.path[len("/api/stream/"):])
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in {"/api/download", "/api/stream"}:
            return self.error_json(404, "Not found.")
        if not self.same_origin():
            return self.error_json(403, "Cross-origin requests are not allowed.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            info_hash = body.get("infoHash", "")
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.error_json(400, "A valid JSON request is required.")
        if not isinstance(info_hash, str) or not INFO_HASH.fullmatch(info_hash):
            return self.error_json(400, "A valid torrent info hash is required.")
        if path == "/api/stream":
            return self.start_stream(info_hash)
        return self.start_download(info_hash)

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
        super().__init__(address, handler)

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
            response["metadataTimeoutSeconds"] = METADATA_TIMEOUT_SECONDS
            return response

    def stop_stream(self, session_id):
        with self.stream_lock:
            item = self.streams.get(session_id)
            if item is None:
                return False
            process = item["process"]
            item["status"] = "stopped"
        if process.poll() is None:
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
