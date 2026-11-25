#!/usr/bin/env python3
"""Yudetter 連携（メールアドレス／ユーザー名の両対応）のオフライン検証。

外部APIへは一切行かず、auth._request を差し替えて確かめる。

    python3 test_auth_yudetter.py
"""

import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("LOOMIX_CACHE_DIR", tempfile.mkdtemp(prefix="loomix-test-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402

USER = {"id": "u1", "username": "tamago", "displayName": "たまご",
        "avatarUrl": "/avatar.png"}


class YudetterLoginTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = {}
        self._orig = auth._request
        auth._request = self.fake_request

    def tearDown(self):
        auth._request = self._orig

    def fake_request(self, method, url, body=None, headers=None, timeout=18, cookie_jar=None):
        self.calls.append({"method": method, "url": url, "body": body,
                           "headers": dict(headers or {})})
        status, data = self.responses.get(url, (200, {"user": dict(USER)}))
        if isinstance(data, Exception):
            raise data
        return status, data, {}, json.dumps(data)

    def login(self, identifier, password="secret"):
        return auth._verify_yudetter({"identifier": identifier, "password": password})

    # -- どのエンドポイントへ送るか -------------------------------------

    def test_username_goes_to_sign_in_by_username(self):
        result = self.login("tamago")
        self.assertEqual(self.calls[0]["url"], auth.YUDETTER_HOST + "/api/users/sign-in-by-username")
        self.assertEqual(self.calls[0]["body"], {"username": "tamago", "password": "secret"})
        self.assertEqual(result["identity"]["handle"], "tamago")
        self.assertEqual(result["identity"]["displayName"], "たまご")
        self.assertEqual(result["identity"]["service"], "yudetter")

    def test_at_prefixed_username_loses_the_at(self):
        self.login("@tamago")
        self.assertEqual(self.calls[0]["url"], auth.YUDETTER_HOST + "/api/users/sign-in-by-username")
        self.assertEqual(self.calls[0]["body"], {"username": "tamago", "password": "secret"})

    def test_email_goes_to_sign_in_email(self):
        self.login("tamago@example.com")
        self.assertEqual(self.calls[0]["url"], auth.YUDETTER_HOST + "/api/auth/sign-in/email")
        self.assertEqual(self.calls[0]["body"], {"email": "tamago@example.com", "password": "secret"})

    def test_email_is_lowercased_like_the_site(self):
        self.login(" Tamago@Example.COM ")
        self.assertEqual(self.calls[0]["body"]["email"], "tamago@example.com")

    def test_origin_header_is_sent_either_way(self):
        self.login("tamago")
        self.assertEqual(self.calls[0]["headers"].get("Origin"), auth.YUDETTER_HOST)
        self.calls.clear()
        self.login("tamago@example.com")
        self.assertEqual(self.calls[0]["headers"].get("Origin"), auth.YUDETTER_HOST)

    def test_blank_identifier_is_rejected_before_any_request(self):
        for value in ("", "   ", "@"):
            with self.assertRaises(auth.AuthError) as ctx:
                self.login(value)
            self.assertEqual(ctx.exception.code, "missing_identifier")
        self.assertEqual(self.calls, [])

    # -- 応答の解釈 -----------------------------------------------------

    def test_user_is_read_from_get_session_when_body_has_none(self):
        self.responses[auth.YUDETTER_HOST + "/api/users/sign-in-by-username"] = (200, {})
        self.responses[auth.YUDETTER_HOST + "/api/auth/get-session"] = (
            200, {"session": {"id": "s1"}, "user": dict(USER)})
        result = self.login("tamago")
        urls = [c["url"] for c in self.calls]
        self.assertIn(auth.YUDETTER_HOST + "/api/auth/get-session", urls)
        self.assertEqual(result["identity"]["handle"], "tamago")

    def test_users_me_is_the_last_fallback(self):
        self.responses[auth.YUDETTER_HOST + "/api/users/sign-in-by-username"] = (200, {})
        self.responses[auth.YUDETTER_HOST + "/api/auth/get-session"] = (200, None)
        self.responses[auth.YUDETTER_HOST + "/api/users/me"] = (200, {"user": dict(USER)})
        result = self.login("tamago")
        self.assertEqual(result["identity"]["handle"], "tamago")

    def test_handle_falls_back_to_the_identifier(self):
        self.responses[auth.YUDETTER_HOST + "/api/users/sign-in-by-username"] = (200, {})
        self.responses[auth.YUDETTER_HOST + "/api/auth/get-session"] = (200, None)
        self.responses[auth.YUDETTER_HOST + "/api/users/me"] = (401, {"error": "認証が必要です"})
        result = self.login("Tamago")
        self.assertEqual(result["identity"]["handle"], "Tamago")

    def test_invalid_credentials_keep_the_service_message(self):
        self.responses[auth.YUDETTER_HOST + "/api/users/sign-in-by-username"] = (
            401, {"error": "ユーザーIDまたはパスワードが正しくありません",
                  "code": "INVALID_CREDENTIALS"})
        with self.assertRaises(auth.AuthError) as ctx:
            self.login("tamago")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "invalid_credentials")
        self.assertIn("ユーザーIDまたはパスワード", ctx.exception.message)

    def test_unverified_email_is_explained(self):
        self.responses[auth.YUDETTER_HOST + "/api/auth/sign-in/email"] = (
            403, {"code": "EMAIL_NOT_VERIFIED", "message": "Email not verified"})
        with self.assertRaises(auth.AuthError) as ctx:
            self.login("tamago@example.com")
        self.assertEqual(ctx.exception.code, "email_not_verified")
        self.assertIn("認証", ctx.exception.message)

    def test_blocked_origin_is_reported_as_service_blocked(self):
        self.responses[auth.YUDETTER_HOST + "/api/users/sign-in-by-username"] = (
            403, {"error": "許可されていないアクセス元です。"})
        with self.assertRaises(auth.AuthError) as ctx:
            self.login("tamago")
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "service_blocked")

    def test_session_material_is_returned_for_safety_lists(self):
        result = self.login("tamago")
        self.assertIn("cookie", result["session"])
        self.assertIn("token", result["session"])


class IdentifierTest(unittest.TestCase):
    def test_shapes(self):
        cases = {
            "tamago": ("username", "tamago"),
            "@tamago": ("username", "tamago"),
            "  @tamago ": ("username", "tamago"),
            "tamago@example.com": ("email", "tamago@example.com"),
            " Tamago@Example.COM": ("email", "tamago@example.com"),
            "たまご": ("username", "たまご"),
        }
        for value, expected in cases.items():
            self.assertEqual(auth._yudetter_identifier(value), expected)

    def test_spec_offers_both_inputs(self):
        spec = auth.AUTH_SPECS["yudetter"]
        field = [f for f in spec["fields"] if f["key"] == "identifier"][0]
        self.assertEqual(field["label"], "メールアドレスまたはユーザー名")
        self.assertIn("user@example.com", field["placeholder"])
        self.assertIn("username", field["placeholder"])
        self.assertIn("ユーザー名", spec["help"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
