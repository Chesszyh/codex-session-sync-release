import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from replica.live import LiveJournal, Owner, observe
from replica.store import Store


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        Store(self.root / "archive").close()
        self.journal = LiveJournal(self.root / "archive")

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def event(self, method, **params):
        return self.journal.append("fixture:/home", "epoch-1", "event", {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}})

    def test_completion_replaces_deltas_and_late_deltas_do_not_duplicate(self):
        self.event("item/started", item={"type": "agentMessage", "id": "item", "text": ""})
        self.event("item/agentMessage/delta", itemId="item", delta="hello ")
        self.event("item/agentMessage/delta", itemId="item", delta="world")
        self.assertEqual(self.journal.snapshot()["items"][0]["data"]["item"]["text"], "hello world")
        for _ in range(2):
            self.event("item/completed", item={"type": "agentMessage", "id": "item", "text": "hello world"})
        self.event("item/agentMessage/delta", itemId="item", delta="world")
        snapshot = self.journal.snapshot()
        self.assertEqual(len(snapshot["items"]), 1)
        self.assertEqual(snapshot["items"][0]["data"]["item"]["text"], "hello world")
        self.assertTrue(snapshot["items"][0]["data"]["complete"])

    def test_gap_survives_restart_and_marks_unfinished_stream_uncertain(self):
        self.event("item/agentMessage/delta", itemId="item", delta="partial")
        self.journal.append("fixture:/home", "epoch-1", "gap", {"reason": "connection_closed"})
        self.journal.close()
        self.journal = LiveJournal(self.root / "archive")
        snapshot = self.journal.snapshot()
        self.assertTrue(snapshot["items"][0]["data"]["uncertain"])
        self.assertEqual(snapshot["sources"][0]["status"], "disconnected")
        self.assertEqual(self.journal.changes(0)["events"][-1]["kind"], "gap")

    def test_acknowledged_retention_keeps_archived_events_and_requires_reset(self):
        first = self.event("item/started", item={"type": "agentMessage", "id": "item", "text": ""})
        with self.assertRaises(ValueError):
            self.journal.prune(first)
        self.journal.acknowledge("browser-A", first)
        self.journal.prune(first)
        self.assertTrue(self.journal.changes(0)["reset_required"])
        self.assertEqual(self.journal.snapshot()["seq"], str(first))
        obj = self.journal.db.execute("SELECT object FROM archived_batches").fetchone()[0]
        batch = self.journal.archive.json(obj)
        self.assertEqual(self.journal.archive.json(batch[0]["object"])["method"], "item/started")
        next_seq = self.event("item/agentMessage/delta", itemId="item", delta="later")
        self.assertGreater(next_seq, first)

    def test_execution_methods_are_rejected_before_transport_use(self):
        owner = object.__new__(Owner)
        for method in ("thread/resume", "thread/start", "turn/start", "command/exec", "turn/interrupt"):
            with self.assertRaises(ValueError):
                owner.call(method, {})

    def test_unknown_notification_is_retained_without_projecting_it(self):
        original = {"method": "future/notification", "params": ["opaque", {"unknown": True}]}
        self.journal.append("fixture:/home", "epoch-1", "event", original)
        self.assertEqual(self.journal.changes(0)["events"][0]["data"], original)
        self.assertEqual(self.journal.snapshot()["items"], [])


BINARY = Path(os.environ.get("REPLICA_TEST_BINARY", "/usr/lib/chatgpt/resources/codex"))


