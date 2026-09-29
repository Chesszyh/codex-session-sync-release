#!/usr/bin/env python3
"""Install the viewer's application shortcut and macOS connection service."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys


def install(server, local=False):
    home = Path.home()
    directory = home / ".local/share/codex-session-sync/launcher"
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "open_viewer.py"
    source = Path(__file__).with_name("open_viewer.py")
    if source.resolve() != script.resolve():
        shutil.copy2(source, script)
    command = shlex.join([sys.executable, str(script)])
    executable = home / ".local/bin/codex-session-viewer"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexec " + command + "\n")
    executable.chmod(0o755)

    config = home / ".config/codex-session-sync"
    config.mkdir(parents=True, exist_ok=True)
    (config / "launcher.json").write_text(json.dumps({"server": server}) + "\n")
    if platform.system() == "Linux" and server:
        units = home / ".config/systemd/user"
        units.mkdir(parents=True, exist_ok=True)
        (units / "codex-session-connection.service").write_text(
            "[Unit]\nDescription=Session archive SSH connection\n[Service]\n"
            "ExecStart=/usr/bin/ssh -NT -o BatchMode=yes -o ConnectTimeout=10 "
            "-o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "
            "-L 127.0.0.1:8765:127.0.0.1:8765 " + server + "\n"
            "Restart=always\nRestartSec=15\n[Install]\nWantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "codex-session-connection.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "codex-session-connection.service"], check=True)
    if platform.system() == "Linux":
        applications = home / ".local/share/applications"
        applications.mkdir(parents=True, exist_ok=True)
        desktop = applications / "codex-session-viewer.desktop"
        desktop.write_text("[Desktop Entry]\nType=Application\nName=会话档案\n"
                           "GenericName=Codex Session Replica\nComment=查看多台电脑的 Codex 会话档案\n"
                           f"Exec={json.dumps(str(executable), ensure_ascii=False)}\n"
                           "Icon=utilities-terminal\nTerminal=false\nCategories=Utility;\n"
                           "Keywords=Codex;会话;历史;档案;\n")
        desktop.chmod(0o755)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(applications)], check=True)
        print(f"应用菜单入口：{desktop}\n命令入口：{executable}")
        return

    if platform.system() != "Darwin":
        raise RuntimeError("启动入口支持 Linux 和 macOS。")
    if not server and not local:
        raise RuntimeError("macOS 安装需要 --server 指定归档机，或用 --local 打开本机服务。")
    label = "xyz.chesszyh.codex-session-viewer"
    plist = None
    if server:
        log = home / "Library/Logs/codex-session-sync"
        log.mkdir(parents=True, exist_ok=True)
        plist = home / "Library/LaunchAgents" / (label + ".plist")
        plist.parent.mkdir(parents=True, exist_ok=True)
        service = {"Label": label, "ProgramArguments": ["/usr/bin/ssh", "-NT", "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                   "-L", "127.0.0.1:8765:127.0.0.1:8765", server],
                   "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
                   "StandardOutPath": str(log / "connection.log"), "StandardErrorPath": str(log / "connection-error.log")}
        domain = f"gui/{os.getuid()}"
        loaded = subprocess.run(["/bin/launchctl", "print", domain + "/" + label], capture_output=True)
        if loaded.returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label], check=True)
        plist.write_bytes(plistlib.dumps(service))
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)

    app = home / "Applications/会话档案.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    launcher = contents / "MacOS/launcher"
    launcher.write_text("#!/bin/sh\nexec " + command + "\n")
    launcher.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "launcher",
        "CFBundleIdentifier": label + ".launcher", "CFBundleName": "会话档案",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True}))
    shortcut = home / "Desktop/会话档案.app"
    if shortcut.parent.is_dir() and not shortcut.exists():
        shortcut.symlink_to(app, target_is_directory=True)
    print(f"应用入口：{app}\n命令入口：{executable}\n连接服务：{plist}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="SSH alias of the archive host")
    parser.add_argument("--local", action="store_true", help="Open services running on this Mac")
    args = parser.parse_args()
    install(args.server, args.local)
