import { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { openCache, byKind, searchItems, synchronize } from "./cache.js";
import { syncLive } from "./live.js";
import { initialMode, searchReadPages } from "./reading.js";
import { sessionInfo } from "./trace.js";
import { SidebarTree } from "./vendor/codex-trace/src/components/SidebarTree";
import type { CodexSessionInfo } from "./vendor/codex-trace/shared/types";
import { Archive, type Thread } from "./archive";

// The login URL bypasses the offline cache once; later reloads need the cached URL.
if (new URLSearchParams(location.search).has("login"))
  history.replaceState(null, "", location.pathname + location.hash);

const db = await openCache(() => {
  document.getElementById("root")!.textContent = "请关闭此网站的其他旧页面，缓存升级后会继续。";
});
function App() {
  const [mode, setMode] = useState<"full" | "demand">(() => initialMode() as "full" | "demand");
  const [threads, setThreads] = useState<Thread[]>([]);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState(() => decodeURIComponent(location.hash.slice(1)));
  const [connection, setConnection] = useState("正在打开本地副本");
  const [needsLogin, setNeedsLogin] = useState(false);
  const [liveStatus, setLiveStatus] = useState("");
  const [archiveStatus, setArchiveStatus] = useState("");
  const [archiveDetails, setArchiveDetails] = useState("");
  const [origin, setOrigin] = useState("");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Map<string, { position: number; snippet: string }>>(
    new Map(),
  );
  const [dates, setDates] = useState(new Set<string>());
  const [visibleCount, setVisibleCount] = useState(150);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => {
    localStorage.setItem("replica-sync-mode", mode);
  }, [mode]);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === "replica-sync-mode") setMode(initialMode() as "full" | "demand");
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setConnection(mode === "demand" ? "正在更新目录" : "正在打开本地副本");
    setLiveStatus("");
    let stopped = false,
      refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let historyTimer: ReturnType<typeof setTimeout>, liveTimer: ReturnType<typeof setTimeout>;
    async function refresh() {
      const [saved, live, sources, hosts] = await Promise.all([
        byKind(db, "thread"),
        byKind(db, "liveThread"),
        byKind(db, "liveStatus"),
        byKind(db, "host"),
      ]);
      if (stopped) return;
      const known = new Set(saved.map((t: Thread) => t.id));
      setThreads(
        mode === "full" ? [...saved, ...live.filter((t: Thread) => !known.has(t.id))] : saved,
      );
      setRevision((n) => n + 1);
      setArchiveStatus(
        hosts.length
          ? `归档来源 ${hosts.length} 个 · 最近采集成功 ${hosts.filter((h: { last_scan?: { complete: boolean } }) => h.last_scan?.complete).length} 个`
          : "",
      );
      setArchiveDetails(
        hosts
          .map(
            (h: { id: string; last_complete?: string }) =>
              `${h.id.split(":")[0]}：${h.last_complete ? new Date(h.last_complete).toLocaleString() : "尚未完成采集"}`,
          )
          .join("\n"),
      );
      if (mode === "full")
        setLiveStatus(
          sources.length
            ? `通知入口已连接 ${sources.filter((s: { status: string }) => s.status === "connected").length} 个${sources.some((s: { status: string }) => s.status === "disconnected") ? " · 通知存在缺口" : ""}`
            : "",
        );
    }
    function schedule() {
      if (!refreshTimer)
        refreshTimer = setTimeout(() => {
          refreshTimer = undefined;
          void refresh().catch((e) => setConnection(`本地副本读取失败：${e.message}`));
        }, 600);
    }
    async function tick() {
      try {
        await synchronize(
          db,
          (value: { state: string; phase: string; received: number; changed: number }) => {
            if (stopped) return;
            setNeedsLogin(false);
            setConnection(
              mode === "demand"
                ? value.state === "syncing"
                  ? `正在更新目录 · ${value.received} 条`
                  : "目录已更新"
                : value.state === "syncing"
                  ? `${value.phase === "snapshot" ? "保存初始副本" : "正在补齐增量"} · 本次已保存 ${value.received} 条`
                  : "副本已同步",
            );
            if (value.changed) schedule();
          },
          { scope: mode === "demand" ? "directory" : undefined, signal: controller.signal },
        );
      } catch (e) {
        const error = e as Error;
        if (!stopped && error.message === "authentication_required") setNeedsLogin(true);
        if (!stopped)
          setConnection(
            error.name === "QuotaExceededError"
              ? "本地空间不足，同步已暂停"
              : error.message === "authentication_required"
                ? "登录已过期 · 阅读本地副本"
                : error.message === "reset_required"
                  ? "归档序号已变化，需要重新连接副本"
                  : "离线 · 阅读本地副本",
          );
      } finally {
        if (!stopped) historyTimer = setTimeout(tick, mode === "demand" ? 15000 : 3000);
      }
    }
    async function liveTick() {
      try {
        await syncLive(db, schedule, controller.signal);
      } catch {
        if (!stopped) setLiveStatus("状态连接中断 · 未捕获的通知可能缺失");
      } finally {
        if (!stopped) liveTimer = setTimeout(liveTick, 1500);
      }
    }
    void refresh().catch((e) => setConnection(`本地副本读取失败：${e.message}`));
    void tick();
    if (mode === "full") void liveTick();
    const onFocus = () => schedule();
    window.addEventListener("focus", onFocus);
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(refreshTimer);
      clearTimeout(historyTimer);
      clearTimeout(liveTimer);
      window.removeEventListener("focus", onFocus);
    };
  }, [mode]);

  useEffect(() => {
    let current = true;
    setMatches(new Map());
    setVisibleCount(150);
    if (query.trim())
      void searchItems(db, query.trim(), () => current)
        .then((value) =>
          mode === "demand" ? searchReadPages(db, query.trim(), value, () => current) : value,
        )
        .then((value) => {
          if (current) setMatches(value as Map<string, { position: number; snippet: string }>);
        });
    return () => {
      current = false;
    };
  }, [query, revision, mode]);
  useEffect(() => {
    function keyboard(e: KeyboardEvent) {
      if (e.key === "/" && !(e.target instanceof HTMLInputElement)) {
        e.preventDefault();
        search.current?.focus();
      }
    }
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);
  function select(id: string) {
    setSelected(id);
    history.replaceState(null, "", id ? `#${encodeURIComponent(id)}` : location.pathname);
  }
  const visible = useMemo(
    () =>
      threads
        .filter(
          (t) =>
            (!origin || t.origin === origin) &&
            (filter === "all" || t.archived === (filter === "archived")) &&
            (!query.trim() ||
              t.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()) ||
              matches.has(t.id)),
        )
        .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0)),
    [threads, origin, filter, query, matches],
  );
  const infos = useMemo(
    () => visible.slice(0, visibleCount).map(sessionInfo) as CodexSessionInfo[],
    [visible, visibleCount],
  );
  const thread = threads.find((t) => t.id === selected);
  return (
    <div className={`replica-app ${thread ? "reading" : ""}`}>
      <aside className="replica-sidebar">
        <header>
          <h1>会话档案</h1>
          <span>Codex Trace</span>
        </header>
        <input
          ref={search}
          id="search"
          type="search"
          placeholder={mode === "demand" ? "搜索目录与已缓存内容" : "搜索会话与内容"}
          aria-label={mode === "demand" ? "搜索目录与已缓存内容" : "搜索会话与内容"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="filters">
          <select
            aria-label="来源"
            value={origin}
            onChange={(e) => {
              setOrigin(e.target.value);
              setVisibleCount(150);
            }}
          >
            <option value="">所有来源</option>
            {[...new Set(threads.map((t) => t.origin))].map((o) => (
              <option key={o}>{o}</option>
            ))}
          </select>
          <select
            aria-label="历史范围"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setVisibleCount(150);
            }}
          >
            <option value="all">全部会话</option>
            <option value="active">未归档</option>
            <option value="archived">已归档</option>
          </select>
        </div>
        <div className="library-count">{visible.length} 条会话</div>
        <SidebarTree
          sessions={infos}
          selectedPath={selected}
          collapsedDates={dates}
          onToggleDate={(date) =>
            setDates((old) => {
              const next = new Set(old);
              if (next.has(date)) next.delete(date);
              else next.add(date);
              return next;
            })
          }
          onSelectSession={(info) => select(info.path)}
          hasMore={visibleCount < visible.length}
          onReachEnd={() => setVisibleCount((n) => n + 150)}
        />
        <footer>
          <label className="sync-choice">
            同步方式
            <select
              aria-label="同步方式"
              value={mode}
              onChange={(e) => setMode(e.target.value as "full" | "demand")}
            >
              <option value="demand">按需阅读</option>
              <option value="full">完整离线副本</option>
            </select>
          </label>
          <small>
            {mode === "demand" ? "打开时加载，已读内容可离线" : "自动保存全部历史，离线可全文搜索"}
          </small>
          <div id="connection" role="status">
            {connection}
          </div>
          {needsLogin && <a href="/?login=1">重新登录</a>}
          <small>目录 {threads.length} 条会话</small>
          <small id="archive-status" title={archiveDetails}>
            {archiveStatus}
          </small>
          <small
            id="live-status"
            title="这是原生通知入口数量，不是在线电脑数量；历史归档独立采集。"
          >
            {liveStatus}
          </small>
        </footer>
      </aside>
      <main>
        {thread ? (
          <Archive
            key={`${thread.id}:${mode}`}
            db={db}
            thread={thread}
            revision={revision}
            hit={matches.get(thread.id)}
            threads={threads}
            onSelect={select}
            mode={mode}
          />
        ) : (
          <div className="welcome">
            <h2>选择一条会话，继续阅读。</h2>
            <p>
              {threads.length} 条会话 · {new Set(threads.map((t) => t.origin)).size} 个来源
            </p>
          </div>
        )}
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
if ("serviceWorker" in navigator) void navigator.serviceWorker.register("/sw.js");
if (navigator.storage?.persist) void navigator.storage.persist();
