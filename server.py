#!/usr/bin/env python3
"""
Loomix - 国産SNS統合タイムラインリーダー
Backend-For-Frontend (BFF) Server

v0.3:
  - カーソル式ページング: GET /api/timeline?before=<ISO8601|postId>&limit=40
  - 過去ログの永続アーカイブ（一度取ったページは二度と外部APIへ取りに行かない）
  - サービスごとのレートリミット（最低間隔・毎分上限・単一実行ロック）
  - 新着は差分同期（既知の投稿に当たったら止める）
  - サイト内プロフィール表示用 /api/user
  - gzip 応答

v0.4:
  - 連携認証（auth.py）: 各SNSの自分のアカウントでログインしてはじめてタイムラインが開く
    POST /api/auth/login, GET /api/auth/status, POST /api/auth/unlink, POST /api/auth/logout
  - Loomix セッションは HMAC 署名トークン。パスワードは保存しない
  - 未連携・連携不足では /api/timeline と /api/user が 401 auth_required を返す
"""

import auth

import collections
import gzip
import json
import logging
import os
import re
import ssl
import tempfile
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
VERSION = "0.4"

SSL_CTX = ssl.create_default_context()

# ---------------------------------------------------------------- 設定

# 1回の応答で返す既定件数と上限
DEFAULT_LIMIT = 40
MAX_LIMIT = 80

# 新着同期の最短間隔（これより短い間隔でクライアントが叩いても外部へは行かない）
CACHE_TTL_SEC = 60

# 初回（アーカイブが空のとき）だけ各サービスから取る深さ
DEPTH = {
    "karotter": 100,
    "yudetter": 100,
    "amehare": 80,
    "pitter": 80,
}

# 各サービスの1ページあたりの取得件数（先方APIの実測値）
PAGE_SIZE = {
    "karotter": 40,
    "yudetter": 40,
    "amehare": 40,
    "pitter": 20,
}

# 相手先サーバーへの負荷制御
MIN_INTERVAL = 0.5          # 同一サービスへの連続リクエストの最低間隔（秒）
COLD_INTERVAL = 0.25        # 初回充填時だけ少し詰める
RATE_PER_MIN = 60           # 同一サービスへの1分あたりの上限
REQUEST_BUDGET_SEC = 20.0   # /api/timeline 1回の応答で外部APIを待つ上限
MAX_ROUNDS = 8              # 1回の応答でアーカイブを伸ばすラウンド数
ARCHIVE_CAP = 6000          # 1サービスあたりの保持上限（メモリ保護）

SERVICES = [
    {"id": "karotter", "name": "Karotter", "label": "karotter", "url": "https://karotter.com"},
    {"id": "yudetter", "name": "Yudetter", "label": "yudetter", "url": "https://yudetter.com"},
    {"id": "amehare", "name": "あめ→はれ", "label": "am-hr", "url": "https://am-hr.com"},
    {"id": "pitter", "name": "Pitter", "label": "pitter", "url": "https://pitter.jp"},
]
SERVICE_BY_ID = {s["id"]: s for s in SERVICES}
SERVICE_IDS = [s["id"] for s in SERVICES]


def _cache_dir():
    base = os.environ.get("LOOMIX_CACHE_DIR")
    if not base:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    try:
        os.makedirs(base, exist_ok=True)
        probe = os.path.join(base, ".write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return base
    except Exception:  # noqa: BLE001 - 書けない環境では /tmp に退避
        fallback = os.path.join(tempfile.gettempdir(), "loomix-cache")
        os.makedirs(fallback, exist_ok=True)
        return fallback


CACHE_DIR = _cache_dir()
auth.configure(CACHE_DIR)


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


def fetch_json(url, headers=None, timeout=12, retries=1):
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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
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
        "linkLabel": kw.get("linkLabel") or "本家で開く",
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
        linkLabel="投稿者ページ",
        isReply=bool(p.get("parentId")),
        replyTo=reply_to,
        isQuote=bool(p.get("quotedPostId")),
        likes=p.get("likesCount"),
        reposts=p.get("rekarotsCount"),
        replies=p.get("repliesCount"),
        views=p.get("viewsCount"),
    )


