# unarr web

A local-first web interface for the [unarr CLI](https://github.com/Unarr-app/unarr-cli). It uses the `unarr` already installed and configured on your machine; credentials stay with the CLI.

Persistent application state is stored in `.data/unarr-web.sqlite3`. SQLite holds settings, library matches, local/provider item snapshots, and provider response caches so the Library view can render without repeating per-item metadata requests. Existing JSON settings are migrated automatically on first startup and retained as a legacy fallback copy.

## Run

Requirements: Python 3.9+ and `unarr` on your `PATH`. There are no third-party dependencies.

```sh
python3 server.py
```

Open <http://127.0.0.1:8787>. To use a specific binary:

```sh
python3 server.py --unarr /path/to/unarr
```

## Trakt

Open **Settings** in the web interface, enter the client ID and client secret from a Trakt API application, and select **Connect Trakt**. The browser opens Trakt's device activation page and the app completes authorization after you enter the displayed code. Connected-user settings are stored server-side in `.data/user-settings.json`, so they persist across browsers, with owner-only file permissions. Override that location with `UNARR_WEB_TRAKT_SETTINGS`. Settings can also be downloaded and restored from the web interface; backup files include private credentials and tokens and must be stored securely.

```sh
TRAKT_CLIENT_ID=your-client-id TRAKT_CLIENT_SECRET=your-client-secret python3 server.py
```

The Discover screen caches dashboard data for five minutes. Trakt artwork is proxied and cached locally under `.cache/trakt-images`; the browser never hotlinks Trakt's image hosts.

Movie and episode Play buttons query the [TorrentClaw API](https://torrentclaw.com/api/docs#/) by exact IMDb/TMDB ID, choose the strongest quality-score/seeder release, and pass its info hash to the local Unarr streaming engine. A free TorrentClaw API key is required because anonymous responses omit torrent hashes. Add it under Settings or with `TORRENTCLAW_API_KEY`.

## Current scope

- Verify and display the locally installed unarr version
- Search movies and shows with type, quality, and sort filters
- Browse Trakt continue-watching, watchlist, history, collection, ratings, recommendations, trending, popular, anticipated, and custom-list shelves
- Queue a selected release for download
- Stream a selected release in the browser with automatic buffering and cleanup
- Browse the local scanned library and play it through same-origin HLS
- Reconcile the library against the filesystem every three seconds while open
- Debounce filesystem changes and automatically run `unarr scan` to refresh metadata
- View local CLI and daemon status

Library HLS uses `ffmpeg` and `ffprobe` from `PATH`, transcodes to browser-safe H.264/AAC, and keeps completed encodes under `.cache/hls` for instant replay. Override that location with `UNARR_WEB_HLS_DIR`. Run `unarr scan ~/Media` to refresh the library index.

Scanned audio tracks can be selected before playback. Text subtitle formats are extracted or reused from Unarr's `.unarr` sidecar cache and attached to the browser player as WebVTT; image-based formats such as Blu-ray PGS are burned into the HLS video.

Playback opens in a dedicated dark player page that shows preparation progress and the scanned total runtime. The catalog and library use the same dark theme.

The server listens only on loopback by default. If you expose it to a network, put an authenticated TLS reverse proxy in front of it.

## Database backup

Settings includes controls to download and restore a complete `.sqlite3` backup. Export uses SQLite's online backup API, so the snapshot is consistent while the server is running. Restore performs an integrity check and validates the unarr-web schema before replacing current state. Backups contain credentials, tokens, API keys, matches, and cached library metadata; store them privately.

## Development

```sh
python3 -m unittest -v
```

This project is independently maintained and is not yet an official Unarr app.
