#!/usr/bin/env python3
"""
Loomix 連携認証 (v0.4)

- 各SNSのログイン画面と同じ経路で、その人のアカウントの資格情報を検証する。
- パスワードは検証にだけ使い、保存しない（メモリにも残さない）。
- 成功したら Loomix のセッショントークン（HMAC 署名）を発行する。
  「どのSNSの・どのアカウントを・いつ連携したか」はトークン自身に入れて
  ブラウザに持たせ、サーバーには何も残さない（再起動でディスクが消える環境でも
  ログイン状態が続くように）。
- タイムラインは、必要なSNSすべての連携が揃うまで開かない（ゲート）。

各SNSの認証仕様（実測）:
  Karotter   POST https://api.karotter.com/api/auth/login {identifier, password}
             -> 200 {accessToken, user{id, username, displayName, avatarUrl}}
  Yudetter   POST https://yudetter.com/api/auth/sign-in/email {email, password}
             Origin: https://yudetter.com が無いと 403（許可されていないアクセス元です）
             -> {token, user{username, displayName, avatarUrl}}
  あめ→はれ  POST https://am-hr.com/api/auth/login {identifier, password}
             Origin: https://am-hr.com が無いと 403（Cross-site request was blocked.）
             -> {sessionToken, user...}
  Pitter     POST https://pitter.jp/system/openAPI.php?action=login {email, password, api_key}
             -> {ok:true, token} / 以後 Authorization: Bearer {token}
"""

import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

VERSION = "0.4"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()

KAROTTER_HOST = "https://karotter.com"
KAROTTER_API = "https://api.karotter.com/api"
YUDETTER_HOST = "https://yudetter.com"
AMEHARE_HOST = "https://am-hr.com"
PITTER_HOST = "https://pitter.jp"

SESSION_TTL_SEC = 60 * 60 * 24 * 30          # 30日（使うたびに延びる）
LOGIN_WINDOW_SEC = 300
LOGIN_MAX_PER_WINDOW = 15                    # 同一IPからのログイン試行

# タイムラインを開ける条件。既定は4サービスすべて。
# 運用で絞りたいときだけ LOOMIX_AUTH_REQUIRED=karotter,pitter のように指定する。
DEFAULT_REQUIRED = ["karotter", "yudetter", "amehare", "pitter"]


class AuthError(Exception):
    def __init__(self, message, status=400, code="auth_failed"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ---------------------------------------------------------------- 入力仕様

AUTH_SPECS = {
    "karotter": {
        "title": "Karotter",
        "host": "karotter.com",
        "fields": [
            {"key": "identifier", "label": "メールアドレスまたはユーザー名",
             "type": "text", "autocomplete": "username"},
            {"key": "password", "label": "パスワード",
             "type": "password", "autocomplete": "current-password"},
        ],
        "help": "Karotterのログイン画面と同じ情報です。",
        "signupUrl": "https://karotter.com/register",
        "registerNote": "Karotterで登録",
    },
    "yudetter": {
        "title": "Yudetter",
        "host": "yudetter.com",
        "fields": [
            {"key": "identifier", "label": "メールアドレス",
             "type": "text", "autocomplete": "username"},
            {"key": "password", "label": "パスワード",
             "type": "password", "autocomplete": "current-password"},
        ],
        "help": "Yudetterのログインはメールアドレスで行います（ユーザーIDでは入れません）。",
        "signupUrl": "https://yudetter.com/sign-up",
        "registerNote": "Yudetterで登録",
    },
    "amehare": {
        "title": "あめ→はれ",
        "host": "am-hr.com",
        "fields": [
            {"key": "identifier", "label": "ユーザーIDまたはメールアドレス",
             "type": "text", "autocomplete": "username"},
            {"key": "password", "label": "パスワード",
             "type": "password", "autocomplete": "current-password"},
        ],
        "help": "あめ→はれのログイン画面と同じ情報です。",
        "signupUrl": "https://am-hr.com/register",
        "registerNote": "あめ→はれで登録",
    },
    "pitter": {
        "title": "Pitter",
        "host": "pitter.jp",
        "fields": [
            {"key": "email", "label": "メールアドレス",
             "type": "text", "autocomplete": "email"},
            {"key": "password", "label": "パスワード",
             "type": "password", "autocomplete": "current-password"},
            {"key": "apiKey", "label": "APIキー（64桁）",
             "type": "password", "autocomplete": "off"},
        ],
        "help": "Pitterの「設定 → APIキー」でAPIキーを発行し、貼り付けてください（公式openAPIの仕様）。",
        "signupUrl": "https://pitter.jp/",
        "registerNote": "Pitterを開く",
    },
}

SERVICE_ORDER = ["karotter", "yudetter", "amehare", "pitter"]


def required_services():
    raw = os.environ.get("LOOMIX_AUTH_REQUIRED")
    if raw is None or raw.strip() == "":
        return list(DEFAULT_REQUIRED)
    ids = [s.strip() for s in raw.split(",") if s.strip()]
    ids = [s for s in ids if s in AUTH_SPECS]
    return ids or list(DEFAULT_REQUIRED)


# ---------------------------------------------------------------- HTTP

def _request(method, url, body=None, headers=None, timeout=18, cookie_jar=None):
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)

    handlers = [urllib.request.HTTPSHandler(context=SSL_CTX)]
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            resp_headers = resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        resp_headers = e.headers
    except Exception as e:  # noqa: BLE001 - ネットワークは何でも起きる
        raise AuthError("連携先に接続できませんでした（%s）" % e, 502, "unreachable")

    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return status, parsed, resp_headers, text