def karotter_page(cursor=None):
    """1ページ分。cursor は先方が返す nextCursor（不透明なトークン）。"""
    url = "%s/posts/recommended?limit=%d&mode=latest" % (KAROTTER_API, PAGE_SIZE["karotter"])
    if cursor:
        url += "&cursor=%s" % urllib.parse.quote(str(cursor))
    data = fetch_json(url, headers=_karotter_headers())
    posts = [karotter_post(p) for p in (data.get("posts") or [])]
    pagination = data.get("pagination") or {}
    return posts, pagination.get("nextCursor")


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


def yudetter_page(cursor=None):
    url = "%s/api/yudates?limit=%d" % (YUDETTER_HOST, PAGE_SIZE["yudetter"])
    if cursor:
        url += "&cursor=%s" % urllib.parse.quote(str(cursor))
    data = fetch_json(url, headers=_yudetter_headers())
    posts = [yudetter_post(p) for p in (data.get("items") or [])]
    return posts, data.get("nextCursor")


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


def amehare_page(cursor=None):
    """cursor は beforeSeq（最後に取った投稿の seq）。"""
    url = "%s/api/timeline?bucket=latest&limit=%d" % (AMEHARE_HOST, PAGE_SIZE["amehare"])
    if cursor:
        url += "&beforeSeq=%s" % urllib.parse.quote(str(cursor))
    data = fetch_json(url, headers=_amehare_headers())
    batch = data.get("timeline") or []
    posts = [amehare_post(item) for item in batch]
    nxt = (batch[-1] or {}).get("seq") if batch else None
    return posts, (nxt if batch else None)


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


def pitter_page(cursor=None):
    """cursor はページ番号（20件/ページ）。"""
    try:
        page = max(1, int(cursor or 1))
    except Exception:
        page = 1
    data = fetch_json(
        "%s/system/Post.php?action=latest&page=%d" % (PITTER_HOST, page),
        headers=_pitter_headers(),
    )
    batch = data.get("data") or []
    posts = [pitter_post(p) for p in batch]
    nxt = page + 1 if len(batch) >= PAGE_SIZE["pitter"] else None
    return posts, nxt


PAGERS = {
    "karotter": karotter_page,
    "yudetter": yudetter_page,
    "amehare": amehare_page,
    "pitter": pitter_page,
}


def post_sort_key(post):
    dt = parse_iso(post.get("createdAt"))
    return dt or datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- アーカイブ
#
# 一度でも取得したページはディスクとメモリに残し、同じ過去範囲は
# 外部APIへ行かずに返す。過去の投稿は基本不変なので、履歴の取得は
# 「サービスごとに一生で1回だけ」に近づく。


