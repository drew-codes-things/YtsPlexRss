from flask import Flask, Response, render_template_string, request, redirect, session, send_from_directory
from markupsafe import Markup
from collections import Counter
from functools import wraps
from contextlib import contextmanager
import json
import logging
import os
import re
import requests
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr
from dotenv import load_dotenv
try:
    import fcntl
except ImportError:
    fcntl = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

LOG_JSON = os.getenv("LOG_JSON", "false").lower() == "true"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        return json.dumps(payload)


access_logger = logging.getLogger("yts.access")
if LOG_JSON and not access_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(JsonFormatter())
    access_logger.addHandler(_h)
    access_logger.setLevel(logging.INFO)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
MISSING_JSON = os.path.join(BASE_DIR, "missing_1080p.json")
MISSING_LOCK = f"{MISSING_JSON}.lock"
TORRENT_DIR = os.path.join(BASE_DIR, "torrent_files")

_dl_lock = threading.Lock()
_dl_in_progress = False
YTS_WEB_USERNAME = (os.getenv("YTS_WEB_USERNAME") or "").strip()
YTS_WEB_PASSWORD = (os.getenv("YTS_WEB_PASSWORD") or "").strip()


def auth_enabled():
    return bool(YTS_WEB_USERNAME and YTS_WEB_PASSWORD)


@app.after_request
def log_request(resp):
    if LOG_JSON and request.path != "/health":
        access_logger.info(f"{request.method} {request.path} {resp.status_code}")
    return resp


@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
    )
    return resp


@app.route("/health")
def health():
    return Response('{"status": "ok"}', mimetype="application/json")


def requires_auth(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not auth_enabled():
            return view_func(*args, **kwargs)
        auth = request.authorization
        if auth and secrets.compare_digest(auth.username or "", YTS_WEB_USERNAME) and secrets.compare_digest(auth.password or "", YTS_WEB_PASSWORD):
            return view_func(*args, **kwargs)
        return Response(
            "Authentication required",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="YTS-RSS"'},
        )
    return wrapped


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


def load_missing_with_warning():
    if not os.path.exists(MISSING_JSON):
        return [], None
    try:
        with _missing_file_lock(exclusive=False):
            with open(MISSING_JSON, "r", encoding="utf-8") as f:
                return json.load(f), None
    except json.JSONDecodeError as e:
        warning = f"{MISSING_JSON} is corrupted ({e})."
        print(f"WARNING: {warning}")
        return [], warning
    except Exception as e:
        warning = f"Could not read {MISSING_JSON}: {e}."
        print(f"WARNING: {warning}")
        return [], warning


def load_missing():
    items, _ = load_missing_with_warning()
    return items


def save_missing(items):
    temp_path = f"{MISSING_JSON}.tmp"
    with _missing_file_lock(exclusive=True):
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, MISSING_JSON)


def parse_size_bytes(size_str):
    try:
        parts = size_str.strip().split()
        if len(parts) < 2:
            return None
        val = float(parts[0])
        unit = parts[1].upper()
        multipliers = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        factor = multipliers.get(unit)
        if factor is None:
            return None
        return int(val * factor)
    except Exception:
        return None


def fmt_bytes(b):
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} EB"


def is_valid_magnet(magnet):
    if not magnet:
        return False
    return magnet.lower().startswith("magnet:?xt=")


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def has_valid_csrf(submitted_token):
    expected = session.get("_csrf_token")
    if not expected or not submitted_token:
        return False
    return secrets.compare_digest(expected, submitted_token)


def extract_infohash(magnet):
    try:
        match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet)
        return match.group(1).upper() if match else ""
    except Exception:
        return ""


