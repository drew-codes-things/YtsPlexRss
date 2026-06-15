import time
import json
import argparse
import sys
import threading
import requests
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import quote_plus
from datetime import datetime, timezone
from plexapi.myplex import MyPlexAccount
from tqdm import tqdm
from dotenv import load_dotenv

try:
    import tty
    import termios
    HAS_TTY = True
except ImportError:
    HAS_TTY = False

try:
    import fcntl
except ImportError:
    fcntl = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

PLEX_USER_TOKEN = os.getenv("PLEX_USER_TOKEN")
PLEX_SERVER_NAME = os.getenv("PLEX_SERVER_NAME", "Epos")
LIBRARY_NAME = os.getenv("LIBRARY_NAME", "Movies")
MIN_YEAR = int(os.getenv("MIN_YEAR", 2025))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", 1.2))
USE_MAL_FILTER = os.getenv("USE_MAL_FILTER", "false").lower() == "true"
QUALITY = os.getenv("QUALITY", "1080p")

MISSING_JSON = os.path.join(BASE_DIR, "missing_1080p.json")
MISSING_LOCK = f"{MISSING_JSON}.lock"
SCAN_STATE_FILE = os.path.join(BASE_DIR, "scan_state.json")
MAL_CACHE_FILE = os.path.join(BASE_DIR, "mal_cache.json")
PLEX_CACHE_FILE = os.path.join(BASE_DIR, "plex_cache.json")
TORRENT_DIR = os.path.join(BASE_DIR, "torrent_files")

MAL_LOCK = threading.Lock()

BASE_URL = "https://movies-api.accel.li/api/v2/list_movies.json"
MAL_SEARCH_URL = "https://api.jikan.moe/v4/anime"

BRANDING = "Star's YTS -> PLEX -> RSS Tool"

MAL_CACHE = {}


def _normalise_title(title):
    return re.sub(r'[^a-z0-9]', '', title.lower())


def load_mal_cache():
    global MAL_CACHE
    try:
        with open(MAL_CACHE_FILE, "r", encoding="utf-8") as f:
            MAL_CACHE = json.load(f)
    except Exception:
        MAL_CACHE = {}


def save_mal_cache():
    try:
        with open(MAL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(MAL_CACHE, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def is_anime_on_mal(title, year):
    if not USE_MAL_FILTER:
        return False

    key = f"{title.strip().lower()}_{year}"
    with MAL_LOCK:
        if key in MAL_CACHE:
            return MAL_CACHE[key]

    result = False
    try:
        params = {"q": title, "limit": 5}
        r = requests.get(MAL_SEARCH_URL, params=params, timeout=8)
        if r.status_code == 200:
            for mal_entry in r.json().get("data", []):
                if mal_entry.get("type") != "Movie":
                    continue
                mal_year = mal_entry.get("year") or (mal_entry.get("aired", {}).get("from") or "")[:4]
                try:
                    if mal_year and abs(int(mal_year) - year) <= 2:
                        result = True
                        break
                except (ValueError, TypeError):
                    pass
        else:
            time.sleep(0.35)
            return False
    except Exception:
        time.sleep(0.35)
        return False

    with MAL_LOCK:
        MAL_CACHE[key] = result
        save_mal_cache()
    time.sleep(0.35)
    return result


def prewarm_mal(candidates, workers):
    with MAL_LOCK:
        todo = [(t, y) for (t, y) in candidates if f"{t.strip().lower()}_{y}" not in MAL_CACHE]
    if not todo:
        return
    print(f"Pre-checking {len(todo):,} titles against MyAnimeList ({workers} workers)...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(tqdm(ex.map(lambda ty: is_anime_on_mal(ty[0], ty[1]), todo),
                  total=len(todo), desc="MAL Anime Filter", unit="title"))


def save_plex_cache(local_movies):
    try:
        with open(PLEX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "saved": datetime.now(timezone.utc).isoformat(),
                "movies": [[t, y] for (t, y) in sorted(local_movies)],
            }, f)
    except Exception:
        pass


