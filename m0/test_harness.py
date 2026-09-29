import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from audit import audit
from extract_fixture import sanitize
from fixtures import generate
from run import assess


class HarnessTests(unittest.TestCase):
    def test_audit_reports_database_only_threads_without_reading_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = sqlite3.connect(root / "state_5.sqlite")
            db.execute("CREATE TABLE threads (history_mode TEXT, archived INTEGER, rollout_path TEXT, first_user_message TEXT)")
            db.execute("INSERT INTO threads VALUES ('paginated',1,?,?)", (str(root / "missing.jsonl"), "PRIVATE_SENTINEL"))
            db.commit()
            db.close()
            result = audit(root, root)
            table = result["databases"]["state_5.sqlite"]["tables"]["threads"]
            self.assertEqual(table["selected_paths_missing"], 1)
            self.assertEqual(table["history_mode"], {"paginated": 1})
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
            self.assertEqual(sqlite3.connect(root / "state_5.sqlite").execute("SELECT first_user_message FROM threads").fetchone()[0], "PRIVATE_SENTINEL")

    def test_census_failure_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            (root / "sessions/bad.jsonl").write_bytes(b"bad\n")
            result = audit(root, root)
            self.assertFalse(result["directories"]["sessions"]["complete"])
            self.assertTrue(result["errors"])

    def test_redaction_preserves_enum_not_private_text(self):
        source = {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": "PRIVATE_SENTINEL"}]}
        result = sanitize(source)
        self.assertEqual(result["phase"], "final_answer")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_matching_selected_text_does_not_hide_wrong_head(self):
        case = {"name": "selected-head", "expected_text": ["new"], "forbidden_text": ["old"], "expected_warning": None}
        self.assertEqual(assess(case, {"text": "new old"})["transcript"], "unsupported")

    def test_redaction_preserves_path_uri_wire_type(self):
        self.assertEqual(sanitize("file:///private/work", "cwd"), "file:///fixture/workspace")
        self.assertEqual(sanitize("/private/work", "cwd"), "/fixture/workspace")

    def test_empty_result_is_not_missing_dependency_success(self):
        case = {"name": "missing-parent", "expected_text": [], "forbidden_text": [], "expected_warning": "missing dependency"}
        result = assess(case, {"text": ""})
        self.assertEqual(result["transcript"], "untested")
        self.assertEqual(result["warning_coverage"], "unsupported")

    def test_fixture_dependencies_and_incomplete_byte_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = {c["name"]: c for c in generate(Path(directory))}
            partial = cases["partial-utf8"]
            content = Path(partial["path"]).read_bytes()
            self.assertEqual(content[partial["complete_end"] - 1:partial["complete_end"]], b"\n")
            self.assertTrue(content.endswith(b"\xe4\xb8"))
            child = json.loads(Path(cases["referenced-fork"]["path"]).read_text())
            base = child["payload"]["history_base"]
            parent = next(p for p in Path(cases["referenced-fork"]["home"]).rglob("*.jsonl") if base["thread_id"] in p.name)
            self.assertEqual(base["end_byte_offset"], parent.stat().st_size)
            self.assertEqual(child["ordinal"], base["end_ordinal_exclusive"])


if __name__ == "__main__":
    unittest.main()
