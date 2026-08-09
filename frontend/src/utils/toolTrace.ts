export type ExecutionState = 'running' | 'completed' | 'cancelled' | 'error';
export type ExecutionLifecycleEvent =
  | 'turn_start'
  | 'turn_finish'
  | 'turn_idle'
  | 'turn_cancelled'
  | 'turn_error';
export type ToolCallStatus = 'running' | 'completed' | 'failed' | 'cancelled';

export type ToolCategory =
  | 'read'
  | 'search'
  | 'command'
  | 'mutation'
  | 'skill'
  | 'other';

export interface ToolDisplay {
  category: ToolCategory;
  action: string;
  target: string;
  command: string;
  metrics: Record<string, number>;
  error_summary: string;
  is_skill: boolean;
}

export interface ToolExecutionResult {
  ok: boolean;
  summary: string;
  result: string;
  duration_ms?: number;
  display?: ToolDisplay;
}

export interface SubagentTaskSnapshot {
  id: string;
  description: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
}

export interface LegacyDiff {
  file_path: string;
  diff_text: string;
}

export interface ToolExecutionCall {
  id: string;
  name: string;
  label: string;
  started_at_ms?: number;
  display?: ToolDisplay;
  result?: ToolExecutionResult;
  diff?: LegacyDiff;
  subagent_tasks?: SubagentTaskSnapshot[];
}

export interface ToolTraceRow {
  id: string;
  kind:
    | 'read-group'
    | 'search-group'
    | 'command'
    | 'mutation'
    | 'skill'
    | 'tool'
    | 'legacy-diffs';
  status: ToolCallStatus;
  action: string;
  detail: string;
  command: string;
  errorSummary: string;
  count: number;
  durationMs?: number;
  metrics: Record<string, number>;
  calls: ToolExecutionCall[];
  legacyDiffs?: LegacyDiff[];
}

const LEGACY_CATEGORIES: Readonly<Record<string, ToolCategory>> = {
  read_file: 'read',
  search_code: 'search',
  glob_files: 'search',
  execute_command: 'command',
  write_file: 'mutation',
  edit_file: 'mutation',
};

function displayFor(call: ToolExecutionCall): ToolDisplay | undefined {
  return call.result?.display ?? call.display;
}

function categoryFor(call: ToolExecutionCall): ToolCategory {
  const display = displayFor(call);
  if (display?.is_skill) return 'skill';
  return display?.category ?? LEGACY_CATEGORIES[call.name] ?? 'other';
}

function statusFor(call: ToolExecutionCall, state: ExecutionState): ToolCallStatus {
  if (call.result) return call.result.ok ? 'completed' : 'failed';
  if (state === 'cancelled') return 'cancelled';
  if (state === 'error') return 'failed';
  return state === 'completed' ? 'completed' : 'running';
}

function kindFor(category: ToolCategory): ToolTraceRow['kind'] {
  if (category === 'read') return 'read-group';
  if (category === 'search') return 'search-group';
  if (category === 'command') return 'command';
  if (category === 'mutation') return 'mutation';
  if (category === 'skill') return 'skill';
  return 'tool';
}

function metricsFor(call: ToolExecutionCall): Record<string, number> {
  return { ...(displayFor(call)?.metrics ?? {}) };
}

function durationFor(call: ToolExecutionCall): number | undefined {
  return call.result?.duration_ms;
}

function rowFor(call: ToolExecutionCall, index: number, state: ExecutionState): ToolTraceRow {
  const display = displayFor(call);
  const category = categoryFor(call);
  const durationMs = durationFor(call);
  return {
    id: `${kindFor(category)}:${call.id || `missing-${index + 1}`}`,
    kind: kindFor(category),
    status: statusFor(call, state),
    action: display?.action || call.label || call.name,
    detail: display?.target || '',
    command: display?.command || '',
    errorSummary: display?.error_summary || '',
    count: 1,
    ...(durationMs === undefined ? {} : { durationMs }),
    metrics: metricsFor(call),
    calls: [call],
  };
}

function canJoinGroup(row: ToolTraceRow, next: ToolTraceRow): boolean {
  const groupedKind = row.kind === 'read-group' || row.kind === 'search-group';
  if (!groupedKind || row.kind !== next.kind || row.status !== next.status) return false;
  return row.status === 'completed' || row.status === 'running';
}

function mergeMetrics(
  current: Record<string, number>,
  next: Record<string, number>,
): Record<string, number> {
  const merged = { ...current };
  for (const [name, value] of Object.entries(next)) {
    merged[name] = (merged[name] ?? 0) + value;
  }
  return merged;
}

function appendToGroup(group: ToolTraceRow, row: ToolTraceRow): ToolTraceRow {
  const durationMs =
    group.durationMs === undefined || row.durationMs === undefined
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

function legacyDiffRow(legacyDiffs: LegacyDiff[]): ToolTraceRow {
  return {
    id: 'legacy-diffs',
    kind: 'legacy-diffs',
    status: 'completed',
    action: '未绑定的历史文件变更',
    detail: legacyDiffs.map((diff) => diff.file_path).filter(Boolean).join(', '),
    command: '',
    errorSummary: '',
    count: legacyDiffs.length,
    metrics: legacyDiffs.reduce(
      (totals, diff) => {
        const changes = countDiffChanges(diff.diff_text);
        totals.added += changes.added;
        totals.removed += changes.removed;
        return totals;
      },
      { added: 0, removed: 0 },
    ),
    calls: [],
    legacyDiffs: legacyDiffs.map((diff) => ({ ...diff })),
  };
}

export function buildToolTraceRows(
  calls: ToolExecutionCall[],
  legacyDiffs: LegacyDiff[] = [],
  state: ExecutionState = 'running',
): ToolTraceRow[] {
  const rows: ToolTraceRow[] = [];
  const rowIdCounts = new Map<string, number>();
  for (const [index, call] of calls.entries()) {
    const next = rowFor(call, index, state);
    const previous = rows.at(-1);
    if (previous && canJoinGroup(previous, next)) {
      rows[rows.length - 1] = appendToGroup(previous, next);
    } else {
      const occurrence = (rowIdCounts.get(next.id) ?? 0) + 1;
      rowIdCounts.set(next.id, occurrence);
      if (occurrence > 1) next.id = `${next.id}#${occurrence}`;
      rows.push(next);
    }
  }
  if (legacyDiffs.length > 0) rows.push(legacyDiffRow(legacyDiffs));
  return rows;
}

export function countDiffChanges(diffText: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diffText.split(/\r?\n/)) {
    if (/^(?:\+\+\+|---)(?:[\t ]|$)/.test(line)) continue;
    if (line.startsWith('+')) added += 1;
    if (line.startsWith('-')) removed += 1;
  }
  return { added, removed };
}

export function reduceExecutionState(
  current: ExecutionState | undefined,
  event: ExecutionLifecycleEvent,
): ExecutionState {
  if (event === 'turn_start') return 'running';
  if (current === 'error' || current === 'cancelled') return current;
  if (event === 'turn_error') return 'error';
  if (event === 'turn_cancelled') return 'cancelled';
  return 'completed';
}
