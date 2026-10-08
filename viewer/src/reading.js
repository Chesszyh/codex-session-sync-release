import { fetchReplica } from "./api.js";
import { loadIndex, loadTurnPage, indexFromPreviews, toTurn } from "./trace.js";
import { progress } from "./cache.js";

const request = (req) =>
  new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
const complete = (tx) =>
  new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onabort = tx.onerror = () => reject(tx.error);
  });
const keyFor = (session, ...parts) => JSON.stringify([session, ...parts]);
const get = (db, key) => request(db.transaction("reads").objectStore("reads").get(key));

export function initialMode() {
  const saved = localStorage.getItem("replica-sync-mode");
  return saved === "full" || saved === "demand"
    ? saved
    : matchMedia("(pointer: coarse)").matches
      ? "demand"
      : "full";
}

async function save(db, entry) {
  const tx = db.transaction("reads", "readwrite"),
    done = complete(tx),
    store = tx.objectStore("reads");
  const previous = await request(store.get(entry.key));
  if (previous && BigInt(previous.version) > BigInt(entry.version)) {
    await done;
    return;
  }
  if (entry.kind === "page") {
    const index = await request(store.get(keyFor(entry.session, "index")));
    if (index && BigInt(index.version) > BigInt(entry.version)) {
      await done;
      return;
    }
  }
  if (entry.kind === "index") {
    if (previous && previous.version !== entry.version) {
      // A changed head may remove or reorder items; never combine its index with older pages.
      const cursor = store.index("session").openCursor(entry.session);
      cursor.onsuccess = () => {
        const row = cursor.result;
        if (row) {
          if (row.value.kind === "page") row.delete();
          row.continue();
        }
      };
    }
  }
  store.put(entry);
  await done;
}

function notice(error, cached) {
  if (error.message === "authentication_required")
    return cached ? "登录已过期 · 阅读已缓存内容" : "登录已过期，请重新登录";
  if (error.name === "QuotaExceededError") return "本地空间不足，当前内容未能保存供离线阅读";
  return cached ? "离线 · 阅读已缓存内容" : "此内容尚未缓存，请联网后打开";
}

async function fullSessionAvailable(db, session) {
  const p = await progress(db);
  if (p.phase !== "changes") return false;
  const rows = await request(
    db
      .transaction("entities")
      .objectStore("entities")
      .index("sessionKind")
      .getAll([session, "thread"]),
  );
  return rows.some((r) => r.data && BigInt(r.seq) <= BigInt(p.cursor));
}

export async function readIndex(db, session, signal) {
  const key = keyFor(session, "index"),
    cached = await get(db, key);
  let response;
  try {
    const params = new URLSearchParams({ session });
    if (cached) params.set("version", cached.version);
    const res = await fetchReplica(`/api/read/index?${params}`, { signal });
    if (!res.ok) throw new Error("connection_failed");
    response = await res.json();
    signal?.throwIfAborted();
  } catch (error) {
    if (signal?.aborted) throw error;
    if (cached) return { value: indexFromPreviews(cached.data), notice: notice(error, true) };
    if (await fullSessionAvailable(db, session))
      return {
        value: { ...(await loadIndex(db, session)), version: "full" },
        notice: notice(error, true),
      };
    throw new Error(notice(error, false));
  }
  const data = response.unchanged ? cached.data : response;
  let message = "";
  if (!response.unchanged) {
    try {
      await save(db, { key, session, kind: "index", version: data.version, data });
    } catch (error) {
      message = notice(error, false);
    }
  }
  return { value: indexFromPreviews(data), notice: message };
}

export async function readPage(db, session, turn, index, after, signal) {
  if (index.version === "full")
    return {
      value: await loadTurnPage(db, session, turn, index, after),
      notice: "离线 · 阅读已缓存内容",
    };
  const key = keyFor(session, "page", turn, after),
    cached = await get(db, key);
  let data;
  try {
    const params = new URLSearchParams({
      session,
      turn,
      after: String(after),
      version: index.version,
    });
    const response = await fetchReplica(`/api/read/page?${params}`, { signal });
    if (response.status === 409) throw new Error("session_changed");
    if (!response.ok) throw new Error("connection_failed");
    data = await response.json();
    signal?.throwIfAborted();
  } catch (error) {
    if (signal?.aborted || error.message === "session_changed") throw error;
    if (!cached || cached.version !== index.version) throw new Error(notice(error, false));
    return {
      value: { ...cached.data, turn: toTurn(turn, cached.data.rows, index.metadata.get(turn)) },
      notice: notice(error, true),
    };
  }
  let message = "";
  try {
    await save(db, { key, session, kind: "page", version: data.version, data });
  } catch (error) {
    message = notice(error, false);
  }
  return {
    value: { ...data, turn: toTurn(turn, data.rows, index.metadata.get(turn)) },
    notice: message,
  };
}

export async function searchReadPages(db, query, found, current) {
  const needle = query.toLocaleLowerCase();
  return new Promise((resolve, reject) => {
    const cursor = db.transaction("reads").objectStore("reads").index("kind").openCursor("page");
    cursor.onerror = () => reject(cursor.error);
    cursor.onsuccess = () => {
      const entry = cursor.result;
      if (!entry || !current()) {
        resolve(found);
        return;
      }
      for (const row of entry.value.data.rows) {
        const text = row.text || "",
          offset = text.toLocaleLowerCase().indexOf(needle);
        if (offset >= 0 && !found.has(entry.value.session))
          found.set(entry.value.session, {
            position: row.position,
            snippet: text.slice(Math.max(0, offset - 35), offset + 120),
          });
      }
      entry.continue();
    };
  });
}
