import { scanSession, turnItems } from "./cache.js";

export const recordId = (row) => JSON.stringify([row.turn, row.item.id]);
const json = (value) =>
  value == null ? null : typeof value === "string" ? value : JSON.stringify(value, null, 2);
const iso = (value) => (value == null ? "" : new Date(value).toISOString());
const status = (value) =>
  ({ completed: "complete", interrupted: "aborted", failed: "error", inProgress: "ongoing" })[
    value
  ] ||
  value ||
  "unknown";

export function sessionInfo(thread) {
  const m = thread.metadata || {};
  let source = m.source;
  if (typeof source === "string" && source.startsWith("{")) source = JSON.parse(source);
  const parent = m.parent_thread_id ?? source?.subagent?.thread_spawn?.parent_thread_id;
  const date =
    thread.updated_at == null
      ? ""
      : iso(typeof thread.updated_at === "number" ? thread.updated_at * 1000 : thread.updated_at);
  const localDate = date ? new Date(date) : null;
  return {
    id: thread.id,
    path: thread.id,
    cwd: thread.cwd ?? null,
    thread_name: thread.title,
    start_time: date,
    date_group: localDate
      ? `${localDate.getFullYear()}-${String(localDate.getMonth() + 1).padStart(2, "0")}-${String(localDate.getDate()).padStart(2, "0")}`
      : "日期未知",
    is_ongoing: thread.coverage.status === "live",
    parent_session_id: parent ? JSON.stringify([thread.origin, parent]) : null,
    is_external_worker: !!parent,
    is_inline_worker: false,
    worker_nickname: m.agent_nickname ?? null,
    worker_role: m.agent_role ?? null,
    originator: thread.origin,
    is_archived: thread.archived,
  };
}

function blankTurn(id, meta = {}) {
  return {
    turn_id: id,
    started_at: meta.startedAt ?? null,
    completed_at: meta.completedAt ?? null,
    duration_ms: meta.durationMs ?? null,
    status: status(meta.status),
    user_message: null,
    agent_messages: [],
    tool_calls: [],
    tool_call_orders: [],
    final_answer: null,
    total_tokens: null,
    model: meta.model ?? null,
    cwd: null,
    reasoning_effort: null,
    error: json(meta.error),
    has_compaction: false,
    thread_name: null,
    collab_spawns: [],
    trace_id: null,
    forked_from_thread_id: null,
    compaction_meta: null,
  };
}

const toolKinds = {
  commandExecution: "exec_command",
  fileChange: "patch_apply",
  mcpToolCall: "mcp_tool",
  dynamicToolCall: "mcp_tool",
  webSearch: "web_search",
  imageGeneration: "image_generation",
};
const messageTypes = new Set(["userMessage", "agentMessage", "reasoning", "plan"]);
export function itemText(item) {
  if (item.type === "userMessage")
    return (item.content || [])
      .filter((c) => c.type === "text")
      .map((c) => c.text)
      .join("\n");
  if (item.type === "reasoning")
    return [...(item.summary || []), ...(item.content || [])].join("\n");
  if (item.type === "commandExecution")
    return (item.command || "") + "\n" + (item.aggregatedOutput || "");
  return item.text ?? json(item);
}
export function toTool(row) {
  const i = row.item;
  const collab = i.type === "collabAgentToolCall";
  const kind = collab
    ? {
        spawnAgent: "spawn_agent",
        wait: "wait_agent",
        sendInput: "followup_task",
        closeAgent: "interrupt_agent",
      }[i.tool] || "unknown"
    : toolKinds[i.type] || "unknown";
  return {
    call_id: recordId(row),
    record_id: recordId(row),
    kind,
    name: i.tool || i.type,
    arguments:
      i.arguments ??
      (collab
        ? { receiverThreadIds: i.receiverThreadIds, prompt: i.prompt, agentsStates: i.agentsStates }
        : {}),
    input_text: null,
    output:
      i.type === "commandExecution"
        ? (i.aggregatedOutput ?? null)
        : json(i.result ?? i.contentItems ?? i),
    exit_code: i.exitCode ?? null,
    command: i.command == null ? null : [i.command],
    cwd: i.cwd ?? null,
    duration_secs: i.durationMs == null ? null : i.durationMs / 1000,
    mcp_server: i.server ?? null,
    mcp_tool: i.tool ?? null,
    plugin_id: i.pluginId ?? null,
    script_path: null,
    subagent_id: null,
    subagent_name: null,
    patch_success:
      i.type === "fileChange"
        ? i.status === "completed"
          ? true
          : i.status === "failed"
            ? false
            : null
        : null,
    patch_changes: i.changes
      ? Object.fromEntries(
          i.changes.map((c) => [
            c.path,
            { type: c.kind?.type || c.kind || "update", unified_diff: c.diff },
          ]),
        )
      : null,
    web_query: i.query ?? null,
    web_url: i.action?.url ?? null,
    image_prompt: i.prompt ?? null,
    image_file_path: null,
    worker_session: null,
    status: i.status ?? "unknown",
    output_truncated: null,
  };
}

