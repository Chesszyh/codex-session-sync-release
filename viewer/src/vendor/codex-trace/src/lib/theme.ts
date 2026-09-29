// Theme constants — dark theme colors for codex-trace.

export const colors = {
  // Background
  bg: "#1a1a2e",
  bgSurface: "#16213e",
  bgElevated: "#222244",
  bgHover: "#2a2a4a",

  // Text hierarchy
  textPrimary: "#d0d0d0",
  textSecondary: "#8a8a8a",
  textDim: "#767676",
  textMuted: "#585858",

  // Accents
  accent: "#5fafff",
  error: "#ff0000",
  info: "#5f87ff",

  // Surfaces
  border: "#5f5f87",

  // Model family (GPT variants)
  modelGpt4: "#5fafff",
  modelGpt5: "#87d787",
  modelO: "#ff8700",

  // Token highlight
  tokenHigh: "#ff8700",

  // Ongoing indicator
  ongoing: "#5faf00",

  // Context usage thresholds
  contextOk: "#87d787",
  contextWarn: "#ff8700",
  contextCrit: "#ff0000",

  // Tool category colors
  toolExec: "#767676",
  toolPatch: "#5faf5f",
  toolMcp: "#af87ff",
  toolWeb: "#5f87ff",
  toolImage: "#af5fff",

  // Collab
  collab: "#5f87d7",
} as const;

export function getModelColor(model: string): string {
  const m = model.toLowerCase();
  if (m.startsWith("o")) return colors.modelO;
  if (m.includes("gpt-5") || m.includes("gpt5")) return colors.modelGpt5;
  return colors.modelGpt4;
}

export function getContextColor(pct: number): string {
  if (pct < 50) return colors.contextOk;
  if (pct < 80) return colors.contextWarn;
  return colors.contextCrit;
}

// Spinner frames (braille)
export const spinnerFrames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
