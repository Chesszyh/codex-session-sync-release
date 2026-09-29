"""Full census plus resumable raw capture over local files or SSH."""
import base64
import datetime
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import uuid

from .source import Source


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class SourceClient:
    def __init__(self, home, sqlite_home, ssh=None):
        self.local = Source(home, sqlite_home) if ssh is None else None
        self.process = None
        self.source_bytes_received = 0
        self.transport_bytes_received = 0
        if ssh:
            code = Path(__file__).with_name("source.py").read_bytes().hex()
            remote = shlex.join(["python3", "-u", "-c", "exec(bytes.fromhex(" + repr(code) + "))", "--home", home, "--sqlite-home", sqlite_home])
            self.process = subprocess.Popen(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", ssh, remote],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def request(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def receive(self):
        line = self.process.stdout.readline()
        if not line:
            raise ConnectionError("source transport closed")
        self.transport_bytes_received += len(line)
        value = json.loads(line)
        if value["kind"] == "error":
            raise RuntimeError(value["error"])
        return value

    def census(self):
        if self.local:
            return self.local.census()
        self.request({"method": "census"})
        return self.receive()

    def metadata(self):
        if self.local:
            try:
                return self.local.metadata()
            except sqlite3.Error as error:
                raise RuntimeError(f"source metadata read failed: {error}") from error
        self.request({"method": "metadata"})
        return self.receive()["metadata"]

    def chunks(self, path, start=0, physical=False, length=None):
        if self.local:
            for row in self.local.chunks(path, start, physical, length):
                if row["kind"] == "chunk":
                    self.source_bytes_received += len(row["bytes"]) * 3 // 4 - row["bytes"].count("=")
                yield row
            return
        self.request({"method": "read", "path": path, "start": start, "physical": physical, "length": length})
        while True:
            row = self.receive()
            if row["kind"] == "chunk":
                self.source_bytes_received += len(row["bytes"]) * 3 // 4 - row["bytes"].count("=")
            yield row
            if row["kind"] == "end":
                return

    def close(self):
        if self.process:
            self.process.stdin.close()
            self.process.stdout.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)


def fetch_small(client, path, start, length):
    data = bytearray()
    for row in client.chunks(path, start, length=length):
        if row["kind"] == "chunk":
            data.extend(base64.b64decode(row["bytes"]))
    return bytes(data)


def publish(store, home, file, scan, manifest, stamp, in_progress=False):
    obj = store.put_json(manifest)
    stamp = {**stamp, **({"in_progress": True} if in_progress else {})}
    with store.db:
        store.db.execute("INSERT INTO generations VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET read_end=excluded.read_end,committed_end=excluded.committed_end,manifest_object=excluded.manifest_object",
            (manifest["generation"], home, file["rollout_id"], file["thread_id"], int(manifest["read_end"]), int(manifest["committed_end"]), obj))
        store.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?) ON CONFLICT(home_id,path) DO UPDATE SET generation=excluded.generation,stamp=excluded.stamp,encoding=excluded.encoding,last_scan=excluded.last_scan,state='present'",
            (home, file["path"], manifest["generation"], json.dumps(stamp, sort_keys=True), file["encoding"], scan, "present"))
        store.change(home, "raw", file["path"], obj)