CSS = """
body {
    font-family: 'Montserrat', sans-serif;
    background-color: #121212;
    color: #f0f0f0;
    margin: 0;
    padding: 0;
    line-height: 1.6;
}
.container { max-width: 1600px; margin: 0 auto; padding: 40px 30px; }
.warning-banner {
    background: #4a1f1f;
    border: 1px solid #a94b4b;
    color: #ffdede;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 0 0 20px 0;
    font-size: 0.9rem;
}
h1 {
    font-family: 'Playfair Display', serif;
    color: #ef4444;
    font-size: clamp(2.5rem, 7vw, 4rem);
    margin: 0;
    text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000;
}
h2 {
    font-family: 'Playfair Display', serif;
    color: #ef4444;
    font-size: 2rem;
    margin-top: 0;
    margin-bottom: 20px;
    border-bottom: 2px solid #ef4444;
    padding-bottom: 10px;
}
.section {
    background-color: #1e1e1e;
    border-radius: 12px;
    padding: 30px 40px;
    margin-bottom: 40px;
    box-shadow: 0 6px 12px rgba(0,0,0,0.4);
}
.layout {
    display: grid;
    grid-template-columns: 1fr 2.5fr 1fr;
    gap: 40px;
    align-items: start;
}
.feed { grid-column: 1 / 2; }
.main-content { grid-column: 2 / 3; }
.sidebar { grid-column: 3 / 4; }
a { color: #dc2626; text-decoration: none; transition: color 0.3s; }
a:hover { color: #ef4444; }
.btn {
    display: inline-block;
    background: #dc2626;
    color: #fff;
    border: none;
    padding: 9px 18px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
    transition: background 0.2s;
}
.btn:hover { background: #b91c1c; }
.btn-secondary {
    background: #333;
    margin-left: 8px;
}
.btn-secondary:hover { background: #444; }
input[type=text], input[type=search] {
    background: #2a2a2a;
    border: 1px solid #444;
    color: #f0f0f0;
    padding: 8px 14px;
    border-radius: 6px;
    font-size: 0.95rem;
    width: 100%;
    box-sizing: border-box;
    margin-bottom: 16px;
}
table { width: 100%; border-collapse: collapse; }
thead tr { border-bottom: 2px solid #ef4444; }
th {
    text-align: left;
    padding: 12px 10px;
    color: #ef4444;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
}
th:hover { color: #fff; }
th .sort-arrow { font-size: 0.75rem; margin-left: 4px; opacity: 0.5; }
th.sorted .sort-arrow { opacity: 1; }
tbody tr { border-bottom: 1px solid #2a2a2a; transition: background 0.15s; }
tbody tr:hover { background: #252525; }
td { padding: 13px 10px; vertical-align: middle; }
.badge {
    background: #2a2a2a;
    border-radius: 20px;
    padding: 3px 10px;
    font-size: 0.82rem;
    color: #ccc;
}
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.stat-box {
    background: #2a2a2a;
    border-radius: 8px;
    padding: 16px;
    text-align: center;
}
.stat-box .val { font-size: 1.6rem; font-weight: 700; color: #ef4444; }
.stat-box .lbl { font-size: 0.8rem; color: #888; margin-top: 4px; }
.pagination { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 20px; align-items: center; }
.page-btn {
    background: #2a2a2a;
    color: #f0f0f0;
    border: none;
    padding: 7px 13px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 0.9rem;
}
.page-btn.active { background: #dc2626; }
.page-btn:hover:not(.active) { background: #333; }
#bar-chart { width: 100%; margin-top: 10px; }
.bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.85rem; }
.bar-row .year-lbl { width: 42px; color: #aaa; text-align: right; flex-shrink: 0; }
.bar-fill { height: 18px; background: #dc2626; border-radius: 3px; transition: width 0.3s; min-width: 2px; }
.bar-count { color: #888; font-size: 0.8rem; }
.status-list { display: flex; flex-direction: column; gap: 10px; padding: 0; }
.status-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #2a2a2a;
    border-radius: 8px;
    padding: 12px 16px;
    list-style: none;
}
.status-badge { background: #dc2626; color: #fff; border-radius: 12px; padding: 2px 10px; font-size: 0.8rem; }
#bulk-bar {
    display: none;
    background: #1a1a1a;
    border: 1px solid #dc2626;
    border-radius: 8px;
    padding: 12px 18px;
    margin-bottom: 16px;
    align-items: center;
    gap: 14px;
}
@media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .feed, .main-content, .sidebar { grid-column: 1; }
    .stat-grid { grid-template-columns: 1fr 1fr; }
}

body {
    background: linear-gradient(135deg, #111827 0%, #000000 50%, #111827 100%);
    background-attachment: fixed;
    min-height: 100vh;
}
.bg-blobs { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: -1; }
.bg-blobs span { position: absolute; border-radius: 9999px; filter: blur(40px); mix-blend-mode: screen; }
.bg-blobs .b1 { top: -1rem; left: -1rem; width: 18rem; height: 18rem; background: #dc2626; opacity: .25; animation: blobPulse 4s ease-in-out infinite; }
.bg-blobs .b2 { right: -1rem; bottom: -2rem; width: 18rem; height: 18rem; background: #ef4444; opacity: .25; animation: blobPulse 4s ease-in-out infinite 2s; }
.bg-blobs .b3 { top: 50%; left: 50%; width: 24rem; height: 24rem; background: #b91c1c; opacity: .18; transform: translate(-50%,-50%); animation: blobPulseC 4s ease-in-out infinite 4s; }
@keyframes blobPulse  { 0%,100% { transform: scale(1); } 50% { transform: scale(1.05); } }
@keyframes blobPulseC { 0%,100% { transform: translate(-50%,-50%) scale(1); } 50% { transform: translate(-50%,-50%) scale(1.05); } }
.topbar {
    position: relative; z-index: 10;
    display: flex; align-items: center; gap: 1rem;
    min-height: 0; text-align: left;
    padding: 1rem 1.5rem; margin: 0 0 40px;
    background: rgba(0,0,0,.4);
    -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(255,255,255,.1);
    box-shadow: 0 20px 40px -20px rgba(0,0,0,.6);
}
.topbar::before { content: none; }
.topbar img.topbar-logo { width: 40px; height: 40px; flex-shrink: 0; }
.topbar h1 {
    margin: 0; font-size: clamp(1.6rem, 4vw, 2.4rem); font-weight: 700;
    background: linear-gradient(to right, #f87171, #ef4444, #dc2626);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
    text-shadow: none;
}
.topbar .topbar-titles { display: flex; flex-direction: column; }
.topbar .topbar-titles .header-sub { color: #fca5a5; opacity: .8; font-size: 1rem; margin: 0; }
"""

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Missing 1080p - tisL</title>
    <link rel="icon" href="/logo.svg" type="image/png">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
    <style>{{ css }}</style>
