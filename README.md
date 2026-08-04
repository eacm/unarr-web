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

## Current scope

- Verify and display the locally installed unarr version
- Search movies and shows with type, quality, and sort filters
- Queue a selected release for download
- View local CLI and daemon status

The server listens only on loopback by default. If you expose it to a network, put an authenticated TLS reverse proxy in front of it.

## Development

```sh
python3 -m unittest -v
```

This project is independently maintained and is not yet an official Unarr app.
