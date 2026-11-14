#!/usr/bin/env python3
"""
Loomix ブロック・ミュート反映 (v0.6)

連携している各SNSの「ブロック」「ミュート」設定を、Loomix のタイムラインへ反映する。

設計の前提:
  - 共有アーカイブ(server.ARCHIVE / .cache/*.jsonl)は誰の設定でも書き換えない。
    ここが持つのは「そのユーザーの非表示対象」だけで、応答を組み立てる段階で
    投稿を間引く。他のユーザーや共有アーカイブには一切影響しない。
  - 対象リストはログイン時に取得し、safety.json に保存する。以後は一定間隔
    (SAFETY_TTL) でバックグラウンド更新する。
  - 更新に使う各SNSのセッション(Bearer/Cookie)はメモリだけに置き、ディスクへは
    書かない。Render の再起動で消えた場合は、最後に取れたリストのまま動く
    （フィルタは効き続ける／更新だけが次のログインまで止まる）。
  - サービス側のAPI仕様上どうしても取れないものは、status と error に理由を残す。

各サービスの取得方法（2026-11-14 実測・クライアント解析）:
  Karotter   GET  {api}/follow/block          -> {users:[{id,username,displayName,...}]}
             GET  {api}/follow/mute           -> {users:[...]}
             GET  {api}/auth/me               -> {user:{...,mutedKeywords:[...]}}
             Authorization: Bearer <accessToken>（ログイン応答の accessToken）
  Yudetter   GET  /api/users/me/blocks        -> （要ログイン。形は未実測なので寛容に解釈）
             Cookie(Better Auth) か Authorization: Bearer
             ミュート機能そのものが無い（UI/バンドルに「ミュート」0件）
  あめ→はれ  GET  /api/safety                 -> {blockedUsers:[{userId,...}],
                                                  mutedUsers:[...], muteWords:[{id,word,...}]}
             x-amehare-session-token: <sessionToken>＋Cookie, Origin: https://am-hr.com
  Pitter     POST /system/openAPI.php?action=me (Bearer)
             -> 応答に blocked_handles が含まれる場合だけ拾う（無ければ取得不可と記録）
             投稿単位のミュート機能は無い（DMのミュートのみ）
  Amiverse   Loomix の連携はID入力のみで認証が無いため、ユーザー固有の設定は取得不可
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()

KAROTTER_HOST = "https://karotter.com"
KAROTTER_API = "https://api.karotter.com/api"
YUDETTER_HOST = "https://yudetter.com"
AMEHARE_HOST = "https://am-hr.com"
PITTER_HOST = "https://pitter.jp"
AMIVERSE_HOST = "https://amiverse.net"

SAFETY_TTL = 600            # これより古いリストは裏で更新する（秒）
MATERIAL_TTL = 60 * 60 * 6  # セッション材料をメモリに置いておく時間
FETCH_TIMEOUT = 8

_CACHE_DIR = None
_lock = threading.RLock()
_store = None               # key -> entry
_material = {}              # key -> {sid: {"data":..., "expires":...}}
_inflight = set()


# ---------------------------------------------------------------- 保存

def configure(cache_dir):
    global _CACHE_DIR
    _CACHE_DIR = cache_dir


def _dir():
    if _CACHE_DIR:
        return _CACHE_DIR
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "loomix-safety")
    os.makedirs(d, exist_ok=True)
    return d


def _path():
    return os.path.join(_dir(), "safety.json")


def _iso(ts=None):
    return datetime.fromtimestamp(float(ts or time.time()), timezone.utc) \
        .isoformat().replace("+00:00", "Z")


def _load():
    global _store
    with _lock:
        if _store is not None:
            return _store
        _store = {}
        try:
            if os.path.exists(_path()):
                with open(_path(), "r", encoding="utf-8") as f:
                    saved = json.load(f)
                keys = saved.get("keys") if isinstance(saved, dict) else None
                if isinstance(keys, dict):
                    _store = {k: v for k, v in keys.items() if isinstance(v, dict)}
        except Exception as e:  # noqa: BLE001 - 壊れていても動作は続ける
            logging.warning("safety store load failed: %s", e)
        return _store


def _save():
    with _lock:
        if _store is None:
            return
        try:
            tmp = _path() + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "keys": _store}, f, ensure_ascii=False)
            os.replace(tmp, _path())
        except Exception as e:  # noqa: BLE001
            logging.warning("safety store save failed: %s", e)


# ---------------------------------------------------------------- HTTP

def _request(method, url, headers=None, body=None, timeout=FETCH_TIMEOUT):
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as res:
            raw = res.read()
            status = res.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except Exception as e:  # noqa: BLE001
        return 0, None, str(e)
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return status, parsed, None


# ---------------------------------------------------------------- 解釈

_HANDLE_KEYS = ("username", "handle", "user_handle", "name_id", "userid", "userId",
                "screen_name", "user_name")
_ID_KEYS = ("id", "userId", "user_id", "aid", "account_id", "accountId")


def _first(node, keys):
    for k in keys:
        v = node.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _norm_user(item):
    """1件のユーザー表現を {handle, id} に。どのサービスでも同じ形で扱う。"""
    if isinstance(item, str):
        h = item.strip().lstrip("@")
        return {"handle": h.lower(), "id": ""} if h else None
    if not isinstance(item, dict):
        return None
    node = item
    for k in ("user", "account", "blocked", "muted", "target", "blockedUser", "mutedUser"):
        if isinstance(item.get(k), dict):
            node = item[k]
            break
    handle = _first(node, _HANDLE_KEYS) or _first(item, _HANDLE_KEYS)
    uid = _first(node, _ID_KEYS) or _first(item, _ID_KEYS)
    if handle:
        handle = handle.lstrip("@").lower()
    if not handle and not uid:
        return None
    return {"handle": handle, "id": uid.lower()}


_LIST_KEYS = ("users", "items", "data", "list", "results", "blocked", "muted",
              "blockedUsers", "mutedUsers", "blocks", "mutes", "blocking", "rows")


def _user_list(payload, keys=_LIST_KEYS, depth=0):
    """payload からユーザー配列らしいものを1つ見つけて正規化する。"""
    if depth > 4 or payload is None:
        return []
    if isinstance(payload, list):
        out = [u for u in (_norm_user(x) for x in payload) if u]
        return out
    if not isinstance(payload, dict):
        return []
    for k in keys:
        if k in payload:
            got = _user_list(payload.get(k), keys, depth + 1)
            if got:
                return got
    # 直下にユーザーらしいdictが並んでいる場合
    direct = [u for u in (_norm_user(payload.get(k)) for k in keys if isinstance(payload.get(k), dict)) if u]
    if direct:
        return direct
    for v in payload.values():
        if isinstance(v, (dict, list)):
            got = _user_list(v, keys, depth + 1)
            if got:
                return got
    return []


def _deep_find(payload, key, depth=0):
    if depth > 5 or payload is None:
        return None
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for v in payload.values():
            got = _deep_find(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(payload, list):
        for v in payload:
            got = _deep_find(v, key, depth + 1)
            if got is not None:
                return got
    return None


def _words(raw):
    out = []
    for item in raw or []:
        if isinstance(item, str):
            w = item.strip()
        elif isinstance(item, dict):
            w = _first(item, ("word", "keyword", "text", "value", "term"))
        else:
            w = ""
        if w and w not in out:
            out.append(w)
    return out[:200]


def _dedupe(users):
    seen = set()
    out = []
    for u in users or []:
        k = (u.get("handle") or "", u.get("id") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out[:500]


# ---------------------------------------------------------------- 各サービスの取得

def _karotter(material, handle):
    token = (material or {}).get("token") or ""
    if not token:
        return {"status": "needs_relink", "error": "Karotterのセッションがありません。連携し直してください。"}
    h = {"Authorization": "Bearer " + token, "Origin": KAROTTER_HOST,
         "Referer": KAROTTER_HOST + "/"}
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_block = ex.submit(_request, "GET", KAROTTER_API + "/follow/block", h)
        f_mute = ex.submit(_request, "GET", KAROTTER_API + "/follow/mute", h)
        f_me = ex.submit(_request, "GET", KAROTTER_API + "/auth/me", h)
        st_b, block, err_b = f_block.result()
        st_m, mute, err_m = f_mute.result()
        st_me, me, _err_me = f_me.result()

    errors = []
    blocked, muted = [], []
    if st_b == 200:
        blocked = _dedupe(_user_list(block))
    else:
        errors.append("block: HTTP %s%s" % (st_b or "-", (" " + err_b) if err_b else ""))
    if st_m == 200:
        muted = _dedupe(_user_list(mute))
    else:
        errors.append("mute: HTTP %s%s" % (st_m or "-", (" " + err_m) if err_m else ""))
    words = _words(_deep_find(me, "mutedKeywords")) if st_me == 200 else []
    if st_me not in (200,):
        errors.append("me: HTTP %s" % (st_me or "-"))
    if st_b == 401 or st_m == 401:
        return {"status": "needs_relink", "blocked": blocked, "muted": muted, "words": words,
                "error": "Karotterのセッションの期限が切れています。連携し直してください。"}
    status = "ok" if (st_b == 200 and st_m == 200) else ("partial" if (blocked or muted or words) else "error")
    return {"status": status, "blocked": blocked, "muted": muted, "words": words,
            "error": "; ".join(errors) or None}


def _yudetter(material, handle):
    material = material or {}
    cookie = material.get("cookie") or ""
    token = material.get("token") or ""
    h = {"Referer": YUDETTER_HOST + "/"}   # 外部 Origin を付けると 403 になる
    if cookie:
        h["Cookie"] = cookie
    if token:
        h["Authorization"] = "Bearer " + token
    st, data, err = _request("GET", YUDETTER_HOST + "/api/users/me/blocks", h)
    if st == 401:
        return {"status": "needs_relink", "error": "Yudetterのセッションの期限が切れています。連携し直してください。"}
    if st != 200:
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}
    blocked = _dedupe(_user_list(data, keys=("users", "blocks", "blockedUsers", "items", "data", "list", "blocking")))
    # 応答の形が想定と違っても、投稿者を拾えるだけ拾う（表示漏れより安全側）
    return {"status": "ok", "blocked": blocked, "muted": [], "words": [],
            "error": None if blocked or data is not None else "応答を解釈できませんでした"}


def _amehare(material, handle):
    token = (material or {}).get("token") or ""
    cookie = (material or {}).get("cookie") or ""
    if not token and not cookie:
        return {"status": "needs_relink", "error": "あめ→はれのセッションがありません。連携し直してください。"}
    h = {"Origin": AMEHARE_HOST, "Referer": AMEHARE_HOST + "/"}
    if token:
        h["x-amehare-session-token"] = token
    if cookie:
        h["Cookie"] = cookie
    st, data, err = _request("GET", AMEHARE_HOST + "/api/safety", h)
    if st in (401, 403):
        return {"status": "needs_relink", "error": "あめ→はれのセッションの期限が切れています。連携し直してください。"}
    if st != 200 or not isinstance(data, dict):
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}
    blocked = _dedupe(_user_list(data.get("blockedUsers")))
    muted = _dedupe(_user_list(data.get("mutedUsers")))
    words = _words(data.get("muteWords"))
    return {"status": "ok", "blocked": blocked, "muted": muted, "words": words, "error": None}


def _pitter(material, handle):
    token = (material or {}).get("token") or ""
    if not token:
        return {"status": "needs_relink", "error": "Pitterのセッションがありません。連携し直してください。"}
    h = {"Authorization": "Bearer " + token, "Origin": PITTER_HOST,
         "Referer": PITTER_HOST + "/", "X-Requested-With": "XMLHttpRequest"}
    st, data, err = _request("POST", PITTER_HOST + "/system/openAPI.php?action=me", h, body={})
    if st in (401, 403):
        return {"status": "needs_relink", "error": "Pitterのセッションの期限が切れています。連携し直してください。"}
    if st != 200:
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}
    handles = _deep_find(data, "blocked_handles")
    if handles is None:
        handles = _deep_find(data, "blockedHandles")
    muted_handles = _deep_find(data, "muted_handles") or _deep_find(data, "mutedHandles")
    if handles is None and muted_handles is None:
        return {"status": "unavailable",
                "error": "Pitterのブロック一覧は公式openAPIに公開されていません（WebのセッションAPIのみ）。"}
    return {"status": "ok",
            "blocked": _dedupe(_user_list(handles)),
            "muted": _dedupe(_user_list(muted_handles)),
            "words": [], "error": None}


def _amiverse(material, handle):
    return {"status": "unavailable",
            "error": "AmiverseはLoomixからのAPI認証に対応していないため、ブロック・ミュート設定を取得できません。"}


FETCHERS = {
    "karotter": _karotter,
    "yudetter": _yudetter,
    "amehare": _amehare,
    "pitter": _pitter,
    "amiverse": _amiverse,
}


def _fetch(sid, material, handle):
    try:
        got = FETCHERS[sid](material, handle)
    except Exception as e:  # noqa: BLE001 - 相手先の想定外応答でTLを止めない
        logging.warning("safety fetch %s failed: %s", sid, e)
        return {"status": "error", "error": str(e)[:200]}
    got.setdefault("blocked", [])
    got.setdefault("muted", [])
    got.setdefault("words", [])
    return got


# ---------------------------------------------------------------- 公開API

def register(key, service, material, handle="", user_id=""):
    """ログイン直後に呼ぶ。そのサービスのブロック・ミュートを取得して保存する。"""
    if not key or service not in FETCHERS:
        return None
    store = _load()
    with _lock:
        entry = store.get(key) or {"createdAt": _iso(), "services": {}}
        store[key] = entry
        item = entry["services"].get(service) or {}
        item["handle"] = handle or item.get("handle") or ""
        item["userId"] = str(user_id or item.get("userId") or "")
        entry["services"][service] = item
    if material:
        with _lock:
            _material[key] = dict(_material.get(key) or {})
            _material[key][service] = {"data": material, "expires": time.time() + MATERIAL_TTL}

    got = _fetch(service, material or {}, handle)
    item.update({
        "fetchedAt": _iso(),
        "status": got.get("status") or "ok",
        "error": got.get("error"),
        "blocked": got.get("blocked") or [],
        "muted": got.get("muted") or [],
        "words": got.get("words") or [],
    })
    entry["updatedAt"] = _iso()
    _save()
    logging.info("safety %s: status=%s blocked=%d muted=%d words=%d",
                 service, item.get("status"), len(item.get("blocked") or []),
                 len(item.get("muted") or []), len(item.get("words") or []))
    return _public_item(item)


def drop(key, service):
    """連携を外したサービスぶんの非表示設定を消す。"""
    if not key:
        return
    store = _load()
    with _lock:
        entry = store.get(key)
        if entry:
            entry.get("services", {}).pop(service, None)
            if not entry.get("services"):
                store.pop(key, None)
            _save()
        mat = _material.get(key)
        if mat:
            mat.pop(service, None)


def forget(key):
    """ログアウト時に呼ぶ。保存した非表示設定とセッション材料を消す。"""
    if not key:
        return
    store = _load()
    with _lock:
        store.pop(key, None)
        _material.pop(key, None)
        _save()


def _public_item(item):
    return {
        "status": item.get("status") or "unknown",
        "error": item.get("error") or None,
        "fetchedAt": item.get("fetchedAt"),
        "blocked": len(item.get("blocked") or []),
        "muted": len(item.get("muted") or []),
        "words": len(item.get("words") or []),
    }


def summary(key):
    store = _load()
    with _lock:
        entry = store.get(key) or {}
        services = entry.get("services") or {}
        out = {sid: _public_item(item) for sid, item in services.items()}
        has_material = bool(_material.get(key))
    counts = {sid: int(item.get("blocked", 0)) + int(item.get("muted", 0)) + int(item.get("words", 0))
              for sid, item in out.items()}
    return {
        "ok": True,
        "active": any(counts.values()),
        "services": out,
        "refreshedAt": entry.get("updatedAt"),
        "canRefresh": has_material,
    }


def refresh(key, force=True):
    """保存済みセッション材料で全サービスを取り直す。"""
    if not key:
        return {"ok": False, "code": "no_key", "error": "連携が見つかりません。"}
    store = _load()
    with _lock:
        entry = store.get(key) or {}
        services = dict(entry.get("services") or {})
        mats = dict(_material.get(key) or {})
    targets = [sid for sid in services if mats.get(sid)]
    if not targets:
        return {"ok": False, "code": "needs_relink",
                "error": "更新には各SNSへの再ログインが必要です（Loomixはセッションを保存しません）。",
                "safety": summary(key)}
    results = {}
    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        futs = {sid: ex.submit(_fetch, sid, (mats.get(sid) or {}).get("data") or {},
                               (services.get(sid) or {}).get("handle") or "")
                for sid in targets}
        for sid, fut in futs.items():
            got = fut.result()
            item = services.get(sid) or {}
            item.update({
                "fetchedAt": _iso(),
                "status": got.get("status") or "ok",
                "error": got.get("error"),
                "blocked": got.get("blocked") or [],
                "muted": got.get("muted") or [],
                "words": got.get("words") or [],
            })
            services[sid] = item
            results[sid] = _public_item(item)
    with _lock:
        entry = store.get(key) or {}
        entry["services"] = services
        entry["updatedAt"] = _iso()
        store[key] = entry
    _save()
    return {"ok": True, "services": results, "safety": summary(key)}


def _stale(key):
    store = _load()
    with _lock:
        entry = store.get(key) or {}
        services = entry.get("services") or {}
        mats = dict(_material.get(key) or {})
    now = time.time()
    for sid, item in services.items():
        if not mats.get(sid):
            continue
        mat = mats[sid]
        if mat.get("expires", 0) < now:
            continue
        try:
            ts = datetime.fromisoformat(str(item.get("fetchedAt") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            return True
        if now - ts > SAFETY_TTL:
            return True
    return False


def maybe_refresh(key):
    """古くなっていれば裏で更新する（応答は待たせない）。"""
    if not key:
        return
    if not _stale(key):
        return
    with _lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def run():
        try:
            refresh(key)
        except Exception as e:  # noqa: BLE001
            logging.warning("safety refresh failed: %s", e)
        finally:
            with _lock:
                _inflight.discard(key)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- フィルタ

class Hider:
    """1ユーザー分のブロック・ミュートで投稿を間引く。共有アーカイブは触らない。"""

    def __init__(self, services):
        self.sets = {}
        for sid, item in (services or {}).items():
            handles, ids = set(), set()
            for u in (item.get("blocked") or []) + (item.get("muted") or []):
                h = str(u.get("handle") or "").strip().lstrip("@").lower()
                i = str(u.get("id") or "").strip().lower()
                if h:
                    handles.add(h)
                if i:
                    ids.add(i)
            words = []
            for w in item.get("words") or []:
                w = str(w or "").strip().lower()
                if w and w not in words:
                    words.append(w)
            if not (handles or ids or words):
                continue
            self.sets[sid] = {
                "handles": handles,
                "ids": ids,
                "words": words[:200],
                "self": str(item.get("handle") or "").strip().lstrip("@").lower(),
            }
        self.active = bool(self.sets)
        self.hidden = 0
        self._count_lock = threading.Lock()

    def _inc(self):
        with self._count_lock:
            self.hidden += 1

    def __call__(self, post):
        return self.keep(post)

    def keep(self, post):
        sid = (post or {}).get("service")
        s = self.sets.get(sid)
        if not s:
            return True
        author = post.get("author") or {}
        h = str(author.get("username") or "").strip().lstrip("@").lower()
        i = str(author.get("id") or "").strip().lower()
        if h and h in s["handles"]:
            self._inc()
            return False
        if i and i in s["ids"]:
            self._inc()
            return False
        if s["words"] and h != s["self"]:
            text = str(post.get("content") or "").lower()
            if text:
                for w in s["words"]:
                    if w in text:
                        self._inc()
                        return False
        return True


def hider(key):
    if not key:
        return None
    store = _load()
    with _lock:
        entry = store.get(key) or {}
        services = dict(entry.get("services") or {})
    if not services:
        return None
    h = Hider(services)
    return h if h.active else None