</head>
<body>
<div class="bg-blobs" aria-hidden="true"><span class="b1"></span><span class="b2"></span><span class="b3"></span></div>
<header class="topbar">
    <img src="/logo.svg" alt="Logo" class="topbar-logo">
    <div class="topbar-titles">
        <h1>Missing 1080p Movies</h1>
        <p class="header-sub">{{ total }} titles waiting</p>
    </div>
</header>
<div class="container">
{% if load_warning %}
<div class="warning-banner">{{ load_warning }}</div>
{% endif %}
{% if torrent_dl == 'started' %}
<div class="warning-banner" style="background:#1a3a1f;border-color:#4a9b5a;color:#d4f5da">.torrent download started in background — files will appear in <code>torrent_files/</code>.</div>
{% elif torrent_dl == 'busy' %}
<div class="warning-banner">A .torrent download is already running.</div>
{% endif %}
{% if invalid_size_count %}
<div class="warning-banner">{{ invalid_size_count }} item(s) have invalid size values; total size excludes them and RSS length falls back to 0 for those entries.</div>
{% endif %}
<div class="layout">

  <div>
    <div class="feed section">
      <h2>Feed Options</h2>

      <p style="color:#aaa;font-size:0.88rem;margin:0 0 6px">RSS Feed</p>
      <code style="display:block; background:#2a2a2a; padding:10px 14px; border-radius:6px; word-break:break-all; font-size:0.85rem; margin-bottom:10px;">{{ base_url }}/yts_missing.rss</code>
      <a href="/yts_missing.rss" class="btn" style="display:block; text-align:center; margin-bottom:20px;">Open RSS Feed</a>

      <p style="color:#aaa;font-size:0.88rem;margin:0 0 6px">.torrent Files</p>
      <p style="font-size:0.83rem;color:#666;margin:0 0 10px">Downloads to <code style="background:#2a2a2a;padding:2px 6px;border-radius:4px;">torrent_files/</code> on the server.</p>
      <form method="post" action="/download_torrents" id="torrent-form" style="margin-bottom:20px">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit" class="btn" style="width:100%">Download .torrent Files</button>
      </form>

      <p style="color:#aaa;font-size:0.88rem;margin:0 0 6px">Both</p>
      <button type="button" class="btn" style="width:100%;background:#555" onclick="doBoth()">RSS + Download .torrents</button>
    </div>
    <div class="section">
      <h2>How to Use</h2>
      <ul class="status-list">
        <li class="status-item"><span>1. Copy RSS URL above</span><span class="status-badge">Step 1</span></li>
        <li class="status-item"><span>2. Add to qBittorrent RSS</span></li>
        <li class="status-item"><span>3. Set auto-download rule</span></li>
        <li class="status-item"><span>4. Click Remove when done</span></li>
      </ul>
    </div>
  </div>

  <div class="main-content section">
    <h2>Missing Movies</h2>
    <input type="search" id="search-box" placeholder="Search by title or year..." oninput="filterTable()">
    <div id="bulk-bar">
      <span id="bulk-count">0 selected</span>
      <form method="post" action="/delete_bulk" id="bulk-form" style="display:inline">
        <input type="hidden" name="ids" id="bulk-ids-input">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit" class="btn">Remove Selected</button>
      </form>
      <button class="btn btn-secondary" onclick="clearSelection()">Clear</button>
    </div>
    <table id="movies-table">
      <thead>
        <tr>
          <th style="width:36px"><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
          <th onclick="sortTable(1)">Title <span class="sort-arrow">v</span></th>
          <th onclick="sortTable(2)" style="text-align:center">Size <span class="sort-arrow">v</span></th>
          <th onclick="sortTable(3)" style="text-align:center">Year <span class="sort-arrow">v</span></th>
          <th onclick="sortTable(4)" style="text-align:center">Added <span class="sort-arrow">v</span></th>
          <th style="text-align:center">Magnet</th>
          <th style="width:90px"></th>
        </tr>
      </thead>
      <tbody>
      {% for item in items %}
        <tr data-id="{{ item.id }}" data-year="{{ item.year }}">
          <td><input type="checkbox" class="row-cb" value="{{ item.id }}" onchange="updateBulkBar()"></td>
          <td>{{ item.title }}</td>
          <td style="text-align:center"><span class="badge">{{ item.size }}</span></td>
          <td style="text-align:center; color:#888">{{ item.year }}</td>
          <td style="text-align:center; color:#888; font-size:0.9rem">{{ item.added[:10] }}</td>
          <td style="text-align:center">
            {% if item.valid_magnet %}
              <a href="{{ item.magnet }}">Magnet</a>
            {% else %}
              <span style="color:#888">Invalid</span>
            {% endif %}
          </td>
          <td style="text-align:center">
            <form method="post" action="/delete/{{ item.id }}" style="margin:0">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <button type="submit" class="btn">Remove</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="pagination" id="pagination"></div>
  </div>

  <div>
    <div class="section">
      <h2>Stats</h2>
      <div class="stat-grid">
        <div class="stat-box"><div class="val">{{ total }}</div><div class="lbl">Missing Titles</div></div>
        <div class="stat-box"><div class="val">{{ total_size }}</div><div class="lbl">Total Size</div></div>
        <div class="stat-box"><div class="val">{{ year_range }}</div><div class="lbl">Year Range</div></div>
        <div class="stat-box"><div class="val">{{ newest_year }}</div><div class="lbl">Newest Year</div></div>
      </div>
    </div>
    <div class="section">
      <h2>By Year</h2>
      <div id="bar-chart">
        {% for year, count, pct in year_chart %}
        <div class="bar-row">
          <span class="year-lbl">{{ year }}</span>
          <div class="bar-fill" style="width:{{ pct }}%"></div>
          <span class="bar-count">{{ count }}</span>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

