import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from m0.fixtures import generate, encode as raw_encode, message, meta, THREAD
from replica.collect import SourceClient, collect
from replica.normalize import sync, DECODER
from replica.store import Store
from replica.view import View


@unittest.skipUnless(DECODER.exists(), "build the pinned official adapter first")
class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.cases = {c["name"]: c for c in generate(Path(cls.temporary.name) / "fixtures")}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, case_name):
        case = self.cases[case_name]
        home = Path(case["home"])
        sidecar = json.loads((home.parent / "metadata.json").read_text())
        db = sqlite3.connect(home / "state_5.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,archived INTEGER)")
        db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?)", (THREAD, str(home / sidecar["selected_rollout"]), "测试会话", 0))
        db.commit()
        db.close()
        store = Store(self.root / "archive")
        client = SourceClient(str(home), str(home))
        report = collect(store, client, "fixture", str(home), str(home))
        self.assertTrue(report["complete"])
        client.close()
        store.close()
        return case

    def content(self, view):
        return "\n".join(row["text"] for thread in view.threads() for row in view.items(thread["id"], limit=10000))

    def test_index_name_reaches_view_and_rename_emits_change(self):
        case = self.capture("item-update")
        home = Path(case["home"])
        index = home / "session_index.jsonl"
        try:
            for name in ("Actual title", "Renamed title"):
                with index.open("a") as stream:
                    stream.write(json.dumps({"id": THREAD, "thread_name": name}) + "\n")
                self.capture("item-update")
                sync(self.root / "archive")
                view = View(self.root / "archive", readonly=True)
                try:
                    self.assertEqual(view.threads()[0]["title"], name)
                finally:
                    view.close()
        finally:
            index.unlink()

    def test_official_paginated_updates_replace_the_same_item(self):
        case = self.capture("item-update")
        result = sync(self.root / "archive")
        self.assertEqual(result["items"], 4)
        view = View(self.root / "archive", readonly=True)
        try:
            text = self.content(view)
            for expected in case["expected_text"]:
                self.assertIn(expected, text)
            for forbidden in case["forbidden_text"]:
                self.assertNotIn(forbidden, text)
            self.assertEqual(view.threads()[0]["coverage"]["status"], "complete")
            cursor = view.seq()
        finally:
            view.close()
        self.assertEqual(sync(self.root / "archive")["decoded_bytes"], 0)
        view = View(self.root / "archive", readonly=True)
        self.assertEqual(view.seq(), cursor)
        view.close()

    def test_legacy_uses_official_builder_and_selected_head_excludes_old_history(self):
        case = self.capture("selected-head")
        sync(self.root / "archive")
        view = View(self.root / "archive", readonly=True)
        try:
            text = self.content(view)
            self.assertIn("M0_SELECTED_HEAD", text)
            for forbidden in case["forbidden_text"]:
                self.assertNotIn(forbidden, text)
        finally:
            view.close()

    def test_fork_resolves_the_saved_parent_prefix(self):
        self.capture("referenced-fork")
        sync(self.root / "archive")
        view = View(self.root / "archive", readonly=True)
        try:
            child = next(t for t in view.threads() if t["thread_id"] == THREAD)
            self.assertEqual(child["coverage"]["status"], "complete")
            self.assertEqual(child["items"], 4)
            self.assertTrue(all(i["source"]["generation"] != child["generation"] for i in view.items(child["id"])))
        finally:
            view.close()

    def test_missing_parent_and_bad_lines_are_visible_without_losing_items(self):
        for name, issue in (("missing-parent", "missing_or_unproven_parent"), ("invalid-complete-line", "decode_error")):
            with self.subTest(name=name):
                self.capture(name)
                sync(self.root / "archive")
                view = View(self.root / "archive", readonly=True)
                try:
                    self.assertTrue(any(issue in t["coverage"]["issues"] for t in view.threads()))
                finally:
                    view.close()

    def test_snapshot_watermark_and_change_replay_survive_reconnect(self):
        root = self.root / "journal"
        root.mkdir()
        view = View(root)
        with view.db:
            view.set_entity("item", "item", "s", {"text": "first"})
        watermark = view.seq()
        with view.db:
            view.set_entity("item", "item", "s", {"text": "updated"})
            view.set_entity("other", "item", "s", {"text": "new"})
        self.assertEqual(view.feed(snapshot=watermark)["changes"][0]["data"]["text"], "first")
        view.close()
        view = View(root, readonly=True)
        try:
            replay = view.feed(after=watermark)
            self.assertEqual([r["data"]["text"] for r in replay["changes"]], ["updated", "new"])
            self.assertTrue(view.feed(after=int(replay["cursor"]))["done"])
        finally:
            view.close()


if __name__ == "__main__":
    unittest.main()
