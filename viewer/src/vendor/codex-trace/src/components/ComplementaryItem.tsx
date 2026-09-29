import type { AgentMessage } from "../../shared/types";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { OutputIcon } from "./Icons";
import { formatExactTime } from "../lib/format";

interface ComplementaryItemProps {
  msg: AgentMessage;
}

export function ComplementaryItem({ msg }: ComplementaryItemProps) {
  return (
    <div className="complementary-item">
      <div className="complementary-item__header">
        <span className="complementary-item__icon">
          <OutputIcon />
        </span>
        <span className="complementary-item__name">{msg.label || "Codex"}</span>
        {msg.timestamp && (
          <span className="complementary-item__time">{formatExactTime(msg.timestamp)}</span>
        )}
      </div>
      <div className="complementary-item__body">
        <div className="turn-detail__markdown">
          <MarkdownRenderer content={msg.text} />
        </div>
      </div>
    </div>
  );
}
