#!/usr/bin/env python3
"""
NewsNow → RSS Bridge
Fetches NewsNow JSON API and serves RSS 2.0.

Routes:
  GET /rss?id=<source_id>   → RSS feed for that source
  GET /rss                  → 400 (id required)

Usage: python3 newsnow-rss.py [port]
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8083
NEWSNOW_URL = os.environ.get("NEWSNOW_URL", "http://newsnow:4444")
CACHE_TTL = 600  # 10 minutes

_cache_lock = threading.Lock()
_cache: dict = {}  # source_id → (xml, timestamp)


def _cache_get(source_id: str):
    with _cache_lock:
        entry = _cache.get(source_id)
        if entry and (time.time() - entry[1]) < CACHE_TTL:
            return entry[0]
    return None


def _cache_set(source_id: str, xml: str) -> None:
    with _cache_lock:
        _cache[source_id] = (xml, time.time())


def _rss_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fetch_rss(source_id: str) -> str:
    cached = _cache_get(source_id)
    if cached:
        return cached

    url = f"{NEWSNOW_URL}/api/s?id={urllib.parse.quote(source_id)}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch {source_id}: {exc}") from exc

    if data.get("status") != "success":
        raise RuntimeError(f"NewsNow non-success for {source_id}: {data.get('status')}")

    items = data.get("items") or []
    now = datetime.utcnow()
    updated_ms = data.get("updatedTime")
    updated_dt = datetime.utcfromtimestamp(updated_ms / 1000) if updated_ms else now

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{_esc(source_id)}</title>",
        f"<link>{NEWSNOW_URL}</link>",
        f"<description>NewsNow: {_esc(source_id)}</description>",
        f"<lastBuildDate>{_rss_date(updated_dt)}</lastBuildDate>",
    ]

    for item in items[:30]:
        title = item.get("title", "").strip()
        url_raw = item.get("url", "")
        extra = item.get("extra") or {}
        hot = extra.get("hover") or extra.get("info") or ""
        desc = f"{title} [{hot}]" if hot else title
        guid = str(item.get("id", url_raw))
        lines += [
            "<item>",
            f"<title>{_esc(title)}</title>",
            f"<link>{_esc(url_raw)}</link>",
            f"<description><![CDATA[{desc}]]></description>",
            f"<guid>{_esc(guid)}</guid>",
            f"<pubDate>{_rss_date(updated_dt)}</pubDate>",
            "</item>",
        ]

    lines += ["</channel>", "</rss>"]
    xml = "\n".join(lines)
    _cache_set(source_id, xml)
    return xml


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/rss", "/rss/"):
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        ids = params.get("id", [])
        if not ids:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"?id=<source_id> required")
            return

        source_id = ids[0]
        try:
            body = fetch_rss(source_id).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(exc).encode())

    def log_message(self, format, *args):  # noqa: A002
        print(f"  [{self.address_string()}] {format % args}", flush=True)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"NewsNow RSS bridge on port {PORT} → {NEWSNOW_URL}", flush=True)
    server.serve_forever()
