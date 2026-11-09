import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402  (workspace root の BFF 実装を共有)


def send(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "public, s-maxage=20, stale-while-revalidate=60")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        subpath = path[4:] if path.startswith("/api") else path
        if not subpath.startswith("/"):
            subpath = "/" + subpath

        if subpath == "/timeline":
            service = query.get("service", ["all"])[0]
            before = query.get("before", [None])[0]
            force = query.get("refresh", ["0"])[0] not in ("", "0", "false")
            try:
                limit = int(query.get("limit", [str(server.DEFAULT_LIMIT)])[0] or server.DEFAULT_LIMIT)
            except Exception:
                limit = server.DEFAULT_LIMIT
            try:
                result = server.timeline_page(before, limit, service, force=force)
            except ValueError as e:
                send(self, {"ok": False, "error": str(e)}, status=400)
                return
            except Exception:
                send(self, {"ok": False, "error": "タイムラインを取得できませんでした"}, status=502)
                return
            send(self, result)
            return

        if subpath == "/user":
            service = query.get("service", [""])[0]
            handle = query.get("id", [""])[0] or query.get("username", [""])[0]
            if service not in server.SERVICE_BY_ID or not handle:
                send(self, {"ok": False, "error": "service と id が必要です"}, status=400)
                return
            try:
                result = server.get_profile(service, handle)
            except Exception:
                result = None
            if not result:
                send(self, {"ok": False, "error": "プロフィールを取得できませんでした"}, status=502)
                return
            send(self, result)
            return

        if subpath == "/services":
            send(self, {"ok": True, "version": server.VERSION, "services": server.SERVICES})
            return

        if subpath == "/health":
            send(self, server.health())
            return

        send(self, {"ok": False, "error": "not found"}, status=404)