def _json(status, data):
    return data if isinstance(data, dict) else {}


def _pitter_hex(value):
    return bool(re.match(r"^[0-9a-fA-F]{64}$", (value or "").strip()))


def _avatar(host, path):
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


def _identity(service, handle, display_name, avatar_url, profile_url, user_id=""):
    return {
        "service": service,
        "handle": str(handle or "").lstrip("@"),
        "displayName": display_name or handle or "",
        "avatarUrl": avatar_url or "",
        "profileUrl": profile_url or "",
        "userId": str(user_id or ""),
        "linkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------- 各SNSの検証

def _verify_karotter(creds):
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    status, data, _h, _t = _request(
        "POST", KAROTTER_API + "/auth/login",
        {"identifier": creds["identifier"], "password": creds["password"]},
        {"Origin": KAROTTER_HOST, "Referer": KAROTTER_HOST + "/login"},
        cookie_jar=jar,
    )
    data = _json(status, data)
    if status == 429:
        raise AuthError("Karotterが混み合っています。少し待ってやり直してください。", 429, "rate_limited")
    if status != 200:
        msg = data.get("error") or "Karotterにログインできませんでした。"
        raise AuthError(msg, 401, "invalid_credentials")
    user = data.get("user") or {}
    handle = user.get("username") or creds["identifier"]
    return _identity(
        "karotter", handle, user.get("displayName") or handle,
        _avatar(KAROTTER_HOST, user.get("avatarUrl")),
        "%s/%s" % (KAROTTER_HOST, handle), user.get("id"),
    )


def _verify_yudetter(creds):
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    status, data, _h, _t = _request(
        "POST", YUDETTER_HOST + "/api/auth/sign-in/email",
        {"email": creds["identifier"], "password": creds["password"]},
        {"Origin": YUDETTER_HOST, "Referer": YUDETTER_HOST + "/"},
        cookie_jar=jar,
    )
    data = _json(status, data)
    if status == 403 and "アクセス元" in str(data.get("error", "")):
        raise AuthError("Yudetterがこのサーバーからのログインを受け付けませんでした。", 502, "service_blocked")
    if status != 200:
        msg = data.get("message") or data.get("error") or "Yudetterにログインできませんでした。"
        raise AuthError(msg, 401, "invalid_credentials")
    user = data.get("user") or {}
    if not user:
        token = data.get("token")
        hdrs = {"Origin": YUDETTER_HOST, "Referer": YUDETTER_HOST + "/"}
        if token:
            hdrs["Authorization"] = "Bearer " + token
        st2, me, _h2, _t2 = _request("GET", YUDETTER_HOST + "/api/users/me", headers=hdrs, cookie_jar=jar)
        if st2 == 200:
            me = _json(st2, me)
            user = me.get("user") or me
    handle = user.get("username") or creds["identifier"]
    return _identity(
        "yudetter", handle, user.get("displayName") or handle,
        _avatar(YUDETTER_HOST, user.get("avatarUrl")),
        "%s/%s" % (YUDETTER_HOST, handle), user.get("id"),
    )


def _verify_amehare(creds):
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    status, data, _h, _t = _request(
        "POST", AMEHARE_HOST + "/api/auth/login",
        {"identifier": creds["identifier"], "password": creds["password"]},
        {"Origin": AMEHARE_HOST, "Referer": AMEHARE_HOST + "/"},
        cookie_jar=jar,
    )
    data = _json(status, data)
    if status == 403:
        raise AuthError("あめ→はれがこのサーバーからのログインを受け付けませんでした。", 502, "service_blocked")
    if status != 200:
        msg = data.get("error") or "あめ→はれにログインできませんでした。"
        raise AuthError(msg, 401, "invalid_credentials")
    token = data.get("sessionToken") or (data.get("session") or {}).get("token")
    user = data.get("me") or data.get("user") or (data.get("session") or {}).get("user") or {}
    if not user and token:
        st2, ses, _h2, _t2 = _request(
            "GET", AMEHARE_HOST + "/api/auth/session",
            headers={"Origin": AMEHARE_HOST, "Referer": AMEHARE_HOST + "/",
                     "x-amehare-session-token": token},
            cookie_jar=jar,
        )
        if st2 == 200:
            ses = _json(st2, ses)
            user = ses.get("me") or ses.get("user") or {}
    uid = user.get("userId") or user.get("id") or creds["identifier"]
    return _identity(
        "amehare", uid, user.get("displayName") or uid,
        _avatar(AMEHARE_HOST, user.get("avatarUrl")),
        "%s/users/%s" % (AMEHARE_HOST, urllib.parse.quote(str(uid))), uid,
    )


def _verify_pitter(creds):
    if not _pitter_hex(creds.get("apiKey")):
        raise AuthError("APIキーは64桁の16進数です。Pitterの「設定 → APIキー」で発行した値を貼り付けてください。",
                        400, "invalid_api_key")
    base_headers = {"Origin": PITTER_HOST, "Referer": PITTER_HOST + "/",
                    "X-Requested-With": "XMLHttpRequest"}
    status, data, _h, _t = _request(
        "POST", PITTER_HOST + "/system/openAPI.php?action=login",
        {"email": creds["email"], "password": creds["password"], "api_key": creds["apiKey"].strip()},
        base_headers,
    )
    data = _json(status, data)
    if status != 200 or not data.get("ok", status == 200):
        msg = data.get("error") or "Pitterにログインできませんでした。"
        raise AuthError(msg, 401, "invalid_credentials")
    token = data.get("token") or data.get("access_token")
    if not token:
        raise AuthError("Pitterのログイン応答にトークンがありませんでした。", 502, "service_error")

    user = {}
    st2, me, _h2, _t2 = _request(
        "POST", PITTER_HOST + "/system/openAPI.php?action=me", {},
        dict(base_headers, Authorization="Bearer " + token),
    )
    if st2 == 200:
        me = _json(st2, me)
        user = me.get("data") or me.get("user") or {}
        if isinstance(user, dict) and isinstance(user.get("user"), dict):
            user = user["user"]
    handle = (user.get("user_handle") or user.get("handle") or user.get("username")
              or user.get("name_id") or user.get("id") or creds["email"])
    name = user.get("user_name") or user.get("display_name") or user.get("name") or handle
    avatar = (user.get("user_icon_url") or user.get("icon_url")
              or user.get("avatar_url") or user.get("avatarUrl"))
    return _identity(
        "pitter", handle, name, _avatar(PITTER_HOST, avatar),
        "%s/?profile=%s" % (PITTER_HOST, urllib.parse.quote(str(handle))),
        user.get("id") or handle,
    )


VERIFIERS = {
    "karotter": _verify_karotter,
    "yudetter": _verify_yudetter,
    "amehare": _verify_amehare,
    "pitter": _verify_pitter,
}


# ---------------------------------------------------------------- セッション
#
# サーバー側にセッションを持たない。トークン自体に「どのSNSを・どのアカウントで
# 連携したか」を入れ、HMAC で署名する。Render の無料プランは再起動・スピンダウンで
# ディスクが消えるため、状態をディスクに置くと15分の無操作で連携が失われてしまう。

import base64

SESSION_TTL_SEC = 60 * 60 * 24 * 60          # 60日（連携のたびに延長）

_CACHE_DIR = None
_lock = threading.RLock()
_attempts = {}
_secret = None


def configure(cache_dir):
    """server.py から呼ぶ。署名鍵の退避先を auth と共有する。"""
    global _CACHE_DIR
    _CACHE_DIR = cache_dir


def _dir():
    if _CACHE_DIR:
        return _CACHE_DIR
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "loomix-auth")
    os.makedirs(d, exist_ok=True)
    return d


