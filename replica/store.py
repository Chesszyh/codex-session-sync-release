"""Encrypted immutable objects and transactionally committed archive inventory."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def ensure_directory(path):
    if path.is_dir():
        return
    ensure_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path, data):
    ensure_directory(path.parent)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root, readonly=False):
        self.root = Path(root).resolve()
        self.lock = None
        self.new_object_bytes = 0
        if readonly:
            self.objects = self.root / "objects"
            self.cipher = AESGCM((self.root / "archive.key").read_bytes())
            self.db = sqlite3.connect((self.root / "archive.sqlite").as_uri() + "?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            return
        ensure_directory(self.root)
        os.chmod(self.root, 0o700)
        self.lock = (self.root / "collector.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.objects = self.root / "objects"
        ensure_directory(self.objects)
        key = self.root / "archive.key"
        if not key.exists():
            if (self.root / "archive.sqlite").exists():
                raise RuntimeError("archive key is missing; do not create a replacement key")
            durable_write(key, AESGCM.generate_key(bit_length=256))
        self.cipher = AESGCM(key.read_bytes())
        self.db = sqlite3.connect(self.root / "archive.sqlite", timeout=30)
        os.chmod(self.root / "archive.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS homes (
          id TEXT PRIMARY KEY, host TEXT NOT NULL, home TEXT NOT NULL, sqlite_home TEXT NOT NULL,
          last_attempt TEXT, last_complete TEXT, status TEXT, inventory_object TEXT);
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY, home_id TEXT NOT NULL, started TEXT NOT NULL,
          finished TEXT, complete INTEGER NOT NULL DEFAULT 0, report_object TEXT);
        CREATE TABLE IF NOT EXISTS generations (
          id TEXT PRIMARY KEY, home_id TEXT NOT NULL, rollout_id TEXT NOT NULL, thread_id TEXT,
          read_end INTEGER NOT NULL, committed_end INTEGER NOT NULL, manifest_object TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (
          home_id TEXT NOT NULL, path TEXT NOT NULL, generation TEXT NOT NULL, stamp TEXT NOT NULL,
          encoding TEXT NOT NULL, last_scan INTEGER NOT NULL, state TEXT NOT NULL,
          PRIMARY KEY(home_id,path));
        CREATE TABLE IF NOT EXISTS metadata_revisions (
          seq INTEGER PRIMARY KEY, home_id TEXT NOT NULL, thread_id TEXT NOT NULL, object TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS threads (
          home_id TEXT NOT NULL, thread_id TEXT NOT NULL, metadata_object TEXT NOT NULL,
          head_path TEXT, archived INTEGER, last_scan INTEGER NOT NULL,
          PRIMARY KEY(home_id,thread_id));
        CREATE TABLE IF NOT EXISTS changes (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, home_id TEXT NOT NULL, kind TEXT NOT NULL,
          entity TEXT NOT NULL, object TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lineage_edges (
          child TEXT PRIMARY KEY, parent TEXT NOT NULL, end_byte TEXT NOT NULL, end_ordinal TEXT NOT NULL);
        """)
        self.new_object_bytes = 0

    def close(self):
        self.db.close()
        if self.lock:
            self.lock.close()

    def put(self, data):
        identity = hashlib.sha256(data).hexdigest()
        path = self.objects / identity[:2] / identity[2:]
        if not path.exists():
            nonce = os.urandom(12)
            durable_write(path, nonce + self.cipher.encrypt(nonce, data, identity.encode()))
            self.new_object_bytes += len(data)
        return identity

    def get(self, identity):
        encrypted = (self.objects / identity[:2] / identity[2:]).read_bytes()
        plain = self.cipher.decrypt(encrypted[:12], encrypted[12:], identity.encode())
        if hashlib.sha256(plain).hexdigest() != identity:
            raise RuntimeError("archive object identity mismatch")
        return plain

    def put_json(self, value):
        return self.put(encode(value))

    def json(self, identity):
        return json.loads(self.get(identity))

    def change(self, home, kind, entity, obj):
        return self.db.execute("INSERT INTO changes(home_id,kind,entity,object) VALUES (?,?,?,?)", (home, kind, entity, obj)).lastrowid

    def register(self, host, home, sqlite_home):
        identity = host + ":" + home
        row = self.db.execute("SELECT * FROM homes WHERE id=?", (identity,)).fetchone()
        if row and row["sqlite_home"] != sqlite_home:
            raise ValueError("sqlite_home changed for an existing source identity")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO homes(id,host,home,sqlite_home,status) VALUES (?,?,?,?,?)", (identity, host, home, sqlite_home, "not_scanned"))
        return identity

    def manifest(self, generation):
        row = self.db.execute("SELECT manifest_object FROM generations WHERE id=?", (generation,)).fetchone()
        return self.json(row[0])

    def read_range(self, manifest, start, length, physical=False):
        end = start + length
        output = bytearray()
        for segment in manifest["physical_segments" if physical else "segments"]:
            lo, hi = int(segment["start"]), int(segment["end"])
            if lo < end and hi > start:
                data = self.get(segment["object"])
                output.extend(data[max(start-lo, 0):min(end-lo, hi-lo)])
        if len(output) != length:
            raise RuntimeError("archive segment gap")
        return bytes(output)

    def export(self, generation, output):
        manifest = self.manifest(generation)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
            end = int(manifest["committed_end"])
            for segment in manifest["segments"]:
                start = int(segment["start"])
                if start >= end:
                    break
                target.write(self.get(segment["object"])[:min(int(segment["end"]), end)-start])
        return {"bytes": end, "generation": generation, "encoding": "jsonl"}

    def status(self):
        homes = [dict(row) for row in self.db.execute("SELECT id,status,last_attempt,last_complete FROM homes")]
        for home in homes:
            latest = self.db.execute("SELECT id,report_object FROM scans WHERE home_id=? AND finished IS NOT NULL ORDER BY id DESC LIMIT 1", (home["id"],)).fetchone()
            if latest:
                report = self.json(latest["report_object"])
                home["last_scan"] = {"id": latest["id"], "complete": report["complete"], "errors": len(report["errors"]),
                                     "dependency_issues": len(report["dependencies"]["issues"]) if "dependencies" in report else None}
        return {"homes": homes,
                "files": self.db.execute("SELECT count(*) FROM files").fetchone()[0],
                "generations": self.db.execute("SELECT count(*) FROM generations").fetchone()[0],
                "threads": self.db.execute("SELECT count(*) FROM threads").fetchone()[0],
                "committed_bytes": self.db.execute("SELECT coalesce(sum(committed_end),0) FROM generations").fetchone()[0],
                "seq": self.db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]}