def load_plex_cache():
    try:
        with open(PLEX_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {(t, y) for t, y in data.get("movies", [])}, data.get("saved")
    except Exception:
        return None, None


def get_local_1080p_movies_from_plex():
    print(f"\n{BRANDING}")
    print("=" * 70)
    print("Connecting to Plex...")
    account = MyPlexAccount(token=PLEX_USER_TOKEN)
    server = account.resource(PLEX_SERVER_NAME).connect()
    movies_section = server.library.section(LIBRARY_NAME)
    print(f"Scanning Plex library '{LIBRARY_NAME}'...")
    local = set()
    for video in movies_section.all():
        if video.type != "movie":
            continue
        year = video.year if video.year else 0
        if any(media.videoResolution == "1080" for media in video.media):
            local.add((video.title.strip().lower(), year))
    print(f"Found {len(local)} {QUALITY} movies in Plex.")
    return local


def title_matches_plex(yts_title, year, local_movies):
    if (yts_title.lower(), year) in local_movies:
        return True
    norm = _normalise_title(yts_title)
    return any(
        plex_year == year and _normalise_title(plex_title) == norm
        for plex_title, plex_year in local_movies
    )


def build_magnet(hash_str, title):
    trackers = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://tracker.dler.org:6969/announce",
        "udp://open.stealth.si:80/announce",
        "udp://open.demonii.com:1337/announce",
        "https://tracker.moeblog.cn:443/announce",
        "udp://open.dstud.io:6969/announce",
        "udp://tracker.srv00.com:6969/announce",
        "https://tracker.zhuqiy.com:443/announce",
        "https://tracker.pmman.tech:443/announce",
        "udp://tracker.openbittorrent.com:80/announce",
        "udp://tracker.zer0day.to:1337/announce",
        "udp://exodus.desync.com:6969/announce",
    ]
    tr = "&tr=".join(quote_plus(t) for t in trackers)
    return f"magnet:?xt=urn:btih:{hash_str}&dn={quote_plus(title)}&tr={tr}"


