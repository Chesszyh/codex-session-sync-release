#!/usr/bin/env python3
"""Install archive services for the current macOS user."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--public-origin", help="HTTPS viewer origin for the separate public service")
parser.add_argument("--access-team", help="Cloudflare Access team subdomain")
parser.add_argument("--access-audience", help="Cloudflare Access application AUD tag")
parser.add_argument("--public-port", type=int, default=8767, help="Loopback port for the Access-authenticated public service")
args = parser.parse_args()
if any((args.public_origin, args.access_team, args.access_audience)) and not all((args.public_origin, args.access_team, args.access_audience)):
    parser.error("--public-origin, --access-team and --access-audience must be supplied together")
if args.public_origin and (args.public_port == 8765 or not 1 <= args.public_port <= 65535):
    parser.error("--public-port must be a valid port other than the local viewer port 8765")
root = Path(__file__).resolve().parent.parent
if args.public_origin:
    sys.path.insert(0, str(root))
    from replica.gateway import create_server
    try:
        check = create_server(None, 0, public_origin=args.public_origin,
                              access_team=args.access_team, access_audience=args.access_audience)
        check.server_close()
    except ValueError as error:
        parser.error(str(error))
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
if args.public_origin:
    services["public-viewer"] = ["serve", "--port", str(args.public_port), "--public-origin", args.public_origin,
                                  "--access-team", args.access_team, "--access-audience", args.access_audience]
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
