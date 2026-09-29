#!/usr/bin/env python3
"""Inventory Codex process transports and open storage paths without credentials."""
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess


def collect():
    system = platform.system()
    processes = []
    if system == "Linux":
        candidates = []
        for path in Path("/proc").iterdir():
            if path.name.isdecimal():
                try:
                    if (path / "comm").read_text().strip() == "codex":
                        candidates.append(int(path.name))
                except OSError:
                    pass
    else:
        lines = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True).splitlines()
        candidates = [int(line.split(None, 1)[0]) for line in lines if len(line.split(None, 1)) == 2 and Path(line.split(None, 1)[1]).name == "codex"]
    for pid in candidates:
        try:
            roots = {}
            paths = []
            if system == "Linux":
                path = Path("/proc") / str(pid)
                args = [v.decode() for v in (path / "cmdline").read_bytes().split(b"\0") if v]
                executable = os.readlink(path / "exe")
                for value in (path / "environ").read_bytes().split(b"\0"):
                    if b"=" in value:
                        key, val = value.split(b"=", 1)
                        if key in (b"CODEX_HOME", b"CODEX_SQLITE_HOME"):
                            roots[key.decode()] = val.decode()
                for fd in (path / "fd").iterdir():
                    try:
                        paths.append(os.readlink(fd))
                    except OSError:
                        pass
                version_binary = str(path / "exe")
            else:
                args = shlex.split(subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True).strip())
                executable = args[0]
                result = subprocess.run(["lsof", "-a", "-p", str(pid), "-Fn"], capture_output=True, text=True)
                paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
                version_binary = executable
            if "app-server" not in args:
                continue
            index = args.index("--listen") if "--listen" in args else None
            transport = args[index + 1] if index is not None else "stdio:// (default)"
            proxy = "proxy" in args
            if proxy:
                transport = "proxy to control socket"
            version = subprocess.run([version_binary, "--version"], capture_output=True, text=True, timeout=5)
            storage = sorted({p for p in paths if p.endswith(".sqlite")})
            sockets = sorted({p for p in paths if p.endswith(".sock")})
            processes.append({"pid": pid, "executable": executable, "version": version.stdout.strip(),
                "version_basis": "running executable" if system == "Linux" else "binary path on disk",
                "transport": transport, "proxy": proxy, "environment_roots": roots if system == "Linux" else "not inspected",
                "open_databases": storage, "named_sockets": sockets,
                "open_rollouts": sum("/sessions/" in p or "/archived_sessions/" in p for p in paths),
                "writer_lock_files_open": sum("/thread-writer-locks/" in p for p in paths)})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            processes.append({"pid": pid, "error_type": type(exc).__name__})
    return {"platform": system, "processes": processes,
            "note": "Open lock files are observations, not proof of ownership release. No lock is acquired or removed."}


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
