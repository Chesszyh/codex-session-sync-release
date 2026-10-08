"""Portable source-side reader. Only the Python standard library is required."""
import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
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

    @contextmanager
    def open_file(self, relative):
        parts = relative.split("/")
        if (any(part in ("", ".", "..") for part in parts)
                or (relative != "session_index.jsonl"
                    and (len(parts) < 2 or parts[0] not in ("sessions", "archived_sessions")))):
            raise ValueError("path is outside the allowed session roots")
        directory = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Pin every directory so a concurrent rename cannot redirect the final open.
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("session source must be a regular file")
                yield stream
        finally:
            os.close(directory)

    @staticmethod
    def stamp(stream):
        info = os.fstat(stream.fileno())
        return {"device": str(info.st_dev), "inode": str(info.st_ino), "size": info.st_size,
                "mtime_ns": str(info.st_mtime_ns), "ctime_ns": str(info.st_ctime_ns)}

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
            try:
                with self.open_file("session_index.jsonl") as stream:
                    for line in stream:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("thread_name"), str) and entry["thread_name"].strip():
                            names[entry["id"]] = entry["thread_name"].strip()
            except FileNotFoundError:
                pass
            return {"schema": schema, "tables": tables, "thread_names": names,
                    "capture_consistency": "single read transaction; session index read separately"}
        finally:
            db.close()

    def open_reader(self, raw, relative, physical=False):
        if relative.endswith(".zst") and not physical:
            if not self.zstd:
                raise RuntimeError("zstd executable is required to read compressed rollouts")
            process = subprocess.Popen([self.zstd, "-dc"], stdin=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return process.stdout, process
        return raw, None

    def census(self):
        errors, files = [], []
        for root_name in ("sessions", "archived_sessions"):
            root = self.home / root_name
            if root.is_symlink():
                errors.append({"root": root_name, "error": "session root must not be a symbolic link"})
                continue
            if not root.exists():
                if root_name == "sessions":
                    errors.append({"root": root_name, "error": "missing sessions root"})
                continue
            def failure(error):
                errors.append({"root": root_name, "error": type(error).__name__})
            for parent, directories, names in os.walk(root, onerror=failure):
                for name in directories[:]:
                    path = Path(parent) / name
                    if path.is_symlink():
                        directories.remove(name)
                        errors.append({"path": str(path.relative_to(self.home)), "error": "session directory must not be a symbolic link"})
                for name in names:
                    if name.endswith(".pending"):
                        errors.append({"path": str((Path(parent) / name).relative_to(self.home)), "error": "pending migration"})
                    if not name.endswith((".jsonl", ".jsonl.zst")):
                        continue
                    path = Path(parent) / name
                    relative = str(path.relative_to(self.home))
                    try:
                        with self.open_file(relative) as raw:
                            stamp = self.stamp(raw)
                            stream, process = self.open_reader(raw, relative)
                            try:
                                header = stream.readline(BLOCK)
                            finally:
                                if process:
                                    stream.close()
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
        try:
            with self.open_file("session_index.jsonl") as index:
                files.append({"path": "session_index.jsonl", "stamp": self.stamp(index), "encoding": "jsonl",
                              "rollout_id": "session_index", "thread_id": None, "history_mode": None,
                              "runtime": None, "history_base": None, "warning": None})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            errors.append({"path": "session_index.jsonl", "error": str(error)})
        try:
            metadata = self.metadata()
        except (OSError, sqlite3.Error, ValueError) as error:
            metadata = None
            errors.append({"database": "state_5.sqlite", "error": str(error)})
        return {"files": files, "metadata": metadata, "errors": errors, "complete": not errors,
                "home": str(self.home), "sqlite_home": str(self.sqlite_home)}

    def chunks(self, relative, start, physical=False, length=None):
        with self.open_file(relative) as raw:
            before = self.stamp(raw)
            stream, process = self.open_reader(raw, relative, physical)
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
                after = self.stamp(raw)
                stable = before == after
                append_only = (before["inode"], before["device"]) == (after["inode"], after["device"]) and after["size"] > before["size"]
                yield {"kind": "end", "before": before, "after": after, "end": offset,
                       "stable": stable, "append_only_observation": append_only}
            finally:
                if process:
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
