"""Measure a GB-sized record and suffix transfer through the real collector."""
import argparse
import json
from pathlib import Path
import resource
import sqlite3
import time

from replica.collect import SourceClient, collect
from replica.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mib", type=int, default=1024)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    home = (args.root / "home").resolve()
    (home / "sessions").mkdir(parents=True)
    identity = "00000000-0000-4000-8000-000000000001"
    path = home / "sessions" / ("rollout-2026-09-22T00-00-00-" + identity + ".jsonl")
    with path.open("wb") as stream:
        stream.write((json.dumps({"type": "session_meta", "payload": {"id": identity, "history_mode": "paginated"}, "ordinal": 0}) + "\n").encode())
        stream.write(b'{"type":"future_record","payload":{"text":"')
        for _ in range(args.mib):
            stream.write(b"x" * 1048576)
        stream.write(b'"},"ordinal":1}\n')
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("CREATE TABLE threads(id TEXT,rollout_path TEXT,archived INTEGER)")
    db.execute("INSERT INTO threads VALUES (?,?,0)", (identity, str(path)))
    db.commit()
    db.close()
    store = Store(args.root / "archive")
    client = SourceClient(str(home), str(home))
    started = time.monotonic()
    first = collect(store, client, "stress", str(home), str(home))
    unchanged = collect(store, client, "stress", str(home), str(home))
    suffix = b'{"type":"future_record","ordinal":2}\n'
    with path.open("ab") as stream:
        stream.write(suffix)
    append = collect(store, client, "stress", str(home), str(home))
    generation = store.db.execute("SELECT id FROM generations").fetchone()[0]
    output = args.root / "restored.jsonl"
    store.export(generation, output)
    with path.open("rb") as source, output.open("rb") as restored:
        while True:
            data = source.read(1048576)
            if data != restored.read(len(data)):
                raise RuntimeError("restored raw bytes differ")
            if not data:
                break
    assert first["complete"] and unchanged["complete"] and append["complete"]
    assert unchanged["source_bytes_received"] == 0
    assert append["read_bytes"] == len(suffix)
    assert append["source_bytes_received"] == 65536 + len(suffix)
    result = {"source_bytes": path.stat().st_size, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "elapsed_seconds": round(time.monotonic()-started, 3), "first_raw_bytes": first["source_bytes_received"],
              "unchanged_raw_bytes": unchanged["source_bytes_received"], "append_new_bytes": append["read_bytes"],
              "append_with_prefix_check_bytes": append["source_bytes_received"], "restored_bytes_equal": True}
    store.close()
    client.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