def _secret_path():
    return os.path.join(_dir(), "session-secret")


def _get_secret():
    global _secret
    if _secret:
        return _secret
    env = os.environ.get("LOOMIX_SESSION_SECRET")
    if env and len(env) >= 16:
        _secret = env.encode("utf-8")
        return _secret
    path = _secret_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if len(saved) >= 32:
                _secret = saved.encode("utf-8")
                return _secret
        value = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        _secret = value.encode("utf-8")
    except Exception as e:  # noqa: BLE001 - 書けない環境ではプロセス内だけで持つ
        logging.warning("session secret file unavailable: %s", e)
        _secret = secrets.token_hex(32).encode("utf-8")
    return _secret


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body):
    return hmac.new(_get_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()[:40]


def _encode(claims):
    raw = json.dumps(claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64(raw)
    return "lx1.%s.%s" % (body, _sign(body))


def _decode(token):
    """トークンを検証して claims を返す。無効・期限切れは None。"""
    if not token or not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != "lx1":
        return None
    body, sig = parts[1], parts[2]
    if not body or len(body) > 4000:
        return None
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        claims = json.loads(_unb64(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict) or claims.get("v") != 1:
        return None
    if float(claims.get("exp") or 0) < time.time():
        return None
    services = claims.get("services")
    if not isinstance(services, dict):
        return None
    claims["services"] = {k: v for k, v in services.items() if k in AUTH_SPECS and isinstance(v, dict)}
    return claims


def _new_claims(services=None, created=None):
    now = int(time.time())
    return {
        "v": 1,
        "iat": int(created or now),
        "exp": now + SESSION_TTL_SEC,
        "services": services or {},
    }


def _iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- レート制限

def _check_rate(ip):
    now = time.time()
    key = ip or "-"
    with _lock:
        dq = _attempts.setdefault(key, collections.deque())
        while dq and now - dq[0] > LOGIN_WINDOW_SEC:
            dq.popleft()
        if len(dq) >= LOGIN_MAX_PER_WINDOW:
            raise AuthError("ログインの試行が多すぎます。数分待ってからやり直してください。",
                            429, "too_many_attempts")
        dq.append(now)


# ---------------------------------------------------------------- 公開API

def bearer_token(headers):
    if not headers:
        return ""
    try:
        value = headers.get("Authorization") or ""
    except Exception:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _public_identity(item):
    return {
        "service": item.get("service"),
        "handle": item.get("handle") or "",
        "displayName": item.get("displayName") or "",
        "avatarUrl": item.get("avatarUrl") or "",
        "profileUrl": item.get("profileUrl") or "",
        "linkedAt": item.get("linkedAt"),
    }


def _status_for(claims):
    required = required_services()
    linked = {}
    if claims:
        for sid, item in (claims.get("services") or {}).items():
            public = _public_identity(item)
            public["service"] = sid
            linked[sid] = public
    missing = [sid for sid in required if sid not in linked]
    session_info = None
    if claims:
        session_info = {
            "createdAt": _iso(claims.get("iat")),
            "expiresAt": _iso(claims.get("exp")),
        }
    return {
        "ok": True,
        "version": VERSION,
        "authenticated": bool(claims),
        "gateOpen": bool(claims) and not missing,
        "required": required,
        "linked": linked,
        "missing": missing,
        "progress": {"linked": len([s for s in required if s in linked]),
                     "required": len(required)},
        "session": session_info,
        "providers": {sid: AUTH_SPECS[sid] for sid in SERVICE_ORDER},
    }


def status(token):
    return _status_for(_decode(token))


def login(payload, token="", client_ip=""):
    payload = payload or {}
    service = str(payload.get("service") or "").strip()
    spec = AUTH_SPECS.get(service)
    if not spec:
        raise AuthError("連携するSNSを選んでください。", 400, "bad_service")

    _check_rate(client_ip)

    creds = {}
    for field in spec["fields"]:
        key = field["key"]
        value = payload.get(key)
        value = "" if value is None else str(value)
        if field["type"] != "password":
            value = value.strip()
        if not value:
            raise AuthError("%sの「%s」を入力してください。" % (spec["title"], field["label"]),
                            400, "missing_field")
        creds[key] = value

    identity = VERIFIERS[service](creds)

    current = _decode(token)
    services = dict((current or {}).get("services") or {})
    services[service] = identity
    claims = _new_claims(services, created=(current or {}).get("iat"))
    new_token = _encode(claims)

    logging.info("auth linked: service=%s handle=%s", service, identity.get("handle"))
    return {"token": new_token, "service": service, "account": _public_identity(identity),
            "auth": _status_for(claims)}


def unlink(token, service):
    claims = _decode(token)
    if not claims:
        raise AuthError("連携が見つかりません。もう一度ログインしてください。", 401, "auth_required")
    if service not in AUTH_SPECS:
        raise AuthError("連携するSNSを選んでください。", 400, "bad_service")
    services = dict(claims.get("services") or {})
    services.pop(service, None)
    if not services:
        return {"token": "", "auth": _status_for(None)}
    new_token = _encode(_new_claims(services, created=claims.get("iat")))
    return {"token": new_token, "auth": _status_for(_decode(new_token))}


def logout(token):
    """資格情報を持たないのでサーバー側で消すものは無い（ブラウザがトークンを捨てる）。"""
    return {"ok": True}