class Archive:
    def __init__(self, sid):
        self.sid = sid
        self.posts = {}
        self.tail_cursor = None      # 古い方向へ進むためのカーソル
        self.exhausted = False       # それ以上古い投稿は無い
        self.floor_ts = None         # カーソルが到達した時点（ここまで漏れなく読んだ）
        self.pages = 0               # 取得済みページ数（起動後）
        self.error = None
        self._sorted = None
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()   # 同一サービスへの同時リクエストを1本に
        self._rate_lock = threading.Lock()
        self._last_call = 0.0
        self._recent = collections.deque()
        self._appended = 0
        self._used_cursors = set()
        self._last_prefetch = 0.0

    # -- データ

    def __len__(self):
        with self._lock:
            return len(self.posts)

    def add(self, posts):
        """新規分だけをアーカイブへ入れ、ディスクにも追記する。"""
        fresh = []
        with self._lock:
            for p in posts:
                pid = p.get("id")
                if not pid or pid in self.posts:
                    continue
                self.posts[pid] = p
                fresh.append(p)
            if fresh:
                self._sorted = None
        if fresh:
            self._append_to_disk(fresh)
        return len(fresh)

    def has(self, pid):
        with self._lock:
            return pid in self.posts

    def _items(self):
        """(時刻, 投稿) の降順リスト。時刻はここで一度だけ解釈して使い回す。"""
        with self._lock:
            if self._sorted is None:
                items = []
                for p in self.posts.values():
                    dt = parse_iso(p.get("createdAt"))
                    if dt is not None:
                        items.append((dt, p))
                items.sort(key=lambda x: x[0], reverse=True)
                self._sorted = items
            return self._sorted

    def newest(self, limit):
        return [p for _, p in self._items()[:limit]]

    def older_than(self, ts, limit, respect_floor=True):
        """ts より古い投稿（境界含む）。respect_floor なら探索済みの範囲だけ。

        先方の並びには、古い投稿のリポストが日付だけ古く混ざることがある。
        探索済みの床より下は「まだ見ていない時代」なので、床を越えて返すと
        ページの下端が飛んでしまう。床より上だけを返す。
        """
        floor = self.floor_ts if (respect_floor and not self.exhausted) else None
        out = []
        for dt, p in self._items():
            if dt > ts:
                continue
            if floor is not None and dt < floor:
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return out

    def headroom(self, ts):
        """ts より古い、探索済み範囲の投稿数。"""
        floor = self.floor_ts if not self.exhausted else None
        n = 0
        for dt, _p in self._items():
            if dt > ts:
                continue
            if floor is not None and dt < floor:
                break
            n += 1
        return n

    def has_older_than(self, ts):
        items = self._items()
        return bool(items) and items[-1][0] < ts

    def reserve_ok(self, ts, limit):
        """ts より古い範囲を「limit 件以上」漏れなく持っているか。

        ページは新しい順に limit 件を切り出すので、各サービスが limit 件の
        余裕を持っていれば、ページの下端まで全サービスの探索が届いている。
        """
        if self.exhausted:
            return True
        return self.headroom(ts) >= limit

    def oldest_ts(self):
        items = self._items()
        return items[-1][0] if items else None

    # -- ディスク

    def _path(self):
        return os.path.join(CACHE_DIR, "%s.jsonl" % self.sid)

    def _append_to_disk(self, posts):
        try:
            with open(self._path(), "a", encoding="utf-8") as f:
                for p in posts:
                    f.write(json.dumps(p, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._appended += len(posts)
        except Exception as e:  # noqa: BLE001 - ディスクが無くても動作は続ける
            logging.warning("archive append %s failed: %s", self.sid, e)

    def load_from_disk(self):
        path = self._path()
        if not os.path.exists(path):
            return
        loaded = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        p = json.loads(line)
                    except Exception:
                        continue  # 書き込み途中で落ちた行は捨てる
                    pid = p.get("id")
                    if pid and pid not in self.posts:
                        self.posts[pid] = p
                        loaded += 1
        except Exception as e:  # noqa: BLE001
            logging.warning("archive load %s failed: %s", self.sid, e)
        if loaded:
            self._sorted = None
            logging.info("archive %s loaded %d posts", self.sid, loaded)

    # -- レート制限

    def take_token(self, min_interval=MIN_INTERVAL, max_wait=1.5):
        """外部APIを叩いてよいか判定。最低間隔はここで待って守る。"""
        with self._rate_lock:
            now = time.time()
            while self._recent and now - self._recent[0] > 60:
                self._recent.popleft()
            if len(self._recent) >= RATE_PER_MIN:
                logging.warning("%s rate limit reached (%d/min)", self.sid, RATE_PER_MIN)
                return False
            wait = self._last_call + min_interval - now
            if wait > max_wait:
                return False
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            self._last_call = now
            self._recent.append(now)
            return True

    # -- 取得

    def deepen(self, want_ts=None, reserve=0, max_pages=1, min_interval=MIN_INTERVAL):
        """アーカイブを古い方向へ伸ばす。want_ts より古い範囲に reserve 件
        たまったら止める（None なら指定ページ数だけ）。"""
        progressed = False
        for _ in range(max_pages):
            if self.exhausted:
                break
            if want_ts is not None and self.reserve_ok(want_ts, reserve):
                break
            if len(self) >= ARCHIVE_CAP:
                self.exhausted = True
                break
            with self._fetch_lock:
                # 待っている間に対応が済んでいるかもしれない
                if self.exhausted or (want_ts is not None and self.reserve_ok(want_ts, reserve)):
                    break
                if not self.take_token(min_interval=min_interval):
                    break
                cursor = self.tail_cursor
                try:
                    posts, nxt = PAGERS[self.sid](cursor)
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)[:200]
                    logging.warning("%s deepen failed: %s", self.sid, e)
                    break
                self.add(posts)
                self.pages += 1
                self.error = None
                if not posts or not nxt or nxt == cursor or nxt in self._used_cursors:
                    self.exhausted = True
                # ページ末尾の時刻＝次に読む位置
                last = parse_iso(posts[-1].get("createdAt")) if posts else None
                if last and (self.floor_ts is None or last < self.floor_ts):
                    self.floor_ts = last
                if nxt and len(self._used_cursors) < 20000:
                    self._used_cursors.add(nxt)
                self.tail_cursor = nxt
                progressed = True
                if not posts:
                    break
        return progressed

    def fill_newest(self):
        """新着を差分で取り込む。既知の投稿に当たったらそこで止める。"""
        with self._fetch_lock:
            if len(self) == 0:
                return self._fill_initial()
            if not self.take_token(min_interval=max(CACHE_TTL_SEC / 10.0, MIN_INTERVAL)):
                return False
            try:
                posts, nxt = PAGERS[self.sid](None)
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:200]
                logging.warning("%s refresh failed: %s", self.sid, e)
                return False
            overlap = any(self.has(p.get("id")) for p in posts)
            self.add(posts)
            self.pages += 1
            self.error = None
            # 既知の投稿に届いていない＝取りこぼしがある。最大2ページだけ追加で追う。
            pages = 1
            while not overlap and nxt and not self.exhausted and pages < 3:
                if not self.take_token():
                    break
                try:
                    posts, nxt = PAGERS[self.sid](nxt)
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)[:200]
                    break
                overlap = any(self.has(p.get("id")) for p in posts)
                self.add(posts)
                self.pages += 1
                pages += 1
                if not posts:
                    break
            return True

    def _fill_initial(self):
        """空のアーカイブを DEPTH まで充填し、続きのカーソルを覚える。"""
        target = DEPTH.get(self.sid, 40)
        cursor = None
        pages = 0
        while len(self) < target and pages < 8:
            if not self.take_token(min_interval=COLD_INTERVAL):
                break
            try:
                posts, nxt = PAGERS[self.sid](cursor)
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:200]
                logging.warning("%s initial fill failed: %s", self.sid, e)
                break
            self.add(posts)
            self.pages += 1
            pages += 1
            self.tail_cursor = nxt
            self.error = None
            if posts:
                last = parse_iso(posts[-1].get("createdAt"))
                if last and (self.floor_ts is None or last < self.floor_ts):
                    self.floor_ts = last
            if not posts or not nxt:
                self.exhausted = True
                break
            cursor = nxt
        return True


