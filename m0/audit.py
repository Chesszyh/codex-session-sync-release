#!/usr/bin/env python3
"""Read-only Codex storage census; emits counts and schema, never message bodies."""
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import platform
import sqlite3


def audit(home, sqlite_home):
    result = {"captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "platform": platform.platform(), "home": str(home),
              "sqlite_home": str(sqlite_home), "roots_source": "explicit arguments",
              "databases": {}, "directories": {}, "errors": []}
    for name in ("state_5.sqlite", "thread_history_1.sqlite"):
        path = sqlite_home / name
        if not path.is_file():
            result["databases"][name] = {"present": False}
            continue
        result["databases"][name] = {"present": True, "readable": False}
        try:
            # mode=ro honors live WAL; immutable=1 would miss committed WAL rows.
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            info = {"present": True, "readable": True, "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                    "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0], "tables": {}}
            for table in tables:
                if table not in ("threads", "thread_turns", "thread_items", "thread_history_projection_state", "_sqlx_migrations"):
                    continue
                columns = [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]
                entry = {"columns": columns, "rows": db.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]}
                if table == "threads":
                    for col in ("history_mode", "archived"):
                        if col in columns:
                            entry[col] = {str(k): v for k, v in db.execute('SELECT "' + col + '",count(*) FROM threads GROUP BY "' + col + '"')}
                    if "rollout_path" in columns:
                        paths = [r[0] for r in db.execute("SELECT rollout_path FROM threads")]
                        entry["selected_paths_missing"] = sum(not Path(p).is_file() for p in paths if p)
                info["tables"][table] = entry
            db.rollback()
            db.close()
            result["databases"][name] = info
        except (sqlite3.Error, OSError) as exc:
            result["errors"].append({"database": name, "error": str(exc)})
    for directory in ("sessions", "archived_sessions"):
        root = home / directory
        info = {"present": root.is_dir(), "files": 0, "bytes": 0, "largest_bytes": 0,
                "encodings": {}, "first_record_types": {}, "history_modes": {},
                "history_base_files": 0, "runtime_versions": {}, "pending_files": 0,
                "header_unreadable": 0, "complete": root.is_dir()}
        counters = {key: collections.Counter() for key in ("encodings", "first_record_types", "history_modes", "runtime_versions")}
        def onerror(error):
            info["complete"] = False
            result["errors"].append({"directory": directory, "error": str(error)})
        for parent, _, files in os.walk(root, onerror=onerror):
            for name in files:
                if name.endswith(".pending"):
                    info["pending_files"] += 1
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                    continue
                path = Path(parent) / name
                try:
                    size = path.stat().st_size
                    info["files"] += 1
                    info["bytes"] += size
                    info["largest_bytes"] = max(info["largest_bytes"], size)
                    encoding = "zstd" if name.endswith(".zst") else "jsonl"
                    counters["encodings"][encoding] += 1
                    if encoding == "zstd":
                        continue
                    with path.open("rb") as source:
                        line = source.readline(2 * 1024 * 1024)
                    if not line.endswith(b"\n"):
                        info["header_unreadable"] += 1
                        continue
                    record = json.loads(line)
                    counters["first_record_types"][str(record.get("type"))] += 1
                    meta = record.get("payload", {})
                    counters["history_modes"][str(meta.get("history_mode", "absent"))] += 1
                    counters["runtime_versions"][str(meta.get("cli_version", "absent"))] += 1
                    info["history_base_files"] += bool(meta.get("history_base"))
                except (OSError, ValueError, TypeError) as exc:
                    info["complete"] = False
                    result["errors"].append({"directory": directory, "error_type": type(exc).__name__})
        info.update({key: dict(value) for key, value in counters.items()})
        result["directories"][directory] = info
    result["capture_consistency"] = "per-database read transaction; filesystem census is not atomic"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--sqlite-home", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.home.resolve(), args.sqlite_home.resolve()), indent=2))
