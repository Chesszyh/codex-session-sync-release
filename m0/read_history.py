#!/usr/bin/env python3
"""Read a standalone paginated rollout through official code in a new isolated home."""
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import re
import subprocess

from rpc_probe import Client

ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((ROOT / "adapters/official/source.json").read_text())
EXAMPLES = ROOT / ".m0/official-target/debug/examples"


def save_json(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def all_pages(client, method, params):
    items = []
    seen = set()
    while True:
        response = client.call(method, params)
        if "error" in response:
            raise RuntimeError(method + ": " + json.dumps(response["error"]))
        page = response["result"]
        items.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return items
        if cursor in seen:
            raise RuntimeError(method + " repeated cursor")
        seen.add(cursor)
        params = {**params, "cursor": cursor}


def read_history(rollout, output, binary, page_size=50):
    rollout = Path(rollout).resolve()
    output = Path(output).resolve()
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != SPEC["tested_runtime"]:
        raise ValueError("Runtime has not passed this importer: " + version)
    for helper in ("replica_read", "replica_materialize"):
        if not (EXAMPLES / helper).is_file():
            raise ValueError("Build official helpers with adapters/official/prepare.py --download")
    with rollout.open("rb") as source:
        metadata = json.loads(source.readline())["payload"]
        if metadata.get("history_mode") != "paginated" or metadata.get("history_base"):
            raise ValueError("This reader supports standalone paginated rollouts; legacy and parent references need their own adapters")
        thread_id = metadata["id"]
        if not rollout.name.endswith(thread_id + ".jsonl"):
            raise ValueError("This reader requires a plain rollout whose rollout ID equals its thread ID; reverted heads need a selected-head adapter")
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        home = output / "home"
        sqlite_home = output / "sqlite"
        sqlite_home.mkdir(mode=0o700)
        day = datetime.datetime.fromisoformat(metadata["timestamp"].replace("Z", "+00:00"))
        dest = home / "sessions" / day.strftime("%Y/%m/%d") / rollout.name
        dest.parent.mkdir(parents=True)
        before = os.fstat(source.fileno())
        source.seek(0)
        remaining = before.st_size
        with os.fdopen(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as target:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("Source truncated during capture")
                target.write(chunk)
                remaining -= len(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Source changed during capture; use a stable completed rollout")
    projection = output / "projection.jsonl"
    with os.fdopen(os.open(projection, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as decoded:
        subprocess.run([str(EXAMPLES / "replica_read"), str(dest)], stdout=decoded, check=True)
    expected = {}
    record_count = 0
    errors = []
    with projection.open() as decoded:
        for line in decoded:
            row = json.loads(line)
            if row["kind"] != "record":
                errors.append({"kind": row["kind"], "start": row["start"], "end": row["end"]})
                continue
            record_count += 1
            for item in row["items"]:
                expected[(item["turnId"], item["item"]["id"])] = item["item"]
    if errors:
        save_json(output / "coverage.json", {"complete": False, "errors": errors})
        raise RuntimeError("Official decoder rejected records; inspect coverage.json and private projection.jsonl")
    if not expected:
        raise RuntimeError("No canonical items found; refusing to report empty history as success")
    with (output / "materialize.log").open("w") as log:
        subprocess.run([str(EXAMPLES / "replica_materialize"), str(home), str(sqlite_home), str(dest)], check=True, stdout=log, stderr=subprocess.STDOUT)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(sqlite_home)}
    client = Client([str(binary), "app-server", "--listen", "stdio://"], env)
    try:
        init = client.call("initialize", {"clientInfo": {"name": "codex_session_replica_reader", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        if "error" in init or Path(init["result"]["codexHome"]).resolve() != home:
            raise RuntimeError("Runtime did not initialize in the isolated home")
        client.send({"method": "initialized", "params": {}})
        response = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        if "error" in response:
            raise RuntimeError("thread/read failed: " + json.dumps(response["error"]))
        thread = response["result"]["thread"]
        turns = all_pages(client, "thread/turns/list", {"threadId": thread_id, "itemsView": "notLoaded", "limit": page_size, "sortDirection": "asc"})
        items = all_pages(client, "thread/items/list", {"threadId": thread_id, "limit": page_size, "sortDirection": "asc"})
        loaded = all_pages(client, "thread/loaded/list", {"limit": 100})
        if loaded:
            raise RuntimeError("Read-only history probe unexpectedly loaded a thread")
    finally:
        client.close()
    actual = {(item["turnId"], item["item"]["id"]): item["item"] for item in items}
    if len(actual) != len(items) or actual != expected:
        save_json(output / "coverage.json", {"complete": False, "expected_items": len(expected), "actual_items": len(actual)})
        raise RuntimeError("API history differs from official raw projection")
    with dest.open("rb") as snapshot, rollout.open("rb") as source:
        while True:
            chunk = snapshot.read(1024 * 1024)
            if chunk != source.read(len(chunk)):
                raise RuntimeError("Source or isolated rollout changed during reading")
            if not chunk:
                break
        if source.read(1):
            raise RuntimeError("Source grew during reading")
    result = {"thread": thread, "turns": turns, "items": items}
    save_json(output / "history.json", result)
    lines = ["# 会话历史", ""]
    labels = {"userMessage": "用户消息", "agentMessage": "助手消息", "commandExecution": "命令执行", "fileChange": "文件修改"}
    for entry in items:
        item = entry["item"]
        text = item.get("text") or item.get("aggregatedOutput") or json.dumps(item.get("content", item), ensure_ascii=False, indent=2)
        if item["type"] == "commandExecution":
            text = item["command"] + "\n\n" + (item.get("aggregatedOutput") or "")
        fence = "`" * max(3, max((len(part) for part in re.findall(r"`+", text)), default=0) + 1)
        lines.extend(["## " + labels.get(item["type"], item["type"]), "", fence + "text", text, fence, ""])
    with os.fdopen(os.open(output / "history.md", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as readable:
        readable.write("\n".join(lines))
    summary = {"complete": True, "official_revision": SPEC["revision"], "runtime": version, "records": record_count,
               "turns": len(turns), "items": len(items), "item_types": dict(collections.Counter(entry["item"]["type"] for entry in items)),
               "page_size": page_size, "raw_bytes": before.st_size, "raw_unchanged": True, "loaded_threads": 0,
               "scope": "all officially projected canonical items; raw-only context and opaque fields remain in the copied rollout"}
    save_json(output / "coverage.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing directories are refused")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--page-size", type=int, default=50)
    args = parser.parse_args()
    if args.page_size < 1:
        parser.error("--page-size must be positive")
    print(json.dumps(read_history(args.rollout, args.output, args.binary, args.page_size), ensure_ascii=False, indent=2))
