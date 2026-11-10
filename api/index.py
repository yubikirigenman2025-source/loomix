import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402  (workspace root の BFF 実装を共有)


def send(handler, obj, status=200, cache_control="no-store"):
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    """Vercel 用エントリ。認証を含むすべての判定は server.api_dispatch に任せる。"""

    def _handle(self, method):
        parsed = urlparse(self.path)
        subpath = parsed.path[4:] if parsed.path.startswith("/api") else parsed.path
        if not subpath.startswith("/"):
            subpath = "/" + subpath
        path = "/api" + subpath

        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    send(self, {"ok": False, "error": "JSON を解釈できません"}, 400)
                    return

        status, obj, cache_control = server.api_dispatch(
            method, path, parse_qs(parsed.query), body, self.headers, ""
        )
        send(self, obj, status, cache_control)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")
