#!/usr/bin/env python3
"""Local web interface for an installed unarr CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
INFO_HASH = re.compile(r"^(?:[a-fA-F0-9]{40}|[A-Z2-7a-z2-7]{32})$")
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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' https: data:; connect-src 'self'")
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
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/download":
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
        return self.command_json(["download", info_hash, "--no-color"], status=202, transform=lambda text: {"output": text.strip()})

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
        super().__init__(address, handler)


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
