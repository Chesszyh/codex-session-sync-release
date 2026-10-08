import gzip
import http.client
import json
from pathlib import Path
import tempfile
import threading
import socket
import time
import unittest
from unittest.mock import patch

from replica.gateway import create_server
from replica.view import View


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        self.text = "生成历史内容，压缩传输不改变任何条目。" * 2000
        view = View(root)
        with view.db:
            view.set_entity("item", "item", "session", {"text": self.text})
        view.close()
        self.start_server()

    def start_server(self, public_origin=None):
        self.public = bool(public_origin)
        with patch("replica.access.AccessVerifier") as verifier:
            verifier.return_value.verify.side_effect = lambda token: token == "test-access-token"
            self.server = create_server(self.root, 0, public_origin=public_origin,
                                        access_team="test" if public_origin else None,
                                        access_audience="a" * 64 if public_origin else None)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def request(self, method, path, headers, body=None, authenticated=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            headers = dict(headers)
            if self.public and authenticated:
                headers.setdefault("Cf-Access-Jwt-Assertion", "test-access-token")
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def get(self, path, encoding):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            conn.request("GET", path, headers={"Accept-Encoding": encoding})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_compressed_snapshot_preserves_payload_and_cursor(self):
        status, headers, body = self.get("/api/snapshot", "gzip, deflate, br")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", headers.get("Vary", ""))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        _, _, original = self.get("/api/snapshot", "identity")
        self.assertEqual(gzip.decompress(body), original)
        self.assertLess(len(body), len(original))
        decoded = json.loads(gzip.decompress(body))
        self.assertEqual(decoded["changes"][0]["data"]["text"], self.text)
        self.assertEqual(decoded["cursor"], "1")

    def test_opt_out_and_small_responses_stay_uncompressed(self):
        for encoding in ["identity", "gzip;q=0"]:
            with self.subTest(encoding=encoding):
                _, headers, body = self.get("/api/snapshot", encoding)
                self.assertNotIn("Content-Encoding", headers)
                self.assertEqual(json.loads(body)["cursor"], "1")
        _, headers, body = self.get("/api/changes?after=1", "gzip")
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(body)["changes"], [])

    def test_public_host_requires_explicit_configuration(self):
        status, _ = self.request("GET", "/api/status", {"Host": "archive.example.com"})
        self.assertEqual(status, 403)
        self.stop_server()
        self.start_server("https://archive.example.com")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host, expected in [("archive.example.com", 200), ("other.example.com", 403),
                               (f"127.0.0.1:{self.server.server_port}", 403)]:
            with self.subTest(host=host):
                status, _ = self.request("GET", "/api/status", {"Host": host})
                self.assertEqual(status, expected)
        status, _ = self.request("GET", "/api/status", {
            "Host": "other.example.com", "X-Forwarded-Host": "archive.example.com",
        })
        self.assertEqual(status, 403)

    def test_ack_accepts_only_configured_https_origin_or_loopback(self):
        from replica.live import LiveJournal
        from replica.store import Store
        Store(self.root).close()
        journal = LiveJournal(self.root)
        journal.close()
        self.stop_server()
        self.start_server("https://archive.example.com")
        for origin, expected in [("https://archive.example.com", 200),
                                 ("http://archive.example.com", 403),
                                 ("https://other.example.com", 403)]:
            with self.subTest(origin=origin):
                status, _ = self.request("POST", "/api/live/ack", {
                    "Host": "archive.example.com", "Origin": origin,
                    "Content-Type": "application/json",
                }, json.dumps({"consumer": "public-test", "seq": "0"}))
                self.assertEqual(status, expected)
        status, _ = self.request("POST", "/api/live/ack", {
            "Origin": f"http://127.0.0.1:{self.server.server_port}",
        }, json.dumps({"consumer": "local-test", "seq": "0"}))
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/turn/start", {"Host": "archive.example.com"}, "{}")
        self.assertEqual(status, 405)

    def test_invalid_public_origin_is_rejected_before_listening(self):
        for origin in ["http://archive.example.com", "https://user:pass@archive.example.com",
                       "https://archive.example.com/path", "https://archive.example.com?x=1",
                       "https://archive.example.com#fragment", "https://", "https://archive.example.com:bad"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_server(self.root, 0, public_origin=origin, access_team="test", access_audience="a" * 64)

    def test_public_mode_requires_complete_auth_configuration(self):
        for config in ({"public_origin": "https://archive.example.com"}, {"access_team": "test"},
                       {"public_origin": "https://archive.example.com", "access_audience": "a" * 64}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                create_server(self.root, 0, **config)

    def test_public_routes_require_token_and_local_listener_rejects_proxy_headers(self):
        for header in ("CF-Ray", "X-Forwarded-For", "Forwarded", "Cf-Access-Jwt-Assertion"):
            self.assertEqual(self.request("GET", "/api/status", {header: "test"})[0], 403)
        self.stop_server()
        self.start_server("https://archive.example.com")
        for token in (None, "forged-token"):
            headers = {"Host": "archive.example.com"}
            if token:
                headers["Cf-Access-Jwt-Assertion"] = token
            for path in ("/", "/app.js", "/style.css", "/sw.js", "/api/status", "/api/snapshot", "/api/live/snapshot"):
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.request("GET", path, headers, authenticated=False)[0], 403)
            self.assertEqual(self.request("POST", "/api/live/ack", headers, "{}", authenticated=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", {"Host": "archive.example.com"})[0], 200)

    def test_partial_headers_and_body_are_closed_on_timeout(self):
        self.server.request_timeout = 0.15
        for payload in [b"GET / HTTP/1.1\r\n", (
                f"POST /api/live/ack HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                "Content-Length: 100\r\n\r\n{").encode()]:
            with self.subTest(payload=payload), socket.create_connection(self.server.server_address, timeout=2) as conn:
                conn.sendall(payload)
                self.assertEqual(conn.recv(1024), b"")
        self.assertEqual(self.get("/api/status", "identity")[0], 200)

    def test_excess_connections_are_closed_and_capacity_recovers(self):
        self.server.slots = threading.BoundedSemaphore(1)
        self.server.request_timeout = 2
        with socket.create_connection(self.server.server_address, timeout=2) as first:
            first.sendall(b"GET / HTTP/1.1\r\n")
            deadline = time.monotonic() + 1
            while self.server.slots.acquire(blocking=False):
                self.server.slots.release()
                if time.monotonic() > deadline:
                    self.fail("first connection was not accepted")
                time.sleep(0.01)
            with socket.create_connection(self.server.server_address, timeout=2) as second:
                self.assertEqual(second.recv(1024), b"")
            first.sendall(f"Host: 127.0.0.1:{self.server.server_port}\r\n\r\n".encode())
            while first.recv(4096):
                pass
        deadline = time.monotonic() + 1
        while not self.server.slots.acquire(blocking=False):
            if time.monotonic() > deadline:
                self.fail("connection capacity was not released")
            time.sleep(0.01)
        self.server.slots.release()
        self.assertEqual(self.get("/api/status", "identity")[0], 200)

    def reading_fixture(self):
        view = View(self.root)
        with view.db:
            view.set_entity("thread", "thread", "session", {"id": "session", "title": "目录"})
            view.set_entity("turn", "turn", "session", {"id": "turn", "status": "completed"})
            view.set_entity("other", "item", "other", {"position": 0, "turn": "turn", "text": "UNOPENED_BODY"})
            for i in range(65):
                view.set_entity("row-" + str(i), "item", "session", {
                    "position": i, "turn": "turn", "item": {"type": "agentMessage", "id": str(i)},
                    "text": "preview" + "x" * 700,
                })
        view.close()

    def test_directory_feed_has_independent_cursor_and_no_body(self):
        self.reading_fixture()
        status, _, raw = self.get("/api/snapshot?scope=directory", "identity")
        page = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual([r["kind"] for r in page["changes"]], ["thread"])
        self.assertNotIn(b"UNOPENED_BODY", raw)
        _, _, raw = self.get("/api/snapshot?scope=directory&after=" + page["cursor"] + "&at=" + page["seq"], "identity")
        self.assertTrue(json.loads(raw)["done"])
        self.assertEqual(json.loads(raw)["cursor"], page["seq"])
        view = View(self.root)
        with view.db:
            view.delete_entity("thread")
        view.close()
        _, _, raw = self.get("/api/changes?scope=directory&after=" + page["seq"], "identity")
        self.assertIsNone(json.loads(raw)["changes"][0]["data"])
        status, _, _ = self.get("/api/snapshot?scope=invalid", "identity")
        self.assertEqual(status, 400)

    def test_reading_previews_pages_and_version_guard(self):
        self.reading_fixture()
        status, _, raw = self.get("/api/read/index?session=session", "identity")
        self.assertEqual(status, 200)
        index = json.loads(raw)
        self.assertNotIn(b"UNOPENED_BODY", raw)
        self.assertLessEqual(max(len(r["text"] or "") for r in index["previews"]), 600)
        _, _, raw = self.get("/api/read/index?session=session&version=" + index["version"], "identity")
        self.assertEqual(json.loads(raw), {"version": index["version"], "unchanged": True})
        path = "/api/read/page?session=session&turn=turn&version=" + index["version"]
        _, _, raw = self.get(path, "identity")
        first = json.loads(raw)
        self.assertEqual(len(first["rows"]), 60)
        self.assertTrue(first["more"])
        _, _, raw = self.get(path + "&after=59", "identity")
        self.assertEqual([r["position"] for r in json.loads(raw)["rows"]], [60, 61, 62, 63, 64])
        self.assertFalse(json.loads(raw)["more"])
        view = View(self.root)
        with view.db:
            view.delete_entity("row-0")
        view.close()
        status, _, raw = self.get(path, "identity")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["error"], "session_changed")

    def test_reading_page_stops_after_large_item(self):
        view = View(self.root)
        with view.db:
            for i in range(2):
                view.set_entity("large-" + str(i), "item", "large", {
                    "turn": "turn", "position": i, "text": "x" * (2 * 1024 * 1024),
                })
        version = view.read_version("large")
        view.close()
        _, _, raw = self.get("/api/read/page?session=large&turn=turn&version=" + version, "identity")
        page = json.loads(raw)
        self.assertEqual(len(page["rows"]), 1)
        self.assertTrue(page["more"])
