import type { ReactNode } from "react";
import type { AgentMessage, CodexToolCall, CodexTurn } from "../../shared/types";
import { ToolCallItem } from "./ToolCallItem";
import { ComplementaryItem } from "./ComplementaryItem";
import { OngoingDots } from "./OngoingDots";
import { BackIcon, CodexIcon } from "./Icons";
import { shortModel } from "../lib/format";
import { getContextColor, getModelColor } from "../lib/theme";
import { contextRemainingPercent, formatTokens, formatDuration } from "../../shared/format";

interface TurnDetailProps {
  turn: CodexTurn;
  renderRecord?: (id: string) => ReactNode;
  expanded: Set<number>;
  onToggle: (i: number) => void;
  onBack: () => void;
  openWorkerCallId?: string | null;
  onOpenWorkerPanel?: (tool: CodexToolCall) => void;
}

export function TurnDetail({
  turn,
  renderRecord,
  expanded,
  onToggle,
  onBack,
  openWorkerCallId,
  onOpenWorkerPanel,
}: TurnDetailProps) {
  type TimelineItem =
    | { order: number; kind: "msg"; msg: AgentMessage }
    | { order: number; kind: "tool"; tool: CodexToolCall; index: number };
  const timeline: TimelineItem[] = [];
  turn.agent_messages.forEach((msg) => {
    timeline.push({ order: msg.order ?? 0, kind: "msg", msg });
  });
  turn.tool_calls.forEach((tool, index) => {
    const order = turn.tool_call_orders?.[index] ?? Number.MAX_SAFE_INTEGER;
    timeline.push({ order, kind: "tool", tool, index });
  });
  timeline.sort((a, b) => a.order - b.order);
  const model = turn.model ? shortModel(turn.model) : "";
  const modelColor = turn.model ? getModelColor(turn.model) : undefined;

  const metaParts: string[] = [];
  if (turn.total_tokens?.total_tokens)
    metaParts.push(`${formatTokens(turn.total_tokens.total_tokens)} tok`);
  if (turn.duration_ms) metaParts.push(formatDuration(turn.duration_ms));
  const tokenInfo = turn.total_tokens;
  const contextLeftPercent = tokenInfo
    ? contextRemainingPercent(tokenInfo.context_window_tokens, tokenInfo.model_context_window)
    : null;
  const contextUsedPercent = contextLeftPercent === null ? null : 100 - contextLeftPercent;
  const contextTitle =
    tokenInfo && tokenInfo.context_window_tokens !== null
      ? `${formatTokens(tokenInfo.context_window_tokens)} / ${formatTokens(
          tokenInfo.model_context_window,
        )} context tokens`
      : undefined;

  return (
    <div className="turn-detail">
      <div className="message-detail__header">
        <button className="message-detail__back" onClick={onBack}>
          <BackIcon /> Back
        </button>
        <span className="message-detail__role-icon">
          <CodexIcon />
        </span>
        <span className="message-detail__title">Codex</span>
        <span className="record-status">
          {
            {
              complete: "完成",
              aborted: "中断",
              cancelled: "已取消",
              ongoing: "进行中",
              error: "失败",
              unknown: "状态未记录",
            }[turn.status]
          }
        </span>
        {model && <span style={{ color: modelColor, fontWeight: 600, fontSize: 12 }}>{model}</span>}
        {turn.status === "ongoing" && <OngoingDots count={3} />}
        {(contextLeftPercent !== null || metaParts.length > 0) && (
          <div className="message-detail__meta">
            {contextLeftPercent !== null && contextUsedPercent !== null && (
              <div className="message-detail__context info-bar__context" title={contextTitle}>
                <span>ctx {contextLeftPercent}% left</span>
                <div className="info-bar__context-bar">
                  <div
                    className="info-bar__context-fill"
                    style={{
                      width: `${contextUsedPercent}%`,
                      backgroundColor: getContextColor(contextUsedPercent),
                    }}
                  />
                </div>
              </div>
            )}
            {metaParts.length > 0 && (
              <span className="message-detail__meta-text">{metaParts.join(" · ")}</span>
            )}
          </div>
        )}
      </div>

      <div className="turn-detail__body">
        <div className="turn-detail__content">
          {turn.error && (
            <div className="turn-detail__section turn-detail__section--error">
              <div className="turn-detail__section-label">Error</div>
              <pre className="turn-detail__error">{turn.error}</pre>
            </div>
          )}

          {turn.warnings && turn.warnings.length > 0 && (
            <div className="turn-detail__section turn-detail__section--warning">
              <div className="turn-detail__section-label">Warnings</div>
              {turn.warnings.map((warning) => (
                <pre key={warning} className="turn-detail__warning">
                  {warning}
                </pre>
              ))}
            </div>
          )}

          {timeline.length > 0 && (
            <div className="turn-detail__section turn-detail__section--activity">
              {timeline.map((item, i) => {
                const id =
                  (item.kind === "msg" ? item.msg.record_id : item.tool.record_id) || String(i);
                return (
                  <article key={id} data-record-id={id}>
                    {item.kind === "msg" ? (
                      <ComplementaryItem msg={item.msg} />
                    ) : (
                      <ToolCallItem
                        tool={item.tool}
                        expanded={expanded.has(item.index)}
                        onToggle={() => onToggle(item.index)}
                        isWorkerOpen={item.tool.call_id === openWorkerCallId}
                        onOpenWorker={onOpenWorkerPanel}
                      />
                    )}
                    {renderRecord?.(id)}
                  </article>
                );
              })}
            </div>
          )}

          {turn.has_compaction && (
            <div className="turn-detail__compaction-note">Context was compacted in this turn.</div>
          )}
        </div>
      </div>
    </div>
  );
}
