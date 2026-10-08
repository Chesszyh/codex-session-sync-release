import { fetchReplica } from "./api.js";

const request = req => new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
const complete = tx => new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error); });

export async function openCache(onBlocked = () => {}) {
  const opening = indexedDB.open("codex-session-replica", 3);
  opening.onblocked = onBlocked;
  opening.onupgradeneeded = event => {
    const db = opening.result;
    const reads = db.createObjectStore("reads", { keyPath: "key" });
    reads.createIndex("session", "session");
    reads.createIndex("kind", "kind");
    if (db.objectStoreNames.contains("entities")) {
      if (event.oldVersion < 2) {
        opening.transaction.objectStore("entities").createIndex("turnPosition", ["session", "data.turn", "data.position"]);
        opening.transaction.objectStore("entities").createIndex("sessionKind", ["session", "kind"]);
      }
      return;
    }
    const entities = db.createObjectStore("entities", { keyPath: "key" });
    entities.createIndex("kind", "kind");
    entities.createIndex("session", "session");
    entities.createIndex("position", ["session", "data.position"]);
    entities.createIndex("turnPosition", ["session", "data.turn", "data.position"]);
    entities.createIndex("sessionKind", ["session", "kind"]);
    db.createObjectStore("meta");
  };
  const db = await request(opening);
  db.onversionchange = () => db.close();
  return db;
}

export async function scanSession(db, session, visit, kind, includeDeleted = false) {
  return new Promise((resolve, reject) => {
    const cursor = db.transaction("entities").objectStore("entities").index("sessionKind").openCursor(IDBKeyRange.only([session, kind]));
    cursor.onerror = () => reject(cursor.error);
    cursor.onsuccess = () => {
      const row = cursor.result;
      if (!row) { resolve(); return; }
      if (row.value.data || includeDeleted) visit(row.value);
      row.continue();
    };
  });
}

export async function turnItems(db, session, turn, after = -1, limit = 60) {
  return new Promise((resolve, reject) => {
    const range = IDBKeyRange.bound([session, turn, after], [session, turn, Number.MAX_SAFE_INTEGER], true, false);
    const cursor = db.transaction("entities").objectStore("entities").index("turnPosition").openCursor(range);
    const rows = [];
    cursor.onerror = () => reject(cursor.error);
    cursor.onsuccess = () => {
      const row = cursor.result;
      if (!row || rows.length === limit) { resolve({ rows, more: !!row }); return; }
      rows.push(row.value.data);
      row.continue();
    };
  });
}

export async function progress(db, key = "progress") {
  return (await request(db.transaction("meta").objectStore("meta").get(key))) || { phase: "snapshot", cursor: "0" };
}

export async function applyPage(db, page, next, key = "progress") {
  const tx = db.transaction(["entities", "meta"], "readwrite");
  const done = complete(tx);
  const entities = tx.objectStore("entities");
  for (const change of page.changes) {
    const previous = entities.get(change.key);
    previous.onsuccess = () => {
      if (!previous.result || BigInt(change.seq) >= BigInt(previous.result.seq)) entities.put(change);
    };
  }
  tx.objectStore("meta").put(next, key);
  await done;
}

export async function byKind(db, kind) {
  return (await request(db.transaction("entities").objectStore("entities").index("kind").getAll(kind))).filter(row => row.data).map(row => row.data);
}

export async function sessionItems(db, session, before = Number.MAX_SAFE_INTEGER, limit = 80) {
  const index = db.transaction("entities").objectStore("entities").index("position");
  const range = IDBKeyRange.bound([session, 0], [session, before], false, true);
  return new Promise((resolve, reject) => {
    const rows = [];
    const cursor = index.openCursor(range, "prev");
    cursor.onerror = () => reject(cursor.error);
    cursor.onsuccess = () => {
      const row = cursor.result;
      if (!row || rows.length >= limit) { resolve(rows.reverse()); return; }
      if (row.value.data) rows.push(row.value.data);
      row.continue();
    };
  });
}

export async function searchItems(db, query, current = () => true) {
  const needle = query.toLocaleLowerCase();
  return new Promise((resolve, reject) => {
    const found = new Map();
    const cursor = db.transaction("entities").objectStore("entities").index("kind").openCursor("item");
    cursor.onerror = () => reject(cursor.error);
    cursor.onsuccess = () => {
      const row = cursor.result;
      if (!row || !current()) { resolve(found); return; }
      const entry = row.value;
      const text = entry.data?.text || "";
      const offset = text.toLocaleLowerCase().indexOf(needle);
      if (offset >= 0 && !found.has(entry.session)) found.set(entry.session, { position: entry.data.position, snippet: text.slice(Math.max(0, offset - 35), offset + 120) });
      row.continue();
    };
  });
}

export async function synchronize(db, notify, { scope, signal } = {}) {
  // A mode switch waits for the cancelled writer to release its transaction.
  return navigator.locks.request("replica-history-sync", signal ? { signal } : { ifAvailable: true }, async lock => {
    if (!lock || signal?.aborted) return;
    const key = scope === "directory" ? "directory-progress" : "progress";
    let p = await progress(db, key);
    let received = 0;
    while (true) {
      signal?.throwIfAborted();
      const params = new URLSearchParams({ after: p.cursor, limit: "100" });
      if (scope) params.set("scope", scope);
      if (p.at) params.set("at", p.at);
      const response = await fetchReplica(`/api/${p.phase === "snapshot" ? "snapshot" : "changes"}?${params}`, { signal });
      if (!response.ok) throw new Error(response.status === 409 ? "reset_required" : "connection_failed");
      const page = await response.json();
      signal?.throwIfAborted();
      const next = p.phase === "snapshot" && !page.done ? { phase: "snapshot", at: page.seq, cursor: page.cursor } : { phase: "changes", cursor: page.cursor };
      await applyPage(db, page, next, key);
      const phase = p.phase;
      p = next;
      received += page.changes.length;
      notify({ phase, received, state: page.done ? "online" : "syncing", cursor: page.cursor, total: page.seq, changed: page.changes.length,
               directoryChanged: page.changes.some(change => change.kind === "thread" || change.kind === "host"),
               sessions: new Set(page.changes.map(change => change.session)) });
      if (page.done) return;
    }
  });
}
