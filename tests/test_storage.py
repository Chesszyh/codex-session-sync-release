import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from replica.store import OBJECT_HEADER, Store, durable_write, encode
from scripts.repack_objects import repack


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def path(self, identity):
        return self.store.objects / identity[:2] / identity[2:]

    def legacy(self, data, nonce=None):
        identity = hashlib.sha256(data).hexdigest()
        nonce = os.urandom(12) if nonce is None else nonce
        durable_write(self.path(identity), nonce + self.store.cipher.encrypt(nonce, data, identity.encode()))
        return identity

    def test_compressed_roundtrip_identity_restart_and_legacy(self):
        data = encode({"text": "中文 session history " * 10000})
        identity = self.store.put(data)
        self.assertEqual(identity, hashlib.sha256(data).hexdigest())
        self.assertLess(self.path(identity).stat().st_size, len(data) // 10)
        stored = self.path(identity).read_bytes()
        self.assertEqual(self.store.put(data), identity)
        self.assertEqual(self.path(identity).read_bytes(), stored)
        old = self.legacy(b"old archive content")
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.get(identity), data)
        self.assertEqual(self.store.get(old), b"old archive content")

    def test_legacy_nonce_can_match_new_format_header(self):
        identity = self.legacy(b"legacy header collision", OBJECT_HEADER + b"1234")
        self.assertEqual(self.store.get(identity), b"legacy header collision")

    def test_repack_preview_apply_restart_and_legacy_rollback(self):
        data = b"old repeated history" * 10000
        identity = self.legacy(data)
        census = self.census()
        inventory = self.store.put_inventory(census)
        before = {p: p.read_bytes() for p in self.store.objects.glob("*/*")}
        preview = repack(self.store)
        self.assertGreater(preview["candidates"], 0)
        self.assertEqual(preview["rewritten"], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.objects.glob("*/*")})
        applied = repack(self.store, apply=True)
        self.assertEqual(applied["after_bytes"], preview["after_bytes"])
        self.assertEqual(self.store.get(identity), data)
        self.store.close()
        self.store = Store(self.root)
        repack(self.store, apply=True, uncompressed=True)
        for obj, expected in ((identity, data), (inventory, encode(census))):
            encrypted = self.path(obj).read_bytes()
            self.assertEqual(self.store.cipher.decrypt(encrypted[:12], encrypted[12:], obj.encode()), expected)
            self.assertEqual(self.store.get(obj), expected)
        self.assertEqual(self.store.json(inventory), census)

    def test_repack_interruption_retains_old_object(self):
        data = b"history" * 10000
        identity = self.legacy(data)
        before = self.path(identity).read_bytes()
        with patch("replica.store.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                repack(self.store, apply=True)
        self.assertEqual(self.path(identity).read_bytes(), before)
        self.assertEqual(self.store.get(identity), data)
        repack(self.store, apply=True)
        self.assertEqual(self.store.get(identity), data)

    def test_incompressible_and_small_objects_do_not_expand(self):
        for data in (b"", b"x", os.urandom(32768)):
            identity = self.store.put(data)
            self.assertEqual(self.store.get(identity), data)
            self.assertEqual(self.path(identity).stat().st_size, len(data) + 28)

    def test_authentication_and_plaintext_identity(self):
        identity = self.store.put(b"repeat" * 10000)
        path = self.path(identity)
        encrypted = path.read_bytes()
        path.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
        with self.assertRaises(InvalidTag):
            self.store.get(identity)
        wrong = "0" * 64
        nonce = os.urandom(12)
        durable_write(self.path(wrong), nonce + self.store.cipher.encrypt(nonce, b"wrong", wrong.encode()))
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.store.get(wrong)

    def census(self):
        return {"home": "/fixture", "sqlite_home": "/fixture", "complete": True, "errors": [],
                "files": [{"path": f"sessions/{i}.jsonl", "stamp": {"size": i}} for i in range(1024)],
                "metadata": {"schema": {"threads": ["id", "title"]}, "tables": {
                    "threads": [{"id": str(i), "title": hashlib.sha256(str(i).encode()).hexdigest() * 8} for i in range(1024)],
                    "projects": []}, "thread_names": {"name": "中文"}},
                "metadata_after_capture": None, "extra": {"unknown": [1, None]}}

    def test_inventory_reuses_chunks_without_changing_logical_bytes(self):
        first = self.census()
        original = copy.deepcopy(first)
        identity = self.store.put_inventory(first)
        self.assertEqual(first, original)
        self.assertEqual(identity, hashlib.sha256(encode(first)).hexdigest())
        self.assertEqual(self.store.get(identity), encode(first))
        before = {str(p): p.stat().st_size for p in self.store.objects.glob("*/*")}
        self.assertEqual(self.store.put_inventory(first), identity)
        self.assertEqual(before, {str(p): p.stat().st_size for p in self.store.objects.glob("*/*")})
        second = copy.deepcopy(first)
        second["metadata"]["tables"]["threads"][500]["title"] = "updated"
        second["metadata_after_capture"] = second["metadata"]
        second_id = self.store.put_inventory(second)
        added = [p for p in self.store.objects.glob("*/*") if str(p) not in before]
        self.assertLess(sum(p.stat().st_size for p in added), 15000)
        self.assertLessEqual(len(added), 2)
        self.store.close()
        self.store = Store(self.root, readonly=True)
        self.assertEqual(self.store.json(identity), first)
        self.assertEqual(self.store.json(second_id), second)
        self.assertEqual(self.store.get(second_id), encode(second))

    def test_interrupted_inventory_publication_can_retry(self):
        census = self.census()
        identity = hashlib.sha256(encode(census)).hexdigest()
        original = durable_write
        def fail_manifest(path, data):
            if path == self.path(identity):
                raise OSError("interrupted")
            original(path, data)
        with patch("replica.store.durable_write", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.store.put_inventory(census)
        self.assertFalse(self.path(identity).exists())
        self.assertEqual(self.store.json(self.store.put_inventory(census)), census)


if __name__ == "__main__":
    unittest.main()