def load_scan_state():
    try:
        with open(SCAN_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_page": 1}


def save_scan_state(state):
    with open(SCAN_STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_movies():
    movies = []

    if MIN_YEAR > 0:
        sort_by, order = "year", "desc"
        print(f"\nFAST MODE: Only {MIN_YEAR}+ movies (newest first)")
        start_page = 1
    else:
        sort_by, order = "download_count", "desc"
        print("\nFULL MODE: Scanning ALL movies on YTS")
        state = load_scan_state()
        start_page = state.get("last_page", 1)
        if start_page > 1:
            print(f"Resuming from page {start_page}...")

    params = {"quality": QUALITY, "limit": 50, "page": 1, "sort_by": sort_by, "order": order}
    try:
        r = requests.get(BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Failed to fetch first YTS page: {e}")
        return movies

    movie_count = data.get("data", {}).get("movie_count", 0)
    total_pages = (movie_count + 49) // 50
    print(f"YTS total: {movie_count:,} movies across {total_pages} pages")

    if start_page == 1:
        movies.extend(data.get("data", {}).get("movies", []))

    for page in tqdm(range(max(2, start_page), total_pages + 1), desc="YTS Scan", unit="page"):
        params["page"] = page
        try:
            r = requests.get(BASE_URL, params=params, timeout=20)
            r.raise_for_status()
            page_movies = r.json().get("data", {}).get("movies", [])
            if not page_movies:
                break
            movies.extend(page_movies)

            if MIN_YEAR <= 0:
                save_scan_state({"last_page": page})

            if MIN_YEAR > 0:
                page_years = [m.get("year", 0) for m in page_movies if m.get("year")]
                if page_years and min(page_years) < MIN_YEAR:
                    tqdm.write(f"[OK] Reached movies before {MIN_YEAR} -- stopping early.")
                    break

            time.sleep(SLEEP_SECONDS)
        except Exception as e:
            tqdm.write(f"Error on page {page}: {e} -- progress saved.")
            break
    else:
        save_scan_state({"last_page": 1})

    print(f"Finished fetching {len(movies):,} movies.")
    return movies


@contextmanager
def _missing_file_lock(exclusive=False):
    with open(MISSING_LOCK, "a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_handle.fileno(), lock_mode)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def load_missing():
    if not os.path.exists(MISSING_JSON):
        return []
    try:
        with _missing_file_lock(exclusive=False):
            with open(MISSING_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
    except json.JSONDecodeError as e:
        print(f"WARNING: {MISSING_JSON} is corrupted ({e}). Starting with empty list.")
        return []
    except Exception as e:
        print(f"WARNING: Could not read {MISSING_JSON}: {e}. Starting with empty list.")
        return []


def save_missing(items):
    temp_path = f"{MISSING_JSON}.tmp"
    with _missing_file_lock(exclusive=True):
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, MISSING_JSON)


def prune_already_on_plex(local_movies, items):
    before = len(items)
    pruned = [
        item for item in items
        if not title_matches_plex(
            re.sub(r"\s*\(\d{4}\)\s*$", "", item["title"]),
            item.get("year", 0),
            local_movies,
        )
    ]
    removed = before - len(pruned)
    if removed:
        print(f"Auto-removed {removed} titles now present in Plex.")
    return pruned


def parse_args():
    p = argparse.ArgumentParser(description="YTS -> Plex missing-movie scanner")
    p.add_argument("--since", type=int, metavar="YEAR",
                   help="Only scan YTS movies from this year onwards (overrides MIN_YEAR)")
    p.add_argument("--quality", metavar="PROFILE",
                   help="YTS quality profile to scan, e.g. 1080p / 2160p (overrides QUALITY)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mal", dest="mal", action="store_true", default=None,
                   help="Enable the MyAnimeList anime filter for this run")
    g.add_argument("--no-mal", dest="mal", action="store_false",
                   help="Disable the MyAnimeList anime filter for this run")
    p.add_argument("--mal-workers", type=int, default=3,
                   help="Parallel workers for MAL lookups (default: 3)")
    p.add_argument("--cached-plex", action="store_true",
                   help="Reuse the last Plex scan from plex_cache.json instead of reconnecting")
    return p.parse_args()


def getch():
    if not HAS_TTY or not os.isatty(sys.stdin.fileno()):
        return input().strip()[:1]
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def prompt_menu():
    print("\n  1  Download .torrent files to torrent_files/")
    print("  2  Run scan again")
    print("  3  Exit")
    print("\nSelect [1-3]: ", end="", flush=True)
    while True:
        key = getch()
        if key in ("1", "2", "3"):
            print(key)
            return key


def _safe_filename(title):
    return re.sub(r'[^\w\s\-]', '', title).strip()


def download_torrents():
    items = load_missing()
    if not items:
        print("No missing movies found.")
        return

    os.makedirs(TORRENT_DIR, exist_ok=True)
    skipped = 0
    failed = 0

    for item in tqdm(items, unit="file", desc="Downloading", dynamic_ncols=True):
        filename = _safe_filename(item.get("title", "unknown")) + ".torrent"
        dest = os.path.join(TORRENT_DIR, filename)

        if os.path.exists(dest):
            skipped += 1
            continue

        torrent_url = item.get("torrent_url") or ""
        hm = re.search(r'urn:btih:([a-fA-F0-9]{40})', item.get("magnet", ""), re.IGNORECASE)
        gg_url = f"https://yts.gg/torrent/download/{hm.group(1).upper()}" if hm else None
        urls = list(dict.fromkeys(u for u in [torrent_url, gg_url] if u))
        if not urls:
            skipped += 1
            continue

        network_error = False
        downloaded = False
        for url in urls:
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    f.write(r.content)
                time.sleep(0.5)
                downloaded = True
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                network_error = True
                continue
            except Exception:
                continue
        if not downloaded:
            if network_error:
                tqdm.write("YTS is blocked by your ISP. Change your DNS or use a VPN.")
                break
            tqdm.write(f"  failed: {item.get('title')}")
            failed += 1

    print(f"\nSaved to: {TORRENT_DIR}")
    if skipped:
        print(f"Skipped:  {skipped} (already downloaded or no valid URL)")
    if failed:
        print(f"Failed:   {failed}")


def main():
    global MAL_CACHE, MIN_YEAR, QUALITY, USE_MAL_FILTER
    args = parse_args()
    if args.since is not None:
        MIN_YEAR = args.since
    if args.quality:
        QUALITY = args.quality
    if args.mal is not None:
        USE_MAL_FILTER = args.mal

    load_mal_cache()

    if args.cached_plex:
        local_movies, saved = load_plex_cache()
        if local_movies is None:
            print("No Plex cache found; scanning Plex live.")
            local_movies = get_local_1080p_movies_from_plex()
            save_plex_cache(local_movies)
        else:
            print(f"Using cached Plex scan ({len(local_movies):,} movies, saved {saved}).")
    else:
        local_movies = get_local_1080p_movies_from_plex()
        save_plex_cache(local_movies)

    existing_missing = load_missing()
    existing_missing = prune_already_on_plex(local_movies, existing_missing)
    save_missing(existing_missing)

    existing_keys = {(item["title"].lower().strip(), item.get("year", 0)) for item in existing_missing}

    print("\nStarting YTS scan...")
    yts_movies = fetch_movies()

    new_items = []
    skipped_plex = 0
    filtered_anime = 0
    filtered_other = 0

    if USE_MAL_FILTER:
        candidates = []
        for movie in yts_movies:
            t, y = movie.get("title"), movie.get("year")
            if not t or not y or (MIN_YEAR > 0 and y < MIN_YEAR):
                continue
            if title_matches_plex(t, y, local_movies):
                continue
            if (f"{t} ({y})".lower().strip(), y) in existing_keys:
                continue
            candidates.append((t, y))
        prewarm_mal(candidates, args.mal_workers)

    for movie in yts_movies:
        try:
            title = movie.get("title")
            year = movie.get("year")
            if not title or not year or (MIN_YEAR > 0 and year < MIN_YEAR):
                filtered_other += 1
                continue

            if title_matches_plex(title, year, local_movies):
                skipped_plex += 1
                continue

            display_title = f"{title} ({year})"
            key = (display_title.lower().strip(), year)
            if key in existing_keys:
                skipped_plex += 1
                continue

            if USE_MAL_FILTER and is_anime_on_mal(title, year):
                filtered_anime += 1
                continue

            torrents = [t for t in movie.get("torrents", []) if t.get("quality") == QUALITY]
            if not torrents:
                filtered_other += 1
                continue

            best = max(torrents, key=lambda t: int(t.get("size_bytes", 0)))

            new_items.append({
                "id": str(uuid.uuid4()),
                "title": display_title,
                "size": best.get("size", "Unknown"),
                "size_bytes": int(best.get("size_bytes", 0)),
                "magnet": build_magnet(best["hash"], display_title),
                "torrent_url": best.get("url", ""),
                "added": datetime.now(timezone.utc).isoformat(),
                "year": year,
            })
            existing_keys.add(key)
        except Exception:
            filtered_other += 1
            continue

    if new_items:
        existing_missing.extend(new_items)
        save_missing(existing_missing)
        print(f"Added {len(new_items)} new missing movies.")

    save_mal_cache()

    print("\n" + "=" * 70)
    print(f"{BRANDING}")
    print(f"Scanned from YTS       : {len(yts_movies):,}")
    print(f"Already in Plex/list   : {skipped_plex:,}")
    print(f"Filtered (Anime)       : {filtered_anime:,}")
    print(f"Filtered (other)       : {filtered_other:,}")
    print(f"New missing            : {len(new_items):,}")
    print(f"Total missing now      : {len(existing_missing):,}")
    print("=" * 70)


if __name__ == "__main__":
    while True:
        main()
        choice = prompt_menu()
        if choice == "1":
            download_torrents()
        elif choice == "2":
            continue
        else:
            break