def capture(store, client, home, file, scan):
    previous = store.db.execute("SELECT * FROM files WHERE home_id=? AND path=?", (home, file["path"])).fetchone()
    if previous and json.loads(previous["stamp"]) == file["stamp"]:
        with store.db:
            store.db.execute("UPDATE files SET last_scan=?,state='present' WHERE home_id=? AND path=?", (scan, home, file["path"]))
        return {"read_bytes": 0, "new_generation": False, "skipped": True}
    old = store.manifest(previous["generation"]) if previous else None
    if old is None:
        matching = store.db.execute("SELECT id FROM generations WHERE home_id=? AND rollout_id=? ORDER BY rowid DESC LIMIT 1", (home, file["rollout_id"])).fetchone()
        if matching:
            old = store.manifest(matching[0])
            aliases = store.db.execute("SELECT * FROM files WHERE home_id=? AND generation=?", (home, matching[0])).fetchall()
            for alias in aliases:
                before = json.loads(alias["stamp"])
                if not before.get("in_progress") and alias["encoding"] == file["encoding"] and all(before[k] == file["stamp"][k] for k in ("device", "inode", "size", "mtime_ns")):
                    with store.db:
                        store.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (home, file["path"], matching[0], json.dumps(file["stamp"], sort_keys=True), file["encoding"], scan, "present"))
                        store.change(home, "path", file["path"], store.put_json({"path": file["path"], "generation": matching[0]}))
                    return {"read_bytes": 0, "new_generation": False, "skipped": True}
    start = 0
    if previous and file["encoding"] == "jsonl":
        stamp = json.loads(previous["stamp"])
        unchanged_identity = (stamp["device"], stamp["inode"]) == (file["stamp"]["device"], file["stamp"]["inode"])
        if unchanged_identity and (file["stamp"]["size"] > stamp["size"] or stamp.get("in_progress")):
            boundary = int(old["read_end"])
            length = min(boundary, 65536)
            if fetch_small(client, file["path"], boundary-length, length) == store.read_range(old, boundary-length, length):
                start = boundary
    segments = list(old["segments"]) if start else []
    committed = int(old["committed_end"]) if start else 0
    read_end, read_bytes = start, 0
    prefix_equal = old is not None
    candidate_generation = old["generation"] if start else str(uuid.uuid4())
    last_checkpoint = start
    def manifest_for(generation, physical):
        return {"schema_version": 1, "generation": generation, "origin": home,
                "path": file["path"], "thread_id": file["thread_id"], "rollout_id": file["rollout_id"],
                "history_mode": file["history_mode"], "source_runtime": file["runtime"],
                "history_base": file["history_base"], "encoding": file["encoding"],
                "read_end": str(read_end), "committed_end": str(committed),
                "segments": segments, "physical_segments": physical, "source_warning": file["warning"],
                "record_validation": "newline boundary only; syntax and unknown types remain for the versioned normalizer"}
    for row in client.chunks(file["path"], start):
        if row["kind"] == "begin":
            expected_stamp, observed = file["stamp"], row["stamp"]
            if expected_stamp != observed and not (
                (expected_stamp["device"], expected_stamp["inode"]) == (observed["device"], observed["inode"])
                and observed["size"] > expected_stamp["size"] and file["encoding"] == "jsonl"):
                raise RuntimeError("source changed after census")
            captured_stamp = observed
            continue
        if row["kind"] == "end":
            if file["encoding"] == "zstd" and not row["stable"]:
                raise RuntimeError("compressed source changed during capture")
            if not row["stable"] and not row["append_only_observation"]:
                raise RuntimeError("source replaced or truncated during capture")
            captured_stamp = row["before"]
            continue
        data = base64.b64decode(row["bytes"])
        offset = row["start"]
        if offset != read_end:
            raise RuntimeError("source chunk gap")
        if not start and prefix_equal:
            compare = min(len(data), max(0, int(old["read_end"])-offset))
            if compare and data[:compare] != store.read_range(old, offset, compare):
                prefix_equal = False
        obj = store.put(data)
        read_end += len(data)
        read_bytes += len(data)
        segments.append({"start": str(offset), "end": str(read_end), "object": obj})
        newline = data.rfind(b"\n")
        if newline >= 0:
            committed = offset + newline + 1
        if (old is None or start) and file["encoding"] == "jsonl" and read_end-last_checkpoint >= 8 * 1024 * 1024:
            publish(store, home, file, scan, manifest_for(candidate_generation, []), captured_stamp, in_progress=True)
            last_checkpoint = read_end
    if old and read_end < int(old["read_end"]):
        prefix_equal = False
    same = bool(old and (start or prefix_equal))
    generation = old["generation"] if same else candidate_generation
    physical = []
    if file["encoding"] == "zstd":
        for row in client.chunks(file["path"], physical=True):
            if row["kind"] == "begin":
                continue
            if row["kind"] == "chunk":
                data = base64.b64decode(row["bytes"])
                read_bytes += len(data)
                physical.append({"start": str(row["start"]), "end": str(row["start"]+len(data)), "object": store.put(data)})
            elif row["before"] != captured_stamp or not row["stable"]:
                raise RuntimeError("compressed physical and logical snapshots differ")
    publish(store, home, file, scan, manifest_for(generation, physical), captured_stamp)
    return {"read_bytes": read_bytes, "new_generation": not same, "skipped": False}


