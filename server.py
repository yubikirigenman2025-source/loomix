#!/usr/bin/env python3
"""
Loomix - 国産SNS統合タイムラインリーダー (Step 1 MVP)
Backend-For-Frontend (BFF) Server
"""

import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PORT = int(os.environ.get("PORT", 8080))
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")

SSL_CTX = ssl.create_default_context()
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Loomix/0.1"
}

CACHE_TTL_SEC = 20
_timeline_cache = {"data": None, "timestamp": 0}


def fetch_json(url, headers=None, timeout=8):
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- Normalizers ---

def fetch_karotter(limit=20):
    posts = []
    try:
        url = f"https://api.karotter.com/api/posts/recommended?limit={limit}&mode=latest"
        data = fetch_json(url)
        for p in data.get("posts", []):
            author = p.get("author") or {}
            avatar = author.get("avatarUrl")
            if avatar and avatar.startswith("/"):
                avatar = f"https://karotter.com{avatar}"
            
            created_at = p.get("createdAt")
            post_id = p.get("id")
            username = author.get("username", "")
            
            posts.append({
                "id": f"karotter_{post_id}",
                "rawId": str(post_id),
                "service": "karotter",
                "serviceName": "Karotter",
                "serviceIcon": "🥕",
                "badgeColor": "#f97316",
                "author": {
                    "username": username,
                    "displayName": author.get("displayName") or username,
                    "avatarUrl": avatar or "",
                    "profileUrl": f"https://karotter.com/{username}" if username else "https://karotter.com"
                },
                "content": p.get("content") or "",
                "media": p.get("mediaUrls") or [],
                "createdAt": created_at,
                "url": f"https://karotter.com/{username}/status/p_{post_id}" if username else f"https://karotter.com/posts/{post_id}",
                "metrics": {
                    "likes": p.get("likesCount", 0),
                    "reposts": p.get("rekarotsCount", 0),
                    "replies": p.get("repliesCount", 0)
                }
            })
    except Exception as e:
        logging.warning("Karotter fetch error: %s", e)
    return posts


def fetch_yudetter(limit=20):
    posts = []
    try:
        url = f"https://yudetter.com/api/yudates?limit={limit}"
        data = fetch_json(url)
        for p in data.get("items", []):
            author = p.get("author") or {}
            avatar = author.get("avatarUrl")
            if avatar and avatar.startswith("/"):
                avatar = f"https://yudetter.com{avatar}"
            
            created_at = p.get("createdAt")
            post_id = p.get("id")
            username = author.get("username", "")

            media = p.get("mediaUrls") or []
            if not media and p.get("imageUrl"):
                media = [p.get("imageUrl")]

            posts.append({
                "id": f"yudetter_{post_id}",
                "rawId": str(post_id),
                "service": "yudetter",
                "serviceName": "Yudetter",
                "serviceIcon": "🥚",
                "badgeColor": "#eab308",
                "author": {
                    "username": username,
                    "displayName": author.get("displayName") or username,
                    "avatarUrl": avatar or "",
                    "profileUrl": f"https://yudetter.com/{username}" if username else "https://yudetter.com"
                },
                "content": p.get("content") or "",
                "media": media,
                "createdAt": created_at,
                "url": f"https://yudetter.com/yudates/{post_id}",
                "metrics": {
                    "likes": p.get("likeCount", 0),
                    "reposts": p.get("reyudateCount", 0),
                    "replies": p.get("replyCount", 0)
                }
            })
    except Exception as e:
        logging.warning("Yudetter fetch error: %s", e)
    return posts


def fetch_amehare(limit=20):
    posts = []
    try:
        url = f"https://am-hr.com/api/timeline?bucket=latest&limit={limit}"
        data = fetch_json(url)
        jst = timezone(timedelta(hours=9))
        
        for item in data.get("timeline", []):
            canonical = item.get("canonical") or {}
            actor = item.get("actor") or canonical.get("author") or {}
            post_id = item.get("id") or canonical.get("id")
            
            avatar = actor.get("avatarUrl")
            if avatar and avatar.startswith("/"):
                avatar = f"https://am-hr.com{avatar}"

            raw_time = item.get("createdAt") or canonical.get("createdAt")
            created_at = None
            if raw_time:
                try:
                    dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=jst)
                    created_at = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                except Exception:
                    created_at = raw_time

            body = canonical.get("body") or item.get("body") or ""
            media = []
            if canonical.get("imageUrl"):
                media.append(canonical.get("imageUrl"))

            counts = canonical.get("counts") or {}
            userId = actor.get("userId") or ""

            posts.append({
                "id": f"amehare_{post_id}",
                "rawId": str(post_id),
                "service": "amehare",
                "serviceName": "あめ→はれ",
                "serviceIcon": "☀️",
                "badgeColor": "#0284c7",
                "author": {
                    "username": userId,
                    "displayName": actor.get("displayName") or userId,
                    "avatarUrl": avatar or "",
                    "profileUrl": f"https://am-hr.com/users/{userId}" if userId else "https://am-hr.com"
                },
                "content": body,
                "media": media,
                "createdAt": created_at,
                "url": f"https://am-hr.com/posts/{post_id}",
                "metrics": {
                    "likes": counts.get("reactions", 0),
                    "reposts": counts.get("reposts", 0),
                    "replies": counts.get("comments", 0)
                }
            })
    except Exception as e:
        logging.warning("あめ→はれ fetch error: %s", e)
    return posts


