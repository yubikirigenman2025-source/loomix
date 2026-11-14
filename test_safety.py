#!/usr/bin/env python3
"""ブロック・ミュート反映のオフライン検証。

外部APIへは一切行かない。取得済みリストの解釈・フィルタ・共有アーカイブへの
非干渉をモックデータで確かめる。

    python3 test_safety.py
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LOOMIX_CACHE_DIR", tempfile.mkdtemp(prefix="loomix-test-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safety  # noqa: E402
import server  # noqa: E402


def post(service, pid, username, content="hello", uid=None, minutes_ago=0):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": "%s_%s" % (service, pid),
        "rawId": str(pid),
        "service": service,
        "serviceName": service,
        "serviceLabel": service,
        "author": {"id": str(uid or username), "username": username,
                   "displayName": username, "avatarUrl": "", "profileUrl": ""},
        "content": content,
        "media": [],
        "createdAt": created.isoformat().replace("+00:00", "Z"),
        "url": "",
        "linkLabel": "",
        "isReply": False,
        "replyTo": None,
        "isQuote": False,
        "metrics": {"likes": 0, "reposts": 0, "replies": 0, "views": 0},
    }


class HiderTest(unittest.TestCase):
    def hider(self, services):
        return safety.Hider(services)

    def test_blocked_and_muted_authors_are_hidden(self):
        h = self.hider({
            "karotter": {
                "handle": "me",
                "blocked": [{"handle": "spam", "id": "11"}],
                "muted": [{"handle": "noisy", "id": "22"}],
                "words": [],
            }
        })
        self.assertTrue(h.active)
        self.assertFalse(h.keep(post("karotter", 1, "spam")))
        self.assertFalse(h.keep(post("karotter", 2, "noisy")))
        self.assertTrue(h.keep(post("karotter", 3, "friend")))
        self.assertEqual(h.hidden, 2)

    def test_id_match_and_at_prefix(self):
        h = self.hider({"yudetter": {"handle": "me", "blocked": [{"handle": "", "id": "618"}],
                                     "muted": [], "words": []}})
        self.assertTrue(h.active)
        self.assertFalse(h.keep(post("yudetter", 1, "someone", uid=618)))
        self.assertTrue(h.keep(post("yudetter", 2, "someone", uid=999)))

    def test_mute_words_apply_to_others_only(self):
        h = self.hider({"amehare": {"handle": "me", "blocked": [], "muted": [],
                                    "words": ["ネタバレ", "spoil"]}})
        self.assertTrue(h.active)
        self.assertFalse(h.keep(post("amehare", 1, "other", "これはネタバレです")))
        self.assertFalse(h.keep(post("amehare", 2, "other", "SPOILER inside")))
        # 自分の投稿には自分のミュートワードを適用しない
        self.assertTrue(h.keep(post("amehare", 3, "me", "ネタバレ注意")))
        # ワードはそのサービスの投稿にだけ効く
        self.assertTrue(h.keep(post("karotter", 4, "other", "ネタバレ注意")))

    def test_one_service_setting_does_not_hide_other_services(self):
        h = self.hider({"karotter": {"handle": "me", "blocked": [{"handle": "x", "id": ""}],
                                     "muted": [], "words": []}})
        self.assertFalse(h.keep(post("karotter", 1, "x")))
        self.assertTrue(h.keep(post("pitter", 2, "x")))

    def test_empty_settings_are_inactive(self):
        h = self.hider({"karotter": {"handle": "me", "blocked": [], "muted": [], "words": []}})
        self.assertFalse(h.active)

    def test_unavailable_service_is_reported_not_hidden(self):
        h = self.hider({"amiverse": {"handle": "me", "blocked": [], "muted": [], "words": [],
                                     "status": "unavailable"}})
        self.assertFalse(h.active)
        self.assertTrue(h.keep(post("amiverse", 1, "anyone")))


class ParserTest(unittest.TestCase):
    def test_karotter_users_list(self):
        payload = {"users": [
            {"id": 11, "username": "Spam", "displayName": "spam"},
            {"id": 12, "username": "noisy"},
        ]}
        got = safety._dedupe(safety._user_list(payload))
        self.assertEqual([u["handle"] for u in got], ["spam", "noisy"])
        self.assertEqual(got[0]["id"], "11")

    def test_amehare_safety_payload(self):
        payload = {
            "blockedUsers": [{"userId": "abc", "displayName": "A"}],
            "mutedUsers": [{"userId": "def", "displayName": "B"}],
            "muteWords": [{"id": "w1", "word": "ネタバレ"}, {"id": "w2", "word": "spoil"}],
        }
        blocked = safety._user_list(payload.get("blockedUsers"))
        muted = safety._user_list(payload.get("mutedUsers"))
        words = safety._words(payload.get("muteWords"))
        self.assertEqual([u["handle"] for u in blocked], ["abc"])
        self.assertEqual([u["handle"] for u in muted], ["def"])
        self.assertEqual(words, ["ネタバレ", "spoil"])

    def test_yudetter_unknown_shape_is_tolerated(self):
        for payload in (
            {"users": [{"username": "a"}]},
            {"blocks": [{"user": {"username": "a", "id": "1"}}]},
            [{"username": "a"}],
            {"data": {"blocking": [{"handle": "a"}]}},
        ):
            got = safety._dedupe(safety._user_list(
                payload, keys=("users", "blocks", "blockedUsers", "items", "data", "list", "blocking")))
            self.assertIn("a", [u["handle"] for u in got], payload)

    def test_yudetter_unknown_payload_is_flagged_not_silently_empty(self):
        self.assertTrue(safety._has_nonempty_list({"blocks": [{"weird": 1}]}))
        self.assertFalse(safety._has_nonempty_list({"users": []}))

    def test_pitter_blocked_handles(self):
        payload = {"ok": True, "data": {"user": {"handle": "me", "blocked_handles": ["x", "@y"]}}}
        raw = safety._deep_find(payload, "blocked_handles")
        self.assertEqual([u["handle"] for u in safety._user_list(raw)], ["x", "y"])


class ArchiveFilterTest(unittest.TestCase):
    """共有アーカイブは間引かず、応答だけが減ること。"""

    def setUp(self):
        self.sid = "karotter"
        self.archive = server.Archive(self.sid)
        self.posts = [
            post(self.sid, 1, "spam", minutes_ago=1),
            post(self.sid, 2, "friend", minutes_ago=2),
            post(self.sid, 3, "noisy", minutes_ago=3),
            post(self.sid, 4, "friend2", minutes_ago=4),
        ]
        self.archive.add(self.posts)
        self.archive.exhausted = True
        self.hider = safety.Hider({
            "karotter": {"handle": "me",
                         "blocked": [{"handle": "spam", "id": ""}],
                         "muted": [{"handle": "noisy", "id": ""}],
                         "words": []}
        })
        self.keep = self.hider.keep

    def tearDown(self):
        try:
            os.remove(self.archive._path())
        except OSError:
            pass

    def test_newest_fills_limit_with_kept_posts(self):
        kept = self.archive.newest(10, keep=self.keep)
        self.assertEqual([p["id"] for p in kept], ["karotter_2", "karotter_4"])
        # 共有アーカイブはそのまま
        self.assertEqual(len(self.archive), 4)

    def test_older_than_skips_hidden_and_keeps_limit(self):
        ts = datetime.now(timezone.utc)
        kept = self.archive.older_than(ts, 10, keep=self.keep)
        self.assertEqual([p["id"] for p in kept], ["karotter_2", "karotter_4"])

    def test_no_keep_returns_everything(self):
        kept = self.archive.newest(10)
        self.assertEqual(len(kept), 4)

    def test_other_users_view_is_untouched(self):
        other = safety.Hider({"karotter": {"handle": "someone", "blocked": [],
                                           "muted": [], "words": ["hello"]}})
        self.assertEqual(other.keep(self.posts[0]) and True, False)  # ワードで隠れる
        self.assertEqual(len(self.archive.newest(10)), 4)


class TimelinePageTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(server.ARCHIVE)
        self._sync = server.sync_newest
        server.sync_newest = lambda *a, **kw: None
        self.archive = server.Archive("karotter")
        for i in range(6):
            self.archive.add([post("karotter", i, "u%d" % i, minutes_ago=i)])
        self.archive.exhausted = True
        server.ARCHIVE["karotter"] = self.archive
        self.hider = safety.Hider({
            "karotter": {"handle": "me",
                         "blocked": [{"handle": "u0", "id": ""},
                                     {"handle": "u1", "id": ""},
                                     {"handle": "u2", "id": ""}],
                         "muted": [], "words": []}
        })
        self.keep = self.hider.keep

    def tearDown(self):
        server.sync_newest = self._sync
        for sid, a in self.saved.items():
            server.ARCHIVE[sid] = a
        try:
            os.remove(self.archive._path())
        except OSError:
            pass

    def test_timeline_page_filters_and_reports(self):
        page = server.timeline_page(None, 3, "karotter", allowed=["karotter"],
                                    keep=self.hider, safety_info={"active": True, "services": {}})
        self.assertEqual([p["author"]["username"] for p in page["posts"]], ["u3", "u4", "u5"])
        self.assertEqual(page["safety"]["filtered"], 3)
        self.assertEqual(len(self.archive), 6)  # アーカイブは無傷

    def test_older_page_filters(self):
        ts = datetime.now(timezone.utc)
        page = server.timeline_page(ts.isoformat(), 2, "karotter", allowed=["karotter"], keep=self.keep)
        self.assertEqual([p["author"]["username"] for p in page["posts"]], ["u3", "u4"])

    def test_everything_filtered_still_offers_continuation(self):
        everything = safety.Hider({
            "karotter": {"handle": "me",
                         "blocked": [{"handle": "u%d" % i, "id": ""} for i in range(6)],
                         "muted": [], "words": []}
        })
        page = server.timeline_page(None, 3, "karotter", allowed=["karotter"], keep=everything)
        self.assertEqual(page["posts"], [])
        self.assertIsNotNone(page["nextBefore"])


class AuthFlowTest(unittest.TestCase):
    """ログインで作った safetyKey が、延長・連携変更でも引き継がれること。"""

    def setUp(self):
        import auth
        self.auth = auth
        self.saved_verifiers = dict(auth.VERIFIERS)
        self.saved_fetchers = dict(safety.FETCHERS)

        def fake_verifier(service):
            def run(creds):
                handle = "me_%s" % service
                return {
                    "identity": auth._identity(service, handle, handle, "", "", handle),
                    "session": {"token": "tok-%s" % service},
                }
            return run

        def fake_fetch(material, handle):
            return {"status": "ok", "blocked": [{"handle": "spam", "id": ""}],
                    "muted": [], "words": []}

        for sid in auth.VERIFIERS:
            auth.VERIFIERS[sid] = fake_verifier(sid)
            safety.FETCHERS[sid] = fake_fetch
        auth._attempts.clear()

    def tearDown(self):
        self.auth.VERIFIERS.clear()
        self.auth.VERIFIERS.update(self.saved_verifiers)
        safety.FETCHERS.clear()
        safety.FETCHERS.update(self.saved_fetchers)

    def test_safety_key_survives_extension_and_linking(self):
        first = self.auth.login({"service": "karotter", "identifier": "a", "password": "b"})
        key = self.auth.safety_key(first["token"])
        self.assertTrue(key)
        self.assertEqual(first["safety"]["blocked"], 1)
        # スライディング延長
        st = self.auth.status(first["token"])
        self.assertEqual(self.auth.safety_key(st["token"]), key)
        # 別サービスの追加連携
        second = self.auth.login({"service": "yudetter", "identifier": "a", "password": "b"},
                                 token=st["token"])
        self.assertEqual(self.auth.safety_key(second["token"]), key)
        # 一つの連携解除
        un = self.auth.unlink(second["token"], "yudetter")
        self.assertEqual(self.auth.safety_key(un["token"]), key)

    def test_filter_uses_saved_lists(self):
        res = self.auth.login({"service": "karotter", "identifier": "a", "password": "b"})
        h = safety.hider(self.auth.safety_key(res["token"]))
        self.assertIsNotNone(h)
        self.assertFalse(h.keep(post("karotter", 1, "spam")))
        self.assertTrue(h.keep(post("karotter", 2, "friend")))

    def test_logout_forgets_lists(self):
        res = self.auth.login({"service": "karotter", "identifier": "a", "password": "b"})
        key = self.auth.safety_key(res["token"])
        self.auth.logout(res["token"])
        self.assertIsNone(safety.hider(key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
