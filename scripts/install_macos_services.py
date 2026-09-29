#!/usr/bin/env python3
"""Install archive services for the current macOS user."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
home = Path.home()
store = home / ".local/share/codex-session-sync/replica"
config = home / ".config/codex-session-sync"
logs = home / "Library/Logs/codex-session-sync"
logs.mkdir(parents=True, exist_ok=True)
services = {
    "replica": ["watch", "--config", str(config / "sources.json"), "--interval", "60"],
    "indexer": ["normalize", "--interval", "15"],
    "viewer": ["serve", "--port", "8765"],
}
if (config / "owner.json").exists():
    services["observer"] = ["observe", "--config", str(config / "owner.json")]
for name, args in services.items():
    label = "xyz.chesszyh.codex-session-" + name
    domain = f"gui/{os.getuid()}"
    path = home / "Library/LaunchAgents" / (label + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", domain + "/" + label], check=True)
    path.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "replica", "--store", str(store), *args],
        "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 15, "Umask": 0o077,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(logs / (name + ".log")),
        "StandardErrorPath": str(logs / (name + "-error.log")),
    }))
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print(label)
