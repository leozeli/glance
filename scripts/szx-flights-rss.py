#!/usr/bin/env python3
"""
SZX Cheap Flight RSS Server
Monitors Ctrip price calendar for cheap flights departing from Shenzhen (SZX).
Serves RSS 2.0 at http://localhost:8081/rss

Usage: python3 szx-flights-rss.py [port]
Requires proxy at http://127.0.0.1:7890 for Ctrip API access.
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Config ----------------------------------------------------------------

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
# Set HTTPS_PROXY env var to use a proxy (e.g. local dev with Clash).
# Leave unset for direct access (e.g. overseas VPS).
PROXY = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
CACHE_TTL = 7200  # seconds (2 hours)
LOOK_AHEAD_DAYS = 90
MAX_WORKERS = 12  # parallel route fetches

# Routes: (iata_code, display_name, deal_threshold_cny)
# Thresholds are set ~20% below typical low-season prices for each route
ROUTES = [
    ("SHA", "上海",      360),
    ("BJS", "北京",      480),
    ("CTU", "成都",      430),
    ("CKG", "重庆",      280),
    ("KMG", "昆明",      460),
    ("SYX", "三亚",      430),
    ("URC", "乌鲁木齐",  640),
    ("TSN", "天津",      460),
    ("HGH", "杭州",      320),
    ("WUH", "武汉",      330),
    ("CSX", "长沙",      310),
    ("NKG", "南京",      360),
    ("NNG", "南宁",      320),
    ("HAK", "海口",      380),
    ("XMN", "厦门",      320),
    ("XIY", "西安",      410),
    ("DLC", "大连",      450),
    ("TAO", "青岛",      430),
    ("LJG", "丽江",      460),
    ("TNA", "济南",      430),
]

CTRIP_API = "https://flights.ctrip.com/itinerary/api/12808/lowestPrice"

# --- Cache -----------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_data = None       # str: RSS XML
_cache_time = 0.0        # epoch seconds


def _cache_valid() -> bool:
    return _cache_data is not None and (time.time() - _cache_time) < CACHE_TTL


# --- Ctrip fetch -----------------------------------------------------------

def _fetch_month_prices(dcity: str, acity: str, year: int, month: int) -> dict:
    """Return {datestr: price} or {} on error."""
    url = (
        f"{CTRIP_API}?flightWay=Oneway"
        f"&dcity={dcity}&acity={acity}"
        f"&departuretime={year}-{month:02d}"
    )
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
        opener = urllib.request.build_opener(proxy_handler)
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    req.add_header("Referer", "https://flights.ctrip.com/")
    try:
        with opener.open(req, timeout=12) as resp:
            data = json.loads(resp.read())
        if data.get("msg") == "success":
            rows = data.get("data", {}).get("oneWayPrice") or [{}]
            return rows[0] if rows else {}
    except Exception as exc:
        print(f"  warn: {dcity}→{acity} {year}-{month:02d}: {exc}", flush=True)
    return {}


def fetch_cheap_dates(dest_code: str, threshold: int) -> list:
    """Return sorted list of (date, price) tuples below threshold, next N days."""
    today = date.today()
    cutoff = today + timedelta(days=LOOK_AHEAD_DAYS)

    # Collect month keys to query
    months_needed = set()
    d = today
    while d <= cutoff:
        months_needed.add((d.year, d.month))
        d = d.replace(day=1) + timedelta(days=32)
        d = d.replace(day=1)

    all_prices: dict = {}
    for y, m in sorted(months_needed):
        all_prices.update(_fetch_month_prices("SZX", dest_code, y, m))

    cheap = []
    for date_str, price in all_prices.items():
        try:
            flight_date = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            continue
        if today <= flight_date <= cutoff and price <= threshold:
            cheap.append((flight_date, price))

    cheap.sort(key=lambda x: x[1])
    return cheap[:5]


# --- RSS builder -----------------------------------------------------------

def _rss_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_rss() -> str:
    print("Refreshing SZX flight data...", flush=True)
    now = datetime.utcnow()

    results = []  # list of (code, name, threshold, cheap_dates)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_cheap_dates, code, threshold): (code, name, threshold)
            for code, name, threshold in ROUTES
        }
        for future in as_completed(futures):
            code, name, threshold = futures[future]
            cheap = future.result()
            if cheap:
                results.append((code, name, threshold, cheap))

    # Sort by best (lowest) price across all routes
    results.sort(key=lambda r: r[3][0][1])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>深圳出发特价机票</title>",
        "<link>https://flights.ctrip.com/</link>",
        "<description>SZX出发低价机票监控 (携程价格日历)</description>",
        f"<lastBuildDate>{_rss_date(now)}</lastBuildDate>",
    ]

    for code, name, threshold, cheap in results:
        best_date, best_price = cheap[0]
        dates_summary = ", ".join(f"{d.strftime('%m/%d')}(¥{p})" for d, p in cheap)
        extra = f" 共{len(cheap)}个特价日" if len(cheap) > 1 else ""
        ctrip_url = (
            f"https://flights.ctrip.com/online/list/oneway-szx-{code.lower()}"
            f"?depdate={best_date.strftime('%Y-%m-%d')}"
        )
        lines += [
            "<item>",
            f"<title>深圳→{name} ¥{best_price} ({best_date.strftime('%m/%d')}){extra}</title>",
            f"<link>{ctrip_url}</link>",
            f"<description><![CDATA[特价日期: {dates_summary}<br/>门槛: ≤¥{threshold}]]></description>",
            f"<guid>szx-{code.lower()}-{best_date.strftime('%Y%m%d')}-{best_price}</guid>",
            f"<pubDate>{_rss_date(now)}</pubDate>",
            "</item>",
        ]

    lines += ["</channel>", "</rss>"]
    xml = "\n".join(lines)
    print(f"  → {len(results)}/{len(ROUTES)} routes have deals", flush=True)
    return xml


def get_cached_rss() -> str:
    global _cache_data, _cache_time
    with _cache_lock:
        if _cache_valid():
            return _cache_data

    xml = build_rss()

    with _cache_lock:
        _cache_data = xml
        _cache_time = time.time()

    return xml


# --- HTTP handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/rss", "/rss/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = get_cached_rss().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(exc).encode())

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


# --- Entry point -----------------------------------------------------------

if __name__ == "__main__":
    print(f"SZX flight RSS server starting on :{PORT}", flush=True)
    print(f"Feed URL: http://localhost:{PORT}/rss", flush=True)
    print(f"Cache TTL: {CACHE_TTL}s | Look-ahead: {LOOK_AHEAD_DAYS}d | Routes: {len(ROUTES)}", flush=True)
    # Pre-warm cache in background so first request is fast
    threading.Thread(target=get_cached_rss, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
