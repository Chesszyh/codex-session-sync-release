"""Opt-in integration check against the installed official runtime."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from read_history import read_history

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.environ.get("M0_CODEX_BINARY"), "set M0_CODEX_BINARY to run official history integration")
class OfficialHistoryTests(unittest.TestCase):
    def test_paginated_history_contains_all_four_items(self):
        parent = ROOT / ".m0/history-integration"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            source = next((ROOT / ".m0/fixtures/real-local/home").rglob("*.jsonl"))
            output = root / "read"
            summary = read_history(source, output, os.environ["M0_CODEX_BINARY"], page_size=2)
            result = json.loads((output / "history.json").read_text())
            self.assertEqual(summary["items"], 4)
            self.assertEqual(summary["loaded_threads"], 0)
            self.assertTrue(summary["raw_unchanged"])
            text = json.dumps(result["items"])
            for expected in ("M0_UserMessage", "M0_AgentMessage_commentary", "M0_AgentMessage_final", "M0_TOOL"):
                self.assertIn(expected, text)

    def test_invalid_wire_enum_is_not_silently_dropped(self):
        parent = ROOT / ".m0/history-integration"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            source = next((ROOT / ".m0/fixtures/real-local/home").rglob("*.jsonl"))
            invalid = root / source.name
            invalid.write_bytes(source.read_bytes().replace(b'"phase": "final_answer"', b'"phase": "M0_REDACTED"'))
            with self.assertRaisesRegex(RuntimeError, "decoder rejected"):
                read_history(invalid, root / "read", os.environ["M0_CODEX_BINARY"])
            self.assertFalse(json.loads((root / "read/coverage.json").read_text())["complete"])
            self.assertFalse((root / "read/history.json").exists())

    def test_completed_item_update_replaces_earlier_snapshot(self):
        parent = ROOT / ".m0/history-integration"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            source = next((ROOT / ".m0/fixtures/item-update/home").rglob("*.jsonl"))
            output = Path(directory) / "read"
            summary = read_history(source, output, os.environ["M0_CODEX_BINARY"], page_size=2)
            text = (output / "history.json").read_text()
            self.assertEqual(summary["items"], 4)
            self.assertIn("M0_UPDATED_ITEM", text)
            self.assertNotIn("M0_AgentMessage_commentary", text)


if __name__ == "__main__":
    unittest.main()
