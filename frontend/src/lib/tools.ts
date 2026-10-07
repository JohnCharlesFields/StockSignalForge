/**
 * Single source of truth for tool name → i18n key.
 *
 * Values are i18n keys (looked up in `useI18n().t`), not final labels.
 * The map name "TOOL_I18N_KEY" reflects this: it maps a raw tool identifier
 * to the i18n bundle key whose value is the user-facing label.
 *
 * Consumers that need a final localized string should:
 *   1. Call `localizeToolName(tool)` to get the i18n key (or fallback).
 *   2. Resolve that key against the active i18n table.
 */
export const TOOL_I18N_KEY: Record<string, string> = {
  load_skill: "toolLoadSkill",
  write_file: "toolWriteFile",
  edit_file: "toolEditFile",
  read_file: "toolReadFile",
  run_backtest: "toolRunBacktest",
  bash: "toolBash",
  read_url: "toolReadUrl",
  read_document: "toolReadDocument",
  compact: "toolCompact",
  create_task: "toolCreateTask",
  update_task: "toolUpdateTask",
  spawn_subagent: "toolSpawnSubagent",
};

/**
 * Returns the i18n key (or fallback) for a tool name.
 *
 * - If `tool` is mapped in `TOOL_I18N_KEY`, returns the mapped key.
 * - Else if `fallback` is provided, returns `fallback`.
 * - Else returns `tool` unchanged.
 *
 * Centralizes the previously-duplicated lookup pattern.
 */
export function localizeToolName(tool: string, fallback?: string): string {
  if (tool in TOOL_I18N_KEY) {
    return TOOL_I18N_KEY[tool];
  }
  if (fallback !== undefined) {
    return fallback;
  }
  return tool;
}