def metadata_commit(store, home, metadata, scan, source_home):
    for row in metadata["tables"]["threads"]:
        row = dict(row)
        name = metadata.get("thread_names", {}).get(row["id"])
        if name:
            row["thread_name"] = name
        obj = store.put_json(row)
        identity = row["id"]
        previous = store.db.execute("SELECT metadata_object FROM threads WHERE home_id=? AND thread_id=?", (home, identity)).fetchone()
        raw_path = row.get("rollout_path")
        try:
            path = str(Path(raw_path).relative_to(source_home)) if raw_path else None
        except ValueError:
            path = None
        with store.db:
            if not previous or previous[0] != obj:
                seq = store.change(home, "metadata", identity, obj)
                store.db.execute("INSERT INTO metadata_revisions VALUES (?,?,?,?)", (seq, home, identity, obj))
            store.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?) ON CONFLICT(home_id,thread_id) DO UPDATE SET metadata_object=excluded.metadata_object,head_path=excluded.head_path,archived=excluded.archived,last_scan=excluded.last_scan",
                (home, identity, obj, path, row.get("archived"), scan))


def dependency_report(store, home):
    files = list(store.db.execute("SELECT * FROM files WHERE home_id=? AND state='present'", (home,)))
    generations = {row["id"]: store.manifest(row["id"]) for row in store.db.execute("SELECT id FROM generations WHERE home_id=?", (home,))}
    by_rollout = {}
    for identity, manifest in generations.items():
        by_rollout.setdefault(manifest["rollout_id"], []).append((identity, manifest))
    edges, errors = {}, []
    for identity, manifest in generations.items():
        base = manifest.get("history_base")
        if not base:
            continue
        try:
            parent_rollout = base["thread_id"]
            cutoff = int(base["end_byte_offset"])
            ordinal = int(base["end_ordinal_exclusive"])
            if not isinstance(parent_rollout, str) or cutoff < 0 or ordinal < 0:
                raise ValueError("invalid history base")
        except (KeyError, TypeError, ValueError):
            errors.append({"generation": identity, "kind": "unsupported_history_base"})
            continue
        saved = store.db.execute("SELECT parent FROM lineage_edges WHERE child=?", (identity,)).fetchone()
        candidates = [(saved[0], generations[saved[0]])] if saved else by_rollout.get(parent_rollout, [])
        valid = [(key, m) for key, m in candidates if int(m["committed_end"]) >= cutoff]
        if len(valid) != 1:
            errors.append({"generation": identity, "kind": "missing_or_ambiguous_parent"})
            continue
        parent, parent_manifest = valid[0]
        if cutoff and store.read_range(parent_manifest, cutoff-1, 1) != b"\n":
            errors.append({"generation": identity, "kind": "cutoff_not_record_boundary"})
            continue
        if cutoff:
            tail = store.read_range(parent_manifest, max(0, cutoff-1048576), min(cutoff, 1048576))
            try:
                last = json.loads(tail.rstrip(b"\n").split(b"\n")[-1])
                if int(last["ordinal"]) + 1 != ordinal:
                    raise ValueError("ordinal mismatch")
            except (ValueError, KeyError, TypeError):
                errors.append({"generation": identity, "kind": "ordinal_cutoff_unproven"})
                continue
        edges[identity] = parent
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO lineage_edges VALUES (?,?,?,?)", (identity, parent, str(cutoff), str(base["end_ordinal_exclusive"])))
    for identity in edges:
        seen, cursor = set(), identity
        while cursor in edges:
            if cursor in seen:
                errors.append({"generation": identity, "kind": "lineage_cycle"})
                break
            seen.add(cursor)
            cursor = edges[cursor]
    for thread in store.db.execute("SELECT * FROM threads WHERE home_id=?", (home,)):
        path = thread["head_path"]
        matching = [f for f in files if f["path"] == path or (path and f["path"] == path + ".zst")]
        if not matching:
            errors.append({"thread_id": thread["thread_id"], "kind": "selected_head_missing"})
    return {"edges": len(edges), "issues": errors}