ARCHIVE = {sid: Archive(sid) for sid in SERVICE_IDS}
_archive_loaded = False
_archive_load_lock = threading.Lock()
_sync_lock = threading.Lock()
_last_sync = 0.0
_last_save = 0.0


def _state_path():
    return os.path.join(CACHE_DIR, "state.json")


def ensure_archive():
    global _archive_loaded
    with _archive_load_lock:
        if _archive_loaded:
            return
        for sid in SERVICE_IDS:
            ARCHIVE[sid].load_from_disk()
        try:
            if os.path.exists(_state_path()):
                with open(_state_path(), "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for sid in SERVICE_IDS:
                    st = saved.get(sid) or {}
                    a = ARCHIVE[sid]
                    # 投稿本文（jsonl）とカーソル（state）は同じ場所に保存している。
                    # 片方だけ古い場合は、重複取得になるだけで欠落はしない。
                    if st:
                        if st.get("tailCursor") is not None:
                            a.tail_cursor = st["tailCursor"]
                        a.exhausted = bool(st.get("exhausted"))
                        a.floor_ts = parse_iso(st.get("floorTs")) or a.floor_ts
                    if a.floor_ts is None and len(a):
                        # 状態が欠けている古いアーカイブの拾い直し
                        if a.tail_cursor:
                            a.floor_ts = a.oldest_ts()
                        else:
                            top = a.newest(1)
                            a.floor_ts = parse_iso(top[0].get("createdAt")) if top else None
        except Exception as e:  # noqa: BLE001
            logging.warning("state load failed: %s", e)
        _archive_loaded = True
        logging.info("archive ready: %s", {sid: len(ARCHIVE[sid]) for sid in SERVICE_IDS})


def save_state(force=False):
    """カーソルと終端フラグだけを保存する（本文は jsonl に追記済み）。"""
    global _last_save
    now = time.time()
    if not force and now - _last_save < 10:
        return
    _last_save = now
    data = {}
    for sid in SERVICE_IDS:
        a = ARCHIVE[sid]
        with a._lock:
            data[sid] = {
                "tailCursor": a.tail_cursor,
                "exhausted": a.exhausted,
                "count": len(a.posts),
                "floorTs": iso_z(a.floor_ts) if a.floor_ts else None,
            }
    try:
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except Exception as e:  # noqa: BLE001
        logging.warning("state save failed: %s", e)


def sync_newest(force=False):
    """各サービスの新着を差分同期する（最短 CACHE_TTL_SEC 間隔）。"""
    global _last_sync
    now = time.time()
    if not force and now - _last_sync < CACHE_TTL_SEC:
        return
    with _sync_lock:
        if not force and time.time() - _last_sync < 5:
            return
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda sid: ARCHIVE[sid].fill_newest(), SERVICE_IDS))
        _last_sync = time.time()
        save_state()


