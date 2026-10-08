"""Archive Codex history without opening or resuming native threads."""
import argparse
import json
from pathlib import Path
import signal
import time

from .collect import SourceClient, collect
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("collect", help="Full census and incremental raw/metadata archive")
    capture.add_argument("--host", required=True, help="Stable source host name")
    capture.add_argument("--home", required=True)
    capture.add_argument("--sqlite-home", required=True)
    capture.add_argument("--ssh", help="Existing SSH host alias; omit for local files")
    commands.add_parser("status", help="Counts and last completed scans; no transcript bodies")
    normalize = commands.add_parser("normalize", help="Build readable history from the archive with the pinned official decoder")
    normalize.add_argument("--interval", type=float, default=0, help="Repeat after this many seconds; zero runs once")
    serve = commands.add_parser("serve", help="Serve the read-only viewer on loopback")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--public-origin", help="HTTPS origin for a separate Access-authenticated loopback listener")
    serve.add_argument("--access-team", help="Cloudflare Access team subdomain; required with --public-origin")
    serve.add_argument("--access-audience", help="Cloudflare Access application AUD tag; required with --public-origin")
    live = commands.add_parser("observe", help="Capture events from an existing owner's supported socket")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--seconds", type=float, help="End after a bounded observation; omit to stay connected")
    prune = commands.add_parser("live-prune", help="Archive acknowledged delivery records; stale event cursors must reset")
    prune.add_argument("--through", type=int, required=True)
    inventory = commands.add_parser("inventory", help="Source identity, selected head and archive generation")
    inventory.add_argument("--host")
    changes = commands.add_parser("changes", help="Committed changes after a durable sequence")
    changes.add_argument("--after", type=int, default=0)
    changes.add_argument("--limit", type=int, default=100)
    export = commands.add_parser("export", help="Reassemble a committed raw prefix into a new JSONL file")
    export.add_argument("--generation", required=True)
    export.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="Restore one proven paginated thread into a new isolated native home")
    restore.add_argument("--origin", required=True, help="Exact host:home identity from inventory")
    restore.add_argument("--thread-id", required=True)
    restore.add_argument("--output", type=Path, required=True, help="New directory only")
    restore.add_argument("--binary", type=Path, required=True, help="Native runtime matching the tested allowlist")
    restore.add_argument("--page-size", type=int, default=50)
    watch = commands.add_parser("watch", help="Periodic full census, independently of viewers")
    watch.add_argument("--config", type=Path, required=True)
    watch.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if args.command == "restore":
        from .restore import restore, RestoreRefused
        try:
            report = restore(args.store, args.origin, args.thread_id, args.output, args.binary, args.page_size)
        except RestoreRefused as error:
            parser.exit(2, str(error) + "\n")
        print(json.dumps(report), flush=True)
        return
    if args.command == "observe":
        from .live import observe
        def stop_observer(_signal, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop_observer)
        try:
            result = observe(args.store, json.loads(args.config.read_text()), args.seconds)
        except KeyboardInterrupt:
            return
        print(json.dumps(result), flush=True)
        return
    if args.command == "live-prune":
        from .live import LiveJournal
        journal = LiveJournal(args.store)
        try:
            journal.prune(args.through)
            print(json.dumps({"delivery_archived_through": str(args.through), "raw_events_retained": True}))
        finally:
            journal.close()
        return
    if args.command == "normalize":
        from .normalize import sync
        while True:
            print(json.dumps(sync(args.store)), flush=True)
            if args.interval <= 0:
                return
            time.sleep(args.interval)
    if args.command == "serve":
        from .gateway import create_server
        try:
            server = create_server(args.store, args.port, public_origin=args.public_origin,
                                   access_team=args.access_team, access_audience=args.access_audience)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return
    if args.command == "watch":
        if args.interval < 1:
            parser.error("--interval must be at least 1 second")
        sources = json.loads(args.config.read_text())["sources"]
        running = True
        def stop(_signum, _frame):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while running:
            for source in sources:
                if not running:
                    break
                store = Store(args.store)
                client = SourceClient(source["home"], source["sqlite_home"], source.get("ssh"))
                try:
                    report = collect(store, client, source["host"], source["home"], source["sqlite_home"])
                    print(json.dumps({"host": source["host"], "complete": report["complete"], "read_bytes": report["read_bytes"], "errors": len(report["errors"]), "dependency_issues": len(report["dependencies"]["issues"]) if "dependencies" in report else None, "seq": report["seq"]}), flush=True)
                finally:
                    client.close()
                    store.close()
            deadline = time.monotonic() + args.interval
            while running and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline-time.monotonic())))
        return
    store = Store(args.store, readonly=args.command != "collect")
    try:
        if args.command == "collect":
            client = SourceClient(args.home, args.sqlite_home, args.ssh)
            try:
                result = collect(store, client, args.host, args.home, args.sqlite_home)
                result["error_count"] = len(result["errors"])
                result["dependency_issue_count"] = len(result["dependencies"]["issues"]) if "dependencies" in result else None
                # Detailed paths and diagnostics stay encrypted in the scan object.
                result = {key: value for key, value in result.items() if key not in ("errors", "dependencies")}
                result["status"] = store.status()
            finally:
                client.close()
        elif args.command == "status":
            result = store.status()
        elif args.command == "inventory":
            query = """SELECT t.home_id,t.thread_id,t.head_path,t.archived,f.generation,f.state
                       FROM threads t LEFT JOIN files f ON f.rowid=(
                         SELECT candidate.rowid FROM files candidate
                         WHERE candidate.home_id=t.home_id
                           AND (candidate.path=t.head_path OR candidate.path=t.head_path||'.zst')
                         ORDER BY candidate.state='present' DESC,candidate.encoding='jsonl' DESC LIMIT 1)"""
            store.db.execute("BEGIN")
            result = {"seq": store.status()["seq"], "threads": [dict(row) for row in store.db.execute(query) if not args.host or row["home_id"].startswith(args.host + ":")]}
            result["files"] = [dict(row) for row in store.db.execute("SELECT home_id,path,generation,state,encoding FROM files") if not args.host or row["home_id"].startswith(args.host + ":")]
            raw_only = "SELECT g.home_id,g.thread_id,g.rollout_id,g.id AS generation,f.path FROM generations g JOIN files f ON f.generation=g.id LEFT JOIN threads t ON t.home_id=g.home_id AND t.thread_id=g.thread_id WHERE t.thread_id IS NULL AND g.thread_id IS NOT NULL"
            result["raw_only_threads"] = [dict(row) for row in store.db.execute(raw_only) if not args.host or row["home_id"].startswith(args.host + ":")]
            store.db.rollback()
        elif args.command == "changes":
            rows = [dict(row) for row in store.db.execute("SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (args.after, args.limit))]
            result = {"changes": rows, "next_cursor": rows[-1]["seq"] if rows else args.after}
        elif args.command == "export":
            result = store.export(args.generation, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "collect" and not result["complete"]:
            raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
