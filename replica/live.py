"""Observe an existing owner and retain events, snapshots and explicit capture gaps."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import time
import uuid

from websockets.sync.client import unix_connect

from .store import Store, encode
from .view import session_key

READ_METHODS = {"initialize", "thread/loaded/list", "thread/read", "thread/turns/list", "thread/items/list"}


class Owner:
    def __init__(self, endpoint, receive):
        self.process = None
        self.receive = receive
        self.serial = 0
        if endpoint.get("ssh"):
            local, stdio = socket.socketpair()
            command = shlex.join([endpoint["binary"], "app-server", "proxy", "--sock", endpoint["socket"]])
            self.process = subprocess.Popen(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", endpoint["ssh"], command],
                                            stdin=stdio, stdout=stdio, stderr=subprocess.DEVNULL)
            stdio.close()
            args = {"sock": local}
        else:
            args = {"path": endpoint["socket"]}
        try:
            # The tested owner doesn't negotiate permessage-deflate through its stdio proxy.
            self.ws = unix_connect(**args, compression=None, open_timeout=10, close_timeout=2, max_size=64*1024*1024)
        except Exception:
            if "sock" in args:
                args["sock"].close()
            self.close()
            raise

    def call(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("owner observer only allows history reads")
        self.serial += 1
        self.ws.send(json.dumps({"id": self.serial, "method": method, "params": params}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv(timeout=max(0.01, deadline-time.monotonic())))
            if "method" in message:
                self.receive(message)
            elif message.get("id") == self.serial:
                if "error" in message:
                    raise RuntimeError("owner read failed: " + str(message["error"].get("code")))
                return message["result"]
        raise TimeoutError("owner read timed out")

    def initialize(self):
        result = self.call("initialize", {"clientInfo": {"name": "codex_session_replica", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        return result

    def poll(self, timeout=1):
        try:
            message = json.loads(self.ws.recv(timeout=timeout))
        except TimeoutError:
            return
        if "method" in message:
            self.receive(message)

    def close(self):
        if hasattr(self, "ws"):
            self.ws.close()
        if self.process:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)


def reduce_event(previous, method, params):
    item = params.get("item")
    if method in ("item/started", "item/completed") and isinstance(item, dict):
        return {"item": item, "complete": method == "item/completed", "uncertain": False}
    value = json.loads(json.dumps(previous)) if previous else None
    delta = params.get("delta")
    if method == "item/agentMessage/delta" and isinstance(delta, str):
        if not value:
            value = {"item": {"type": "agentMessage", "id": params["itemId"], "text": ""}, "complete": False, "uncertain": True}
        if value["complete"]:
            return value
        value["item"]["text"] = value["item"].get("text", "") + delta
    elif method == "item/commandExecution/outputDelta" and isinstance(delta, str):
        if not value:
            value = {"item": {"type": "commandExecution", "id": params["itemId"], "command": "", "aggregatedOutput": ""}, "complete": False, "uncertain": True}
        if value["complete"]:
            return value
        value["item"]["aggregatedOutput"] = value["item"].get("aggregatedOutput", "") + delta
    else:
        return previous
    return value


class LiveJournal:
    def __init__(self, root, readonly=False):
        self.archive = Store(root, readonly=True)
        path = Path(root).resolve() / "live.sqlite"
        self.db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else ""), uri=True, timeout=30)
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,origin TEXT,epoch TEXT,kind TEXT,object TEXT);
          CREATE TABLE IF NOT EXISTS sources(origin TEXT PRIMARY KEY,epoch TEXT,status TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS items(origin TEXT,thread TEXT,turn TEXT,item TEXT,data TEXT,seq INTEGER,
            PRIMARY KEY(origin,thread,turn,item));
          CREATE TABLE IF NOT EXISTS cursors(consumer TEXT PRIMARY KEY,seq INTEGER);
          CREATE TABLE IF NOT EXISTS archived_batches(first_seq INTEGER,last_seq INTEGER,object TEXT);
          CREATE TABLE IF NOT EXISTS retention(singleton INTEGER PRIMARY KEY,floor INTEGER);
          INSERT OR IGNORE INTO retention VALUES (1,0);
        """)

    def close(self):
        self.db.close()
        self.archive.close()

    def append(self, origin, epoch, kind, data):
        obj = self.archive.put_json(data)
        with self.db:
            seq = self.db.execute("INSERT INTO events(origin,epoch,kind,object) VALUES (?,?,?,?)", (origin, epoch, kind, obj)).lastrowid
            if kind == "event":
                params, method = data.get("params", {}), data.get("method", "")
                if not isinstance(params, dict):
                    params = {}
                thread, turn = params.get("threadId"), params.get("turnId")
                raw_item = params.get("item")
                item = params.get("itemId") or (raw_item.get("id") if isinstance(raw_item, dict) else None)
                if thread and turn and item:
                    previous = self.db.execute("SELECT data FROM items WHERE origin=? AND thread=? AND turn=? AND item=?", (origin, thread, turn, item)).fetchone()
                    value = reduce_event(json.loads(previous[0]) if previous else None, method, params)
                    if value:
                        value.update(session=session_key(origin, thread), turn=turn, epoch=epoch)
                        self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)", (origin, thread, turn, item, encode(value).decode(), seq))
            elif kind in ("connected", "gap", "snapshot"):
                status = "disconnected" if kind == "gap" else "connected"
                self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?)", (origin, epoch, status, encode(data).decode()))
                if kind == "gap":
                    rows = self.db.execute("SELECT thread,turn,item,data FROM items WHERE origin=?", (origin,)).fetchall()
                    for row in rows:
                        value = json.loads(row["data"])
                        if not value["complete"]:
                            value["uncertain"] = True
                            self.db.execute("UPDATE items SET data=?,seq=? WHERE origin=? AND thread=? AND turn=? AND item=?", (encode(value).decode(), seq, origin, row["thread"], row["turn"], row["item"]))
        return seq

    def seq(self):
        return self.db.execute("SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)").fetchone()[0]

    def snapshot(self):
        self.db.execute("BEGIN")
        try:
            return {"seq": str(self.seq()), "sources": [dict(r) for r in self.db.execute("SELECT origin,epoch,status,data FROM sources")],
                    "items": [{"key": encode([r["origin"], r["thread"], r["turn"], r["item"]]).decode(), "data": json.loads(r["data"]), "seq": str(r["seq"])} for r in self.db.execute("SELECT * FROM items")]}
        finally:
            self.db.rollback()

    def changes(self, after, limit=100):
        self.db.execute("BEGIN")
        try:
            if after < self.db.execute("SELECT floor FROM retention").fetchone()[0] or after > self.seq():
                return {"reset_required": True}
            events, size, cursor = [], 0, after
            for row in self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)):
                payload = self.archive.get(row["object"])
                events.append({"seq": str(row["seq"]), "origin": row["origin"], "epoch": row["epoch"], "kind": row["kind"], "data": json.loads(payload)})
                cursor, size = row["seq"], size + len(payload)
                if size >= 2*1024*1024:
                    break
            return {"events": events, "cursor": str(cursor), "seq": str(self.seq()), "done": cursor >= self.seq()}
        finally:
            self.db.rollback()

    def acknowledge(self, consumer, seq):
        if seq > self.seq() or seq < 0:
            raise ValueError("acknowledgement is outside the committed journal")
        with self.db:
            self.db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(consumer) DO UPDATE SET seq=max(seq,excluded.seq)", (consumer, seq))

    def prune(self, through):
        acknowledged = self.db.execute("SELECT min(seq) FROM cursors").fetchone()[0]
        if acknowledged is None or through > acknowledged:
            raise ValueError("cannot prune past a follower acknowledgement")
        rows = [dict(row) for row in self.db.execute("SELECT * FROM events WHERE seq<=? ORDER BY seq", (through,))]
        if not rows:
            return
        obj = self.archive.put_json(rows)
        with self.db:
            self.db.execute("INSERT INTO archived_batches VALUES (?,?,?)", (rows[0]["seq"], rows[-1]["seq"], obj))
            self.db.execute("DELETE FROM events WHERE seq<=?", (through,))
            self.db.execute("UPDATE retention SET floor=max(floor,?)", (through,))