# ---------------------------------------------------------------- タイムライン


def _sids(service):
    if service in (None, "", "all"):
        return list(SERVICE_IDS)
    return [service]


def _merged(posts):
    """重複を落として新しい順に。"""
    seen = set()
    out = []
    for p in sorted(posts, key=post_sort_key, reverse=True):
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        out.append(p)
    return out


def _errors():
    errs = {}
    for sid in SERVICE_IDS:
        e = ARCHIVE[sid].error
        if e:
            errs[sid] = e
    return errs


def _counts(posts=None):
    if posts is None:
        return {sid: len(ARCHIVE[sid]) for sid in SERVICE_IDS}
    counts = {sid: 0 for sid in SERVICE_IDS}
    for p in posts:
        if p.get("service") in counts:
            counts[p["service"]] += 1
    return counts


def _has_more(sids, next_before_ts):
    if next_before_ts is None:
        return False
    for sid in sids:
        a = ARCHIVE[sid]
        if not a.exhausted:
            return True
        if a.has_older_than(next_before_ts):
            return True
    return False


def resolve_before(raw):
    """before は ISO8601 か投稿ID（Loomix id / 先方 id のどちらでも）。"""
    if raw is None or raw == "":
        return None
    dt = parse_iso(raw)
    if dt:
        return dt
    key = str(raw)
    for sid in SERVICE_IDS:
        a = ARCHIVE[sid]
        with a._lock:
            hit = a.posts.get(key)
            if hit is None:
                for p in a.posts.values():
                    if str(p.get("rawId")) == key:
                        hit = p
                        break
        if hit is not None:
            dt = parse_iso(hit.get("createdAt"))
            if dt:
                return dt
    return None


def newest_page(limit, service):
    sids = _sids(service)
    posts = []
    for sid in sids:
        posts.extend(ARCHIVE[sid].newest(limit))
    posts = _merged(posts)[:limit]
    next_ts = parse_iso(posts[-1].get("createdAt")) if posts else None
    return posts, next_ts


def older_page(ts, limit, service):
    """ts より古い投稿を limit 件。足りなければアーカイブを必要な分だけ伸ばす。

    全サービスが ts より古い範囲を漏れなく持っていないと、欠けた投稿を
    黙って飛ばすことになる。その場合は空ページ＋partial を返し、
    クライアントに少し待って再要求させる（次回は続きから伸ばせる）。
    """
    sids = _sids(service)
    deadline = time.time() + REQUEST_BUDGET_SEC
    for _ in range(MAX_ROUNDS):
        missing = [sid for sid in sids if not ARCHIVE[sid].reserve_ok(ts, limit)]
        if not missing:
            break
        if time.time() >= deadline:
            break
        with ThreadPoolExecutor(max_workers=len(missing)) as executor:
            list(executor.map(
                lambda sid: ARCHIVE[sid].deepen(want_ts=ts, reserve=limit, max_pages=1), missing))

    if any(not ARCHIVE[sid].reserve_ok(ts, limit) for sid in sids):
        save_state()
        return [], True

    posts = []
    for sid in sids:
        posts.extend(ARCHIVE[sid].older_than(ts, limit))
    posts = _merged(posts)[:limit]
    save_state()
    return posts, False


PREFETCH_MIN_GAP = 8.0   # 同じサービスの先取りはこの間隔を空ける


def prefetch(sids, ts, limit):
    """次のページ分の余裕が減ったサービスだけ、裏で1ページ先取りする。

    応答は待たない。取るのは一生で1回のページなので総量は増えない。
    """
    def run():
        for sid in sids:
            a = ARCHIVE[sid]
            if a.exhausted:
                continue
            if a.headroom(ts) >= limit + PAGE_SIZE[sid]:
                continue  # まだ余裕がある
            now = time.time()
            if now - a._last_prefetch < PREFETCH_MIN_GAP:
                continue
            a._last_prefetch = now
            a.deepen(want_ts=ts, reserve=limit + PAGE_SIZE[sid], max_pages=1)

    threading.Thread(target=run, daemon=True).start()


