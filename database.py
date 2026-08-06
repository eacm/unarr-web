"""SQLite persistence for unarr-web."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path


SCHEMA_VERSION = 1


class AppDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._initialize()

    def connect(self, path=None):
        connection = sqlite3.connect(str(path or self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self):
        with self.lock, self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_matches (
                    item_id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'show')),
                    trakt_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    image TEXT NOT NULL DEFAULT '',
                    released TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    synced_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_items (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(source, item_id)
                );
                CREATE INDEX IF NOT EXISTS library_items_source_updated
                    ON library_items(source, updated_at DESC);
            """)
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        os.chmod(self.path, 0o600)

    def is_empty(self):
        with self.connect() as connection:
            return connection.execute("SELECT NOT EXISTS(SELECT 1 FROM settings)").fetchone()[0] == 1

    def load_settings(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value_json FROM settings").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return result

    def save_settings(self, values):
        now = time.time()
        with self.lock, self.connect() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                [(key, json.dumps(value, separators=(",", ":")), now) for key, value in values.items()],
            )

    def load_matches(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT item_id, media_type, trakt_id, title, image, released FROM library_matches"
            ).fetchall()
        return {row["item_id"]: {"type": row["media_type"], "traktId": row["trakt_id"], "title": row["title"], "image": row["image"], "released": row["released"]} for row in rows}

    def save_match(self, item_id, value):
        with self.lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO library_matches(item_id, media_type, trakt_id, title, image, released, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET media_type=excluded.media_type, trakt_id=excluded.trakt_id, title=excluded.title, image=excluded.image, released=excluded.released, updated_at=excluded.updated_at",
                (item_id, value["type"], value["traktId"], value.get("title", ""), value.get("image", ""), value.get("released", ""), time.time()),
            )

    def save_matches(self, values):
        now = time.time()
        with self.lock, self.connect() as connection:
            connection.executemany(
                "INSERT INTO library_matches(item_id, media_type, trakt_id, title, image, released, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET media_type=excluded.media_type, trakt_id=excluded.trakt_id, title=excluded.title, image=excluded.image, released=excluded.released, updated_at=excluded.updated_at",
                [(item_id, value["type"], value["traktId"], value.get("title", ""), value.get("image", ""), value.get("released", ""), now) for item_id, value in values.items()],
            )

    def delete_match(self, item_id):
        with self.lock, self.connect() as connection:
            connection.execute("DELETE FROM library_matches WHERE item_id = ?", (item_id,))

    def cache_provider(self, provider, payload):
        now = time.time()
        encoded = json.dumps(payload, separators=(",", ":"))
        with self.lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO provider_cache(provider, payload_json, synced_at) VALUES(?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET payload_json=excluded.payload_json, synced_at=excluded.synced_at",
                (provider, encoded, now),
            )

    def get_provider_cache(self, provider):
        with self.connect() as connection:
            row = connection.execute("SELECT payload_json, synced_at FROM provider_cache WHERE provider = ?", (provider,)).fetchone()
        if not row:
            return None, 0
        try:
            return json.loads(row["payload_json"]), float(row["synced_at"])
        except json.JSONDecodeError:
            return None, 0

    def invalidate_provider_cache(self, provider):
        with self.lock, self.connect() as connection:
            connection.execute("DELETE FROM provider_cache WHERE provider = ?", (provider,))

    def replace_library_items(self, source, items):
        now = time.time()
        rows = [(source, str(item.get("id") or item.get("filePath") or index), json.dumps(item, separators=(",", ":")), now) for index, item in enumerate(items)]
        with self.lock, self.connect() as connection:
            connection.execute("DELETE FROM library_items WHERE source = ?", (source,))
            connection.executemany("INSERT INTO library_items(source, item_id, payload_json, updated_at) VALUES(?, ?, ?, ?)", rows)

    def get_library_items(self, source):
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM library_items WHERE source = ? ORDER BY item_id", (source,)).fetchall()
        items = []
        for row in rows:
            try:
                items.append(json.loads(row["payload_json"]))
            except json.JSONDecodeError:
                continue
        return items

    def backup_to(self, destination: Path):
        destination = Path(destination)
        with self.lock, self.connect() as source, self.connect(destination) as target:
            source.backup(target)
        os.chmod(destination, 0o600)

    def restore_from(self, source_path: Path):
        source_path = Path(source_path)
        source = sqlite3.connect(str(source_path), timeout=30)
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
            version = source.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
            if integrity != "ok" or not version or int(version[0]) > SCHEMA_VERSION:
                raise ValueError("The SQLite backup is invalid or uses a newer schema.")
            required = {"settings", "library_matches", "provider_cache", "library_items", "app_meta"}
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables):
                raise ValueError("This is not an unarr-web SQLite backup.")
            with self.lock, self.connect() as target:
                source.backup(target)
        except sqlite3.DatabaseError as error:
            raise ValueError("This is not a valid SQLite database.") from error
        finally:
            source.close()
        self._initialize()
