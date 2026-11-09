import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Add parent directory to path so we can import from server.py if needed, or inline the logic
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Strip /api prefix if present or handle both
        subpath = path
        if subpath.startswith("/api"):
            subpath = subpath[4:]
        if not subpath.startswith("/"):
            subpath = "/" + subpath

        if subpath == "/timeline":
            service_filter = query.get("service", ["all"])[0]
            force = "refresh" in query
            data = server.get_merged_timeline(force_refresh=force)
            
            filtered_posts = data["posts"]
            if service_filter != "all":
                filtered_posts = [p for p in filtered_posts if p["service"] == service_filter]

            resp = {
                "ok": True,
                "fetchedAt": data["fetchedAt"],
                "total": len(filtered_posts),
                "allTotal": data["total"],
                "serviceCounts": data["serviceCounts"],
                "posts": filtered_posts
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=60")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        elif subpath == "/services":
            services = [
                {"id": "karotter", "name": "Karotter", "icon": "🥕", "url": "https://karotter.com", "color": "#f97316"},
                {"id": "yudetter", "name": "Yudetter", "icon": "🥚", "url": "https://yudetter.com", "color": "#eab308"},
                {"id": "amehare", "name": "あめ→はれ", "icon": "☀️", "url": "https://am-hr.com", "color": "#0284c7"},
                {"id": "pitter", "name": "Pitter", "icon": "🕊️", "url": "https://pitter.jp", "color": "#a855f7"}
            ]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "services": services}, ensure_ascii=False).encode("utf-8"))
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "not found"}')
