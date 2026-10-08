"""Loopback HTTP gateway for archived history. No native execution methods."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import gzip
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .view import View

STATIC = Path(__file__).resolve().parent.parent / "viewer/dist"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}


class GatewayServer(ThreadingHTTPServer):
    request_timeout = 10
    max_connections = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_timeout)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def create_server(root, port=8765, static=STATIC, public_origin=None, access_team=None, access_audience=None):
    public_host = None
    verifier = None
    if any((public_origin, access_team, access_audience)) and not all((public_origin, access_team, access_audience)):
        raise ValueError("public access requires --public-origin, --access-team and --access-audience together")
    if public_origin:
        origin = urlsplit(public_origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.password is not None or origin.path not in ("", "/")
                or origin.query or origin.fragment):
            raise ValueError("public origin must be an HTTPS origin, for example https://archive.example.com")
        origin.port  # Reject malformed ports before opening the listener.
        public_host = origin.netloc
        public_origin = "https://" + public_host
        from .access import AccessVerifier
        verifier = AccessVerifier(access_team, access_audience)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Replica"
        sys_version = ""

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
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if public_host:
                hosts = {public_host}
            if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in hosts:
                self.reply(403, b'{"error":"unrecognized_host"}')
                return False
            if verifier:
                tokens = self.headers.get_all("Cf-Access-Jwt-Assertion", [])
                if len(tokens) != 1 or not verifier.verify(tokens[0]):
                    self.reply(403, b'{"error":"authentication_required"}')
                    return False
            elif any(name in self.headers for name in ("Cf-Access-Jwt-Assertion", "CF-Ray", "X-Forwarded-For", "X-Forwarded-Host", "Forwarded")):
                self.reply(403, b'{"error":"proxy_requires_public_listener"}')
                return False
            return True

        def do_GET(self):
            if not self.authorized():
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
                    scope = query.get("scope", [None])[0]
                    if scope not in (None, "directory"):
                        raise ValueError("invalid scope")
                    after = max(0, int(query.get("after", [0])[0]))
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                    if after > view.seq():
                        self.reply(409, b'{"error":"reset_required"}')
                        return
                    snapshot = int(query.get("at", [view.seq()])[0]) if url.path.endswith("snapshot") else None
                    result = view.feed(after, limit, snapshot, scope)
                elif url.path == "/api/read/index":
                    result = view.read_index(query["session"][0], query.get("version", [None])[0])
                elif url.path == "/api/read/page":
                    result = view.read_page(query["session"][0], query["turn"][0],
                                            max(-1, int(query.get("after", [-1])[0])),
                                            min(60, max(1, int(query.get("limit", [60])[0]))),
                                            query["version"][0])
                    if result.get("error"):
                        self.reply(409, json.dumps(result).encode())
                        return
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
            if not self.authorized():
                return
            if self.path != "/api/live/ack":
                self.reply(405, b'{"error":"read_only"}')
                return
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origins = {None, *("http://" + host for host in expected)}
            if public_host:
                expected = {public_host}
                origins = {None, public_origin}
            if self.headers.get("Host") not in expected or self.headers.get("Origin") not in origins:
                self.reply(403, b'{"error":"unrecognized_origin"}')
                return
            from .live import LiveJournal
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (length < 1 or length > 1024 or "Transfer-Encoding" in self.headers
                        or len(self.headers.get_all("Content-Length", [])) != 1):
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

    return GatewayServer(("127.0.0.1", port), Handler)
