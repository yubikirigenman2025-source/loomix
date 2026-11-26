#!/usr/bin/env python3
"""
Loomix 統合通知 (v0.7)

連携している各SNSの「自分宛ての通知」を、Loomix の1つの一覧にまとめる。

設計の前提:
  - 通知は「その人だけが見られるもの」なので、共有アーカイブ(server.ARCHIVE)には
    一切入れない。取得もブラウザへの中継も、リクエストのたびにその人次第で行う。
  - 取得に使う各SNSのセッション材料(Bearer / Cookie)は safety.py がログイン時に
    メモリだけに持っているものを借りる。ディスクには書かない。Loomix を再起動
    したり保持時間を過ぎると材料は消えるので、その場合は「連携し直す」を案内する
    (needs_relink)。パスワードは受け取らない。
  - 通知を「既読にする」書き込みは一切しない。本家の未読状態を Loomix から
    変えないため、表示だけを行う(isRead は各SNSが返した値をそのまま載せる)。
  - ブロック・ミュートしている相手からの通知は、タイムラインと同じ扱いで隠す
    (safety.Hider を再利用。本家の設定は変更しない)。
  - 結果はユーザーごとに30秒だけメモリにキャッシュする(タブ切替のたびに
    相手先を叩かないため)。

各SNSの通知API(2026-11-27 実測・クライアント解析):
  Karotter   GET  {api}/notifications?page=1&limit=40
             Authorization: Bearer <ログイン応答の accessToken>
             -> {notifications:[{id, type, isRead, createdAt, actor, actors[],
                                 actorCount, post, posts[], postId, postCount,
                                 notificationIds[], reactionLabel}]}
                type: LIKE / REKAROT / REPLY / MENTION / FOLLOW / FOLLOW_REQUEST /
                      FOLLOWED_POST / REACTION / QUOTE / DM / SYSTEM / REPORT_UPDATE /
                      COMMUNITY_*
             未認証は 401 {"error":"認証が必要です"}
  Yudetter   GET  /api/notifications?limit=40      Cookie(Better Auth) か Bearer
             -> {items:[{id, type, read, createdAt, detail, actor{username,
                          displayName, avatarUrl}, yudate{id, content}, actionYudate,
                          itemId}]}
                type: like / reyudate / reply / quote / mention / follow /
                      reaction:<emoji> / super_yudate:<額> / market_* /
                      game_reward_request / badge_application_*
             未認証は 401
  あめ→はれ  GET  /api/notifications?limit=40&bucket=main|sub
             x-amehare-session-token: <sessionToken> ＋ Cookie, Origin: https://am-hr.com
             -> {notifications:[{id, seq, type, createdAt("YYYY-MM-DD HH:MM:SS" JST),
                                 actor{userId, displayName, avatarUrl},
                                 reactionType, reactionActors[], teaActors[],
                                 post{id, body}, replyPost{id, body}, entityKey}]}
                type: reply / repost / reaction(reactionType: heart=お花, tea=お茶) /
                      report(entityKey が "report:")
             未認証は 401
  Pitter     POST /system/openAPI.php?action=notifications   Authorization: Bearer
             -> web版と同じ {data:{notifications:[{type, actor_handle, actor_name,
                actor_icon_url, post_id, post_content, is_read, created_at(unix秒),
                emoji, message}]}} を想定して寛容に解釈する
                type: like / repost / reply / quote / mention / follow /
                      follow_request / reaction / tip / shadowban / poll_ended /
                      post_deleted / dm_group_request
             未認証は 401
  Amiverse   POST /v1/notifications にサインイン済みセッションが必要(401
             「サインインしてください」)。Loomix の連携はID入力のみで認証が無いため
             取得できない → unavailable
"""

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()

KAROTTER_HOST = "https://karotter.com"
KAROTTER_API = "https://api.karotter.com/api"
YUDETTER_HOST = "https://yudetter.com"
AMEHARE_HOST = "https://am-hr.com"
PITTER_HOST = "https://pitter.jp"
AMIVERSE_HOST = "https://amiverse.net"

JST = timezone(timedelta(hours=9))

