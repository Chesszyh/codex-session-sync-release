#!/usr/bin/env python3
"""Probe isolated history reads or an existing owner's loaded list over stdio."""
import argparse
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from replica.rpc import Client


def probe(command, thread_id=None, env=None):
    client = Client(command, env)
    try:
        init = client.call("initialize", {"clientInfo": {"name": "codex_session_replica_m0", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized", "params": {}})
        result = {"initialize": init}
        loaded = client.call("thread/loaded/list", {"limit": 100})
        result["loaded"] = {"error": loaded.get("error"), "count": len(loaded.get("result", {}).get("data", [])), "has_next_page": bool(loaded.get("result", {}).get("nextCursor"))}
        if thread_id:
            result["list"] = client.call("thread/list", {"limit": 10, "sourceKinds": [], "modelProviders": []})
            result["read"] = client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
            result["turns"] = client.call("thread/turns/list", {"threadId": thread_id, "limit": 100, "itemsView": "full"})
            result["items"] = client.call("thread/items/list", {"threadId": thread_id, "limit": 100})
        return result
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--existing-owner", action="store_true")
    parser.add_argument("--socket")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--sqlite-home", type=Path)
    parser.add_argument("--thread-id")
    args = parser.parse_args()
    if args.existing_owner:
        if args.thread_id:
            parser.error("existing-owner probe only initializes and lists loaded threads")
        command = [args.binary, "app-server", "proxy"]
        if args.socket:
            command += ["--sock", args.socket]
        env = None
    else:
        if not args.home or not args.sqlite_home:
            parser.error("isolated probes require both roots")
        for path in (args.home, args.sqlite_home):
            path.mkdir(parents=True, exist_ok=True)
        command = [args.binary, "app-server", "--listen", "stdio://"]
        env = {"PATH": os.environ["PATH"], "HOME": str(args.home.resolve()), "CODEX_HOME": str(args.home.resolve()), "CODEX_SQLITE_HOME": str(args.sqlite_home.resolve())}
    try:
        result = probe(command, args.thread_id, env)
    except (TimeoutError, RuntimeError) as exc:
        result = {"error": str(exc), "status": "untested"}
    print(json.dumps(result, indent=2))
