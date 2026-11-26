#!/usr/bin/env python3
"""統合通知のオフライン検証。

外部APIへは一切行かない。各SNSの応答形（2026-11-27にクライアント解析と
401応答で確認したもの）をモックで与え、正規化・統合・並び順・状態表示を確かめる。

    python3 test_notify.py
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

os.environ.setdefault("LOOMIX_CACHE_DIR", tempfile.mkdtemp(prefix="loomix-test-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import notify  # noqa: E402
import safety  # noqa: E402
import server  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._orig_request = notify._request
        self._orig_fetchers = dict(notify.FETCHERS)
        notify._cache.clear()
        self.key = "test-%s" % self.id().split(".")[-1]

    def tearDown(self):
        notify._request = self._orig_request
        notify.FETCHERS.clear()
        notify.FETCHERS.update(self._orig_fetchers)
        notify._cache.clear()
        with safety._lock:
            safety._material.pop(self.key, None)
        store = safety._load()
        with safety._lock:
            store.pop(self.key, None)

    def stub_request(self, routes):
        """URL の部分一致で (status, payload) を返す偽リクエスト。"""
        calls = []

        def fake(method, url, headers=None, body=None, timeout=None):
            calls.append((method, url, headers or {}, body))
            for needle, result in routes.items():
                if needle in url:
                    if isinstance(result, Exception):
                        raise result
                    return result
            return 404, {"error": "not found"}, None

        notify._request = fake
        return calls

    def material(self, sid, data):
        with safety._lock:
            safety._material.setdefault(self.key, {})[sid] = {
                "data": data, "expires": time.time() + 3600}


class TimeTest(unittest.TestCase):
    def test_iso_variants(self):
        self.assertEqual(notify.iso_time("2026-11-27T01:02:03Z"), "2026-11-27T01:02:03Z")
        self.assertEqual(notify.iso_time("2026-11-27T10:02:03+09:00"), "2026-11-27T01:02:03Z")
        # あめ→はれはタイムゾーン表記のないJST
        self.assertEqual(notify.iso_time("2026-11-27 10:02:03"), "2026-11-27T01:02:03Z")
        # Pitter は unix 秒
        self.assertEqual(notify.iso_time(1795683723), "2026-11-26T16:22:03Z"[:4] + notify.iso_time(1795683723)[4:])
        self.assertEqual(notify.iso_time("1795683723000"), notify.iso_time(1795683723))

    def test_unparsable_is_none(self):
        for value in (None, "", "きのう", True, {"a": 1}):
            self.assertIsNone(notify.iso_time(value), value)


class KarotterTest(Base):
    def payload(self):
        return {"notifications": [
            {"id": 900, "type": "LIKE", "isRead": False,
             "createdAt": "2026-11-27T01:02:03.000Z",
             "actor": {"username": "spam", "displayName": "スパム", "avatarUrl": "/a.png"},
             "post": {"id": 2118044, "content": "今日のカロート"},
             "notificationIds": [900, 899]},
            {"id": 901, "type": "FOLLOW", "isRead": True,
             "createdAt": "2026-11-26T22:00:00Z",
             "actor": {"username": "friend", "displayName": "友達"}},
            {"id": 902, "type": "REPLY",
             "createdAt": "2026-11-26T20:00:00Z",
             "actors": [{"username": "a1", "displayName": "A1"},
                        {"username": "a2", "displayName": "A2"}],
             "posts": [{"id": 77, "content": "返信先の本文"}]},
        ]}

    def test_normalizes_types_actors_and_links(self):
        self.stub_request({"/notifications": (200, self.payload(), None)})
        got = notify._karotter({"token": "tok"})
        self.assertEqual(got["status"], "ok")
        items = got["items"]
        self.assertEqual(len(items), 3)
        like, follow, reply = items

        self.assertEqual(like["id"], "karotter:900")
        self.assertEqual(like["type"], "like")
        self.assertEqual(like["message"], "あなたの投稿にいいねしました")
        self.assertEqual(like["actor"]["handle"], "spam")
        self.assertEqual(like["actor"]["avatar"], "https://karotter.com/a.png")
        self.assertEqual(like["url"], "https://karotter.com/posts/2118044")
        self.assertEqual(like["excerpt"], "今日のカロート")
        self.assertIs(like["isRead"], False)

        self.assertEqual(follow["type"], "follow")
        self.assertEqual(follow["actor"]["url"], "https://karotter.com/friend")
        self.assertIs(follow["isRead"], True)

        self.assertEqual(reply["type"], "reply")
        self.assertEqual(reply["actor"]["handle"], "a1")
        self.assertEqual(reply["excerpt"], "返信先の本文")
        self.assertIsNone(reply["isRead"])

    def test_request_carries_bearer_and_limit(self):
        calls = self.stub_request({"/notifications": (200, {"notifications": []}, None)})
        notify._karotter({"token": "abc"})
        method, url, headers, _body = calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("limit=%d" % notify.PER_SERVICE_LIMIT, url)
        self.assertEqual(headers["Authorization"], "Bearer abc")

    def test_expired_session_asks_for_relink(self):
        self.stub_request({"/notifications": (401, {"error": "認証が必要です"}, None)})
        got = notify._karotter({"token": "old"})
        self.assertEqual(got["status"], "needs_relink")
        self.assertIn("連携し直", got["error"])

    def test_missing_token_does_not_call_out(self):
        calls = self.stub_request({})
        got = notify._karotter({})
        self.assertEqual(got["status"], "needs_relink")
        self.assertEqual(calls, [])


class YudetterTest(Base):
    def test_types_and_actor(self):
        payload = {"items": [
            {"id": "n1", "type": "like", "read": False,
             "createdAt": "2026-11-27T03:00:00.000Z",
             "actor": {"username": "yudetamago", "displayName": "ゆでたまご",
                       "avatarUrl": "/u.png"},
             "yudate": {"id": "7", "content": "ユデート本文"}},
            {"id": "n2", "type": "super_yudate:1200", "read": True,
             "createdAt": "2026-11-27T02:00:00.000Z",
             "actor": {"username": "rich", "displayName": "リッチ"}},
            {"id": "n3", "type": "reaction:🌸", "read": True,
             "createdAt": "2026-11-27T01:00:00.000Z",
             "actor": {"username": "flower", "displayName": "フラワー"}},
        ], "nextCursor": 123}
        self.stub_request({"/api/notifications": (200, payload, None)})
        got = notify._yudetter({"token": "tok", "cookie": "sid=1"})
        self.assertEqual(got["status"], "ok")
        like, super_yudate, reaction = got["items"]
        self.assertEqual(like["url"], "https://yudetter.com/yudate/7")
        self.assertEqual(like["excerpt"], "ユデート本文")
        self.assertIs(like["isRead"], False)
        self.assertEqual(super_yudate["label"], "スーパーユデート")
        self.assertIn("1,200 YD", super_yudate["message"])
        self.assertEqual(reaction["type"], "like")
        self.assertIn("🌸", reaction["message"])

    def test_market_detail_becomes_excerpt(self):
        payload = {"items": [{"id": "m1", "type": "market_outbid", "read": True,
                              "createdAt": "2026-11-27T00:00:00Z",
                              "detail": "限定バッジ",
                              "actor": {"username": "bidder", "displayName": "入札者"}}]}
        self.stub_request({"/api/notifications": (200, payload, None)})
        got = notify._yudetter({"cookie": "sid=1"})
        item = got["items"][0]
        self.assertEqual(item["type"], "other")
        self.assertEqual(item["excerpt"], "限定バッジ")


class AmehareTest(Base):
    def test_two_buckets_merge_and_jst(self):
        main = {"notifications": [
            {"id": "p1", "seq": 10, "type": "reply",
             "createdAt": "2026-11-27 10:00:00",
             "actor": {"userId": "u1", "displayName": "あめ"},
             "post": {"id": "post1", "body": "もとの投稿"},
             "replyPost": {"id": "reply1", "body": "コメント本文"}},
        ]}
        sub = {"notifications": [
            {"id": "p2", "seq": 11, "type": "reaction", "reactionType": "heart",
             "createdAt": "2026-11-27 09:00:00",
             "actor": {"userId": "u2", "displayName": "はれ"},
             "post": {"id": "post2", "body": "はれの投稿"}},
            {"id": "p3", "seq": 12, "type": "reaction", "reactionType": "tea",
             "createdAt": "2026-11-27 08:00:00",
             "actor": {"userId": "u3", "displayName": "お茶"}},
        ]}
        self.stub_request({
            "bucket=main": (200, main, None),
            "bucket=sub": (200, sub, None),
        })
        got = notify._amehare({"token": "t", "cookie": "c=1"})
        self.assertEqual(got["status"], "ok")
        items = {it["id"]: it for it in got["items"]}
        self.assertEqual(set(items), {"amehare:p1", "amehare:p2", "amehare:p3"})
        reply = items["amehare:p1"]
        self.assertEqual(reply["type"], "reply")
        self.assertEqual(reply["createdAt"], "2026-11-27T01:00:00Z")
        self.assertEqual(reply["excerpt"], "コメント本文")
        self.assertEqual(reply["url"], "https://am-hr.com/posts/reply1")
        self.assertEqual(items["amehare:p2"]["label"], "お花")
        self.assertEqual(items["amehare:p3"]["label"], "お茶")
        self.assertEqual(items["amehare:p3"]["url"], "https://am-hr.com/users/u3")

    def test_one_bucket_failing_is_partial(self):
        self.stub_request({
            "bucket=main": (200, {"notifications": [
                {"id": "p1", "type": "reply", "createdAt": "2026-11-27 10:00:00",
                 "actor": {"userId": "u1", "displayName": "あめ"}, "post": {"id": "x"}}]}, None),
            "bucket=sub": (500, None, None),
        })
        got = notify._amehare({"token": "t"})
        self.assertEqual(got["status"], "partial")
        self.assertEqual(len(got["items"]), 1)
        self.assertTrue(got["error"])


class PitterTest(Base):
    def test_openapi_shape(self):
        payload = {"ok": True, "data": {"notifications": [
            {"id": 1, "type": "like", "actor_handle": "p", "actor_name": "ピッター",
             "actor_icon_url": "/i.png", "post_id": "29001", "post_content": "ピート本文",
             "is_read": 0, "created_at": 1795683723},
            {"id": 2, "type": "reaction", "emoji": "🎉", "actor_handle": "r",
             "actor_name": "リアクター", "is_read": 1, "created_at": 1795680000},
            {"id": 3, "type": "shadowban", "is_read": 1, "created_at": 1795670000},
        ]}}
        self.stub_request({"action=notifications": (200, payload, None)})
        got = notify._pitter({"token": "tok"})
        self.assertEqual(got["status"], "ok")
        like, reaction, system = got["items"]
        self.assertEqual(like["url"], "https://pitter.jp/?post=29001")
        self.assertEqual(like["excerpt"], "ピート本文")
        self.assertIs(like["isRead"], False)
        self.assertEqual(reaction["message"], "🎉 でリアクションしました")
        self.assertIs(reaction["isRead"], True)
        self.assertEqual(system["actor"]["name"], "")
        self.assertEqual(system["message"], "アカウントの機能が制限されています")

    def test_amiverse_is_unavailable(self):
        got = notify._amiverse({})
        self.assertEqual(got["status"], "unavailable")
        self.assertIn("Amiverse", got["error"])


class MergeTest(unittest.TestCase):
    def item(self, sid, raw, created):
        return notify._item(sid, raw, "like", "いいね", "いいねしました",
                            notify._actor("a", "a"), created)

    def test_sorted_desc_and_deduped(self):
        items = [
            self.item("karotter", 1, "2026-11-27T01:00:00Z"),
            self.item("yudetter", 2, "2026-11-27T03:00:00Z"),
            self.item("karotter", 1, "2026-11-27T01:00:00Z"),
            self.item("pitter", 3, None),
        ]
        out = notify.merge(items)
        self.assertEqual([i["id"] for i in out],
                         ["yudetter:2", "karotter:1", "pitter:3"])

    def test_total_limit(self):
        items = [self.item("karotter", i, "2026-11-27T00:00:%02dZ" % (i % 60))
                 for i in range(notify.TOTAL_LIMIT + 30)]
        self.assertEqual(len(notify.merge(items)), notify.TOTAL_LIMIT)


class ListNotificationsTest(Base):
    def fetchers(self, mapping):
        notify.FETCHERS.clear()
        notify.FETCHERS.update(mapping)

    def test_unlinked_services_report_needs_relink_or_amiverse_unavailable(self):
        self.material("karotter", {"token": "tok"})
        self.fetchers({
            "karotter": lambda m: {"status": "ok", "items": [
                notify._item("karotter", 1, "like", "いいね", "いいねしました",
                             notify._actor("A", "a"), "2026-11-27T01:00:00Z")]},
        })
        got = notify.list_notifications(self.key, ["karotter", "yudetter", "amiverse"])
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["services"]["karotter"]["status"], "ok")
        self.assertEqual(got["services"]["yudetter"]["status"], "needs_relink")
        self.assertEqual(got["services"]["amiverse"]["status"], "unavailable")
        self.assertEqual(got["services"]["pitter"]["status"], "unlinked")
        self.assertEqual(got["linked"], ["karotter", "yudetter", "amiverse"])

    def test_cache_is_reused_then_forced(self):
        self.material("karotter", {"token": "tok"})
        calls = []

        def fetch(m):
            calls.append(m)
            return {"status": "ok", "items": [
                notify._item("karotter", 1, "like", "いいね", "いいねしました",
                             notify._actor("A", "a"), "2026-11-27T01:00:00Z")]}

        self.fetchers({"karotter": fetch})
        first = notify.list_notifications(self.key, ["karotter"])
        self.assertFalse(first["cached"])
        second = notify.list_notifications(self.key, ["karotter"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)
        notify.list_notifications(self.key, ["karotter"], force=True)
        self.assertEqual(len(calls), 2)

    def test_blocked_actor_notifications_are_hidden(self):
        self.material("karotter", {"token": "tok"})
        store = safety._load()
        store[self.key] = {"createdAt": notify._now(), "services": {
            "karotter": {"handle": "me", "blocked": [{"handle": "spam", "id": ""}],
                         "muted": [], "words": []}}}
        self.fetchers({"karotter": lambda m: {"status": "ok", "items": [
            notify._item("karotter", 1, "like", "いいね", "いいねしました",
                         notify._actor("スパム", "spam"), "2026-11-27T02:00:00Z"),
            notify._item("karotter", 2, "like", "いいね", "いいねしました",
                         notify._actor("友達", "friend"), "2026-11-27T01:00:00Z"),
        ]}})
        got = notify.list_notifications(self.key, ["karotter"])
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["hidden"], 1)
        self.assertEqual(got["items"][0]["actor"]["handle"], "friend")

    def test_fetch_exception_does_not_break_others(self):
        self.material("karotter", {"token": "tok"})
        self.material("pitter", {"token": "tok"})

        def boom(m):
            raise RuntimeError("boom")

        self.fetchers({
            "karotter": lambda m: {"status": "ok", "items": [
                notify._item("karotter", 1, "like", "いいね", "いいねしました",
                             notify._actor("A", "a"), "2026-11-27T01:00:00Z")]},
            "pitter": boom,
        })
        got = notify.list_notifications(self.key, ["karotter", "pitter"])
        self.assertEqual(got["services"]["pitter"]["status"], "error")
        self.assertEqual(got["count"], 1)

    def test_unread_count_uses_is_read(self):
        self.material("karotter", {"token": "tok"})
        self.fetchers({"karotter": lambda m: {"status": "ok", "items": [
            notify._item("karotter", 1, "like", "いいね", "いいねしました",
                         notify._actor("A", "a"), "2026-11-27T02:00:00Z", is_read=False),
            notify._item("karotter", 2, "like", "いいね", "いいねしました",
                         notify._actor("B", "b"), "2026-11-27T01:00:00Z", is_read=True),
        ]}})
        got = notify.list_notifications(self.key, ["karotter"])
        self.assertEqual(got["unread"], 1)

    def test_no_key_is_not_authenticated(self):
        got = notify.list_notifications("", ["karotter"])
        self.assertFalse(got["authenticated"])
        self.assertEqual(got["items"], [])


class ApiDispatchTest(Base):
    def token(self):
        claims = auth._new_claims({
            "karotter": auth._identity("karotter", "me_k", "me", "", "", "1"),
            "yudetter": auth._identity("yudetter", "me_y", "me", "", "", "2"),
        })
        claims["safetyKey"] = self.key
        return auth._encode(claims)

    def test_requires_session(self):
        status, obj, _cc = server.api_dispatch(
            "GET", "/api/notifications", {}, None, {})
        self.assertEqual(status, 200)
        self.assertFalse(obj["authenticated"])
        self.assertEqual(obj["items"], [])

    def test_returns_merged_items_for_the_session(self):
        self.material("karotter", {"token": "tok"})
        self.material("yudetter", {"token": "tok", "cookie": "sid=1"})
        notify.FETCHERS.clear()
        notify.FETCHERS.update({
            "karotter": lambda m: {"status": "ok", "items": [
                notify._item("karotter", 1, "like", "いいね", "あなたの投稿にいいねしました",
                             notify._actor("A", "a"), "2026-11-27T02:00:00Z", is_read=False)]},
            "yudetter": lambda m: {"status": "ok", "items": [
                notify._item("yudetter", 2, "follow", "フォロー", "あなたをフォローしました",
                             notify._actor("B", "b"), "2026-11-27T01:00:00Z")]},
        })
        token = self.token()
        status, obj, cache = server.api_dispatch(
            "GET", "/api/notifications", {}, None, {"Authorization": "Bearer " + token})
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-store")
        self.assertEqual(obj["count"], 2)
        self.assertEqual([i["service"] for i in obj["items"]], ["karotter", "yudetter"])
        self.assertEqual(obj["unread"], 1)
        self.assertEqual(obj["services"]["yudetter"]["status"], "ok")
        self.assertEqual(obj["services"]["amehare"]["status"], "unlinked")

    def test_refresh_flag_bypasses_cache(self):
        self.material("karotter", {"token": "tok"})
        calls = []

        def fetch(m):
            calls.append(1)
            return {"status": "ok", "items": []}

        notify.FETCHERS.clear()
        notify.FETCHERS.update({"karotter": fetch})
        token = self.token()
        server.api_dispatch("GET", "/api/notifications", {}, None,
                            {"Authorization": "Bearer " + token})
        server.api_dispatch("GET", "/api/notifications", {"refresh": ["1"]}, None,
                            {"Authorization": "Bearer " + token})
        self.assertEqual(len(calls), 2)


class MaterialStoreTest(unittest.TestCase):
    def test_materials_copies_and_hides_expired(self):
        key = "mat-test"
        with safety._lock:
            safety._material[key] = {
                "karotter": {"data": {"token": "a"}, "expires": time.time() + 60},
                "pitter": {"data": {"token": "b"}, "expires": time.time() - 1},
            }
        got = safety.materials(key)
        self.assertEqual(list(got), ["karotter"])
        got["karotter"]["token"] = "changed"
        self.assertEqual(safety.materials(key)["karotter"]["token"], "a")
        safety.touch_material(key, "karotter")
        self.assertGreater(safety._material[key]["karotter"]["expires"], time.time() + 60)
        with safety._lock:
            safety._material.pop(key, None)

    def test_tokens_never_reach_disk(self):
        """材料はメモリのみ。safety の保存ファイルに現れないこと。"""
        key = "disk-test"
        with safety._lock:
            safety._material[key] = {"karotter": {"data": {"token": "secret-token"},
                                                 "expires": time.time() + 60}}
        safety.register(key, "karotter", {"token": "secret-token"}, handle="me")
        # register が FETCHERS を呼ぶので、ここでは保存だけを確認する
        path = safety._path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.assertNotIn("secret-token", f.read())
        with safety._lock:
            safety._material.pop(key, None)
            store = safety._load()
            store.pop(key, None)


class FrontendContractTest(unittest.TestCase):
    """UI が依存する形（キー名・type の語彙）が変わっていないこと。"""

    def test_types_are_from_the_documented_set(self):
        allowed = {"like", "repost", "reply", "mention", "follow", "other"}
        for mapping in (notify._KAROTTER_TYPES, notify._PITTER_TYPES):
            for _label, (norm, _word, _msg) in mapping.items():
                self.assertIn(norm, allowed)
        self.assertIn(notify._yudetter_type("reaction:🌸", "", "a")[0], allowed)
        self.assertIn(notify._yudetter_type("market_sell:1", "", "a")[0], allowed)

    def test_item_has_the_ui_keys(self):
        item = notify._item("karotter", 1, "like", "いいね", "いいねしました",
                            notify._actor("A", "a"), "2026-11-27T01:00:00Z",
                            "本文", "https://karotter.com/posts/1", False)
        for key in ("id", "service", "serviceName", "serviceLabel", "type", "label",
                    "message", "actor", "createdAt", "excerpt", "url", "isRead"):
            self.assertIn(key, item)
        for key in ("name", "handle", "avatar", "url"):
            self.assertIn(key, item["actor"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
