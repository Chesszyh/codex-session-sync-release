import { useEffect, useRef, useState } from "react";
import { TurnDetail } from "./vendor/codex-trace/src/components/TurnDetail";
import { TurnList } from "./vendor/codex-trace/src/components/TurnList";
import type { CodexTurn, TurnSummary } from "./vendor/codex-trace/shared/types";
import { loadIndex, loadTurnPage, recordId } from "./trace.js";
import { readIndex, readPage } from "./reading.js";

type Index = Omit<Awaited<ReturnType<typeof loadIndex>>, "version"> & { version?: string };
type Page = Awaited<ReturnType<typeof loadTurnPage>>;
function SourceRecord({ row, origin }: { row: Page["rows"][number]; origin: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="source" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>来源与原始条目</summary>
      {open && (
        <>
          <pre>
            {JSON.stringify(
              {
                origin,
                turn: row.turn,
                startedAtMs: row.startedAtMs,
                completedAtMs: row.completedAtMs,
                ...row.source,
              },
              null,
              2,
            )}
          </pre>
          <pre>{JSON.stringify(row.item, null, 2)}</pre>
        </>
      )}
    </details>
  );
}
export type Thread = {
  id: string;
  origin: string;
  thread_id: string;
  title: string;
  archived: boolean;
  items: number;
  generation?: string;
  cwd?: string;
  updated_at?: number;
  metadata?: Record<string, unknown>;
  coverage: { status: string; issues: string[] };
};

