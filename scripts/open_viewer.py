#!/usr/bin/env python3
"""Start the configured viewer and open its local address."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8765"
SERVICES = ["codex-session-replica.service", "codex-session-indexer.service", "codex-session-viewer.service"]
TUNNEL_LABEL = "xyz.chesszyh.codex-session-viewer"


def ready():
    try:
        connection = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with connection.open(URL + "/api/status", timeout=1) as response:
            value = json.load(response)
        return all(key in value for key in ("threads", "items", "source_seq"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def run(*command):
    subprocess.run(command, check=True, timeout=30)


def main():
    system = platform.system()
    path = Path.home() / ".config/codex-session-sync/launcher.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    if config.get("server"):
        if not ready():
            if system == "Darwin":
                run("/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{TUNNEL_LABEL}")
            else:
                run("systemctl", "--user", "start", "codex-session-connection.service")
    elif system == "Linux":
        run("systemctl", "--user", "start", *SERVICES)
    elif system == "Darwin":
        for service in ("replica", "indexer", "viewer"):
            run("/bin/launchctl", "kickstart", f"gui/{os.getuid()}/xyz.chesszyh.codex-session-{service}")
    else:
        raise RuntimeError("启动入口支持 Linux 和 macOS。")
    deadline = time.monotonic() + 25
    while not ready():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"无法连接会话档案：{URL}。请检查归档机和连接服务。")
        time.sleep(0.5)
    if system == "Darwin":
        run("/usr/bin/open", URL)
    else:
        # Some desktop openers stay alive for the browser's entire lifetime.
        subprocess.Popen(["xdg-open", URL], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    print(f"会话档案已打开：{URL}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