def collect(store, client, host, source_home, sqlite_home):
    home = store.register(host, source_home, sqlite_home)
    started = now()
    received_before = client.source_bytes_received
    transport_before = client.transport_bytes_received
    objects_before = store.new_object_bytes
    with store.db:
        scan = store.db.execute("INSERT INTO scans(home_id,started) VALUES (?,?)", (home, started)).lastrowid
        store.db.execute("UPDATE homes SET last_attempt=?,status='scanning' WHERE id=?", (started, home))
    report = {"scan": scan, "read_bytes": 0, "new_generations": 0, "skipped_files": 0, "errors": [], "complete": False}
    try:
        census = client.census()
        if census["home"] != source_home or census["sqlite_home"] != sqlite_home:
            raise ValueError("effective source roots differ from configured identity")
        report["errors"].extend(census["errors"])
        for file in census["files"]:
            try:
                result = capture(store, client, home, file, scan)
                report["read_bytes"] += result["read_bytes"]
                report["new_generations"] += result["new_generation"]
                report["skipped_files"] += result["skipped"]
            except (OSError, ValueError, RuntimeError) as error:
                report["errors"].append({"path": file["path"], "error": str(error)})
        if census["metadata"] is not None:
            metadata_commit(store, home, census["metadata"], scan, source_home)
            final_metadata = client.metadata()
            report["metadata_changed_during_capture"] = final_metadata != census["metadata"]
            if final_metadata != census["metadata"]:
                metadata_commit(store, home, final_metadata, scan, source_home)
                census["metadata_after_capture"] = final_metadata
        report["capture_consistency"] = "eventually consistent; per-database read transactions, complete visible raw prefixes"
        report["complete"] = census["complete"] and not report["errors"]
        inventory = store.put_json(census)
        report["files_seen"] = len(census["files"])
        report["metadata_threads"] = len(census["metadata"]["tables"]["threads"]) if census["metadata"] else None
        previous_inventory = store.db.execute("SELECT inventory_object FROM homes WHERE id=?", (home,)).fetchone()[0]
        with store.db:
            if report["complete"]:
                store.db.execute("UPDATE files SET state='missing' WHERE home_id=? AND last_scan<>?", (home, scan))
                store.db.execute("UPDATE homes SET last_complete=? WHERE id=?", (now(), home))
            store.db.execute("UPDATE homes SET inventory_object=?,status=? WHERE id=?", (inventory, "online" if report["complete"] else "incomplete", home))
            if previous_inventory != inventory:
                store.change(home, "inventory", str(scan), inventory)
        report["dependencies"] = dependency_report(store, home)
    except (OSError, ValueError, RuntimeError) as error:
        report["errors"].append({"error": str(error)})
        with store.db:
            store.db.execute("UPDATE homes SET status='offline' WHERE id=?", (home,))
    report["seq"] = store.status()["seq"]
    report["source_bytes_received"] = client.source_bytes_received - received_before
    report["transport_bytes_received"] = client.transport_bytes_received - transport_before
    report["new_object_bytes"] = store.new_object_bytes - objects_before
    obj = store.put_json(report)
    with store.db:
        store.db.execute("UPDATE scans SET finished=?,complete=?,report_object=? WHERE id=?", (now(), report["complete"], obj, scan))
    return report
