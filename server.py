#!/usr/bin/env python3
"""
Loomix - 国産SNS統合タイムラインリーダー
Backend-For-Frontend (BFF) Server

v0.2:
  - 各サービスの取得深度を拡大（karotter 100 / yudetter 100 / am-hr 80 / pitter 80）
  - 深い時系列マージと stale-while-revalidate キャッシュ
  - サイト内プロフィール表示用 /api/user（各サービスの公開プロフィール + 直近投稿）
  - 返信（reply）判定の正規化
  - gzip 応答
"""

import gzip
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PORT = int(os.environ.get("PORT", 8080))
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")

SSL_CTX = ssl.create_default_context()

# 相手先サーバーへの負荷を抑えるため、キャッシュが古い場合は
# まず手元のデータを返し、裏で1回だけ更新する（stale-while-revalidate）。
CACHE_TTL_SEC = 60
CACHE_STALE_SEC = 60 * 30

DEPTH = {
    "karotter": 100,
    "yudetter": 100,
    "amehare": 80,
    "pitter": 80,
}

SERVICES = [
    {"id": "karotter", "name": "Karotter", "label": "karotter", "url": "https://karotter.com"},
    {"id": "yudetter", "name": "Yudetter", "label": "yudetter", "url": "https://yudetter.com"},
    {"id": "amehare", "name": "あめ→はれ", "label": "am-hr", "url": "https://am-hr.com"},
    {"id": "pitter", "name": "Pitter", "label": "pitter", "url": "https://pitter.jp"},
]
SERVICE_BY_ID = {s["id"]: s for s in SERVICES}


def browser_headers(extra=None):
    h = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    if extra:
        h.update(extra)
    return h


class FetchError(Exception):
    pass


def fetch_json(url, headers=None, timeout=12, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or browser_headers())
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 - ネットワークは何でも起きる
            last = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise FetchError(str(last))


def abs_url(base, path):
    if not path:
        return ""
    path = str(path).replace("\\/", "/")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("//"):
        return "https:" + path
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def iso_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def make_post(**kw):
    """共通スキーマの投稿。サービス固有の差をここで吸収する。"""
    svc = SERVICE_BY_ID[kw["service"]]
    return {
        "id": kw["id"],
        "rawId": str(kw.get("rawId", "")),
        "service": svc["id"],
        "serviceName": svc["name"],
        "serviceLabel": svc["label"],
        "author": {
            "id": str(kw.get("authorId") or kw.get("username") or ""),
            "username": kw.get("username") or "",
            "displayName": kw.get("displayName") or kw.get("username") or "",
            "avatarUrl": kw.get("avatarUrl") or "",
            "profileUrl": kw.get("profileUrl") or "",
        },
        "content": kw.get("content") or "",
        "media": [m for m in (kw.get("media") or []) if m],
        "createdAt": kw.get("createdAt"),
        "url": kw.get("url") or kw.get("profileUrl") or "",
        "isReply": bool(kw.get("isReply")),
        "replyTo": kw.get("replyTo") or None,
        "isQuote": bool(kw.get("isQuote")),
        "metrics": {
            "likes": int(kw.get("likes") or 0),
            "reposts": int(kw.get("reposts") or 0),
            "replies": int(kw.get("replies") or 0),
            "views": int(kw.get("views") or 0),
        },
    }


# ---------------------------------------------------------------- Karotter

KAROTTER_HOST = "https://karotter.com"
KAROTTER_API = "https://api.karotter.com/api"


def _karotter_headers():
    return browser_headers({"Referer": KAROTTER_HOST + "/", "Origin": KAROTTER_HOST})


def karotter_post(p):
    author = p.get("author") or {}
    username = author.get("username") or ""
    reply_to = None
    if p.get("parentId"):
        users = p.get("replyToUsers") or []
        target = users[0] if users else {}
        reply_to = {
            "username": target.get("username") or "",
            "displayName": target.get("displayName") or target.get("username") or "",
            "url": (KAROTTER_HOST + "/" + target["username"]) if target.get("username") else "",
        }
    profile_url = (KAROTTER_HOST + "/" + username) if username else KAROTTER_HOST
    return make_post(
        service="karotter",
        id="karotter_%s" % p.get("id"),
        rawId=p.get("id"),
        username=username,
        displayName=author.get("displayName"),
        avatarUrl=abs_url(KAROTTER_HOST, author.get("avatarUrl")),
        profileUrl=profile_url,
        content=p.get("content"),
        media=[abs_url(KAROTTER_HOST, u) for u in (p.get("mediaUrls") or [])],
        createdAt=iso_z(parse_iso(p.get("createdAt"))) if parse_iso(p.get("createdAt")) else None,
        # 数値IDからは本家のパーマリンク（p_xxxxx）を引けないため、プロフィールへ送る
        url=profile_url,
        isReply=bool(p.get("parentId")),
        replyTo=reply_to,
        isQuote=bool(p.get("quotedPostId")),
        likes=p.get("likesCount"),
        reposts=p.get("rekarotsCount"),
        replies=p.get("repliesCount"),
        views=p.get("viewsCount"),
    )