PER_SERVICE_LIMIT = 40      # 1サービスあたりの取得件数（先方の既定ページに合わせる）
TOTAL_LIMIT = 120           # 1回の応答で返す上限
CACHE_TTL = 30              # 同じユーザーの再表示に使う秒数
FETCH_TIMEOUT = 10
EXCERPT_LEN = 140

SERVICE_ORDER = ["karotter", "yudetter", "amehare", "pitter", "amiverse"]
SERVICE_NAMES = {
    "karotter": ("Karotter", "karotter"),
    "yudetter": ("Yudetter", "yudetter"),
    "amehare": ("あめ→はれ", "am-hr"),
    "pitter": ("Pitter", "pitter"),
    "amiverse": ("Amiverse", "amiverse"),
}

_cache = {}                 # safetyKey -> {"expires": float, "payload": dict}
_locks = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------- 小物

def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return _iso(datetime.now(timezone.utc))


def iso_time(value):
    """各SNSの時刻表現を ISO8601(UTC) に揃える。解釈できなければ None。

    - ISO8601（Z / +09:00 / オフセット無しはUTC扱い）
    - "YYYY-MM-DD HH:MM:SS" と "YYYY/MM/DD HH:MM:SS"（あめ→はれはJST）
    - unix 秒 / ミリ秒（Pitter の created_at）
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() or (text.replace(".", "", 1).isdigit() and len(text) <= 16):
        try:
            return _from_epoch(float(text))
        except Exception:  # noqa: BLE001
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return _iso(datetime.strptime(text, fmt).replace(tzinfo=JST))
        except Exception:  # noqa: BLE001
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _iso(dt)


def _from_epoch(value):
    ts = float(value)
    if ts > 1e12:          # ミリ秒
        ts /= 1000.0
    return _iso(datetime.fromtimestamp(ts, timezone.utc))


def _abs(host, path):
    if not path:
        return ""
    path = str(path).replace("\\/", "/")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("//"):
        return "https:" + path
    if not path.startswith("/"):
        path = "/" + path
    return host + path


def _first(node, keys):
    if not isinstance(node, dict):
        return ""
    for k in keys:
        v = node.get(k)
        if v not in (None, "", False):
            return str(v).strip()
    return ""


def _excerpt(text):
    text = " ".join(str(text or "").split())
    if len(text) <= EXCERPT_LEN:
        return text
    return text[:EXCERPT_LEN].rstrip() + "…"


def _actor(name, handle, avatar="", url=""):
    return {
        "name": str(name or handle or "").strip(),
        "handle": str(handle or "").lstrip("@").strip(),
        "avatar": avatar or "",
        "url": url or "",
    }


def _item(service, raw_id, ntype, label, message, actor, created_at,
          excerpt="", url="", is_read=None):
    name, label_code = SERVICE_NAMES[service]
    return {
        "id": "%s:%s" % (service, raw_id),
        "service": service,
        "serviceName": name,
        "serviceLabel": label_code,
        "type": ntype,
        "label": label,
        "message": message,
        "actor": actor,
        "createdAt": created_at,
        "excerpt": excerpt or "",
        "url": url or actor.get("url") or "",
        "isRead": is_read if isinstance(is_read, bool) else None,
    }


def _request(method, url, headers=None, body=None, timeout=FETCH_TIMEOUT):
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v})
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
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = e.code
    except Exception as e:  # noqa: BLE001
        return 0, None, str(e)[:160]
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        parsed = None
    return status, parsed, None


def _notifications_list(payload, depth=0):
    """応答のどこかにある通知配列を1つ見つける（先方の形が多少違っても拾う）。"""
    if depth > 4 or payload is None:
        return None
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("notifications", "items", "rows", "list", "data", "result"):
        if key in payload:
            got = _notifications_list(payload.get(key), depth + 1)
            if got is not None:
                return got
    return None


def _hider_filter(key, items):
    """ブロック・ミュートしている相手からの通知を落とす（本家の設定は触らない）。"""
    if not items:
        return items, 0
    try:
        import safety
        keep = safety.hider(key)
    except Exception as e:  # noqa: BLE001 - 非表示設定が引けなくても通知は出す
        logging.warning("notification filter unavailable: %s", e)
        return items, 0
    if keep is None:
        return items, 0
    out = []
    hidden = 0
    for item in items:
        actor = item.get("actor") or {}
        pseudo = {
            "service": item.get("service"),
            "author": {"username": actor.get("handle") or "",
                       "id": actor.get("handle") or ""},
            "content": "",
        }
        if keep.keep(pseudo):
            out.append(item)
        else:
            hidden += 1
    return out, hidden


# ---------------------------------------------------------------- Karotter

_KAROTTER_TYPES = {
    "LIKE": ("like", "いいね", "あなたの投稿にいいねしました"),
    "REKAROT": ("repost", "リカロート", "あなたの投稿をリカロートしました"),
    "REPLY": ("reply", "返信", "あなたの投稿に返信しました"),
    "MENTION": ("mention", "メンション", "あなたをメンションしました"),
    "FOLLOW": ("follow", "フォロー", "あなたをフォローしました"),
    "FOLLOW_REQUEST": ("follow", "フォローリクエスト", "あなたにフォローリクエストを送りました"),
    "FOLLOWED_POST": ("other", "新着投稿", "新しい投稿をしました"),
    "REACTION": ("like", "リアクション", "あなたの投稿にリアクションしました"),
    "QUOTE": ("repost", "引用", "あなたの投稿を引用しました"),
    "DM": ("other", "メッセージ", "メッセージを送りました"),
    "SYSTEM": ("other", "お知らせ", "お知らせがあります"),
    "REPORT_UPDATE": ("other", "通報の更新", "通報の対応状況が更新されました"),
    "COMMUNITY_INVITE": ("other", "コミュニティ招待", "コミュニティに招待しました"),
    "COMMUNITY_JOIN": ("other", "コミュニティ参加", "コミュニティに参加しました"),
    "COMMUNITY_REMOVAL": ("other", "コミュニティ退出", "コミュニティから退出しました"),
}


def _karotter(mat):
    token = (mat or {}).get("token") or ""
    if not token:
        return {"status": "needs_relink",
                "error": "Karotterのセッションがありません。連携し直してください。"}
    url = "%s/notifications?page=1&limit=%d" % (KAROTTER_API, PER_SERVICE_LIMIT)
    st, data, err = _request("GET", url, {
        "Authorization": "Bearer " + token,
        "Origin": KAROTTER_HOST,
        "Referer": KAROTTER_HOST + "/notifications",
    })
    if st in (401, 403):
        return {"status": "needs_relink",
                "error": "Karotterのセッションの期限が切れています。連携し直してください。"}
    if st != 200:
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}

    rows = _notifications_list(data) or []
    items = []
    for n in rows:
        if not isinstance(n, dict):
            continue
        actors = n.get("actors")
        if not isinstance(actors, list):
            actors = []
        if isinstance(n.get("actor"), dict):
            actors = [n["actor"]] + [a for a in actors if isinstance(a, dict)]
        actor_node = actors[0] if actors else {}
        handle = _first(actor_node, ("username", "handle"))
        actor = _actor(actor_node.get("displayName") or handle, handle,
                       _abs(KAROTTER_HOST, actor_node.get("avatarUrl")),
                       ("%s/%s" % (KAROTTER_HOST, handle)) if handle else "")

        ntype = str(n.get("type") or "").upper()
        norm, label, message = _KAROTTER_TYPES.get(
            ntype, ("other", n.get("reactionLabel") or "通知", "通知があります"))

        post = n.get("post")
        if not isinstance(post, dict):
            posts = n.get("posts") if isinstance(n.get("posts"), list) else []
            post = posts[0] if posts and isinstance(posts[0], dict) else {}
        excerpt = _excerpt(post.get("content")) if post else ""
        target = ""
        if post.get("id"):
            target = "%s/posts/%s" % (KAROTTER_HOST, post["id"])
        elif n.get("postId"):
            target = "%s/posts/%s" % (KAROTTER_HOST, n["postId"])
        elif actor["url"]:
            target = actor["url"]
        else:
            target = KAROTTER_HOST + "/notifications"

        raw_id = n.get("id")
        if raw_id in (None, ""):
            ids = n.get("notificationIds")
            raw_id = (ids[0] if isinstance(ids, list) and ids else "")
        if raw_id in (None, ""):
            continue
        items.append(_item(
            "karotter", raw_id, norm, label, message, actor,
            iso_time(n.get("createdAt")), excerpt, target,
            n.get("isRead") if isinstance(n.get("isRead"), bool) else None,
        ))
    return {"status": "ok", "items": items}


# ---------------------------------------------------------------- Yudetter

def _yudetter_type(raw, detail, actor_name):
    text = str(raw or "")
    base = text.split(":")[0]
    if text.startswith("super_yudate:"):
        amount = text.split(":", 1)[1] or "0"
        try:
            label = "{:,} YD".format(int(float(amount)))
        except Exception:  # noqa: BLE001
            label = amount
        return ("other", "スーパーユデート", "%s のスーパーユデートを送りました" % label)
    direct = {
        "like": ("like", "いいね", "あなたのユデートにいいねしました"),
        "reyudate": ("repost", "リユデート", "あなたのユデートをリユデートしました"),
        "reply": ("reply", "返信", "あなたのユデートに返信しました"),
        "quote": ("repost", "引用", "あなたのユデートを引用しました"),
        "mention": ("mention", "メンション", "あなたをメンションしました"),
        "follow": ("follow", "フォロー", "あなたをフォローしました"),
        "game_reward_request": ("other", "報酬申請", "ゲームの報酬を申請しました"),
    }.get(base)
    if direct:
        return direct
    if base == "reaction":
        emoji = text.split(":", 1)[1] if ":" in text else ""
        word = (" %s で" % emoji) if emoji else ""
        return ("like", "リアクション", "あなたのユデートに%sリアクションしました" % word)
    if base.startswith("market") or base in ("report",):
        return ("other", "マーケット", "マーケットの通知があります")
    if base.startswith("badge_application"):
        return ("other", "本人マーク", detail or "本人マーク申請の状況が更新されました")
    return ("other", "通知", detail or "通知があります")


def _yudetter(mat):
    mat = mat or {}
    cookie = mat.get("cookie") or ""
    token = mat.get("token") or ""
    if not cookie and not token:
        return {"status": "needs_relink",
                "error": "Yudetterのセッションがありません。連携し直してください。"}
    headers = {"Referer": YUDETTER_HOST + "/notifications"}   # 外部Originは403
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = "Bearer " + token
    st, data, err = _request(
        "GET", "%s/api/notifications?limit=%d" % (YUDETTER_HOST, PER_SERVICE_LIMIT), headers)
    if st in (401, 403):
        return {"status": "needs_relink",
                "error": "Yudetterのセッションの期限が切れています。連携し直してください。"}
    if st != 200:
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}

    rows = _notifications_list(data) or []
    items = []
    for n in rows:
        if not isinstance(n, dict):
            continue
        node = n.get("actor") if isinstance(n.get("actor"), dict) else {}
        name = node.get("displayName") or node.get("username") or ""
        handle = node.get("username") or ""
        actor = _actor(name, handle, _abs(YUDETTER_HOST, node.get("avatarUrl")),
                       ("%s/profile/%s" % (YUDETTER_HOST, handle)) if handle else "")
        norm, label, message = _yudetter_type(n.get("type"), n.get("detail"), actor["name"])

        yudate = n.get("yudate") if isinstance(n.get("yudate"), dict) else {}
        if not yudate and isinstance(n.get("actionYudate"), dict):
            yudate = n["actionYudate"]
        excerpt = _excerpt(yudate.get("content")) if yudate else ""
        if not excerpt and str(n.get("type") or "").startswith("market"):
            excerpt = _excerpt(n.get("detail"))
        target = ""
        if yudate.get("id") not in (None, ""):
            target = "%s/yudate/%s" % (YUDETTER_HOST, yudate["id"])
        elif actor["url"]:
            target = actor["url"]
        else:
            target = YUDETTER_HOST + "/notifications"

        raw_id = n.get("id")
        if raw_id in (None, ""):
            continue
        is_read = None
        if isinstance(n.get("read"), bool):
            is_read = bool(n["read"])
        elif isinstance(n.get("isRead"), bool):
            is_read = bool(n["isRead"])
        items.append(_item("yudetter", raw_id, norm, label, message, actor,
                           iso_time(n.get("createdAt")), excerpt, target, is_read))
    return {"status": "ok", "items": items}


# ---------------------------------------------------------------- あめ→はれ

_AMEHARE_BUCKETS = ("main", "sub")


def _amehare_one(mat, bucket):
    headers = {
        "Origin": AMEHARE_HOST,
        "Referer": AMEHARE_HOST + "/",
        "x-amehare-session-token": mat.get("token") or "",
        "Cookie": mat.get("cookie") or "",
    }
    url = "%s/api/notifications?limit=%d&bucket=%s" % (AMEHARE_HOST, PER_SERVICE_LIMIT, bucket)
    return _request("GET", url, headers)


def _amehare(mat):
    token = (mat or {}).get("token") or ""
    cookie = (mat or {}).get("cookie") or ""
    if not token and not cookie:
        return {"status": "needs_relink",
                "error": "あめ→はれのセッションがありません。連携し直してください。"}

    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {b: ex.submit(_amehare_one, mat, b) for b in _AMEHARE_BUCKETS}
        for bucket in _AMEHARE_BUCKETS:
            results.append((bucket, futures[bucket].result()))

    items = []
    errors = []
    ok_count = 0
    needs_relink = False
    for bucket, (st, data, err) in results:
        if st in (401, 403):
            needs_relink = True
            continue
        if st != 200:
            errors.append("%s: HTTP %s%s" % (bucket, st or "-", (" " + err) if err else ""))
            continue
        ok_count += 1
        rows = _notifications_list(data) or []
        for n in rows:
            if not isinstance(n, dict):
                continue
            node = n.get("actor") if isinstance(n.get("actor"), dict) else {}
            user_id = str(node.get("userId") or "")
            name = node.get("displayName") or user_id
            actor = _actor(name, user_id, _abs(AMEHARE_HOST, node.get("avatarUrl")),
                           ("%s/users/%s" % (AMEHARE_HOST, urllib.parse.quote(user_id)))
                           if user_id else "")

            ntype = str(n.get("type") or "")
            reaction = str(n.get("reactionType") or "")
            entity = str(n.get("entityKey") or "")
            if ntype == "reply":
                norm, label, message = ("reply", "コメント", "あなたの投稿にコメントしました")
            elif ntype == "repost":
                norm, label, message = ("repost", "リポスト", "あなたの投稿をリポストしました")
            elif ntype == "report" or entity.startswith("report:"):
                norm, label, message = ("other", "通報", "あなたの投稿を通報しました")
            elif reaction == "heart":
                norm, label, message = ("like", "お花", "あなたの投稿にお花を送りました")
            elif reaction == "tea":
                norm, label, message = ("like", "お茶", "あなたの投稿にお茶を送りました")
            else:
                norm, label, message = ("other", "リアクション", "リアクションしました")

            post = n.get("replyPost") if ntype == "reply" else n.get("post")
            if not isinstance(post, dict):
                post = n.get("post") if isinstance(n.get("post"), dict) else {}
            excerpt = _excerpt(post.get("body")) if post else ""
            post_id = post.get("id") if post else None
            if post_id in (None, "") and isinstance(n.get("post"), dict):
                post_id = n["post"].get("id")
            target = ("%s/posts/%s" % (AMEHARE_HOST, post_id)) if post_id else actor["url"]
            if not target:
                target = AMEHARE_HOST + "/notifications"

            raw_id = n.get("id") or n.get("seq")
            if raw_id in (None, ""):
                continue
            item = _item("amehare", raw_id, norm, label, message, actor,
                         iso_time(n.get("createdAt")), excerpt, target, None)
            item["bucket"] = bucket
            items.append(item)

    if needs_relink and not ok_count:
        return {"status": "needs_relink",
                "error": "あめ→はれのセッションの期限が切れています。連携し直してください。"}
    status = "ok" if (ok_count == len(_AMEHARE_BUCKETS) and not needs_relink) else (
        "partial" if items else "error")
    return {"status": status, "items": items, "error": "; ".join(errors) or None}


# ---------------------------------------------------------------- Pitter

_PITTER_TYPES = {
    "like": ("like", "いいね", "あなたのピートにいいねしました"),
    "repost": ("repost", "リピート", "あなたのピートをリピートしました"),
    "reply": ("reply", "返信", "あなたのピートに返信しました"),
    "quote": ("repost", "インピート", "あなたのピートをインピートしました"),
    "mention": ("mention", "メンション", "あなたをメンションしました"),
    "follow": ("follow", "フォロー", "あなたをフォローしました"),
    "follow_request": ("follow", "フォローリクエスト", "あなたにフォローリクエストを送りました"),
    "tip": ("other", "投げ銭", "コインを投げ銭しました"),
    "dm_group_request": ("other", "グループ招待", "グループに招待しました"),
    "poll_ended": ("other", "投票終了", "投票が終了しました"),
    "post_deleted": ("other", "投稿の削除", "あなたのピートが削除されました"),
    "shadowban": ("other", "アカウント制限", "アカウントの機能が制限されています"),
}


def _pitter(mat):
    token = (mat or {}).get("token") or ""
    if not token:
        return {"status": "needs_relink",
                "error": "Pitterのセッションがありません。連携し直してください。"}
    st, data, err = _request(
        "POST", PITTER_HOST + "/system/openAPI.php?action=notifications",
        {"Authorization": "Bearer " + token, "Origin": PITTER_HOST,
         "Referer": PITTER_HOST + "/", "X-Requested-With": "XMLHttpRequest"},
        body={},
    )
    if st in (401, 403):
        return {"status": "needs_relink",
                "error": "Pitterのセッションの期限が切れています。連携し直してください。"}
    if st != 200:
        return {"status": "error", "error": "HTTP %s%s" % (st or "-", (" " + err) if err else "")}
    if isinstance(data, dict) and data.get("ok") is False:
        return {"status": "error", "error": str(data.get("error") or "Pitterが通知を返しませんでした")[:160]}

    rows = _notifications_list(data) or []
    items = []
    for n in rows:
        if not isinstance(n, dict):
            continue
        ntype = str(n.get("type") or "")
        handle = str(n.get("actor_handle") or "")
        name = n.get("actor_name") or handle
        actor = _actor(name, handle, _abs(PITTER_HOST, n.get("actor_icon_url")),
                       ("%s/?profile=%s" % (PITTER_HOST, urllib.parse.quote(handle)))
                       if handle else "")

        norm, label, message = _PITTER_TYPES.get(ntype, ("other", "通知", "通知があります"))
        if ntype == "reaction":
            emoji = str(n.get("emoji") or "").strip()
            norm, label = "like", "リアクション"
            message = ("%s でリアクションしました" % emoji) if emoji else "あなたのピートにリアクションしました"
        if ntype in ("shadowban", "poll_ended", "post_deleted"):
            actor = _actor("", "", "", PITTER_HOST + "/")

        excerpt = _excerpt(n.get("post_content")) if n.get("post_content") else ""
        target = ""
        if n.get("post_id") not in (None, ""):
            target = "%s/?post=%s" % (PITTER_HOST, n["post_id"])
        elif actor["url"]:
            target = actor["url"]
        else:
            target = PITTER_HOST + "/"

        raw_id = n.get("id")
        if raw_id in (None, ""):
            raw_id = "%s-%s-%s" % (ntype, handle, n.get("post_id") or n.get("created_at") or "")
        is_read = None
        if n.get("is_read") is not None:
            is_read = str(n.get("is_read")) not in ("0", "False", "false")
        items.append(_item("pitter", raw_id, norm, label, message, actor,
                           iso_time(n.get("created_at")), excerpt, target, is_read))
    return {"status": "ok", "items": items}


# ---------------------------------------------------------------- Amiverse

def _amiverse(mat):
    return {"status": "unavailable",
            "error": "AmiverseはLoomixからの認証に対応していないため、通知を取得できません。"}


FETCHERS = {
    "karotter": _karotter,
    "yudetter": _yudetter,
    "amehare": _amehare,
    "pitter": _pitter,
    "amiverse": _amiverse,
}


# ---------------------------------------------------------------- 統合

def _sort_key(item):
    created = item.get("createdAt")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            return (0, -dt.timestamp())
        except Exception:  # noqa: BLE001
            pass
    return (1, 0.0)


def merge(items):
    """重複を落として新しい順に。時刻の無いものは末尾（同じなら元の順）。"""
    seen = set()
    out = []
    for item in items or []:
        iid = item.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append(item)
    out.sort(key=_sort_key)
    return out[:TOTAL_LIMIT]


def _service_entry(status, items=None, error=None, extra=None):
    entry = {"status": status, "error": error or None, "count": len(items or []),
             "fetchedAt": _now()}
    if extra:
        entry.update(extra)
    return entry


def _lock_for(key):
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _empty(linked):
    return {
        "ok": True,
        "authenticated": True,
        "fetchedAt": _now(),
        "count": 0,
        "unread": 0,
        "hidden": 0,
        "items": [],
        "services": {sid: _service_entry("unlinked") for sid in SERVICE_ORDER},
        "linked": list(linked),
        "cached": False,
        "note": "連携しているSNSがありません。" if linked else None,
    }


def list_notifications(key, linked, force=False):
    """連携しているSNSの通知を、そのユーザーのセッション材料でまとめて取る。

    戻り値は UI がそのまま描ける形:
      {ok, fetchedAt, count, unread, items[], services{sid:{status,error,count}},
       linked[], cached}
    status は ok / partial / needs_relink / unavailable / error / unlinked。
    """
    linked = [sid for sid in SERVICE_ORDER if sid in set(linked or [])]
    if not key:
        return {"ok": True, "authenticated": False, "fetchedAt": _now(), "count": 0,
                "unread": 0, "hidden": 0, "items": [], "services": {}, "linked": [],
                "cached": False, "note": "連携が見つかりません。"}
    if not linked:
        return _empty(linked)

    now = time.time()
    if not force:
        hit = _cache.get(key)
        if hit and hit.get("expires", 0) > now:
            payload = dict(hit["payload"])
            payload["cached"] = True
            return payload

    lock = _lock_for(key)
    with lock:
        if not force:
            hit = _cache.get(key)
            if hit and hit.get("expires", 0) > now:
                payload = dict(hit["payload"])
                payload["cached"] = True
                return payload

        import safety
        materials = safety.materials(key)
        services = {sid: _service_entry("unlinked") for sid in SERVICE_ORDER}
        targets = []
        for sid in linked:
            if sid == "amiverse":
                continue  # 認証が無いので、下で unavailable として扱う
            material = materials.get(sid)
            if not material:
                services[sid] = _service_entry(
                    "needs_relink",
                    error="このSNSの通知取得には連携し直しが必要です（Loomixはセッションを保存しません）。")
                continue
            targets.append((sid, material))

        results = {}
        if targets:
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
                futures = {sid: ex.submit(_fetch_one, sid, material) for sid, material in targets}
                for sid, fut in futures.items():
                    results[sid] = fut.result()

        all_items = []
        for sid in linked:
            if sid == "amiverse":
                services[sid] = _service_entry(
                    "unavailable",
                    error="AmiverseはLoomixからの認証に対応していないため、通知を取得できません。")
                continue
            got = results.get(sid)
            if got is None:
                continue
            status = str(got.get("status") or "error")
            items = got.get("items") or []
            if status in ("ok", "partial"):
                try:
                    safety.touch_material(key, sid)
                except Exception:  # noqa: BLE001
                    pass
            services[sid] = _service_entry(status, items, got.get("error"))
            all_items.extend(items)

        merged = merge(all_items)
        merged, hidden = _hider_filter(key, merged)
        payload = {
            "ok": True,
            "authenticated": True,
            "fetchedAt": _now(),
            "count": len(merged),
            "unread": sum(1 for it in merged if it.get("isRead") is False),
            "hidden": hidden,
            "items": merged,
            "services": services,
            "linked": linked,
            "cached": False,
        }
        _cache[key] = {"expires": time.time() + CACHE_TTL, "payload": payload}
        return payload


def _fetch_one(sid, material):
    try:
        got = FETCHERS[sid](material)
    except Exception as e:  # noqa: BLE001 - 1つのSNSの失敗で全体を止めない
        logging.warning("notifications %s failed: %s", sid, e)
        return {"status": "error", "error": str(e)[:160], "items": []}
    if not isinstance(got, dict):
        return {"status": "error", "error": "unexpected response", "items": []}
    return got


def forget(key):
    """ログアウト時に、そのユーザーの通知キャッシュを捨てる。"""
    if not key:
        return
    _cache.pop(key, None)
