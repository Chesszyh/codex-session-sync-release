// Format utilities for codex-trace UI.
// Pure shared utilities are re-exported from shared/format.ts.

export {
  formatTokens,
  formatDuration,
  formatCost,
  truncate,
  shortPath,
  timeAgo,
} from "../../shared/format";

/**
 * Abbreviates a model name: "gpt-5.4" -> "gpt-5.4", "o4-mini" -> "o4-mini".
 * Strips common vendor prefixes for display.
 */
export function shortModel(m: string): string {
  return m.replace(/^openai\//, "").replace(/^anthropic\//, "");
}

/**
 * Returns the first non-empty line of text.
 */
export function firstLine(text: string): string {
  const idx = text.indexOf("\n");
  return idx === -1 ? text : text.slice(0, idx);
}

/**
 * Pretty-prints a JSON string. Returns the original string on parse failure.
 */
export function formatJson(input: string): string {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return input;
  }
}

/**
 * Formats a timestamp as yyyy-mm-dd hh:mm:ss.
 */
export function formatExactTime(ts: string): string {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}:${ss}`;
  } catch {
    return "";
  }
}