export function toTurn(id, rows, meta = {}) {
  const turn = blankTurn(id, meta);
  for (const row of [...rows].sort((a, b) => a.position - b.position)) {
    const i = row.item;
    if (messageTypes.has(i.type)) {
      const text = row.text ?? itemText(i);
      turn.agent_messages.push({
        text,
        phase: i.phase ?? null,
        timestamp: iso(row.startedAtMs),
        is_reasoning: i.type === "reasoning",
        order: row.position,
        record_id: recordId(row),
        label:
          { userMessage: "你", reasoning: "思考", plan: "计划" }[i.type] ||
          (i.phase === "final_answer" ? "答复" : "Codex"),
      });
      if (i.type === "userMessage")
        turn.user_message = [turn.user_message, text].filter(Boolean).join("\n\n");
      if (i.phase === "final_answer")
        turn.final_answer = [turn.final_answer, text].filter(Boolean).join("\n\n");
    } else {
      turn.tool_calls.push(toTool(row));
      turn.tool_call_orders.push(row.position);
    }
  }
  return turn;
}

function summarize(summaries, row) {
  let summary = summaries.get(row.turn);
  if (!summary) {
    summary = {
      ...blankTurn(row.turn),
      position: row.position,
      user_message: null,
      agent_preview: null,
      last_agent_timestamp: null,
      tool_call_count: 0,
      reasoning_count: 0,
      has_detail: true,
      items: 0,
      previewPosition: -1,
    };
    delete summary.agent_messages;
    delete summary.tool_calls;
    delete summary.tool_call_orders;
    summaries.set(row.turn, summary);
  }
  summary.position = Math.min(summary.position, row.position);
  summary.items++;
  const i = row.item;
  if (
    i.type === "userMessage" &&
    (summary.userPosition == null || row.position < summary.userPosition)
  ) {
    summary.user_message = row.text?.slice(0, 600) || "";
    summary.userPosition = row.position;
  } else if (i.type === "agentMessage" && row.position > summary.previewPosition) {
    summary.agent_preview = row.text?.slice(0, 600) || "";
    summary.previewPosition = row.position;
    summary.last_agent_timestamp = iso(row.startedAtMs) || null;
  } else if (i.type === "reasoning") summary.reasoning_count++;
  if (!messageTypes.has(i.type)) summary.tool_call_count++;
}

export function indexFromPreviews(data) {
  const summaries = new Map(),
    metadata = new Map(data.turns.map((row) => [row.id, row]));
  for (const row of data.previews) summarize(summaries, row);
  finishSummaries(summaries, metadata);
  return {
    summaries: [...summaries.values()].sort((a, b) => a.position - b.position),
    metadata,
    live: [],
    version: data.version,
  };
}

function finishSummaries(summaries, metadata) {
  for (const [id, meta] of metadata) {
    if (!summaries.has(id))
      summaries.set(id, {
        ...blankTurn(id),
        position: Number.MAX_SAFE_INTEGER,
        items: 0,
        has_detail: true,
        tool_call_count: 0,
        reasoning_count: 0,
      });
    Object.assign(summaries.get(id), {
      status: status(meta.status),
      started_at: meta.startedAt ?? null,
      completed_at: meta.completedAt ?? null,
      duration_ms: meta.durationMs ?? null,
    });
  }
}

export async function loadIndex(db, session) {
  const summaries = new Map(),
    metadata = new Map(),
    pending = new Map();
  // Only notification identities need a set; full historical bodies never accumulate here.
  await scanSession(
    db,
    session,
    (entry) => pending.set(recordId(entry.data), entry.data),
    "liveItem",
  );
  await scanSession(db, session, (entry) => metadata.set(entry.data.id, entry.data), "turn");
  let lastPosition = -1;
  await scanSession(
    db,
    session,
    (entry) => {
      if (!entry.data) {
        // Selected-head tombstones also suppress notifications from the discarded history.
        for (const [id, row] of pending) {
          if (entry.key === JSON.stringify(["item", session, row.turn, row.item.id]))
            pending.delete(id);
        }
        return;
      }
      summarize(summaries, entry.data);
      lastPosition = Math.max(lastPosition, entry.data.position);
      pending.delete(recordId(entry.data));
    },
    "item",
    true,
  );
  const live = [...pending.values()]
    .sort((a, b) => (BigInt(a.first_seq) < BigInt(b.first_seq) ? -1 : 1))
    .map((row, index) => ({
      ...row,
      live: true,
      position: lastPosition + index + 1,
      text: itemText(row.item),
      source: { generation: "通知 " + row.epoch, start: row.first_seq, end: row.first_seq },
    }));
  live.forEach((row) => summarize(summaries, row));
  finishSummaries(summaries, metadata);
  return {
    summaries: [...summaries.values()].sort((a, b) => a.position - b.position),
    metadata,
    live,
    version: undefined,
  };
}

export async function loadTurnPage(db, session, turn, index, after = -1, limit = 60) {
  const page = await turnItems(db, session, turn, after, limit);
  const rows = [...page.rows];
  const overlay = index.live.filter((row) => row.turn === turn && row.position > after);
  if (!page.more) rows.push(...overlay.slice(0, limit - rows.length));
  return {
    rows,
    more: page.more || (!page.more && overlay.length > limit - page.rows.length),
    turn: toTurn(turn, rows, index.metadata.get(turn)),
  };
}
