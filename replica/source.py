"""Portable source-side reader. Only the Python standard library is required."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

BLOCK = 1024 * 1024
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Source:
    def __init__(self, home, sqlite_home, zstd=None):
        self.home = Path(home).resolve()
        self.sqlite_home = Path(sqlite_home).resolve()
        self.zstd = zstd or shutil.which("zstd")
        if self.zstd is None and Path("/opt/homebrew/bin/zstd").is_file():
            self.zstd = "/opt/homebrew/bin/zstd"

    def path(self, relative):
        path = (self.home / relative).resolve()
        path.relative_to(self.home)
        if relative != "session_index.jsonl" and not relative.startswith(("sessions/", "archived_sessions/")):
            raise ValueError("path is outside the allowed session roots")
        return path

    @staticmethod
    def stamp(path):
        stat = path.stat()
        return {"device": str(stat.st_dev), "inode": str(stat.st_ino), "size": stat.st_size,
                "mtime_ns": str(stat.st_mtime_ns), "ctime_ns": str(stat.st_ctime_ns)}

    def metadata(self):
        path = self.sqlite_home / "state_5.sqlite"
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables, schema = {}, {}
            for name in ("threads", "projects", "project_roots", "thread_sections", "thread_attachments"):
                if name in names:
                    schema[name] = [r[1] for r in db.execute('PRAGMA table_info("' + name + '")')]
                    tables[name] = [dict(r) for r in db.execute('SELECT * FROM "' + name + '"')]
            if "threads" not in tables:
                raise ValueError("state database has no threads table")
            names = {}
            index = self.home / "session_index.jsonl"
            if index.exists():
                with index.open() as stream:
                    for line in stream:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("thread_name"), str) and entry["thread_name"].strip():
                            names[entry["id"]] = entry["thread_name"].strip()
            return {"schema": schema, "tables": tables, "thread_names": names,
                    "capture_consistency": "single read transaction; session index read separately"}
        finally:
            db.close()

    def open_reader(self, path, physical=False):
        if path.name.endswith(".zst") and not physical:
            if not self.zstd:
                raise RuntimeError("zstd executable is required to read compressed rollouts")
            process = subprocess.Popen([self.zstd, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return process.stdout, process
        return path.open("rb"), None

    def census(self):
        errors, files = [], []
        for root_name in ("sessions", "archived_sessions"):
            root = self.home / root_name
            if not root.exists():
                if root_name == "sessions":
                    errors.append({"root": root_name, "error": "missing sessions root"})
                continue
            def failure(error):
                errors.append({"root": root_name, "error": type(error).__name__})
            for parent, _, names in os.walk(root, onerror=failure):
                for name in names:
                    if name.endswith(".pending"):
                        errors.append({"path": str((Path(parent) / name).relative_to(self.home)), "error": "pending migration"})
                    if not name.endswith((".jsonl", ".jsonl.zst")):
                        continue
                    path = Path(parent) / name
                    relative = str(path.relative_to(self.home))
                    try:
                        self.path(relative)
                        stamp = self.stamp(path)
                        stream, process = self.open_reader(path)
                        try:
                            header = stream.readline(BLOCK)
                        finally:
                            stream.close()
                            if process:
                                if process.poll() is None:
                                    process.terminate()
                                process.wait()
                        warning = None
                        try:
                            if not header.endswith(b"\n"):
                                raise ValueError("incomplete metadata")
                            record = json.loads(header)
                            if not isinstance(record, dict) or record.get("type") != "session_meta":
                                raise ValueError("missing session_meta")
                            meta = record["payload"]
                            if not isinstance(meta, dict):
                                raise ValueError("unsupported session_meta")
                        except (ValueError, KeyError, TypeError):
                            meta = {}
                            warning = "metadata unreadable; raw retained"
                        identities = UUID.findall(name)
                        files.append({"path": relative, "stamp": stamp, "encoding": "zstd" if name.endswith(".zst") else "jsonl",
                                      "rollout_id": identities[-1] if identities else relative,
                                      "thread_id": meta.get("id"), "history_mode": meta.get("history_mode"),
                                      "runtime": meta.get("cli_version"), "history_base": meta.get("history_base"),
                                      "warning": warning})
                    except (OSError, ValueError, RuntimeError) as error:
                        errors.append({"path": relative, "error": str(error)})
        index = self.home / "session_index.jsonl"
        if index.is_file():
            files.append({"path": "session_index.jsonl", "stamp": self.stamp(index), "encoding": "jsonl",
                          "rollout_id": "session_index", "thread_id": None, "history_mode": None,
                          "runtime": None, "history_base": None, "warning": None})
        try:
            metadata = self.metadata()
        except (OSError, sqlite3.Error, ValueError) as error:
            metadata = None
            errors.append({"database": "state_5.sqlite", "error": str(error)})
        return {"files": files, "metadata": metadata, "errors": errors, "complete": not errors,
                "home": str(self.home), "sqlite_home": str(self.sqlite_home)}

    def chunks(self, relative, start, physical=False, length=None):
        path = self.path(relative)
        before = self.stamp(path)
        stream, process = self.open_reader(path, physical)
        try:
            yield {"kind": "begin", "stamp": before}
            if process:
                remaining = start
                while remaining:
                    data = stream.read(min(BLOCK, remaining))
                    if not data:
                        raise RuntimeError("decoded stream shortened")
                    remaining -= len(data)
                remaining = length
            else:
                if start > before["size"]:
                    raise RuntimeError("file shortened")
                stream.seek(start)
                remaining = before["size"] - start
                if length is not None:
                    remaining = min(remaining, length)
            offset = start
            while remaining is None or remaining:
                data = stream.read(BLOCK if remaining is None else min(BLOCK, remaining))
                if not data:
                    if remaining:
                        raise RuntimeError("file truncated while reading")
                    break
                yield {"kind": "chunk", "start": offset, "bytes": base64.b64encode(data).decode()}
                offset += len(data)
                if remaining is not None:
                    remaining -= len(data)
            if process and length is not None and process.poll() is None:
                process.terminate()
                process.wait()
            elif process and process.wait() != 0:
                raise RuntimeError("zstd decompression failed")
            after = self.stamp(path)
            stable = before == after
            append_only = (before["inode"], before["device"]) == (after["inode"], after["device"]) and after["size"] > before["size"]
            yield {"kind": "end", "before": before, "after": after, "end": offset,
                   "stable": stable, "append_only_observation": append_only}
        finally:
            stream.close()
            if process and process.poll() is None:
                process.terminate()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--sqlite-home", required=True)
    args = parser.parse_args()
    source = Source(args.home, args.sqlite_home)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["method"] == "census":
                responses = [{"kind": "census", **source.census()}]
            elif request["method"] == "metadata":
                responses = [{"kind": "metadata", "metadata": source.metadata()}]
            elif request["method"] == "read":
                responses = source.chunks(request["path"], request["start"], request.get("physical", False), request.get("length"))
            else:
                raise ValueError("unknown source method")
            for response in responses:
                print(json.dumps(response, separators=(",", ":")), flush=True)
        except Exception as error:
            print(json.dumps({"kind": "error", "error": str(error)}), flush=True)


if __name__ == "__main__":
    main()
