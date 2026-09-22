/**
 * 工具执行行的分类/分组/状态归约（对齐桌面 frontend/src/utils/toolTrace.ts 精简版）。
 * 输入 ToolCallUi[]（含 display），输出可直接渲染的行。
 */
import type { ToolCallUi, ToolDisplay } from '../stores/app';

export type ToolRowKind = 'read-group' | 'search-group' | 'command' | 'mutation' | 'skill' | 'tool';
export type ToolRowStatus = 'running' | 'completed' | 'failed' | 'cancelled';

export interface ToolTraceRow {
  id: string;
  kind: ToolRowKind;
  status: ToolRowStatus;
  action: string;
  detail: string;
  command: string;
  errorSummary: string;
  count: number;
  durationMs?: number;
  metrics: Record<string, number>;
  calls: ToolCallUi[];
}

const LEGACY_CATEGORIES: Readonly<Record<string, string>> = {
  read_file: 'read',
  search_code: 'search',
  glob_files: 'search',
  execute_command: 'command',
  write_file: 'mutation',
  edit_file: 'mutation',
};

function displayFor(call: ToolCallUi): ToolDisplay | undefined {
  return call.result?.display ?? call.display;
}

function categoryFor(call: ToolCallUi): string {
  const display = displayFor(call);
  if (display?.is_skill) return 'skill';
  return display?.category ?? LEGACY_CATEGORIES[call.name] ?? 'other';
}

function kindFor(category: string): ToolRowKind {
  if (category === 'read') return 'read-group';
  if (category === 'search') return 'search-group';
  if (category === 'command') return 'command';
  if (category === 'mutation') return 'mutation';
  if (category === 'skill') return 'skill';
  return 'tool';
}

function statusFor(call: ToolCallUi, state: string): ToolRowStatus {
  if (call.result) return call.result.ok ? 'completed' : 'failed';
  if (state === 'cancelled') return 'cancelled';
  if (state === 'error') return 'failed';
  return state === 'completed' ? 'completed' : 'running';
}

function rowFor(call: ToolCallUi, index: number, state: string): ToolTraceRow {
  const display = displayFor(call);
  const category = categoryFor(call);
  const durationMs = call.result?.duration_ms;
  return {
    id: `${kindFor(category)}:${call.id || `missing-${index + 1}`}`,
    kind: kindFor(category),
    status: statusFor(call, state),
    action: display?.action || call.label || call.name,
    detail: display?.target || '',
    command: display?.command || '',
    errorSummary: display?.error_summary || '',
    count: 1,
    ...(durationMs === undefined || durationMs === null ? {} : { durationMs }),
    metrics: { ...(display?.metrics ?? {}) },
    calls: [call],
  };
}

function canJoinGroup(row: ToolTraceRow, next: ToolTraceRow): boolean {
  const grouped = row.kind === 'read-group' || row.kind === 'search-group';
  if (!grouped || row.kind !== next.kind || row.status !== next.status) return false;
  return row.status === 'completed' || row.status === 'running';
}

function mergeMetrics(a: Record<string, number>, b: Record<string, number>): Record<string, number> {
  const merged = { ...a };
  for (const [k, v] of Object.entries(b)) merged[k] = (merged[k] ?? 0) + v;
  return merged;
}

function appendToGroup(group: ToolTraceRow, row: ToolTraceRow): ToolTraceRow {
  const durationMs = group.durationMs === undefined || row.durationMs === undefined
    ? undefined
    : group.durationMs + row.durationMs;
  return {
    ...group,
    count: group.count + row.count,
    ...(durationMs === undefined ? { durationMs: undefined } : { durationMs }),
    metrics: mergeMetrics(group.metrics, row.metrics),
    calls: [...group.calls, ...row.calls],
  };
}

export function buildToolTraceRows(calls: ToolCallUi[], state = 'running'): ToolTraceRow[] {
  const rows: ToolTraceRow[] = [];
  const idCounts = new Map<string, number>();
  for (const [index, call] of calls.entries()) {
    const next = rowFor(call, index, state);
    const prev = rows[rows.length - 1];
    if (prev && canJoinGroup(prev, next)) {
      rows[rows.length - 1] = appendToGroup(prev, next);
    } else {
      const occurrence = (idCounts.get(next.id) ?? 0) + 1;
      idCounts.set(next.id, occurrence);
      if (occurrence > 1) next.id = `${next.id}#${occurrence}`;
      rows.push(next);
    }
  }
  return rows;
}

export function rowStatusLabel(row: ToolTraceRow): string {
  if (row.status === 'running') return '执行中…';
  if (row.status === 'failed') return '失败';
  if (row.status === 'cancelled') return '已取消';
  return '完成';
}
