# unarr web

A local-first web interface for the [unarr CLI](https://github.com/Unarr-app/unarr-cli). It uses the `unarr` already installed and configured on your machine; credentials stay with the CLI.

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

Create a Trakt API application, then provide its client ID. Add an OAuth access token to unlock personal shelves such as Continue watching, Watchlist, History, Collection, Ratings, Recommendations, and Custom lists. Public Trending, Popular, and Anticipated shelves only need the client ID.

```sh
TRAKT_CLIENT_ID=your-client-id TRAKT_ACCESS_TOKEN=your-access-token python3 server.py
```

The Discover screen caches dashboard data for five minutes. Trakt artwork is proxied and cached locally under `.cache/trakt-images`; the browser never hotlinks Trakt's image hosts.

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

## Development

```sh
python3 -m unittest -v
```

This project is independently maintained and is not yet an official Unarr app.
