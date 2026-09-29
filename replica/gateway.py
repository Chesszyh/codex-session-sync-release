"""Loopback HTTP gateway for archived history. No native execution methods."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import gzip
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .view import View

STATIC = Path(__file__).resolve().parent.parent / "viewer/dist"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}


def create_server(root, port=8765, static=STATIC):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def reply(self, status, data, mime="application/json; charset=utf-8"):
            compressed = (mime.startswith("application/json") and len(data) >= 4096
                          and "gzip" in [token.strip().lower() for token in self.headers.get("Accept-Encoding", "").split(",")])
            if compressed:
                data = gzip.compress(data, compresslevel=1)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            if mime.startswith("application/json"):
                self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                self.reply(403, b'{"error":"unrecognized_host"}')
                return
            url = urlsplit(self.path)
            if url.path in ("/api/live/snapshot", "/api/live/changes"):
                from .live import LiveJournal
                if not (Path(root) / "live.sqlite").exists():
                    self.reply(200, b'{"available":false}')
                    return
                journal = LiveJournal(root, readonly=True)
                try:
                    query = parse_qs(url.query)
                    result = journal.snapshot() if url.path.endswith("snapshot") else journal.changes(max(0, int(query.get("after", [0])[0])))
                    self.reply(200, json.dumps({"available": True, **result}, ensure_ascii=False).encode())
                except ValueError:
                    self.reply(400, b'{"error":"invalid_query"}')
                finally:
                    journal.close()
                return
            if url.path in ASSETS:
                name, mime = ASSETS[url.path]
                path = static / name
                if not path.is_file():
                    self.reply(503, b'{"error":"viewer_not_built"}')
                else:
                    self.reply(200, path.read_bytes(), mime)
                return
            view = View(root, readonly=True)
            try:
                query = parse_qs(url.query)
                if url.path in ("/api/changes", "/api/snapshot"):
                    after = max(0, int(query.get("after", [0])[0]))
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                    if after > view.seq():
                        self.reply(409, b'{"error":"reset_required"}')
                        return
                    snapshot = int(query.get("at", [view.seq()])[0]) if url.path.endswith("snapshot") else None
                    result = view.feed(after, limit, snapshot)
                elif url.path == "/api/status":
                    result = view.status()
                elif url.path == "/api/threads":
                    result = {"threads": view.threads()}
                elif url.path == "/api/items":
                    result = {"items": view.items(query["session"][0], int(query.get("after", [-1])[0]), min(200, max(1, int(query.get("limit", [100])[0]))))}
                else:
                    self.reply(404, b'{"error":"not_found"}')
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode())
            except (ValueError, KeyError):
                self.reply(400, b'{"error":"invalid_query"}')
            finally:
                view.close()

        def do_POST(self):
            if self.path != "/api/live/ack":
                self.reply(405, b'{"error":"read_only"}')
                return
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in expected or self.headers.get("Origin") not in {None, *("http://" + host for host in expected)}:
                self.reply(403, b'{"error":"unrecognized_origin"}')
                return
            from .live import LiveJournal
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1024:
                    raise ValueError("invalid acknowledgement size")
                value = json.loads(self.rfile.read(length))
                journal = LiveJournal(root)
                try:
                    journal.acknowledge(value["consumer"], int(value["seq"]))
                finally:
                    journal.close()
                self.reply(200, b'{"acknowledged":true}')
            except (ValueError, KeyError, TypeError):
                self.reply(400, b'{"error":"invalid_acknowledgement"}')

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