def timeline_page(before_raw, limit, service, force=False):
    ensure_archive()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    if service not in (None, "", "all") and service not in SERVICE_BY_ID:
        raise ValueError("unknown service")

    sids = _sids(service)
    sync_newest(force=force)

    if before_raw:
        ts = resolve_before(before_raw)
        if ts is None:
            raise ValueError("before を解釈できません（ISO8601 か投稿IDを指定してください）")
        posts, partial = older_page(ts, limit, service)
        next_ts = parse_iso(posts[-1].get("createdAt")) if posts else ts
        has_more = _has_more(sids, next_ts)
        if posts and not partial:
            prefetch(sids, next_ts, limit)
    else:
        posts, next_ts = newest_page(limit, service)
        partial = False
        has_more = _has_more(sids, next_ts)
        if posts:
            prefetch(sids, next_ts, limit)

    return {
        "ok": True,
        "version": VERSION,
        "fetchedAt": iso_z(datetime.now(timezone.utc)),
        "total": len(posts),
        "allTotal": sum(len(ARCHIVE[sid]) for sid in sids),
        "serviceCounts": _counts(posts),
        "archiveCounts": {sid: len(ARCHIVE[sid]) for sid in sids},
        "errors": _errors(),
        "services": [SERVICE_BY_ID[sid] for sid in sids],
        "posts": posts,
        "limit": limit,
        "nextBefore": iso_z(next_ts) if next_ts else None,
        "hasMore": has_more,
        "partial": partial,
        "retryAfterMs": 1200 if (partial and not posts) else None,
    }


def timeline_posts_for(service, handle):
    """アーカイブから、そのユーザーの投稿だけ拾う（プロフィールのフォールバック）。"""
    a = ARCHIVE.get(service)
    if not a:
        return []
    out = []
    for p in a.newest(ARCHIVE_CAP):
        author = p.get("author") or {}
        keys = {str(author.get("username") or ""), str(author.get("id") or "")}
        if handle in keys:
            out.append(p)
    return out


# ---------------------------------------------------------------- profile


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


def services_public():
    """連携フォームの仕様（項目名・ヘルプ・登録URL）付きのサービス一覧。"""
    out = []
    for s in SERVICES:
        item = dict(s)
        spec = auth.AUTH_SPECS.get(s["id"])
        if spec:
            item["auth"] = spec
        out.append(item)
    return out


def health():
    ensure_archive()
    age = round(time.time() - _last_sync) if _last_sync else None
    oldest = {sid: ARCHIVE[sid].floor_ts for sid in SERVICE_IDS}
    return {
        "ok": True,
        "version": VERSION,
        "cacheAgeSec": age,
        "total": sum(len(ARCHIVE[sid]) for sid in SERVICE_IDS),
        "serviceCounts": {sid: len(ARCHIVE[sid]) for sid in SERVICE_IDS},
        "archive": {
            sid: {
                "count": len(ARCHIVE[sid]),
                "pages": ARCHIVE[sid].pages,
                "floor": iso_z(oldest[sid]) if oldest[sid] else None,
                "exhausted": ARCHIVE[sid].exhausted,
                "hasCursor": bool(ARCHIVE[sid].tail_cursor),
                "error": ARCHIVE[sid].error,
            }
            for sid in SERVICE_IDS
        },
        "throttle": {
            "minIntervalSec": MIN_INTERVAL,
            "ratePerMin": RATE_PER_MIN,
        },
        "authRequired": auth.required_services(),
    }


