#!/usr/bin/env python3
"""Collect and display a generated session through the actual archive pipeline."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from m0.fixtures import THREAD, encode, meta
from replica.collect import SourceClient, collect
from replica.gateway import create_server, STATIC
from replica.normalize import DECODER, sync
from replica.store import Store


def prepare(root):
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    home = root / "source"
    (home / "sessions").mkdir(parents=True)
    records = [meta("paginated")]
    records += [json.loads(line) for line in (ROOT / "m0/fixtures/shapes/macmini.jsonl").read_text().splitlines()]
    for ordinal, record in enumerate(records):
        record["ordinal"] = ordinal
    raw = home / "sessions" / ("rollout-2026-09-22T00-00-00-" + THREAD + ".jsonl")
    raw.write_bytes(encode(records))
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,archived INTEGER)")
        db.execute("INSERT INTO threads VALUES (?,?,?,0)", (THREAD, str(raw), "生成样本：跨设备会话归档"))
    archive = root / "archive"
    store, client = Store(archive), SourceClient(str(home), str(home))
    try:
        capture = collect(store, client, "Demo", str(home), str(home))
        if not capture["complete"]:
            raise RuntimeError("demo collection failed")
        generation = store.db.execute("SELECT id FROM generations").fetchone()[0]
        exported = root / "export.jsonl"
        store.export(generation, exported)
        if exported.read_bytes() != raw.read_bytes():
            raise RuntimeError("raw export differs from source")
    finally:
        client.close()
        store.close()
    projection = sync(archive)
    return {"capture_complete": capture["complete"], "raw_bytes_equal": True,
            "threads": projection["threads"], "items": projection["items"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New directory for generated source and archive")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--prepare-only", action="store_true", help="Create the archive without starting HTTP")
    args = parser.parse_args()
    if not DECODER.is_file():
        parser.error("build the official adapter: python3 adapters/official/prepare.py --download")
    if not args.prepare_only and not (STATIC / "app.js").is_file():
        parser.error("build the viewer: npm --prefix viewer run build")
    root = args.root.resolve()
    if root.exists():
        parser.error("--root must be a new directory")
    result = prepare(root)
    if args.prepare_only:
        print(json.dumps(result), flush=True)
        return
    server = create_server(root / "archive", args.port)
    print(json.dumps({**result, "url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
