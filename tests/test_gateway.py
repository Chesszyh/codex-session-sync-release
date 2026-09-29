import gzip
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from replica.gateway import create_server
from replica.view import View


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.text = "生成历史内容，压缩传输不改变任何条目。" * 2000
        view = View(root)
        with view.db:
            view.set_entity("item", "item", "session", {"text": self.text})
        view.close()
        self.server = create_server(root, 0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

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