def fetch_karotter(limit=DEPTH["karotter"]):
    data = fetch_json(
        "%s/posts/recommended?limit=%d&mode=latest" % (KAROTTER_API, limit),
        headers=_karotter_headers(),
    )
    return [karotter_post(p) for p in (data.get("posts") or [])][:limit]


# ---------------------------------------------------------------- Yudetter

YUDETTER_HOST = "https://yudetter.com"


def _yudetter_headers():
    # 外部 Origin を付けると 403 になるため Referer のみ
    return browser_headers({"Referer": YUDETTER_HOST + "/"})


def yudetter_post(p):
    author = p.get("author") or {}
    username = author.get("username") or ""
    media = list(p.get("mediaUrls") or [])
    if not media and p.get("imageUrl"):
        media = [p["imageUrl"]]
    profile_url = (YUDETTER_HOST + "/" + username) if username else YUDETTER_HOST
    post_id = p.get("id")
    return make_post(
        service="yudetter",
        id="yudetter_%s" % post_id,
        rawId=post_id,
        username=username,
        displayName=author.get("displayName"),
        avatarUrl=abs_url(YUDETTER_HOST, author.get("avatarUrl")),
        profileUrl=profile_url,
        content=p.get("content"),
        media=[abs_url(YUDETTER_HOST, u) for u in media],
        createdAt=iso_z(parse_iso(p.get("createdAt"))) if parse_iso(p.get("createdAt")) else None,
        url="%s/yudates/%s" % (YUDETTER_HOST, post_id),
        isReply=bool(p.get("replyToId")),
        replyTo={"url": "%s/yudates/%s" % (YUDETTER_HOST, p["replyToId"])} if p.get("replyToId") else None,
        isQuote=bool(p.get("quotedYudate")),
        likes=p.get("likeCount"),
        reposts=p.get("reyudateCount"),
        replies=p.get("replyCount"),
    )


def yudetter_items(limit):
    items = []
    cursor = None
    while len(items) < limit:
        page = min(50, limit - len(items))
        url = "%s/api/yudates?limit=%d" % (YUDETTER_HOST, page)
        if cursor:
            url += "&cursor=%s" % urllib.parse.quote(str(cursor))
        data = fetch_json(url, headers=_yudetter_headers())
        batch = data.get("items") or []
        if not batch:
            break
        items.extend(batch)
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return items[:limit]


def fetch_yudetter(limit=DEPTH["yudetter"]):
    return [yudetter_post(p) for p in yudetter_items(limit)]


# ---------------------------------------------------------------- あめ→はれ

AMEHARE_HOST = "https://am-hr.com"
JST = timezone(timedelta(hours=9))


def _amehare_headers():
    return browser_headers({"Referer": AMEHARE_HOST + "/"})


def amehare_time(value):
    if not value:
        return None
    try:
        return iso_z(datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST))
    except Exception:
        pass
    dt = parse_iso(value)
    return iso_z(dt) if dt else None


def amehare_post(item):
    canonical = item.get("canonical") or {}
    actor = item.get("actor") or canonical.get("author") or {}
    post_id = item.get("id") or canonical.get("id")
    user_id = actor.get("userId") or ""
    media = []
    image = canonical.get("imageUrl")
    if image:
        media.append(abs_url(AMEHARE_HOST, image))
    counts = canonical.get("counts") or {}
    profile_url = (AMEHARE_HOST + "/users/" + user_id) if user_id else AMEHARE_HOST
    parent = canonical.get("parentId") or item.get("parentId")
    return make_post(
        service="amehare",
        id="amehare_%s" % post_id,
        rawId=post_id,
        authorId=user_id,
        username=user_id,
        displayName=actor.get("displayName"),
        avatarUrl=abs_url(AMEHARE_HOST, actor.get("avatarUrl")),
        profileUrl=profile_url,
        content=canonical.get("body") or item.get("body"),
        media=media,
        createdAt=amehare_time(canonical.get("createdAt") or item.get("createdAt")),
        url="%s/posts/%s" % (AMEHARE_HOST, post_id),
        isReply=bool(parent) or item.get("kind") == "reply",
        replyTo={"url": "%s/posts/%s" % (AMEHARE_HOST, parent)} if parent else None,
        likes=counts.get("reactions"),
        reposts=counts.get("reposts"),
        replies=counts.get("comments"),
    )