def api_dispatch(method, path, query, body, headers, client_ip=""):
    """API の入口（server.py と api/index.py で共有）。

    戻り値は (status, obj, cache_control)。
    """
    def qget(name, default=None):
        v = query.get(name) if query else None
        return v[0] if v else default

    token = auth.bearer_token(headers)

    # -- 認証（公開） -------------------------------------------------

    if path == "/api/auth/status":
        return 200, auth.status(token), "no-store"

    if path == "/api/auth/providers":
        return 200, {"ok": True, "version": VERSION,
                     "required": auth.required_services(),
                     "services": services_public()}, "public, max-age=300"

    if path == "/api/auth/login":
        if method != "POST":
            return 405, {"ok": False, "error": "POST で送ってください"}, "no-store"
        try:
            result = auth.login(body or {}, token=token, client_ip=client_ip)
        except auth.AuthError as e:
            return e.status, {"ok": False, "error": e.message, "code": e.code,
                              "auth": auth.status(token)}, "no-store"
        return 200, dict({"ok": True}, **result), "no-store"

    if path == "/api/auth/unlink":
        if method != "POST":
            return 405, {"ok": False, "error": "POST で送ってください"}, "no-store"
        service = str((body or {}).get("service") or "")
        try:
            result = auth.unlink(token, service)
        except auth.AuthError as e:
            return e.status, {"ok": False, "error": e.message, "code": e.code}, "no-store"
        return 200, dict({"ok": True}, **result), "no-store"

    if path == "/api/auth/logout":
        auth.logout(token)
        return 200, {"ok": True, "auth": auth.status("")}, "no-store"

    if path == "/api/services":
        return 200, {"ok": True, "version": VERSION, "services": services_public()}, \
            "public, max-age=300"

    if path == "/api/health":
        return 200, health(), "no-cache"

    # -- ゲート（4サービスすべて連携していないと開かない） -------------

    gate = auth.status(token)
    if not gate["gateOpen"]:
        return 401, {
            "ok": False,
            "code": "auth_required",
            "error": "タイムラインを開くには、%dつのSNSすべての連携が必要です。" % len(gate["required"]),
            "auth": gate,
        }, "no-store"

    if path == "/api/timeline":
        service = qget("service", "all")
        force = qget("refresh", "0") not in ("", "0", "false")
        try:
            limit = int(qget("limit", str(DEFAULT_LIMIT)) or DEFAULT_LIMIT)
        except Exception:
            limit = DEFAULT_LIMIT
        before = qget("before")
        try:
            result = timeline_page(before, limit, service, force=force)
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}, "no-store"
        except Exception as e:  # noqa: BLE001
            logging.exception("timeline failed")
            return 502, {"ok": False, "error": "タイムラインを取得できませんでした: %s" % e}, "no-store"
        # 過去ページは不変なので短時間キャッシュしてよい
        return 200, result, "private, max-age=20"

    if path == "/api/user":
        service = qget("service", "")
        handle = qget("id", "") or qget("username", "")
        if service not in SERVICE_BY_ID or not handle:
            return 400, {"ok": False, "error": "service と id が必要です"}, "no-store"
        try:
            result = get_profile(service, handle)
        except Exception as e:  # noqa: BLE001
            logging.warning("profile %s/%s failed: %s", service, handle, e)
            result = None
        if not result:
            return 502, {"ok": False, "error": "プロフィールを取得できませんでした"}, "no-store"
        return 200, result, "private, max-age=120"

    return 404, {"ok": False, "error": "not found"}, "no-store"


class LoomixHandler(SimpleHTTPRequestHandler):
    server_version = "Loomix/" + VERSION
    _static_cache = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def end_headers(self):
        if getattr(self, "_static_cache", None):
            self.send_header("Cache-Control", self._static_cache)
        super().end_headers()

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

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            if length > 64 * 1024:
                self._send_json({"ok": False, "error": "リクエストが大きすぎます"}, status=413)
                return
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._send_json({"ok": False, "error": "JSON を解釈できません"}, status=400)
                    return
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        client_ip = forwarded or (self.client_address or ("",))[0]
        status, obj, cache_control = api_dispatch(
            method, path, parse_qs(parsed.query), body, self.headers, client_ip,
        )
        self._send_json(obj, status=status, cache_control=cache_control)

    def do_GET(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._dispatch("GET")
        # 静的ファイルは毎回再検証させる（デプロイ直後の古いCSS/JSを避ける）
        self._static_cache = "no-cache, must-revalidate"
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._dispatch("POST")
        self._send_json({"ok": False, "error": "not found"}, status=404)


if __name__ == "__main__":
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, LoomixHandler)
    httpd.daemon_threads = True
    logging.info("Loomix BFF server %s listening on http://0.0.0.0:%d", VERSION, PORT)
    logging.info("archive dir: %s", CACHE_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("Server stopped.")