@unittest.skipUnless(BINARY.exists(), "requires the tested native runtime")
class NativeOwnerTest(unittest.TestCase):
    def test_observer_records_native_notification_coverage_without_resume(self):
        class Model(BaseHTTPRequestHandler):
            calls = 0
            def log_message(self, *_args):
                pass

            def do_POST(self):
                Model.calls += 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                item = {"id": "msg_fixture", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "LIVE_ONE LIVE_TWO"}], "status": "completed"}
                events = [
                    {"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": [], "status": "in_progress"}},
                    {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                    {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": "LIVE_ONE "},
                    {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": "LIVE_TWO"},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}},
                ]
                for event in events:
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(0.03)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "owner"
            home.mkdir()
            model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            (home / "config.toml").write_text(f'''model = "gpt-fixture"
model_provider = "fixture"
[model_providers.fixture]
name = "Fixture"
base_url = "http://127.0.0.1:{model.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
''')
            socket_path = home / "app-server-control/app-server-control.sock"
            archive = root / "archive"
            Store(archive).close()
            owner_process = subprocess.Popen([str(BINARY), "app-server", "--listen", "unix://"],
                cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(home)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            observer_thread, driver = None, None
            try:
                deadline = time.monotonic()+15
                while not socket_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(socket_path.exists())
                endpoint = {"host": "fixture", "home": str(home), "socket": str(socket_path)}
                errors = []
                def watch():
                    try:
                        observe(archive, endpoint, seconds=7)
                    except Exception as error:
                        errors.append(type(error).__name__)
                observer_thread = threading.Thread(target=watch)
                observer_thread.start()
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    if (archive / "live.sqlite").exists():
                        journal = LiveJournal(archive)
                        ready = journal.db.execute("SELECT 1 FROM events WHERE kind='snapshot'").fetchone()
                        journal.close()
                        if ready:
                            break
                    time.sleep(0.05)
                driver_methods = []
                driver = Owner(endpoint, lambda message: driver_methods.append(message.get("method")))
                driver.initialize()
                def fixture_call(method, params):
                    driver.serial += 1
                    driver.ws.send(json.dumps({"id": driver.serial, "method": method, "params": params}))
                    while True:
                        message = json.loads(driver.ws.recv(timeout=10))
                        if "method" in message:
                            driver_methods.append(message["method"])
                        if message.get("id") == driver.serial:
                            self.assertNotIn("error", message)
                            return message["result"]
                thread = fixture_call("thread/start", {"model": "gpt-fixture", "modelProvider": "fixture", "cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
                deadline = time.monotonic()+2
                while time.monotonic() < deadline:
                    journal = LiveJournal(archive)
                    started = any(event["data"].get("method") == "thread/started" for event in journal.changes(0)["events"] if event["kind"] == "event")
                    journal.close()
                    if started:
                        break
                    time.sleep(0.02)
                fixture_call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Return the fixture text."}]})
                deadline = time.monotonic()+4
                while time.monotonic() < deadline:
                    driver.poll(0.1)
                    if "turn/completed" in driver_methods:
                        break
                observer_thread.join(timeout=12)
                self.assertFalse(observer_thread.is_alive())
                self.assertFalse(errors)
                journal = LiveJournal(archive)
                try:
                    methods = [event["data"].get("method") for event in journal.changes(0, 1000)["events"] if event["kind"] == "event"]
                    self.assertEqual(Model.calls, 1)
                    self.assertIn("item/agentMessage/delta", driver_methods)
                    self.assertIn("thread/status/changed", methods)
                    items = journal.snapshot()["items"]
                    if "item/agentMessage/delta" in methods:
                        self.assertTrue(any(item["data"]["item"].get("text") == "LIVE_ONE LIVE_TWO" and item["data"]["complete"] for item in items))
                    evidence = Path(".m3/evidence")
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / "native-coverage.json").write_text(json.dumps({"observer_methods": sorted(set(methods)), "driver_methods": sorted(set(driver_methods)), "item_stream_observed": "item/agentMessage/delta" in methods, "model_requests": Model.calls, "observer_resume_calls": 0}, indent=2) + "\n")
                finally:
                    journal.close()
            finally:
                if driver:
                    driver.close()
                owner_process.terminate()
                owner_process.wait(timeout=5)
                if observer_thread:
                    observer_thread.join(timeout=5)
                model.shutdown()
                model.server_close()


if __name__ == "__main__":
    unittest.main()