</div>
</div>

<script>
const PAGE_SIZE = 50;
let currentPage = 1;
let sortCol = -1;
let sortAsc = true;
let visibleRows = [];

function parseSize(sizeStr) {
  if (!sizeStr) return 0;
  const parts = sizeStr.trim().split(/\s+/);
  if (parts.length < 2) return 0;
  let val = parseFloat(parts[0]);
  const unit = parts[1].toUpperCase();
  const multipliers = { "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4 };
  return val * (multipliers[unit] || 1);
}

function getAllRows() {
  return Array.from(document.querySelectorAll('#movies-table tbody tr'));
}

function filterTable() {
  const q = document.getElementById('search-box').value.toLowerCase();
  visibleRows = getAllRows().filter(row => {
    const title = row.cells[1].textContent.toLowerCase();
    const year = row.dataset.year;
    const match = title.includes(q) || year.includes(q);
    row.style.display = match ? '' : 'none';
    return match;
  });
  currentPage = 1;
  document.getElementById('select-all').checked = false;
  paginate();
}

function paginate() {
  const start = (currentPage - 1) * PAGE_SIZE;
  visibleRows.forEach((row, i) => {
    row.style.display = (i >= start && i < start + PAGE_SIZE) ? '' : 'none';
  });
  renderPagination();
}

function renderPagination() {
  const total = visibleRows.length;
  const pages = Math.ceil(total / PAGE_SIZE);
  const el = document.getElementById('pagination');
  if (pages <= 1) { el.innerHTML = ''; return; }
  let html = `<span style="color:#888;font-size:0.9rem">${total} results</span>`;
  for (let p = 1; p <= pages; p++) {
    html += `<button class="page-btn${p === currentPage ? ' active' : ''}" onclick="goPage(${p})">${p}</button>`;
  }
  el.innerHTML = html;
}

function goPage(p) {
  currentPage = p;
  paginate();
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function sortTable(col) {
  if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
  const tbody = document.querySelector('#movies-table tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    let av = a.cells[col].textContent.trim();
    let bv = b.cells[col].textContent.trim();
    if (col === 2) {
      av = parseSize(av);
      bv = parseSize(bv);
    } else if (col === 3) {
      av = parseInt(av) || 0;
      bv = parseInt(bv) || 0;
    } else if (col === 4) {
      av = new Date(av);
      bv = new Date(bv);
    }
    if (av < bv) return sortAsc ? -1 : 1;
    if (av > bv) return sortAsc ? 1 : -1;
    return 0;
  });
  rows.forEach(r => tbody.appendChild(r));
  document.querySelectorAll('th').forEach(th => th.classList.remove('sorted'));
  document.querySelectorAll('th')[col].classList.add('sorted');
  filterTable();
}

function toggleAll(cb) {
  visibleRows.forEach(row => {
    const checkbox = row.querySelector('.row-cb');
    if (checkbox) checkbox.checked = cb.checked;
  });
  updateBulkBar();
}

function updateBulkBar() {
  const checked = document.querySelectorAll('.row-cb:checked');
  const bar = document.getElementById('bulk-bar');
  document.getElementById('bulk-count').textContent = checked.length + ' selected';
  bar.style.display = checked.length > 0 ? 'flex' : 'none';
}

function clearSelection() {
  document.querySelectorAll('.row-cb').forEach(c => c.checked = false);
  document.getElementById('select-all').checked = false;
  updateBulkBar();
}

document.getElementById('bulk-form').addEventListener('submit', function() {
  const ids = Array.from(document.querySelectorAll('.row-cb:checked')).map(c => c.value);
  document.getElementById('bulk-ids-input').value = JSON.stringify(ids);
});

visibleRows = getAllRows();
paginate();

function doBoth() {
  window.open('/yts_missing.rss', '_blank');
  document.getElementById('torrent-form').submit();
}
</script>
</body>
</html>
"""


@app.route("/logo.svg")
def logo():
    return send_from_directory(BASE_DIR, "logo.svg")


@app.route("/")
@requires_auth
def index():
    raw_items, load_warning = load_missing_with_warning()
    items = []
    invalid_size_count = 0
    for item in raw_items:
        magnet = str(item.get("magnet", ""))
        size_bytes = item.get("size_bytes")
        if not isinstance(size_bytes, int):
            size_bytes = parse_size_bytes(item.get("size", ""))
        if size_bytes is None:
            size_bytes = 0
            invalid_size_count += 1
        items.append({
            **item,
            "magnet": magnet,
            "size_bytes": size_bytes,
            "valid_magnet": is_valid_magnet(magnet),
        })
    torrent_dl = request.args.get("torrent_dl")
    base_url = request.host_url.rstrip("/")
    total = len(items)
    total_bytes = sum(item.get("size_bytes", 0) for item in items)
    total_size = fmt_bytes(total_bytes)
    years = [item.get("year", 0) for item in items if item.get("year")]
    year_range = f"{min(years)} - {max(years)}" if years else "N/A"
    newest_year = str(max(years)) if years else "N/A"

    year_counts = sorted(Counter(years).items(), reverse=True)
    max_count = max((c for _, c in year_counts), default=1)
    year_chart = [(y, c, round(c / max_count * 100)) for y, c in year_counts]

    return render_template_string(
        HTML_TEMPLATE,
        items=items,
        total=total,
        total_size=total_size,
        year_range=year_range,
        newest_year=newest_year,
        year_chart=year_chart,
        base_url=base_url,
        load_warning=load_warning,
        invalid_size_count=invalid_size_count,
        csrf_token=get_csrf_token(),
        css=Markup(CSS),
        torrent_dl=torrent_dl,
    )


@app.route("/delete/<string:item_id>", methods=["POST"])
@requires_auth
def delete(item_id):
    if not has_valid_csrf(request.form.get("csrf_token")):
        return Response("Invalid CSRF token", status=403)
    items = [i for i in load_missing() if i.get("id") != item_id]
    save_missing(items)
    return redirect("/")


@app.route("/delete_bulk", methods=["POST"])
@requires_auth
def delete_bulk():
    if not has_valid_csrf(request.form.get("csrf_token")):
        return Response("Invalid CSRF token", status=403)
    raw = request.form.get("ids", "[]")
    try:
        ids = set(json.loads(raw))
    except Exception:
        ids = set()
    items = [i for i in load_missing() if i.get("id") not in ids]
    save_missing(items)
    return redirect("/")


def _item_pub_date(item):
    added = item.get("added", "")
    if added:
        try:
            dt = datetime.fromisoformat(added)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


@app.route("/yts_missing.rss")
@requires_auth
def rss():
    items = load_missing()
    base_url = request.host_url.rstrip("/")

    def _f(name):
        try:
            return float(request.args.get(name)) if request.args.get(name) else None
        except (TypeError, ValueError):
            return None
    min_year = _f("min_year")
    _min_gb = _f("min_size_gb")
    _max_gb = _f("max_size_gb")
    min_size_b = _min_gb * 1024 ** 3 if _min_gb else None
    max_size_b = _max_gb * 1024 ** 3 if _max_gb else None

    def _passes(item):
        if min_year is not None:
            try:
                if int(item.get("year", 0)) < min_year:
                    return False
            except (TypeError, ValueError):
                return False
        size_b = item.get("size_bytes")
        if not isinstance(size_b, int):
            size_b = parse_size_bytes(item.get("size", "")) or 0
        if min_size_b is not None and size_b < min_size_b:
            return False
        if max_size_b is not None and size_b > max_size_b:
            return False
        return True

    rss_items = ""
    for item in items:
        if not _passes(item):
            continue
        pub_date = _item_pub_date(item)
        magnet = str(item.get("magnet", ""))
        if not is_valid_magnet(magnet):
            continue
        title = escape(str(item.get("title", "")))
        infohash = escape(extract_infohash(magnet))
        year_val = item.get("year", "N/A")
        description = escape(f"Size: {item.get('size', 'Unknown')} | Year: {year_val}")
        guid = escape(str(item.get("id", "")))
        magnet_text = escape(magnet)
        enclosure_url = quoteattr(magnet)

        enclosure_length = item.get("size_bytes")
        if not isinstance(enclosure_length, int):
            parsed_size = parse_size_bytes(item.get("size", ""))
            enclosure_length = parsed_size if parsed_size is not None else 0

        rss_items += f"""
  <item>
    <title>{title}</title>
    <description>{description}</description>
    <link>{magnet_text}</link>
    <guid isPermaLink="false">{guid}</guid>
    <pubDate>{pub_date}</pubDate>
    <enclosure url={enclosure_url} length="{enclosure_length}" type="application/x-bittorrent"/>
    <tor:magnetURI>{magnet_text}</tor:magnetURI>
    <tor:infoHash>{infohash}</tor:infoHash>
  </item>"""

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:tor="http://toradio.org/2010/torrent">
  <channel>
    <title>Star's Missing 1080p YTS Movies</title>
    <description>Auto-generated missing 1080p movies from YTS -> Plex</description>
    <link>{escape(base_url)}</link>
    <atom:link href={quoteattr(base_url + '/yts_missing.rss')} rel="self" type="application/rss+xml"/>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>
{rss_items}
  </channel>
</rss>"""

    return Response(rss_xml, mimetype="application/rss+xml")


def _safe_filename(title):
    return re.sub(r'[^\w\s\-]', '', title).strip()


def _run_torrent_download():
    global _dl_in_progress
    items = load_missing()
    os.makedirs(TORRENT_DIR, exist_ok=True)
    for item in items:
        dest = os.path.join(TORRENT_DIR, _safe_filename(item.get("title", "unknown")) + ".torrent")
        if os.path.exists(dest):
            continue
        torrent_url = item.get("torrent_url") or ""
        hm = re.search(r'urn:btih:([a-fA-F0-9]{40})', item.get("magnet", ""), re.IGNORECASE)
        gg_url = f"https://yts.gg/torrent/download/{hm.group(1).upper()}" if hm else None
        urls = list(dict.fromkeys(u for u in [torrent_url, gg_url] if u))
        if not urls:
            continue
        network_error = False
        downloaded = False
        for url in urls:
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    f.write(r.content)
                time.sleep(0.4)
                downloaded = True
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                network_error = True
                continue
            except Exception:
                continue
        if not downloaded and network_error and not warned_blocked:
            print("YTS is blocked by your ISP. Change your DNS or use a VPN.")
            break
    with _dl_lock:
        _dl_in_progress = False


@app.route("/download_torrents", methods=["POST"])
@requires_auth
def trigger_download_torrents():
    global _dl_in_progress
    if not has_valid_csrf(request.form.get("csrf_token")):
        return Response("Invalid CSRF token", status=403)
    with _dl_lock:
        if _dl_in_progress:
            return redirect("/?torrent_dl=busy")
        _dl_in_progress = True
    threading.Thread(target=_run_torrent_download, daemon=True).start()
    return redirect("/?torrent_dl=started")


if __name__ == "__main__":
    bind_host = os.getenv("YTS_BIND_HOST", "127.0.0.1")
    bind_port = int(os.getenv("YTS_PORT", "5000"))
    loopback = bind_host in ("127.0.0.1", "localhost", "::1")
    allow_insecure = os.getenv("YTS_ALLOW_INSECURE", "false").lower() == "true"

    if not loopback and not auth_enabled() and not allow_insecure:
        print(
            f"ERROR: refusing to bind to {bind_host} without auth.\n"
            "  Set YTS_WEB_USERNAME and YTS_WEB_PASSWORD in .env, bind to 127.0.0.1,\n"
            "  or set YTS_ALLOW_INSECURE=true if a reverse proxy handles auth."
        )
        sys.exit(1)

    print("Star's YTS -> PLEX -> RSS Tool")
    print(f"   -> Web UI   : http://{bind_host}:{bind_port}")
    print(f"   -> RSS Feed : http://{bind_host}:{bind_port}/yts_missing.rss")
    if not auth_enabled():
        print("WARNING: YTS_WEB_USERNAME/YTS_WEB_PASSWORD not set; web auth is disabled (loopback only).")
    app.run(host=bind_host, port=bind_port, debug=False)