def observe(root, endpoint, seconds=None):
    lock = (Path(root) / "observer.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    origin = endpoint["host"] + ":" + endpoint["home"]
    journal = LiveJournal(root)
    epoch = str(uuid.uuid4())
    owner = None
    start = time.monotonic()
    counts = {"notifications": 0, "server_requests": 0}
    def receive(message):
        if "id" in message:
            counts["server_requests"] += 1
            journal.append(origin, epoch, "server_request", message)
        else:
            counts["notifications"] += 1
            journal.append(origin, epoch, "event", message)
    journal.append(origin, epoch, "gap", {"reason": "before_connection_unobserved", "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    try:
        owner = Owner(endpoint, receive)
        initialized = owner.initialize()
        if initialized.get("codexHome") != endpoint["home"]:
            raise ValueError("owner effective home differs from configured source")
        journal.append(origin, epoch, "connected", {"scope": "available owner notifications; item streaming subscription is not established", "initialize": initialized})
        loaded, cursor = [], None
        while True:
            result = owner.call("thread/loaded/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            loaded.extend(result.get("data", [])); cursor = result.get("nextCursor")
            if not cursor:
                break
        snapshots = []
        for identity in loaded:
            snapshots.append(owner.call("thread/read", {"threadId": identity, "includeTurns": False}))
        journal.append(origin, epoch, "snapshot", {"loaded": len(loaded), "threads": snapshots, "upstream_watermark": None,
                       "scope": "owner metadata snapshot; pre-connection transient history unknown"})
        while seconds is None or time.monotonic()-start < seconds:
            owner.poll(1)
        return {**counts, "loaded": len(loaded), "seq": str(journal.seq()), "owner_read_succeeded": True}
    finally:
        journal.append(origin, epoch, "gap", {"reason": "connection_closed", "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if owner:
            owner.close()
        journal.close()
        lock.close()
