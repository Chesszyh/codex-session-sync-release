import json
import base64
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import sys
import unittest
from unittest.mock import patch

from replica.collect import SourceClient, collect
from replica.store import Store

THREAD = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
NEW = "00000000-0000-4000-8000-000000000003"


def line(kind, payload, ordinal):
    return (json.dumps({"type": kind, "payload": payload, "ordinal": ordinal}) + "\n").encode()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.db = sqlite3.connect(self.home / "state_5.sqlite")
        self.db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, archived INTEGER, history_mode TEXT, git_sha TEXT, project_id TEXT)")
        self.db.commit()
        self.store = Store(self.root / "archive")
        self.client = SourceClient(str(self.home), str(self.home))
        self.path = self.add_thread(THREAD)

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.db.close()
        self.temp.cleanup()

    def add_thread(self, identity, base=None):
        path = self.home / "sessions" / ("rollout-2026-09-22T00-00-00-" + identity + ".jsonl")
        meta = {"id": identity, "history_mode": "paginated", "cli_version": "fixture"}
        if base:
            meta["history_base"] = base
        path.write_bytes(line("session_meta", meta, 0) + line("event_msg", {"type": "fixture", "text": "SECRET_SENTINEL"}, 1))
        self.db.execute("INSERT INTO threads VALUES (?,?,?,0,'paginated',?,?)", (identity, str(path), "private title", "old-sha", "project-a"))
        self.db.commit()
        return path

    def run_scan(self, host="fixture", client=None):
        return collect(self.store, client or self.client, host, str(self.home), str(self.home))

    def generation(self, path=None, host="fixture"):
        path = path or self.path
        return self.store.db.execute("SELECT generation FROM files WHERE home_id=? AND path=?", (host+":"+str(self.home), str(path.relative_to(self.home)))).fetchone()[0]

    def exported(self, generation):
        out = self.root / ("export-" + str(len(list(self.root.glob("export-*")))))
        self.store.export(generation, out)
        return out.read_bytes()

    def test_append_partial_utf8_and_restart(self):
        first = self.run_scan()
        self.assertTrue(first["complete"])
        identity = self.generation()
        before = self.path.read_bytes()
        with self.path.open("ab") as f:
            f.write(b'{"text":"\xe4\xb8')
        pending = self.run_scan()
        self.assertEqual(pending["read_bytes"], len(b'{"text":"\xe4\xb8'))
        self.assertEqual(self.exported(identity), before)
        self.store.close()
        self.store = Store(self.root / "archive")
        with self.path.open("ab") as f:
            f.write(b'\xad"}\n')
        self.run_scan()
        self.assertEqual(identity, self.generation())
        self.assertEqual(self.exported(identity), self.path.read_bytes())
        self.assertEqual(self.run_scan()["read_bytes"], 0)

    def test_source_rejects_traversal_symlinks_and_special_files(self):
        secret = self.home / "auth.json"
        secret.write_text('{"credential":"FAKE_CREDENTIAL_ONLY"}\n')
        (self.home / "sessions/leak.jsonl").symlink_to(secret)
        outside = self.root / "outside.jsonl"
        outside.write_text('{"credential":"FAKE_CREDENTIAL_ONLY"}\n')
        (self.home / "sessions/outside.jsonl").symlink_to(outside)
        (self.home / "archived_sessions").symlink_to(self.root, target_is_directory=True)
        (self.home / "sessions/directory").symlink_to(self.home, target_is_directory=True)
        (self.home / "session_index.jsonl").symlink_to(secret)
        os.mkfifo(self.home / "sessions/pipe.jsonl")
        for relative in ("sessions/../auth.json", "sessions/leak.jsonl",
                         "sessions/directory/auth.json", "sessions/outside.jsonl",
                         "archived_sessions/outside.jsonl", "session_index.jsonl",
                         "sessions/pipe.jsonl", str(secret)):
            with self.subTest(relative=relative), self.assertRaises((OSError, ValueError)):
                list(self.client.local.chunks(relative, 0))
        report = self.run_scan()
        self.assertFalse(report["complete"])
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT path FROM files")],
                         [str(self.path.relative_to(self.home))])
        self.assertNotIn(b"FAKE_CREDENTIAL_ONLY", self.exported(self.generation()))

    def test_open_source_cannot_be_redirected_by_path_replacement(self):
        before = self.path.read_bytes()
        reader = self.client.local.chunks(str(self.path.relative_to(self.home)), 0)
        self.assertEqual(next(reader)["kind"], "begin")
        self.path.rename(self.path.with_suffix(".old"))
        secret = self.home / "auth.json"
        secret.write_bytes(b"FAKE_CREDENTIAL_ONLY")
        self.path.symlink_to(secret)
        rows = list(reader)
        self.assertEqual(b"".join(base64.b64decode(r["bytes"]) for r in rows if r["kind"] == "chunk"), before)
        with self.assertRaises(OSError):
            list(self.client.local.chunks(str(self.path.relative_to(self.home)), 0))

    def test_directory_replacement_cannot_redirect_an_open_source(self):
        source = self.client.local
        opened = os.open
        def replace_directory(path, flags, **kwargs):
            fd = opened(path, flags, **kwargs)
            if path == "sessions":
                (self.home / "sessions").rename(self.home / "previous-sessions")
                (self.home / "sessions").symlink_to(self.root, target_is_directory=True)
            return fd
        before = self.path.read_bytes()
        (self.root / self.path.name).write_bytes(b"FAKE_CREDENTIAL_ONLY")
        with patch("replica.source.os.open", side_effect=replace_directory):
            rows = list(source.chunks(str(self.path.relative_to(self.home)), 0))
        self.assertEqual(b"".join(base64.b64decode(r["bytes"]) for r in rows if r["kind"] == "chunk"), before)

    def test_atomic_replacement_and_same_size_rewrite_keep_old_generation(self):
        self.run_scan()
        old = self.generation()
        original = self.path.read_bytes()
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(original.replace(b"SECRET_SENTINEL", b"PUBLIC_SENTINEL"))
        os.replace(replacement, self.path)
        self.run_scan()
        new = self.generation()
        self.assertNotEqual(old, new)
        self.assertEqual(self.exported(old), original)
        self.path.write_bytes(original)
        self.run_scan()
        self.assertNotEqual(new, self.generation())
        self.assertEqual(self.exported(self.generation()), original)

    def test_session_index_name_updates_without_changing_database_title(self):
        index = self.home / "session_index.jsonl"
        index.write_text(json.dumps({"id": THREAD, "thread_name": "Actual name"}) + "\n")
        self.run_scan()
        def current():
            obj = self.store.db.execute("SELECT metadata_object FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]
            return self.store.json(obj)
        self.assertEqual(current().get("thread_name"), "Actual name")
        self.assertEqual(current()["title"], "private title")
        with index.open("a") as f:
            f.write(json.dumps({"id": THREAD, "thread_name": "Renamed"}) + "\n")
            f.write('{"id":')
        self.run_scan()
        self.assertEqual(current()["thread_name"], "Renamed")

    def test_metadata_only_null_and_project_changes_have_revisions(self):
        self.run_scan()
        self.db.execute("UPDATE threads SET title='renamed',git_sha=NULL,project_id='project-b'")
        self.db.commit()
        report = self.run_scan()
        self.assertEqual(report["read_bytes"], 0)
        revisions = list(self.store.db.execute("SELECT object FROM metadata_revisions ORDER BY seq"))
        self.assertEqual(len(revisions), 2)
        latest = self.store.json(revisions[-1][0])
        self.assertIsNone(latest["git_sha"])
        self.assertEqual(latest["project_id"], "project-b")
        self.assertEqual(self.store.json(revisions[0][0])["title"], "private title")

    def test_truncation_keeps_old_bytes_and_missing_parent_is_reported(self):
        self.run_scan()
        original = self.path.read_bytes()
        old = self.generation()
        self.path.write_bytes(original.splitlines(keepends=True)[0])
        self.add_thread(CHILD, {"thread_id": NEW, "end_byte_offset": 100, "end_ordinal_exclusive": 2})
        report = self.run_scan()
        self.assertTrue(report["complete"])
        self.assertNotEqual(self.generation(), old)
        self.assertEqual(self.exported(old), original)
        self.assertEqual(self.exported(self.generation()), self.path.read_bytes())
        self.assertEqual(report["dependencies"]["issues"][0]["kind"], "missing_or_ambiguous_parent")

    def test_pending_migration_does_not_mark_missing_paths_deleted(self):
        self.run_scan()
        old = self.generation()
        self.path.rename(Path(str(self.path) + ".pending"))
        report = self.run_scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"][0]["error"], "pending migration")
        self.assertEqual(self.store.db.execute("SELECT state FROM files").fetchone()[0], "present")
        self.assertGreater(len(self.exported(old)), 0)

    def test_archive_rename_and_compression_preserve_logical_generation(self):
        self.run_scan()
        identity = self.generation()
        original = self.path.read_bytes()
        archive = self.home / "archived_sessions" / self.path.name
        archive.parent.mkdir()
        self.path.rename(archive)
        self.db.execute("UPDATE threads SET archived=1,rollout_path=?", (str(archive),))
        self.db.commit()
        self.run_scan()
        self.assertEqual(self.generation(archive), identity)
        compressed = Path(str(archive) + ".zst")
        subprocess.run(["zstd", "-q", str(archive), "-o", str(compressed)], check=True)
        physical = compressed.read_bytes()
        archive.unlink()
        report = self.run_scan()
        self.assertTrue(report["complete"])
        self.assertEqual(self.generation(compressed), identity)
        self.assertEqual(self.exported(identity), original)
        manifest = self.store.manifest(identity)
        self.assertEqual(self.store.read_range(manifest, 0, len(physical), physical=True), physical)
        inventory = json.loads(subprocess.check_output([sys.executable, "-m", "replica", "--store", str(self.store.root), "inventory"], text=True))
        self.assertEqual(len(inventory["threads"]), 1)
        self.assertEqual(inventory["threads"][0]["state"], "present")
        self.assertEqual(inventory["threads"][0]["generation"], identity)

    def test_fork_parent_revert_uses_retained_old_rollout(self):
        self.run_scan()
        parent = self.generation()
        child = self.add_thread(CHILD, {"thread_id": THREAD, "end_byte_offset": self.path.stat().st_size, "end_ordinal_exclusive": 2})
        report = self.run_scan()
        self.assertEqual(report["dependencies"]["edges"], 1)
        self.assertFalse(report["dependencies"]["issues"])
        new_path = self.path.with_name(self.path.stem + "_" + NEW + ".jsonl")
        new_path.write_bytes(line("session_meta", {"id": THREAD, "history_mode": "paginated"}, 0))
        self.path.unlink()
        self.db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(new_path), THREAD))
        self.db.commit()
        report = self.run_scan()
        self.assertFalse(report["dependencies"]["issues"])
        edge = self.store.db.execute("SELECT parent FROM lineage_edges WHERE child=?", (self.generation(child),)).fetchone()[0]
        self.assertEqual(edge, parent)

    def test_incomplete_scan_and_offline_do_not_remove_history(self):
        self.run_scan()
        before = self.exported(self.generation())
        real = self.client.census
        def partial():
            result = real()
            result["files"] = []
            result["errors"] = [{"error": "scan interrupted"}]
            result["complete"] = False
            return result
        self.client.census = partial
        self.assertFalse(self.run_scan()["complete"])
        self.assertEqual(self.store.db.execute("SELECT state FROM files").fetchone()[0], "present")
        def offline():
            raise ConnectionError("offline")
        self.client.census = offline
        self.assertFalse(self.run_scan()["complete"])
        self.assertEqual(self.exported(self.generation()), before)
        self.client.census = real
        self.add_thread(CHILD)
        report = self.run_scan()
        self.assertTrue(report["complete"])
        self.assertEqual(self.store.status()["threads"], 2)

    def test_local_metadata_failure_finishes_scan_and_recovers(self):
        self.assertTrue(self.run_scan()["complete"])
        generation = self.generation()
        original = self.exported(generation)
        with patch.object(self.client.local, "metadata", side_effect=[self.client.local.metadata(), sqlite3.OperationalError("database is locked")]):
            report = self.run_scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"], [{"error": "source metadata read failed: database is locked"}])
        scan = self.store.db.execute("SELECT finished,complete FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(scan["finished"])
        self.assertEqual(scan["complete"], 0)
        self.assertEqual(self.exported(generation), original)
        self.assertEqual(self.store.db.execute("SELECT state FROM files").fetchone()[0], "present")
        self.assertTrue(self.run_scan()["complete"])

    def test_same_id_different_hosts_are_separate(self):
        self.run_scan("A")
        self.path.write_bytes(self.path.read_bytes().replace(b"SECRET_SENTINEL", b"PUBLIC_SENTINEL"))
        self.run_scan("B")
        self.assertNotEqual(self.generation(host="A"), self.generation(host="B"))
        self.assertEqual(self.store.status()["threads"], 2)

    def test_source_failure_before_commit_can_resume(self):
        real = self.client.chunks
        def broken(*args, **kwargs):
            for row in real(*args, **kwargs):
                if row["kind"] == "end":
                    raise ConnectionError("transport interrupted before final frame")
                yield row
        self.client.chunks = broken
        self.assertFalse(self.run_scan()["complete"])
        self.assertEqual(self.store.status()["generations"], 0)
        self.client.chunks = real
        self.assertTrue(self.run_scan()["complete"])
        self.assertEqual(self.exported(self.generation()), self.path.read_bytes())

    def test_encryption_unknown_and_invalid_lines_preserve_exact_bytes(self):
        with self.path.open("ab") as f:
            f.write(b'{"type":"future_record","value":"SECRET_SENTINEL"}\n{bad json}\n')
        original = self.path.read_bytes()
        self.run_scan()
        self.assertEqual(self.exported(self.generation()), original)
        for obj in self.store.objects.glob("*/*"):
            self.assertNotIn(b"SECRET_SENTINEL", obj.read_bytes())
        self.assertEqual((self.store.root / "archive.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unsupported_metadata_and_dependency_preserve_raw_and_report_gaps(self):
        self.path.write_bytes(b'[]\n{bad json}\n')
        report = self.run_scan()
        self.assertTrue(report["complete"])
        self.assertEqual(self.exported(self.generation()), self.path.read_bytes())
        self.assertIsNotNone(self.store.manifest(self.generation())["source_warning"])
        child = self.add_thread(CHILD, {"future_parent_format": True})
        report = self.run_scan()
        self.assertTrue(report["complete"])
        self.assertEqual(report["dependencies"]["issues"][0]["kind"], "unsupported_history_base")
        self.assertEqual(self.exported(self.generation(child)), child.read_bytes())
        self.assertEqual(self.store.status()["homes"][0]["last_scan"]["dependency_issues"], 1)

    def test_reader_sees_committed_archive_while_collector_is_open(self):
        self.run_scan()
        reader = Store(self.root / "archive", readonly=True)
        try:
            self.assertEqual(reader.status()["seq"], self.store.status()["seq"])
            output = self.root / "reader.jsonl"
            reader.export(self.generation(), output)
            self.assertEqual(output.read_bytes(), self.path.read_bytes())
        finally:
            reader.close()

    def test_rename_after_interrupted_checkpoint_finishes_remaining_bytes(self):
        with self.path.open("ab") as stream:
            stream.write(b'{"large":"' + b"x" * (10 * 1048576) + b'"}\n')
        real = self.client.chunks
        def interrupted(*args, **kwargs):
            for row in real(*args, **kwargs):
                if row["kind"] == "chunk" and row["start"] >= 8 * 1048576:
                    raise ConnectionError("interrupted after checkpoint")
                yield row
        self.client.chunks = interrupted
        self.assertFalse(self.run_scan()["complete"])
        self.assertLess(int(self.store.manifest(self.generation())["read_end"]), self.path.stat().st_size)
        archive = self.home / "archived_sessions" / self.path.name
        archive.parent.mkdir()
        self.path.rename(archive)
        self.db.execute("UPDATE threads SET rollout_path=?,archived=1", (str(archive),))
        self.db.commit()
        self.client.chunks = real
        self.assertTrue(self.run_scan()["complete"])
        self.assertEqual(self.exported(self.generation(archive)), archive.read_bytes())

    def test_killed_collector_resumes_after_committed_chunk_checkpoint(self):
        with self.path.open("ab") as stream:
            stream.write(b'{"large":"')
            for _ in range(24):
                stream.write(b"x" * 1048576)
            stream.write(b'"}\n')
        archive = self.root / "crash-archive"
        script = """
import sys,time
from replica.store import Store
from replica.collect import SourceClient,collect
s=Store(sys.argv[1]); c=SourceClient(sys.argv[2],sys.argv[2]); real=c.chunks
def slow(*args,**kwargs):
    size=0
    for row in real(*args,**kwargs):
        yield row
        if row['kind']=='chunk':
            size+=1048576
            if size==8*1048576: time.sleep(5)
c.chunks=slow
collect(s,c,'crash',sys.argv[2],sys.argv[2])
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(archive), str(self.home)])
        checkpoint = 0
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (archive / "archive.sqlite").exists():
                    try:
                        check = sqlite3.connect((archive / "archive.sqlite").as_uri()+"?mode=ro", uri=True)
                        row = check.execute("SELECT read_end FROM generations").fetchone()
                        check.close()
                        if row:
                            checkpoint = row[0]
                            break
                    except sqlite3.Error:
                        pass
                time.sleep(0.02)
            self.assertGreater(checkpoint, 0)
        finally:
            child.kill()
            child.wait()
        resumed = Store(archive)
        try:
            report = collect(resumed, self.client, "crash", str(self.home), str(self.home))
            self.assertTrue(report["complete"])
            self.assertEqual(report["read_bytes"], self.path.stat().st_size-checkpoint)
            self.assertEqual(resumed.status()["generations"], 1)
            self.assertEqual(resumed.status()["committed_bytes"], self.path.stat().st_size)
        finally:
            resumed.close()


if __name__ == "__main__":
    unittest.main()