def fetch_pitter(limit=20):
    posts = []
    try:
        url = "https://pitter.jp/system/Post.php?action=latest"
        data = fetch_json(url)
        for p in data.get("data", [])[:limit]:
            post_id = p.get("id")
            handle = p.get("user_handle") or ""
            name = p.get("user_name") or handle
            avatar = p.get("user_icon_url") or ""
            
            created_ts = p.get("created_at") or p.get("sort_time")
            created_at = None
            if created_ts:
                try:
                    dt = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
                    created_at = dt.isoformat().replace("+00:00", "Z")
                except Exception:
                    pass

            text = p.get("content") or p.get("post_text") or p.get("text") or ""
            media_list = []
            if p.get("media"):
                if isinstance(p["media"], list):
                    media_list = p["media"]
                elif isinstance(p["media"], str):
                    try:
                        media_list = json.loads(p["media"])
                    except Exception:
                        pass

            posts.append({
                "id": f"pitter_{post_id}",
                "rawId": str(post_id),
                "service": "pitter",
                "serviceName": "Pitter",
                "serviceIcon": "🕊️",
                "badgeColor": "#a855f7",
                "author": {
                    "username": handle,
                    "displayName": name,
                    "avatarUrl": avatar,
                    "profileUrl": f"https://pitter.jp/{handle}" if handle else "https://pitter.jp"
                },
                "content": text,
                "media": media_list,
                "createdAt": created_at,
                "url": f"https://pitter.jp/posts/{post_id}",
                "metrics": {
                    "likes": int(p.get("like_count") or 0),
                    "reposts": int(p.get("repost_count") or 0),
                    "replies": int(p.get("reply_count") or 0)
                }
            })
    except Exception as e:
        logging.warning("Pitter fetch error: %s", e)
    return posts


def get_merged_timeline(force_refresh=False):
    global _timeline_cache
    now = time.time()
    if not force_refresh and _timeline_cache["data"] and (now - _timeline_cache["timestamp"] < CACHE_TTL_SEC):
        return _timeline_cache["data"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_k = executor.submit(fetch_karotter)
        f_y = executor.submit(fetch_yudetter)
        f_a = executor.submit(fetch_amehare)
        f_p = executor.submit(fetch_pitter)

        posts = []
        counts = {}
        for service_name, future in [("karotter", f_k), ("yudetter", f_y), ("amehare", f_a), ("pitter", f_p)]:
            try:
                res = future.result()
                posts.extend(res)
                counts[service_name] = len(res)
            except Exception as e:
                logging.error("Failed to gather %s: %s", service_name, e)
                counts[service_name] = 0

    # Sort descending by createdAt
    def parse_dt(item):
        ts = item.get("createdAt")
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            clean = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(clean)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    posts.sort(key=parse_dt, reverse=True)

    result = {
        "ok": True,
        "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total": len(posts),
        "serviceCounts": counts,
        "posts": posts
    }

    _timeline_cache = {"data": result, "timestamp": now}
    return result


class LoomixHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/timeline":
            service_filter = query.get("service", ["all"])[0]
            force = "refresh" in query
            data = get_merged_timeline(force_refresh=force)
            
            filtered_posts = data["posts"]
            if service_filter != "all":
                filtered_posts = [p for p in filtered_posts if p["service"] == service_filter]

            resp = {
                "ok": True,
                "fetchedAt": data["fetchedAt"],
                "total": len(filtered_posts),
                "allTotal": data["total"],
                "serviceCounts": data["serviceCounts"],
                "posts": filtered_posts
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        elif path == "/api/services":
            services = [
                {"id": "karotter", "name": "Karotter", "icon": "🥕", "url": "https://karotter.com", "color": "#f97316"},
                {"id": "yudetter", "name": "Yudetter", "icon": "🥚", "url": "https://yudetter.com", "color": "#eab308"},
                {"id": "amehare", "name": "あめ→はれ", "icon": "☀️", "url": "https://am-hr.com", "color": "#0284c7"},
                {"id": "pitter", "name": "Pitter", "icon": "🕊️", "url": "https://pitter.jp", "color": "#a855f7"}
            ]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "services": services}, ensure_ascii=False).encode("utf-8"))
            return

        return super().do_GET()


if __name__ == "__main__":
    server_address = ("0.0.0.0", PORT)
    httpd = HTTPServer(server_address, LoomixHandler)
    logging.info("Loomix BFF server listening on http://0.0.0.0:%d", PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("Server stopped.")
