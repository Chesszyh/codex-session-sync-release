import type { CodexSessionInfo } from "../../shared/types";
import { shortPath } from "../../shared/format";

export function sessionDisplayName(session: CodexSessionInfo): string {
  if (session.is_inline_worker || session.is_external_worker) {
    const shortId = session.thread_name || session.id;
    if (session.worker_nickname) return `${session.worker_nickname} (${shortId})`;
    if (session.worker_role) return `${session.worker_role} ${shortId}`;
    return `worker ${shortId}`;
  }

  if (session.thread_name) return session.thread_name;
  if (session.cwd) return shortPath(session.cwd);
  return session.thread_name || session.id;
}
