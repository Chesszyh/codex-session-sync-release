import { fetchReplica } from "./api.js";

const req = request => new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
const done = tx => new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error); });
const sessionKey = (origin, thread) => JSON.stringify([origin, thread]);

export function reduce(previous, method, params) {
  if (["item/started", "item/completed"].includes(method) && params.item) return { item: params.item, complete: method === "item/completed", uncertain: false };
  let value = previous ? structuredClone(previous) : null;
  if (!["item/agentMessage/delta", "item/commandExecution/outputDelta"].includes(method) || typeof params.delta !== "string") return value;
  const agent = method === "item/agentMessage/delta";
  if (!value) value = { item: { type: agent ? "agentMessage" : "commandExecution", id: params.itemId, ...(agent ? { text: "" } : { command: "", aggregatedOutput: "" }) }, complete: false, uncertain: true };
  if (!value.complete) { const field = agent ? "text" : "aggregatedOutput"; value.item[field] = (value.item[field] || "") + params.delta; }
  return value;
}

export async function applyLive(db, page, snapshot = false) {
  const tx = db.transaction(["entities", "meta"], "readwrite"), completed = done(tx);
  const store = tx.objectStore("entities"), meta = tx.objectStore("meta");
  let cursor = (await req(meta.get("live_cursor"))) || "0";
  if (snapshot) {
    for (const kind of ["liveItem", "liveThread", "liveStatus"]) for (const key of await req(store.index("kind").getAllKeys(kind))) store.delete(key);
    for (const item of page.items) store.put({ key: "live:" + item.key, kind: "liveItem", session: item.data.session, seq: item.seq, data: { ...item.data, first_seq: item.seq } });
    for (const source of page.sources) store.put({ key: "liveStatus:" + source.origin, kind: "liveStatus", session: "", seq: page.seq, data: { origin: source.origin, status: source.status } });
    cursor = page.seq;
  } else {
    for (const event of page.events) {
      if (BigInt(event.seq) <= BigInt(cursor)) continue;
      const params = event.data.params || {}, method = event.data.method;
      if (event.kind === "event") {
        const thread = params.threadId, turn = params.turnId, item = params.itemId || params.item?.id;
        if (thread && turn && item) {
          const key = "live:" + JSON.stringify([event.origin, thread, turn, item]);
          const previous = await req(store.get(key));
          const value = reduce(previous?.data, method, params);
          if (value) store.put({ key, kind: "liveItem", seq: event.seq, session: sessionKey(event.origin, thread), data: { ...value, session: sessionKey(event.origin, thread), turn, epoch: event.epoch, first_seq: previous?.data.first_seq || event.seq } });
        }
        if (method === "thread/started" && params.thread) {
          const t = params.thread, id = sessionKey(event.origin, t.id);
          store.put({ key: "liveThread:" + id, kind: "liveThread", session: id, seq: event.seq, data: { id, origin: event.origin, thread_id: t.id, title: t.name || t.preview || "新会话", cwd: t.cwd, items: 0, archived: false, updated_at: t.updatedAt, coverage: { status: "live", issues: [] } } });
        }
      } else if (["gap", "connected", "snapshot"].includes(event.kind)) {
        store.put({ key: "liveStatus:" + event.origin, kind: "liveStatus", session: "", seq: event.seq, data: { origin: event.origin, status: event.kind === "gap" ? "disconnected" : "connected", reason: event.data.reason } });
        if (event.kind === "gap") for (const row of await req(store.index("kind").getAll("liveItem"))) {
          if (row.data.session.startsWith(JSON.stringify([event.origin]).slice(0, -1)) && !row.data.complete) { row.data.uncertain = true; store.put(row); }
        }
      }
      cursor = event.seq;
    }
  }
  meta.put(cursor, "live_cursor");
  await completed;
  return cursor;
}

export async function syncLive(db, notify, signal) {
  return navigator.locks.request("replica-live-sync", { ifAvailable: true }, async lock => {
    if (!lock || signal?.aborted) return;
    let cursor = await req(db.transaction("meta").objectStore("meta").get("live_cursor"));
    while (true) {
      signal?.throwIfAborted();
      const response = await fetchReplica(cursor === undefined ? "/api/live/snapshot" : `/api/live/changes?after=${cursor}`, { signal });
      if (!response.ok) return;
      const page = await response.json();
      signal?.throwIfAborted();
      if (!page.available) return;
      if (page.reset_required) { cursor = undefined; continue; }
      const changed = cursor === undefined || page.events.length > 0;
      cursor = await applyLive(db, page, cursor === undefined);
      if (changed) notify();
      let consumer = await req(db.transaction("meta").objectStore("meta").get("live_consumer"));
      if (!consumer) { consumer = crypto.randomUUID(); const tx = db.transaction("meta", "readwrite"); const saved = done(tx); tx.objectStore("meta").put(consumer, "live_consumer"); await saved; }
      await fetchReplica("/api/live/ack", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ consumer, seq: cursor }) });
      if (!page.events || page.done) return;
    }
  });
}