def fetch_amehare(limit=DEPTH["amehare"]):
    timeline = []
    before = None
    while len(timeline) < limit:
        page = min(40, limit - len(timeline))
        url = "%s/api/timeline?bucket=latest&limit=%d" % (AMEHARE_HOST, page)
        if before:
            url += "&beforeSeq=%s" % before
        data = fetch_json(url, headers=_amehare_headers())
        batch = data.get("timeline") or []
        if not batch:
            break
        timeline.extend(batch)
        before = batch[-1].get("seq")
        if not before:
            break
    return [amehare_post(item) for item in timeline[:limit]]


# ---------------------------------------------------------------- Pitter

PITTER_HOST = "https://pitter.jp"


def _pitter_headers(extra=None):
    h = browser_headers({
        "Referer": PITTER_HOST + "/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    if extra:
        h.update(extra)
    return h


def pitter_media(raw):
    items = []
    if not raw:
        return items
    if isinstance(raw, str):
        try:
            items = json.loads(raw.replace("\\/", "/"))
        except Exception:
            return []
    elif isinstance(raw, list):
        items = raw
    urls = []
    for m in items:
        if isinstance(m, dict):
            u = m.get("url") or m.get("src")
        else:
            u = m
        if u:
            urls.append(abs_url(PITTER_HOST, u))
    return urls


def pitter_time(value):
    if not value:
        return None
    try:
        return iso_z(datetime.fromtimestamp(int(value), tz=timezone.utc))
    except Exception:
        return None


def pitter_post(p):
    handle = str(p.get("user_handle") or "")
    name = p.get("user_name") or handle
    post_id = p.get("id")
    profile_url = "%s/?profile=%s" % (PITTER_HOST, urllib.parse.quote(handle)) if handle else PITTER_HOST
    parent = p.get("parent_id")
    return make_post(
        service="pitter",
        id="pitter_%s" % post_id,
        rawId=post_id,
        authorId=handle,
        username=handle,
        displayName=name,
        avatarUrl=abs_url(PITTER_HOST, p.get("user_icon_url")),
        profileUrl=profile_url,
        content=p.get("content") or p.get("post_text") or p.get("text"),
        media=pitter_media(p.get("media")),
        createdAt=pitter_time(p.get("created_at") or p.get("sort_time")),
        url="%s/?post=%s" % (PITTER_HOST, post_id),
        isReply=bool(parent),
        replyTo={"url": "%s/?post=%s" % (PITTER_HOST, parent)} if parent else None,
        likes=p.get("like_count"),
        reposts=p.get("repost_count"),
        replies=p.get("reply_count"),
    )


def fetch_pitter(limit=DEPTH["pitter"]):
    posts = []
    page = 1
    while len(posts) < limit and page <= 6:
        data = fetch_json(
            "%s/system/Post.php?action=latest&page=%d" % (PITTER_HOST, page),
            headers=_pitter_headers(),
        )
        batch = data.get("data") or []
        if not batch:
            break
        posts.extend(batch)
        page += 1
    return [pitter_post(p) for p in posts[:limit]]


# ---------------------------------------------------------------- merge

FETCHERS = {
    "karotter": fetch_karotter,
    "yudetter": fetch_yudetter,
    "amehare": fetch_amehare,
    "pitter": fetch_pitter,
}

_cache = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refreshing = threading.Event()


def merge_all(force=False):
    posts = []
    counts = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {name: executor.submit(fn) for name, fn in FETCHERS.items()}
        for name, future in futures.items():
            try:
                result = future.result()
                posts.extend(result)
                counts[name] = len(result)
            except Exception as e:  # noqa: BLE001
                logging.warning("%s fetch failed: %s", name, e)
                counts[name] = 0
                errors[name] = str(e)[:200]

    def key(p):
        dt = parse_iso(p.get("createdAt"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    posts.sort(key=key, reverse=True)

    return {
        "ok": True,
        "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total": len(posts),
        "serviceCounts": counts,
        "errors": errors,
        "services": SERVICES,
        "posts": posts,
    }


def refresh_cache():
    # 同時アクセスでも更新は1本に絞り、待った側は新しいキャッシュを受け取る
    with _refresh_lock:
        with _cache_lock:
            current = _cache["data"]
            age = time.time() - _cache["ts"]
        if current and age < 3:
            return current
        data = merge_all()
        with _cache_lock:
            _cache["data"] = data
            _cache["ts"] = time.time()
        return data


def background_refresh():
    if _refreshing.is_set():
        return
    _refreshing.set()

    def run():
        try:
            refresh_cache()
        except Exception as e:  # noqa: BLE001
            logging.warning("background refresh failed: %s", e)
        finally:
            _refreshing.clear()

    threading.Thread(target=run, daemon=True).start()


def get_timeline():
    """最新ならキャッシュ、古ければ即返しつつ裏で更新、無ければ同期取得。"""
    now = time.time()
    with _cache_lock:
        data = _cache["data"]
        age = now - _cache["ts"]
    if data and age < CACHE_TTL_SEC:
        return data
    if data and age < CACHE_STALE_SEC:
        background_refresh()
        return data
    return refresh_cache()


# ---------------------------------------------------------------- profile

def timeline_posts_for(service, handle):
    """キャッシュ済みタイムラインから、そのユーザーの投稿だけ拾う（フォールバック）。"""
    with _cache_lock:
        data = _cache["data"] or {}
    out = []
    for p in data.get("posts") or []:
        if p["service"] != service:
            continue
        a = p.get("author") or {}
        keys = {str(a.get("username") or ""), str(a.get("id") or "")}
        if handle in keys:
            out.append(p)
    return out


def _profile(service, handle):
    svc = SERVICE_BY_ID[service]
    profile = {
        "service": service,
        "serviceName": svc["name"],
        "serviceLabel": svc["label"],
        "username": handle,
        "displayName": handle,
        "bio": "",
        "avatarUrl": "",
        "profileUrl": svc["url"],
        "stats": {},
        "joinedAt": None,
    }
    posts = []

    if service == "karotter":
        user = (fetch_json("%s/users/%s" % (KAROTTER_API, urllib.parse.quote(handle)),
                           headers=_karotter_headers()) or {}).get("user") or {}
        profile.update({
            "username": user.get("username") or handle,
            "displayName": user.get("displayName") or handle,
            "bio": user.get("bio") or "",
            "avatarUrl": abs_url(KAROTTER_HOST, user.get("avatarUrl")),
            "profileUrl": KAROTTER_HOST + "/" + (user.get("username") or handle),
            "stats": {
                "posts": user.get("postsCount"),
                "followers": user.get("followersCount"),
                "following": user.get("followingCount"),
            },
            "joinedAt": iso_z(parse_iso(user.get("createdAt"))) if parse_iso(user.get("createdAt")) else None,
        })
        data = fetch_json("%s/users/%s/posts?limit=20" % (KAROTTER_API, urllib.parse.quote(handle)),
                          headers=_karotter_headers())
        posts = [karotter_post(p) for p in (data.get("posts") or [])]

    elif service == "yudetter":
        user = fetch_json("%s/api/users/%s" % (YUDETTER_HOST, urllib.parse.quote(handle)),
                          headers=_yudetter_headers()) or {}
        if isinstance(user, dict) and "user" in user:
            user = user["user"]
        profile.update({
            "username": user.get("username") or handle,
            "displayName": user.get("displayName") or handle,
            "bio": user.get("bio") or "",
            "avatarUrl": abs_url(YUDETTER_HOST, user.get("avatarUrl")),
            "profileUrl": YUDETTER_HOST + "/" + (user.get("username") or handle),
            "stats": {
                "posts": user.get("yudateCount"),
                "followers": user.get("followerCount"),
                "following": user.get("followingCount"),
            },
            "joinedAt": iso_z(parse_iso(user.get("createdAt"))) if parse_iso(user.get("createdAt")) else None,
        })
        data = fetch_json("%s/api/users/%s/yudates?limit=20" % (YUDETTER_HOST, urllib.parse.quote(handle)),
                          headers=_yudetter_headers())
        posts = [yudetter_post(p) for p in (data.get("items") or [])]

    elif service == "amehare":
        data = fetch_json("%s/api/users/%s?bucket=main" % (AMEHARE_HOST, urllib.parse.quote(handle)),
                          headers=_amehare_headers())
        user = data.get("user") or {}
        stats = data.get("stats") or {}
        profile.update({
            "username": user.get("userId") or handle,
            "displayName": user.get("displayName") or handle,
            "bio": user.get("bio") or "",
            "avatarUrl": abs_url(AMEHARE_HOST, user.get("avatarUrl")),
            "profileUrl": AMEHARE_HOST + "/users/" + (user.get("userId") or handle),
            "stats": {
                "posts": stats.get("postCount"),
                "reactions": stats.get("reactionCount"),
            },
            "joinedAt": amehare_time(user.get("createdAt")),
        })
        posts = [amehare_post(item) for item in (data.get("timeline") or [])]

    elif service == "pitter":
        data = fetch_json("%s/system/Post.php?action=profile&handle=%s" % (PITTER_HOST, urllib.parse.quote(handle)),
                          headers=_pitter_headers()) or {}
        data = data.get("data") or {}
        user = data.get("user") or {}
        profile.update({
            "username": user.get("handle") or handle,
            "displayName": user.get("username") or handle,
            "bio": user.get("bio") or "",
            "avatarUrl": abs_url(PITTER_HOST, user.get("icon_url")),
            "profileUrl": "%s/?profile=%s" % (PITTER_HOST, urllib.parse.quote(str(user.get("handle") or handle))),
            "stats": {
                "followers": user.get("follower_count"),
                "following": user.get("following_count"),
            },
            "joinedAt": pitter_time(user.get("created_at")),
            "location": user.get("location") or "",
            "website": user.get("website") or "",
        })
        posts = [pitter_post(p) for p in (data.get("posts") or [])]

    else:
        return None

    if not posts:
        posts = timeline_posts_for(service, handle)

    if not profile["avatarUrl"] or not profile["displayName"]:
        fallback = timeline_posts_for(service, handle)
        if fallback:
            a = fallback[0]["author"]
            profile["displayName"] = profile["displayName"] or a.get("displayName")
            profile["avatarUrl"] = profile["avatarUrl"] or a.get("avatarUrl")

    return {"ok": True, "profile": profile, "posts": posts[:20]}


_profile_cache = {}
_profile_lock = threading.Lock()


def get_profile(service, handle):
    key = (service, handle)
    with _profile_lock:
        hit = _profile_cache.get(key)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    result = _profile(service, handle)
    if result:
        with _profile_lock:
            _profile_cache[key] = (result, time.time())
    return result


# ---------------------------------------------------------------- HTTP

def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class LoomixHandler(SimpleHTTPRequestHandler):
    server_version = "Loomix/0.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def log_message(self, fmt, *args):  # 静かに
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, obj, status=200, cache_control="no-cache", gzip_ok=True):
        body = json_bytes(obj)
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gzip_ok and accepts_gzip and len(body) > 1024:
            body = gzip.compress(body, 5)
            self.send_response(status)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/timeline":
            service_filter = query.get("service", ["all"])[0]
            force = query.get("refresh", ["0"])[0] not in ("", "0", "false")
            data = refresh_cache() if force else get_timeline()

            posts = data["posts"]
            if service_filter != "all":
                posts = [p for p in posts if p["service"] == service_filter]

            self._send_json({
                "ok": True,
                "fetchedAt": data["fetchedAt"],
                "total": len(posts),
                "allTotal": data["total"],
                "serviceCounts": data["serviceCounts"],
                "errors": data.get("errors") or {},
                "services": SERVICES,
                "posts": posts,
            }, cache_control="public, max-age=30")
            return

        if path == "/api/user":
            service = query.get("service", [""])[0]
            handle = query.get("id", [""])[0] or query.get("username", [""])[0]
            if service not in SERVICE_BY_ID or not handle:
                self._send_json({"ok": False, "error": "service と id が必要です"}, status=400)
                return
            try:
                result = get_profile(service, handle)
            except Exception as e:  # noqa: BLE001
                logging.warning("profile %s/%s failed: %s", service, handle, e)
                result = None
            if not result:
                self._send_json({"ok": False, "error": "プロフィールを取得できませんでした"}, status=502)
                return
            self._send_json(result, cache_control="public, max-age=120")
            return

        if path == "/api/services":
            self._send_json({"ok": True, "services": SERVICES})
            return

        if path == "/api/health":
            with _cache_lock:
                age = time.time() - _cache["ts"] if _cache["data"] else None
            self._send_json({
                "ok": True,
                "version": "0.2",
                "cacheAgeSec": round(age) if age is not None else None,
                "total": (_cache["data"] or {}).get("total"),
                "serviceCounts": (_cache["data"] or {}).get("serviceCounts"),
            })
            return

        return super().do_GET()


if __name__ == "__main__":
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, LoomixHandler)
    httpd.daemon_threads = True
    logging.info("Loomix BFF server listening on http://0.0.0.0:%d", PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("Server stopped.")