export function Archive({
  db,
  thread,
  revision,
  hit,
  threads,
  onSelect,
  mode,
}: {
  db: IDBDatabase;
  thread: Thread;
  revision: number;
  hit?: { position: number };
  threads: Thread[];
  onSelect: (id: string) => void;
  mode: "full" | "demand";
}) {
  const [index, setIndex] = useState<Index | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState<Page | null>(null);
  const [after, setAfter] = useState(-1);
  const [previous, setPrevious] = useState<number[]>([]);
  const [expanded, setExpanded] = useState(new Set<number>());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(0);
  const [check, setCheck] = useState(0);
  const lastHit = useRef<number | undefined>(undefined);
  const indexVersion = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (mode !== "demand") return;
    const timer = setInterval(() => setCheck((n) => n + 1), 15000);
    return () => clearInterval(timer);
  }, [mode]);

  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const load =
      mode === "demand"
        ? readIndex(db, thread.id, controller.signal)
        : loadIndex(db, thread.id).then((value) => ({ value, notice: "" }));
    load
      .then(({ value, notice: message }) => {
        if (current) {
          if (mode === "demand" && indexVersion.current !== value.version) setPage(null);
          indexVersion.current = value.version;
          setIndex((old) => (mode === "demand" && old?.version === value.version ? old : value));
          setError("");
          setNotice(message);
        }
      })
      .catch((e) => {
        if (current) setError(String(e));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [db, thread.id, revision, retry, check, mode]);

  useEffect(() => {
    if (!index || hit?.position === lastHit.current) return;
    lastHit.current = hit?.position;
    if (!hit) {
      setAfter(-1);
      setPrevious([]);
      return;
    }
    const target = index.summaries.find(
      (s, i) =>
        s.position <= hit.position &&
        (!index.summaries[i + 1] || index.summaries[i + 1].position > hit.position),
    );
    if (target) {
      setSelected(target.turn_id);
      setAfter(hit.position - 1);
      setPrevious([-1]);
    }
  }, [hit, index]);

  useEffect(() => {
    if (!index || !selected) return;
    if (!index.summaries.some((s) => s.turn_id === selected)) {
      setSelected(null);
      setPage(null);
      return;
    }
    let current = true;
    const controller = new AbortController();
    const load =
      mode === "demand"
        ? readPage(db, thread.id, selected, index, after, controller.signal)
        : loadTurnPage(db, thread.id, selected, index, after).then((value) => ({
            value,
            notice: "",
          }));
    load
      .then(({ value, notice: message }) => {
        if (!current) return;
        if (!value.rows.length && after !== -1) {
          setAfter(-1);
          setPrevious([]);
          return;
        }
        setPage(value);
        setError("");
        setNotice(message);
      })
      .catch((e) => {
        if (!current) return;
        if (e.message === "session_changed") {
          setPage(null);
          setCheck((n) => n + 1);
        } else setError(String(e));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [db, thread.id, selected, index, after, mode, retry]);

  function selectTurn(i: number) {
    setSelected(index!.summaries[i].turn_id);
    setAfter(-1);
    setPrevious([]);
    setExpanded(new Set());
    setPage(null);
  }
  function renderRecord(id: string) {
    const row = page?.rows.find((r) => recordId(r) === id);
    if (!row) return null;
    const targets: string[] =
      row.item.type === "collabAgentToolCall"
        ? row.item.receiverThreadIds || []
        : row.item.type === "subAgentActivity"
          ? [row.item.agentThreadId]
          : [];
    return (
      <div className="record-meta">
        {row.item.status && (
          <span className="record-status">
            {(
              {
                completed: "完成",
                inProgress: "进行中",
                failed: "失败",
                declined: "未执行",
              } as Record<string, string>
            )[row.item.status] || row.item.status}
          </span>
        )}
        {row.live && (
          <span className="record-status">
            {row.uncertain ? "连接中断 · 内容可能不连续" : row.complete ? "通知已完成" : "正在记录"}
          </span>
        )}
        {targets.map((native) => {
          const child = threads.find((t) => t.origin === thread.origin && t.thread_id === native);
          return child ? (
            <button key={native} onClick={() => onSelect(child.id)}>
              打开子代理：{child.title}
            </button>
          ) : (
            <span key={native}>子代理尚未归档：{native}</span>
          );
        })}
        {(row.item.content || [])
          .filter((c: { type: string }) => ["image", "localImage", "document"].includes(c.type))
          .map((c: { type: string; url?: string }, i: number) =>
            c.type === "image" && /^data:image\/(png|jpeg|webp|gif);base64,/i.test(c.url || "") ? (
              <img className="attachment" src={c.url} alt="会话图片" key={i} loading="lazy" />
            ) : (
              <span className="attachment-missing" key={i}>
                附件引用已保留，文件尚未归档
              </span>
            ),
          )}
        <SourceRecord key={id} row={row} origin={thread.origin} />
      </div>
    );
  }
  return (
    <section className="archive">
      <header className="archive-header">
        <button className="mobile-back" onClick={() => onSelect("")}>
          会话列表
        </button>
        <div>
          <h1>{thread.title}</h1>
          <p>
            {thread.origin} {thread.archived ? "· 已归档" : ""}
          </p>
        </div>
      </header>
      <details className="archive-info">
        <summary>{thread.items} 条归档记录 · 历史信息</summary>
        <pre>
          {JSON.stringify(
            {
              thread: thread.thread_id,
              head: thread.generation,
              cwd: thread.cwd,
              coverage: thread.coverage,
            },
            null,
            2,
          )}
        </pre>
      </details>
      {thread.coverage.status === "partial" && (
        <div id="coverage">
          {thread.coverage.issues.includes("legacy_response_context")
            ? "旧格式历史按原始记录显示。"
            : "部分历史暂不可读，已保存的记录仍可查看。"}
        </div>
      )}
      {notice && (
        <div className="reading-notice" role="status">
          {notice}
          {notice.includes("登录") && <a href="/?login=1">重新登录</a>}
        </div>
      )}
      {error && (
        <div className="reading-notice" role="alert">
          {error.replace(/^Error: /, "")}
          <button
            onClick={() => {
              setError("");
              setRetry((n) => n + 1);
            }}
          >
            重试
          </button>
          {error.includes("登录") && <a href="/?login=1">重新登录</a>}
        </div>
      )}
      {!index && !error ? (
        <div className="app__loading">正在读取轮次…</div>
      ) : index ? (
        <div className={`archive-panels ${selected ? "has-detail" : ""}`}>
          <div className="archive-turns">
            <TurnList
              summaries={index.summaries as TurnSummary[]}
              selectedIndex={index.summaries.findIndex((s) => s.turn_id === selected)}
              onSelectTurn={selectTurn}
            />
          </div>
          {selected && (
            <div id="messages" className="archive-detail">
              {page ? (
                <TurnDetail
                  turn={page.turn as CodexTurn}
                  expanded={expanded}
                  onToggle={(i) =>
                    setExpanded((old) => {
                      const next = new Set(old);
                      if (next.has(i)) next.delete(i);
                      else next.add(i);
                      return next;
                    })
                  }
                  onBack={() => {
                    setSelected(null);
                    setPage(null);
                  }}
                  renderRecord={renderRecord}
                />
              ) : (
                <div className="app__loading">正在读取正文…</div>
              )}
              <nav className="record-pages" aria-label="记录分页">
                <button
                  disabled={!previous.length}
                  onClick={() => {
                    setAfter(previous.at(-1)!);
                    setPrevious(previous.slice(0, -1));
                    setPage(null);
                    setExpanded(new Set());
                  }}
                >
                  上一页
                </button>
                <span>每页最多 60 条记录</span>
                <button
                  disabled={!page?.more}
                  onClick={() => {
                    setPrevious([...previous, after]);
                    setAfter(page!.rows.at(-1)!.position);
                    setPage(null);
                    setExpanded(new Set());
                  }}
                >
                  下一页
                </button>
              </nav>
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}
