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

export interface SubagentStage {
  name: string;
  at: number;
  detail: string;
}

export interface SubagentTaskSnapshot {
  id: string;
  description: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
  type?: string;
  background?: boolean;
  stages?: SubagentStage[];
}

export interface LegacyDiff {
  file_path: string;
  diff_text: string;
  added?: number;
  removed?: number;
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
        const changes = diffCounts(diff);
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
  for (const rawLine of diffText.split(/\r?\n/)) {
    const line = rawLine.replace(/\x1b\[[0-9;]*m/g, '').replace(/│/g, '').trim();
    if (/^(?:\+\+\+|---)(?:[\t ]|$)/.test(line)) continue;
    const marker = line.match(/^[\d\s]*([+-])/);
    if (marker?.[1] === '+') added += 1;
    if (marker?.[1] === '-') removed += 1;
  }
  return { added, removed };
}

export function diffCounts(diff: LegacyDiff): { added: number; removed: number } {
  if (Number.isFinite(diff.added) && Number.isFinite(diff.removed)) {
    return { added: Number(diff.added), removed: Number(diff.removed) };
  }
  return countDiffChanges(diff.diff_text);
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

// ── 子代理执行过程扁平视图 ──────────────────────────────────────────────
// 右面板"子代理"工作台用：后端 stages 是事件流水账（tool_started/tool_finished
// 各记一条、模板阶段穿插），这里解析成保持时间顺序的展示行——工具开始/结束
// 配对合并为一行，模板阶段原位保留由组件弱化渲染。

export type SubagentToolStatus = 'running' | 'ok' | 'err';

export interface SubagentFlatRow {
  kind: 'stage' | 'tool';
  /** stage 阶段名，或工具名 */
  name: string;
  /** 行起始时刻（epoch 秒）；工具行取开始时刻 */
  at: number;
  status?: SubagentToolStatus;
  /** ✓ 后的耗时片段，如 "0.8s" / "420ms" */
  durationText?: string;
  /** ✗ 后的失败摘要 */
  errorText?: string;
}

export interface SubagentFlatView {
  rows: SubagentFlatRow[];
  /** model 阶段出现次数 ≈ 模型轮次 */
  rounds: number;
  toolOk: number;
  toolErr: number;
}

const SUBAGENT_TOOL_FINISH_RE = /^(.*?)\s*([✓✗])\s*([\s\S]*)$/;

export function buildSubagentFlatView(stages: SubagentStage[]): SubagentFlatView {
  const rows: SubagentFlatRow[] = [];
  // 同名工具的未完成开始行（并行同名调用时后写覆盖，配对按 LIFO）
  const pending = new Map<string, SubagentFlatRow>();
  let rounds = 0;
  let toolOk = 0;
  let toolErr = 0;

  const finishRow = (row: SubagentFlatRow, ok: boolean, tail: string) => {
    row.status = ok ? 'ok' : 'err';
    if (ok) row.durationText = tail || undefined;
    else row.errorText = tail || undefined;
    if (ok) toolOk += 1;
    else toolErr += 1;
  };

  for (const stage of stages) {
    if (stage.name !== 'tool') {
      if (stage.name === 'model') rounds += 1;
      rows.push({ kind: 'stage', name: stage.name, at: stage.at });
      continue;
    }
    const detail = stage.detail ?? '';
    const finish = detail.match(SUBAGENT_TOOL_FINISH_RE);
    if (!finish || (!finish[1].trim() && !finish[3].trim())) {
      // tool_started：detail 就是工具名
      const row: SubagentFlatRow = {
        kind: 'tool',
        name: detail || 'tool',
        at: stage.at,
        status: 'running',
      };
      rows.push(row);
      if (detail) pending.set(detail, row);
      continue;
    }
    // tool_finished：detail 形如 "name ✓ 0.8s" / "name ✗ 失败摘要"
    const toolName = finish[1].trim();
    const ok = finish[2] === '✓';
    const tail = finish[3].trim();
    let row = toolName ? pending.get(toolName) : undefined;
    if (!row) {
      // 开始行可能已被后端 64 条上限淘汰：就近配对最后一个同名 running 行
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        const candidate = rows[i]!;
        if (
          candidate.kind === 'tool' &&
          candidate.status === 'running' &&
          (!toolName || candidate.name === toolName)
        ) {
          row = candidate;
          break;
        }
      }
    }
    if (row) {
      pending.delete(row.name);
      finishRow(row, ok, tail);
    } else {
      const orphan: SubagentFlatRow = { kind: 'tool', name: toolName || 'tool', at: stage.at };
      finishRow(orphan, ok, tail);
      rows.push(orphan);
    }
  }
  return { rows, rounds, toolOk, toolErr };
}
