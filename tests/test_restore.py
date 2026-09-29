import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from m0.fixtures import THREAD, PARENT, encode, meta, envelope
from replica.collect import SourceClient, collect
from replica.restore import restore, RestoreRefused, SCHEMA, IMPORTER, verify_native
from replica.store import Store

BINARY = Path(os.environ["REPLICA_TEST_BINARY"]) if os.environ.get("REPLICA_TEST_BINARY") else None
NEW = "00000000-0000-4000-8000-000000000003"


@unittest.skipUnless(BINARY is not None, "set REPLICA_TEST_BINARY to a supported fixed runtime")
class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from replica.native_verify import runtime_version
        if not IMPORTER.is_file():
            raise RuntimeError("build the official adapter before running restore integration")
        runtime_version(BINARY.resolve())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "source"
        (self.home / "sessions/2026/09/22").mkdir(parents=True)
        self.db = sqlite3.connect(self.home / "state_5.sqlite")
        for table, fields in SCHEMA.items():
            self.db.execute('CREATE TABLE "' + table + '" (' + ','.join('"' + f + '"' for f in fields) + ')')
        self.items = [json.loads(line) for line in Path("m0/fixtures/shapes/macmini.jsonl").read_text().splitlines()]
        self.path = self.write_rollout(THREAD, [meta("paginated")] + self.items)
        self.metadata = dict.fromkeys(SCHEMA["threads"])
        self.metadata.update(id=THREAD, rollout_path=str(self.path), created_at=1790035200, updated_at=1790035200,
                             created_at_ms=1790035200000, updated_at_ms=1790035200000, recency_at=1790035200,
                             recency_at_ms=1790035200000, source="cli", model_provider="openai", cwd="/fixture/workspace",
                             title="database title", name="metadata-only name", preview="database preview", sandbox_policy='{"type":"read-only"}',
                             approval_mode="never", tokens_used=42, has_user_event=1, archived=0, cli_version="0.155.0-alpha.9.2",
                             first_user_message="", memory_mode="disabled", history_mode="paginated", is_pinned=0)
        self.insert("threads", self.metadata)
        self.db.commit()
        self.archive = self.root / "archive"
        self.origin = "fixture:" + str(self.home)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def insert(self, table, row):
        self.db.execute('INSERT INTO "' + table + '" (' + ','.join('"' + f + '"' for f in row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))

    def write_rollout(self, rollout_id, records, start=0, identity=THREAD):
        records = copy.deepcopy(records)
        for ordinal, record in enumerate(records, start):
            record["ordinal"] = ordinal
        records[0]["payload"]["id"] = identity
        name = identity + ("_" + rollout_id if identity != rollout_id else "")
        path = self.home / "sessions/2026/09/22" / ("rollout-2026-09-22T00-00-00-" + name + ".jsonl")
        path.write_bytes(encode(records))
        return path

    def capture(self):
        self.db.commit()
        store, client = Store(self.archive), SourceClient(str(self.home), str(self.home))
        try:
            self.assertTrue(collect(store, client, "fixture", str(self.home), str(self.home))["complete"])
        finally:
            client.close()
            store.close()

    def run_restore(self, name="restored"):
        return restore(self.archive, self.origin, THREAD, self.root / name, BINARY.resolve(), page_size=2)

    def test_canonical_items_metadata_nulls_and_repeat_official_read(self):
        self.capture()
        result = self.run_restore()
        self.assertTrue(result["raw_unchanged"])
        self.assertEqual(result["items"], 4)
        self.assertEqual(result["loaded_threads"], 0)
        self.assertEqual(result["metadata_fields_verified"], 38)
        self.assertTrue(verify_native(self.root / "restored", BINARY.resolve(), 1)["read_verified"])
        for file in (self.root / "restored").rglob("*"):
            if file.is_file():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600, str(file.relative_to(self.root)))

    def test_section_project_roots_and_attachment_semantics(self):
        self.insert("thread_sections", {"id": "group-a", "name": "Research", "appearance": None})
        self.insert("projects", {"id": "project-a", "name": "Project", "metadata": '{"topic":"test"}', "position": 1, "created_at_ms": 1, "updated_at_ms": 1})
        self.insert("project_roots", {"project_id": "project-a", "position": 0, "path": "/fixture/workspace"})
        self.insert("thread_attachments", {"id": "attachment-a", "thread_id": THREAD, "attachment_type": "test", "identity_key": "file", "payload": '{"path":"/fixture/file"}', "created_at": 1})
        self.db.execute("UPDATE threads SET project_id='project-a',thread_section_id='group-a',section_position=4,section_entered_at_ms=1790035200000")
        self.capture()
        self.assertTrue(self.run_restore()["read_verified"])

    def test_referenced_parent_survives_parent_revert_and_suffix_is_excluded(self):
        parent = self.write_rollout(PARENT, [meta("paginated", PARENT)] + self.items, identity=PARENT)
        prefix = parent.read_bytes()
        base = {"thread_id": PARENT, "end_byte_offset": len(prefix), "end_ordinal_exclusive": 5}
        self.write_rollout(THREAD, [meta("paginated", history_base=base)], start=5)
        self.capture()
        later = copy.deepcopy(self.items[-1]); later["ordinal"] = 5
        later["payload"]["item"]["content"][0]["text"] = "EXCLUDED_PARENT_SUFFIX"
        with parent.open("ab") as stream:
            stream.write(encode([later]))
        parent_new = self.write_rollout(NEW, [meta("paginated", PARENT)], identity=PARENT)
        self.insert("threads", {**self.metadata, "id": PARENT, "rollout_path": str(parent_new)})
        self.capture()
        result = self.run_restore()
        self.assertEqual(result["rollouts"], 2)
        self.assertEqual(result["items"], 4)
        restored_parent = self.root / "restored/home" / parent.relative_to(self.home)
        self.assertEqual(restored_parent.read_bytes(), prefix)

    def test_selected_reverted_head_is_not_inferred_from_thread_id(self):
        records = [meta("paginated")] + self.items[:1]
        selected = self.write_rollout(NEW, records)
        self.db.execute("UPDATE threads SET rollout_path=?", (str(selected),))
        self.capture()
        self.assertEqual(self.run_restore()["items"], 1)

    def test_compressed_archived_rollout_uses_exact_logical_bytes(self):
        original = self.path.read_bytes()
        archived = self.home / "archived_sessions" / self.path.name
        archived.parent.mkdir()
        subprocess.run(["zstd", "-q", str(self.path), "-o", str(archived) + ".zst"], check=True)
        self.path.unlink()
        self.db.execute("UPDATE threads SET rollout_path=?,archived=1,archived_at=1790035300", (str(archived),))
        self.capture()
        self.assertTrue(self.run_restore()["read_verified"])
        self.assertEqual((self.root / "restored/home/archived_sessions" / archived.name).read_bytes(), original)

    def test_missing_parent_wrong_head_and_unknown_schema_stop_before_native_import(self):
        base = {"thread_id": PARENT, "end_byte_offset": 100, "end_ordinal_exclusive": 5}
        self.write_rollout(THREAD, [meta("paginated", history_base=base)], start=5)
        self.capture()
        with self.assertRaisesRegex(RestoreRefused, "dependency"):
            self.run_restore()
        self.assertFalse((self.root / "restored").exists())
        self.db.execute("UPDATE threads SET rollout_path=?", (str(self.home / "sessions/missing.jsonl"),))
        self.capture()
        with self.assertRaisesRegex(RestoreRefused, "selected head"):
            self.run_restore()
        self.db.execute("ALTER TABLE threads ADD COLUMN future_field TEXT")
        self.capture()
        with self.assertRaisesRegex(RestoreRefused, "schema"):
            self.run_restore()

    def test_invalid_record_does_not_create_native_database(self):
        with self.path.open("ab") as stream:
            stream.write(b'{"future":true}\n')
        self.capture()
        with self.assertRaises(RestoreRefused):
            self.run_restore()
        self.assertFalse((self.root / "restored/sqlite/state_5.sqlite").exists())

    def test_large_prefix_and_repeated_item_snapshot(self):
        records = [meta("paginated")] + copy.deepcopy(self.items)
        records[-1]["payload"]["item"]["content"][0]["text"] = "x" * (9 * 1024 * 1024)
        records.append(copy.deepcopy(self.items[-1]))
        self.write_rollout(THREAD, records)
        self.capture()
        result = self.run_restore()
        self.assertEqual(result["items"], 4)
        self.assertGreater(result["raw_bytes"], 9 * 1024 * 1024)

    def test_completed_turn_state_is_verified(self):
        records = [meta("paginated"), envelope("event_msg", {"type": "task_started", "turn_id": "turn-1", "model_context_window": 100000})]
        records += self.items
        records.append(envelope("event_msg", {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"}))
        self.write_rollout(THREAD, records)
        self.capture()
        self.assertEqual(self.run_restore()["turns"], 1)

    def test_wrong_thread_head_and_legacy_mode_are_refused(self):
        parent = self.write_rollout(PARENT, [meta("paginated", PARENT)], identity=PARENT)
        self.db.execute("UPDATE threads SET rollout_path=?", (str(parent),))
        self.capture()
        with self.assertRaisesRegex(RestoreRefused, "different thread"):
            self.run_restore()
        self.db.execute("UPDATE threads SET rollout_path=?,history_mode='legacy'", (str(self.path),))
        self.capture()
        with self.assertRaisesRegex(RestoreRefused, "paginated"):
            self.run_restore()

    def test_old_runtime_and_existing_identical_prefix_divergent_targets_are_refused(self):
        self.capture()
        with patch("replica.restore.subprocess.check_output", return_value="codex-cli 0.154.0\n"):
            with self.assertRaisesRegex(RestoreRefused, "runtime"):
                self.run_restore()
        raw = self.path.read_bytes()
        for name, content in (("identical", raw), ("prefix", raw.splitlines(keepends=True)[0]), ("divergent", raw + b'{}\n')):
            target = self.root / name
            target.mkdir()
            file = target / self.path.name
            file.write_bytes(content)
            with self.assertRaisesRegex(RestoreRefused, "new directory"):
                self.run_restore(name)
            self.assertEqual(file.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
