<div align="center">

# YTS-Plex-RSS

**Scans YTS for movies missing from your Plex library and serves them as a live RSS feed for qBittorrent.**

[![Python](https://img.shields.io/badge/python-3.9+-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-web%20ui-lightgrey?style=flat-square&logo=flask)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

</div>

---

## Overview

YTS-Plex-RSS is a two-script Python tool that bridges your Plex movie library and YTS. `yts_check.py` connects to Plex via `plexapi`, scans every movie in your configured library section, then queries the YTS API to find titles available at your target quality that are not yet in Plex. Missing entries are saved to `missing_1080p.json` with magnet links and direct torrent URLs. `yts_rss.py` is a Flask web server that reads that JSON file, serves a paginated dark-themed dashboard, and exposes `/yts_missing.rss` as a valid Atom/RSS feed that qBittorrent can subscribe to for auto-download.

---

## How It Works

```
yts_check.py  ->  Plex library scan  ->  YTS API scan  ->  missing_1080p.json
yts_rss.py    ->  reads JSON         ->  web dashboard + /yts_missing.rss
qBittorrent   ->  subscribes to RSS  ->  auto-downloads missing movies
```

### yts_check.py

- Connects to your Plex server using `PLEX_USER_TOKEN` and `PLEX_SERVER_NAME`
- Scans the configured library section for all movies with `videoResolution == "1080"`
- Queries YTS in **fast mode** (newest first, stops when movies predate `MIN_YEAR`) or **full mode** (all pages, resumable via `scan_state.json`)
- Matches YTS titles against Plex using exact and punctuation-stripped fuzzy comparison
- Optionally filters out anime titles via the MyAnimeList Jikan API (`USE_MAL_FILTER=true`), with local caching in `mal_cache.json`
- Builds magnet links from the torrent hash and 13 tracker URLs; stores the official YTS `.torrent` URL alongside each entry
- Writes new missing entries to `missing_1080p.json`, pruning any titles that have since appeared in Plex
- After each scan, presents a post-scan menu (single keypress):

```
  1  Download .torrent files to torrent_files/
  2  Run scan again
  3  Exit
```

Selecting **1** downloads a `.torrent` file for every entry in `missing_1080p.json` into a `torrent_files/` folder next to the script. Already-downloaded files are skipped. The stored YTS torrent URL is tried first; if it fails the download falls back to a `yts.gg` URL derived from the magnet hash. The same download is also available via the **Feed Options** panel in the web dashboard.

### yts_rss.py

- Flask server binding to `YTS_BIND_HOST` (default `127.0.0.1`) on `YTS_PORT` (default `5000`)
- Dashboard at `/` shows a sortable, searchable, paginated table (50 rows per page) with title, size, year, date added, magnet link, and remove button
- Sidebar shows stats: total missing, total size, year range, newest year, and a bar chart of missing titles by year
- Individual and bulk removal with CSRF token protection
- **Feed Options** panel in the dashboard offers three choices:
  - **Open RSS Feed** — opens `/yts_missing.rss` directly in the browser for copying into qBittorrent
  - **Download .torrent Files** — triggers a background download of every missing movie's `.torrent` file into `torrent_files/` on the server; skips files already present; shows a status banner when started or if a download is already running
  - **RSS + Download .torrents** — opens the RSS feed in a new tab and simultaneously triggers the `.torrent` download
- `.torrent` downloads try the stored YTS URL first; if that fails (dead domain, non-200 response) they automatically fall back to the `yts.gg` URL derived from the magnet hash
- RSS feed at `/yts_missing.rss` outputs a valid Atom feed with `<enclosure>` and `<tor:magnetURI>` fields
- RSS feed accepts optional filters: `?min_year=2023&min_size_gb=1&max_size_gb=5`
- Optional HTTP Basic Auth via `YTS_WEB_USERNAME` and `YTS_WEB_PASSWORD`. The server **refuses to bind to a non-loopback interface without auth** (set `YTS_ALLOW_INSECURE=true` only if a reverse proxy handles auth)
- Security headers (CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`) on every response; a `/health` endpoint returns `{"status":"ok"}`
- Set `LOG_JSON=true` to emit structured JSON access logs to stdout

### yts_check.py CLI flags

| Flag | Description |
|---|---|
| `--since YEAR` | Only scan YTS movies from this year onwards (overrides `MIN_YEAR`) |
| `--quality PROFILE` | Quality profile to scan, e.g. `1080p` / `2160p` (overrides `QUALITY`) |
| `--mal` / `--no-mal` | Enable/disable the MyAnimeList anime filter for this run |
| `--mal-workers N` | Parallel workers for MAL lookups (default 3) |
| `--cached-plex` | Reuse the last Plex scan from `plex_cache.json` instead of reconnecting to Plex |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires: `flask`, `plexapi`, `requests`, `tqdm`, `python-dotenv`, `markupsafe`

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```env
PLEX_USER_TOKEN=your_plex_token
PLEX_SERVER_NAME=YourServerName
LIBRARY_NAME=Movies
MIN_YEAR=2020
QUALITY=1080p
SLEEP_SECONDS=1.2
USE_MAL_FILTER=false
YTS_WEB_USERNAME=admin
YTS_WEB_PASSWORD=changeme
YTS_BIND_HOST=127.0.0.1
YTS_PORT=5000
```

Leave `YTS_WEB_USERNAME` and `YTS_WEB_PASSWORD` empty to disable HTTP Basic Auth.

### 3. Run the scanner

```bash
python yts_check.py
```

Run this on a schedule (e.g. cron) to keep `missing_1080p.json` up to date. After each scan the post-scan menu appears - press `1` to download `.torrent` files, `2` to scan again, or `3` to exit.

### 4. Start the web server

```bash
python yts_rss.py
```

Then open `http://127.0.0.1:5000` in your browser.

---

## qBittorrent Integration

1. Copy the RSS URL from the dashboard (e.g. `http://127.0.0.1:5000/yts_missing.rss`)
2. Add it to qBittorrent under RSS -> Feeds
3. Create an auto-download rule matching the feed
4. Use the Remove button on the dashboard once a movie has been added to Plex

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PLEX_USER_TOKEN` | required | Plex authentication token |
| `PLEX_SERVER_NAME` | `Epos` | Name of your Plex server resource |
| `LIBRARY_NAME` | `Movies` | Plex library section to scan |
| `MIN_YEAR` | `2025` | Only scan YTS movies from this year onwards (0 = all) |
| `QUALITY` | `1080p` | YTS quality filter |
| `SLEEP_SECONDS` | `1.2` | Delay between YTS page requests |
| `USE_MAL_FILTER` | `false` | Filter out MAL-listed anime movies |
| `YTS_WEB_USERNAME` | _(empty)_ | HTTP Basic Auth username (leave blank to disable) |
| `YTS_WEB_PASSWORD` | _(empty)_ | HTTP Basic Auth password |
| `YTS_BIND_HOST` | `127.0.0.1` | Host to bind the Flask server to |
| `YTS_PORT` | `5000` | Port to bind the Flask server to |
| `YTS_ALLOW_INSECURE` | `false` | Allow non-loopback bind without auth (reverse proxy setups only) |
| `LOG_JSON` | `false` | Emit structured JSON access logs to stdout |

---

## Get the Code

Clone with git:

```bash
git clone https://github.com/drew-codes-things/YtsPlexRss.git
```

Or with the [GitHub CLI](https://cli.github.com/):

```bash
gh repo clone drew-codes-things/YtsPlexRss
```

## License

MIT - made by [Drew](https://github.com/drew-codes-things)
