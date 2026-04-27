#!/usr/bin/env python3
"""
SZX Cheap Round-Trip Flight RSS Server
Monitors Ctrip price calendar for cheap round-trip flights from Shenzhen (SZX).
Serves RSS 2.0 at http://localhost:8082/rss

Usage: python3 szx-flights-rss.py [port]
"""

import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Config ----------------------------------------------------------------

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
PROXY = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
CACHE_TTL = 7200  # seconds (2 hours)
LOOK_AHEAD_DAYS = 90
MIN_STAY = 3    # minimum nights for round-trip
MAX_STAY = 14   # maximum nights to search for return
MAX_WORKERS = 8  # parallel route fetches (each route does 2 API calls now)

# Routes: (iata_code, display_name, rt_threshold_cny)
# Round-trip thresholds ≈ 1.8x one-way (round-trips are rarely exactly 2x)
ROUTES = [
    ("SHA", "上海",       650),
    ("BJS", "北京",       860),
    ("CTU", "成都",       770),
    ("CKG", "重庆",       500),
    ("KMG", "昆明",       830),
    ("SYX", "三亚",       770),
    ("URC", "乌鲁木齐",  1150),
    ("TSN", "天津",       830),
    ("HGH", "杭州",       580),
    ("WUH", "武汉",       590),
    ("CSX", "长沙",       560),
    ("NKG", "南京",       650),
    ("NNG", "南宁",       580),
    ("HAK", "海口",       680),
    ("XMN", "厦门",       580),
    ("XIY", "西安",       740),
    ("DLC", "大连",       810),
    ("TAO", "青岛",       770),
    ("LJG", "丽江",       830),
    ("TNA", "济南",       770),
]

CTRIP_API = "https://flights.ctrip.com/itinerary/api/12808/lowestPrice"

# --- Cache -----------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_data = None
_cache_time = 0.0


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


def _get_all_prices(dcity: str, acity: str, extra_days: int = 0) -> dict:
    """Fetch all prices for the upcoming window."""
    today = date.today()
    cutoff = today + timedelta(days=LOOK_AHEAD_DAYS + extra_days)
    months_needed = set()
    d = today
    while d <= cutoff:
        months_needed.add((d.year, d.month))
        d = d.replace(day=1) + timedelta(days=32)
        d = d.replace(day=1)

    all_prices: dict = {}
    for y, m in sorted(months_needed):
        all_prices.update(_fetch_month_prices(dcity, acity, y, m))
    return all_prices


def fetch_cheap_roundtrips(dest_code: str, rt_threshold: int) -> list:
    """Return sorted list of (dep_date, ret_date, dep_price, ret_price, total)."""
    today = date.today()
    dep_cutoff = today + timedelta(days=LOOK_AHEAD_DAYS)

    # Fetch both directions concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_out = pool.submit(_get_all_prices, "SZX", dest_code, 0)
        f_ret = pool.submit(_get_all_prices, dest_code, "SZX", MAX_STAY)
        outbound_prices = f_out.result()
        return_prices = f_ret.result()

    combos = []
    for dep_str, dep_price in outbound_prices.items():
        try:
            dep_date = datetime.strptime(dep_str, "%Y%m%d").date()
        except ValueError:
            continue
        if not (today <= dep_date <= dep_cutoff):
            continue

        # Find cheapest return within the stay window
        best_ret_date = None
        best_ret_price = 999999
        for stay in range(MIN_STAY, MAX_STAY + 1):
            ret_date = dep_date + timedelta(days=stay)
            ret_price = return_prices.get(ret_date.strftime("%Y%m%d"))
            if ret_price is not None and ret_price < best_ret_price:
                best_ret_date = ret_date
                best_ret_price = ret_price

        if best_ret_date is None:
            continue

        total = dep_price + best_ret_price
        if total <= rt_threshold:
            combos.append((dep_date, best_ret_date, dep_price, best_ret_price, total))

    combos.sort(key=lambda x: x[4])
    return combos[:5]


# --- RSS builder -----------------------------------------------------------

def _rss_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_rss() -> str:
    print("Refreshing SZX round-trip flight data...", flush=True)
    now = datetime.utcnow()
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_cheap_roundtrips, code, threshold): (code, name, threshold)
            for code, name, threshold in ROUTES
        }
        for future in as_completed(futures):
            code, name, threshold = futures[future]
            combos = future.result()
            if combos:
                results.append((code, name, threshold, combos))

    results.sort(key=lambda r: r[3][0][4])  # sort by best total price

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>深圳出发特价往返机票</title>",
        "<link>https://flights.ctrip.com/</link>",
        "<description>SZX出发往返特价机票监控 (携程价格日历)</description>",
        f"<lastBuildDate>{_rss_date(now)}</lastBuildDate>",
    ]

    for code, name, threshold, combos in results:
        dep_date, ret_date, dep_price, ret_price, total = combos[0]
        nights = (ret_date - dep_date).days
        ctrip_url = (
            f"https://flights.ctrip.com/online/list/round-szx-{code.lower()}"
            f"?depdate={dep_date.strftime('%Y-%m-%d')}"
            f"&retdate={ret_date.strftime('%Y-%m-%d')}"
        )
        extra_html = ""
        if len(combos) > 1:
            others = ", ".join(
                f"{c[0].strftime('%m/%d')}-{c[1].strftime('%m/%d')}(¥{c[4]})"
                for c in combos[1:3]
            )
            extra_html = f"<br/>其他方案: {others}"
        lines += [
            "<item>",
            f"<title>深圳⇌{name} 往返¥{total} ({dep_date.strftime('%m/%d')}-{ret_date.strftime('%m/%d')}, {nights}晚)</title>",
            f"<link>{ctrip_url}</link>",
            (
                f"<description><![CDATA["
                f"去程: ¥{dep_price} ({dep_date.strftime('%m/%d')})<br/>"
                f"返程: ¥{ret_price} ({ret_date.strftime('%m/%d')})<br/>"
                f"共{nights}晚{extra_html}"
                f"]]></description>"
            ),
            f"<guid>szx-rt-{code.lower()}-{dep_date.strftime('%Y%m%d')}-{ret_date.strftime('%Y%m%d')}</guid>",
            f"<pubDate>{_rss_date(now)}</pubDate>",
            "</item>",
        ]

    lines += ["</channel>", "</rss>"]
    xml = "\n".join(lines)
    print(f"  → {len(results)}/{len(ROUTES)} routes have round-trip deals", flush=True)
    return xml


def get_cached_rss() -> str:
    with _cache_lock:
        if _cache_valid():
            return _cache_data  # type: ignore[return-value]
    rss = build_rss()
    with _cache_lock:
        global _cache_data, _cache_time
        _cache_data = rss
        _cache_time = time.time()
    return rss


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

    def log_message(self, format, *args):  # noqa: A002
        print(f"  [{self.address_string()}] {format % args}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=get_cached_rss, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"SZX Round-Trip RSS server on port {PORT}", flush=True)
    server.serve_forever()
