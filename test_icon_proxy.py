#!/usr/bin/env python3
"""アイコンのプロキシ転送のオフライン検証。

外部APIへは一切行かず、urllib の応答を差し替えて確かめる。

    python3 test_icon_proxy.py
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("LOOMIX_CACHE_DIR", tempfile.mkdtemp(prefix="loomix-test-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
REAL_URLOPEN = urllib.request.urlopen  # クライアント側の取得は差し替えの影響を受けない


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type="image/png"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IconFetchTest(unittest.TestCase):
    def setUp(self):
        server._ICON_CACHE.clear()
        self.calls = []
        self._orig = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._orig
        server._ICON_CACHE.clear()

    def stub(self, body=PNG, content_type="image/png", fail=None):
        def fake_urlopen(req, context=None, timeout=None):
            self.calls.append(req.full_url)
            if fail:
                raise OSError(fail)
            return FakeResponse(body, content_type)

        urllib.request.urlopen = fake_urlopen

    def test_fetches_only_the_fixed_url(self):
        self.stub()
        ctype, body = server.service_icon("amehare")
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG)
        self.assertEqual(self.calls, ["https://am-hr.com/icons/icon-180.png"])

    def test_memory_cache_avoids_a_second_fetch(self):
        self.stub()
        server.service_icon("amehare")
        server.service_icon("amehare")
        self.assertEqual(len(self.calls), 1)

    def test_force_refetches(self):
        self.stub()
        server.service_icon("amehare")
        server.service_icon("amehare", force=True)
        self.assertEqual(len(self.calls), 2)

    def test_nothing_is_written_to_disk(self):
        self.stub()
        before = sorted(os.listdir(server.CACHE_DIR))
        server.service_icon("amehare")
        self.assertEqual(sorted(os.listdir(server.CACHE_DIR)), before)

    def test_other_services_and_bad_ids_are_refused(self):
        self.stub()
        for sid in ("karotter", "yudetter", "pitter", "amiverse", "", "..", "amehare/../x"):
            with self.assertRaises(KeyError):
                server.service_icon(sid)
        self.assertEqual(self.calls, [])

    def test_upstream_failure_surfaces_as_fetch_error(self):
        self.stub(fail="boom")
        with self.assertRaises(server.FetchError):
            server.service_icon("amehare")

    def test_empty_body_is_an_error(self):
        self.stub(body=b"")
        with self.assertRaises(server.FetchError):
            server.service_icon("amehare")

    def test_non_image_content_type_falls_back_to_png(self):
        self.stub(content_type="text/html; charset=utf-8")
        ctype, _ = server.service_icon("amehare")
        self.assertEqual(ctype, "image/png")

    def test_failed_fetch_is_not_cached(self):
        self.stub(fail="boom")
        with self.assertRaises(server.FetchError):
            server.service_icon("amehare")
        self.stub()
        ctype, _ = server.service_icon("amehare")
        self.assertEqual(ctype, "image/png")


class IconEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server.LoomixHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        server._ICON_CACHE.clear()
        self._orig_urlopen = urllib.request.urlopen
        self._orig_icon = server.service_icon

    def tearDown(self):
        urllib.request.urlopen = self._orig_urlopen
        server.service_icon = self._orig_icon
        server._ICON_CACHE.clear()

    def get(self, path):
        try:
            with REAL_URLOPEN(self.base + path, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def stub(self, body=PNG, content_type="image/png", fail=None):
        def fake_urlopen(req, context=None, timeout=None):
            if fail:
                raise OSError(fail)
            return FakeResponse(body, content_type)

        urllib.request.urlopen = fake_urlopen

    def test_serves_the_icon_as_png_with_cache_headers(self):
        self.stub()
        status, headers, body = self.get("/api/service-icon/amehare")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=%d" % server.ICON_CACHE_SEC)
        self.assertEqual(body, PNG)

    def test_unknown_service_is_404(self):
        self.stub()
        status, headers, body = self.get("/api/service-icon/karotter")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body.decode("utf-8"))["ok"], False)

    def test_query_string_cannot_change_the_target(self):
        self.stub()
        status, _, _ = self.get("/api/service-icon/amehare?url=https://example.com/x.png")
        self.assertEqual(status, 200)
        status, _, _ = self.get("/api/service-icon/amehare%2F..%2Fkarotter")
        self.assertEqual(status, 404)

    def test_upstream_failure_is_502_so_the_monogram_fallback_runs(self):
        self.stub(fail="boom")
        status, _, body = self.get("/api/service-icon/amehare")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body.decode("utf-8"))["ok"], False)

    def test_api_still_answers_normally(self):
        status, headers, body = self.get("/api/services")
        self.assertEqual(status, 200)
        services = json.loads(body.decode("utf-8"))["services"]
        amehare = [s for s in services if s["id"] == "amehare"][0]
        self.assertEqual(amehare["iconUrl"], "/api/service-icon/amehare")
        self.assertEqual(amehare["iconSource"], "https://am-hr.com/icons/icon-180.png")
        others = {s["id"]: s["iconUrl"] for s in services if s["id"] != "amehare"}
        self.assertTrue(all(u.startswith("https://") for u in others.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
