"""Local normalized history and the durable, read-only viewer change feed."""
import json
import os
import sqlite3
from pathlib import Path

from .store import encode


def session_key(home, thread):
    return encode([home, thread]).decode()


class View:
    def __init__(self, root, readonly=False):
        path = Path(root).resolve() / "view.sqlite"
        self.db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else ""), uri=True, timeout=30)
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decoded(
          generation TEXT PRIMARY KEY,end INTEGER NOT NULL,next_ordinal INTEGER NOT NULL,records INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          generation TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,payload TEXT NOT NULL,
          PRIMARY KEY(generation,end));
        CREATE TABLE IF NOT EXISTS issues(
          generation TEXT NOT NULL,end INTEGER NOT NULL,kind TEXT NOT NULL,
          PRIMARY KEY(generation,end,kind));
        CREATE TABLE IF NOT EXISTS thread_versions(session TEXT PRIMARY KEY,version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entities(
          key TEXT PRIMARY KEY,kind TEXT NOT NULL,session TEXT NOT NULL,data TEXT NOT NULL,seq INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS entities_session ON entities(session,kind);
        CREATE TABLE IF NOT EXISTS journal(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL,kind TEXT NOT NULL,
          session TEXT NOT NULL,data TEXT);
        CREATE INDEX IF NOT EXISTS journal_key ON journal(key,seq);
        """)

    def close(self):
        self.db.close()

    def set_entity(self, key, kind, session, value):
        data = encode(value).decode()
        previous = self.db.execute("SELECT data FROM entities WHERE key=?", (key,)).fetchone()
        if previous and previous[0] == data:
            return
        seq = self.db.execute("INSERT INTO journal(key,kind,session,data) VALUES (?,?,?,?)", (key, kind, session, data)).lastrowid
        self.db.execute("INSERT INTO entities VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,seq=excluded.seq", (key, kind, session, data, seq))

    def delete_entity(self, key):
        row = self.db.execute("SELECT kind,session FROM entities WHERE key=?", (key,)).fetchone()
        if row:
            self.db.execute("INSERT INTO journal(key,kind,session,data) VALUES (?,?,?,NULL)", (key, row["kind"], row["session"]))
            self.db.execute("DELETE FROM entities WHERE key=?", (key,))

    def seq(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM journal").fetchone()[0]

    def feed(self, after=0, limit=100, snapshot=None):
        self.db.execute("BEGIN")
        try:
            maximum = self.seq() if snapshot is None else min(snapshot, self.seq())
            query = "SELECT j.* FROM journal j WHERE j.seq>? AND j.seq<=?"
            if snapshot is not None:
                query += " AND j.seq=(SELECT max(p.seq) FROM journal p WHERE p.key=j.key AND p.seq<=?)"
            args = (after, maximum, maximum, limit) if snapshot is not None else (after, maximum, limit)
            rows, size = [], 0
            for row in self.db.execute(query + " ORDER BY j.seq LIMIT ?", args):
                size += len(row["data"] or "")
                rows.append(row)
                if size >= 2 * 1024 * 1024:
                    break
            changes = [{"seq": str(row["seq"]), "key": row["key"], "kind": row["kind"], "session": row["session"],
                        "data": json.loads(row["data"]) if row["data"] is not None else None} for row in rows]
            cursor = rows[-1]["seq"] if rows else maximum
            return {"seq": str(maximum), "cursor": str(cursor), "changes": changes, "done": cursor >= maximum}
        finally:
            self.db.rollback()

    def threads(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM entities WHERE kind='thread'")]

    def items(self, session, after=-1, limit=100):
        rows = self.db.execute("SELECT data FROM entities WHERE session=? AND kind='item' AND json_extract(data,'$.position')>? ORDER BY json_extract(data,'$.position') LIMIT ?", (session, after, limit))
        return [json.loads(row[0]) for row in rows]

    def status(self):
        return {"seq": str(self.seq()), "threads": self.db.execute("SELECT count(*) FROM entities WHERE kind='thread'").fetchone()[0],
                "items": self.db.execute("SELECT count(*) FROM entities WHERE kind='item'").fetchone()[0],
                "source_seq": dict(self.db.execute("SELECT key,value FROM state")).get("source_seq", "0")}
