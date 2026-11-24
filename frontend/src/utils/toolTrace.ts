import type { SubagentFileDiff, SubagentRoundNote, SubagentToolRecord } from '../types/pywebview.d';
import type { FileChangeEntry } from './turnGroups';

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
  | 'computer'
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
  tool_call_id?: string;
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
  args_preview?: string;
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
    | 'command-group'
    | 'command'
    | 'mutation'
    | 'skill'
    | 'computer'
    | 'subagent'
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

export interface BuildToolTraceOptions {
  groupCommands?: boolean;
}

export const SUBAGENT_TOOL_NAME = 'run_subagent';

const LEGACY_CATEGORIES: Readonly<Record<string, ToolCategory>> = {
  read_file: 'read',
  get_outline: 'read',
  search_code: 'search',
  glob_files: 'search',
  find_definition: 'search',
  find_references: 'search',
  execute_command: 'command',
  write_file: 'mutation',
  edit_file: 'mutation',
  multi_edit: 'mutation',
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

function kindFor(category: ToolCategory, name = ''): ToolTraceRow['kind'] {
  if (name === SUBAGENT_TOOL_NAME) return 'subagent';
  if (category === 'read') return 'read-group';
  if (category === 'search') return 'search-group';
  if (category === 'command') return 'command';
  if (category === 'mutation') return 'mutation';
  if (category === 'skill') return 'skill';
  if (category === 'computer') return 'computer';
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
  const kind = kindFor(category, call.name);
  const durationMs = durationFor(call);
  return {
    id: `${kind}:${call.id || `missing-${index + 1}`}`,
    kind,
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

const SUBCOMMAND_CLIS = new Set([
  'git', 'npm', 'pnpm', 'yarn', 'cargo', 'docker', 'kubectl', 'pip', 'uv', 'mvn', 'gradle',
]);

function tokenizeSimpleShell(cmd: string): string[] {
  const matches = cmd.match(/"[^"]*"|'[^']*'|\S+/g);
  return matches ? matches.map((t) => t.replace(/^["']|["']$/g, '')) : [];
}

export function commandFamily(raw: string): string {
  const cleaned = raw
    .trim()
    .replace(/^(?:cd(?:\s+\/d)?|Set-Location)\s+(?:"[^"]+"|'[^']+'|[^\s;&]+)\s*(?:;|&&)\s*/i, '')
    .trim();
  if (!cleaned) return '';
  const tokens = tokenizeSimpleShell(cleaned);
  if (tokens.length === 0) return '';
  const base = (tokens[0]!.replace(/\\/g, '/').split('/').pop() || '')
    .replace(/\.(?:exe|cmd|bat|ps1)$/i, '')
    .toLowerCase();
  if (!base) return '';
  if (SUBCOMMAND_CLIS.has(base)) {
    for (let i = 1; i < tokens.length; i += 1) {
      const tok = tokens[i]!;
      if (tok === '-C' || tok === '-c' || tok === '--git-dir' || tok === '--work-tree' || tok === '--prefix') {
        i += 1;
        continue;
      }
      if (tok.startsWith('-')) continue;
      return `${base} ${tok}`;
    }
  }
  return base;
}

function canJoinGroup(
  row: ToolTraceRow,
  next: ToolTraceRow,
  options?: BuildToolTraceOptions,
): boolean {
  const groupedKind = row.kind === 'read-group' || row.kind === 'search-group';
  if (groupedKind && row.kind === next.kind && row.status === next.status) {
    return row.status === 'completed' || row.status === 'running';
  }
  if (
    options?.groupCommands &&
    (row.kind === 'command' || row.kind === 'command-group') &&
    next.kind === 'command' &&
    row.status === 'completed' &&
    next.status === 'completed'
  ) {
    const famA = commandFamily(row.command || row.detail);
    const famB = commandFamily(next.command || next.detail);
    return Boolean(famA) && famA === famB;
  }
  return false;
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
  const isCmd = group.kind === 'command' || group.kind === 'command-group';
  return {
    ...group,
    ...(isCmd ? {
      kind: 'command-group' as const,
      detail: commandFamily(group.command || group.detail) || group.detail,
    } : {}),
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
  options?: BuildToolTraceOptions,
): ToolTraceRow[] {
  const rows: ToolTraceRow[] = [];
  const rowIdCounts = new Map<string, number>();
  for (const [index, call] of calls.entries()) {
    const next = rowFor(call, index, state);
    const previous = rows.at(-1);
    if (previous && canJoinGroup(previous, next, options)) {
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
  name: string;
  at: number;
  status?: SubagentToolStatus;
  durationText?: string;
  errorText?: string;
}

export interface SubagentFlatView {
  rows: SubagentFlatRow[];
  rounds: number;
  toolOk: number;
  toolErr: number;
}

const SUBAGENT_TOOL_FINISH_RE = /^(.*?)\s*([✓✗])\s*([\s\S]*)$/;

export function buildSubagentFlatView(stages: SubagentStage[]): SubagentFlatView {
  const rows: SubagentFlatRow[] = [];
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
    const toolName = finish[1].trim();
    const ok = finish[2] === '✓';
    const tail = finish[3].trim();
    let row = toolName ? pending.get(toolName) : undefined;
    if (!row) {
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

export interface SubagentMilestoneItem {
  kind: 'milestone';
  name: string;
  at: number;
  detail: string;
}

export interface SubagentRoundGroup {
  round: number;
  startedAt: number;
  tools: SubagentToolRecord[];
  milestones: SubagentMilestoneItem[];
}

export interface SubagentTimelineModel {
  hasStructuredTools: boolean;
  groups: SubagentRoundGroup[];
  rounds: number;
  toolOk: number;
  toolErr: number;
}

const MILESTONE_STAGES = new Set([
  'waiting_approval',
  'budget_notice',
  'summarizing',
  'finalizing',
]);

/**
 * 清洗子代理长指令用于顶部选择器与总览卡片的短标题：
 * 剥离「工作目录 … 分支 …」「目标：」「任务：」等固定引导前缀，突出真实动作对象。
 */
export function cleanSubagentTitle(description: string): string {
  const raw = String(description || '').replace(/\s+/g, ' ').trim();
  if (!raw) return '';
  let text = raw;
  const targetIdx = text.search(/(?:^|[。\n\s])(?:目标|任务)\s*[:：]\s*/);
  if (targetIdx >= 0 && targetIdx < 120) {
    const after = text.slice(targetIdx).replace(/^[。\n\s]*(?:目标|任务)\s*[:：]\s*/, '').trim();
    if (after.length >= 6) text = after;
  } else {
    text = text
      .replace(/^工作目录\s+[^\s，。,]+(?:\s*[，,]\s*分支\s+[^\s，。,]+)?(?:\s*[（(][^）)]*[）)])?[。，,\s]*/i, '')
      .replace(/^(?:目标|任务)\s*[:：]\s*/i, '')
      .trim();
  }
  return text || raw;
}

export function buildSubagentRoundGroups(params: {
  toolRecords?: SubagentToolRecord[];
  stages?: SubagentStage[];
  rounds?: number;
  toolOk?: number;
  toolErr?: number;
}): SubagentTimelineModel {
  const records = Array.isArray(params.toolRecords) ? params.toolRecords : [];
  const stages = Array.isArray(params.stages) ? params.stages : [];
  const flat = buildSubagentFlatView(stages);

  if (records.length === 0) {
    return {
      hasStructuredTools: false,
      groups: [],
      rounds: Math.max(params.rounds ?? 0, flat.rounds),
      toolOk: Math.max(params.toolOk ?? 0, flat.toolOk),
      toolErr: Math.max(params.toolErr ?? 0, flat.toolErr),
    };
  }

  const groupMap = new Map<number, SubagentRoundGroup>();
  let okCount = 0;
  let errCount = 0;

  for (const rec of records) {
    const roundNum = Math.max(1, Number(rec.round) || 1);
    let grp = groupMap.get(roundNum);
    if (!grp) {
      grp = {
        round: roundNum,
        startedAt: Number(rec.started_at) || 0,
        tools: [],
        milestones: [],
      };
      groupMap.set(roundNum, grp);
    } else if (rec.started_at && (!grp.startedAt || rec.started_at < grp.startedAt)) {
      grp.startedAt = rec.started_at;
    }
    grp.tools.push(rec);
    if (rec.status === 'ok') okCount += 1;
    else if (rec.status === 'err') errCount += 1;
  }

  const groups = Array.from(groupMap.values()).sort((a, b) => a.round - b.round);
  for (const stage of stages) {
    if (!MILESTONE_STAGES.has(stage.name)) continue;
    let targetGroup = groups[0]!;
    for (const grp of groups) {
      if (grp.startedAt <= 0 || stage.at >= grp.startedAt) {
        targetGroup = grp;
      } else {
        break;
      }
    }
    targetGroup.milestones.push({
      kind: 'milestone',
      name: stage.name,
      at: stage.at,
      detail: stage.detail || '',
    });
  }

  return {
    hasStructuredTools: true,
    groups,
    rounds: Math.max(params.rounds ?? 0, groups.length),
    toolOk: Math.max(params.toolOk ?? 0, okCount),
    toolErr: Math.max(params.toolErr ?? 0, errCount),
  };
}

export interface SubagentProcessStep {
  id: string;
  round: number;
  thinking: string;
  thinkingSummary: string;
  thinkingLive: boolean;
  text: string;
  calls: ToolExecutionCall[];
  milestones: SubagentMilestoneItem[];
}

export interface SubagentProcessModel {
  hasStructuredProcess: boolean;
  steps: SubagentProcessStep[];
  rounds: number;
  toolOk: number;
  toolErr: number;
}

const VALID_TOOL_CATEGORIES = new Set<ToolCategory>([
  'read', 'search', 'command', 'mutation', 'skill', 'computer', 'other',
]);

function normalizeToolCategory(category?: string, name?: string): ToolCategory {
  if (category && VALID_TOOL_CATEGORIES.has(category as ToolCategory)) {
    return category as ToolCategory;
  }
  if (name && LEGACY_CATEGORIES[name]) {
    return LEGACY_CATEGORIES[name]!;
  }
  return 'other';
}

export function subagentToolToExecutionCall(rec: SubagentToolRecord): ToolExecutionCall {
  const category = normalizeToolCategory(rec.category, rec.name);
  const display: ToolDisplay = {
    category,
    action: rec.action || rec.name,
    target: rec.target || '',
    command: category === 'command' ? (rec.target || '') : '',
    metrics: {},
    error_summary: rec.error_summary || '',
    is_skill: rec.category === 'skill',
  };
  const isFinished = rec.status !== 'running';
  const ok = rec.status === 'ok';
  return {
    id: rec.id || `tool-${rec.seq}`,
    name: rec.name,
    label: rec.action || rec.name,
    ...(rec.started_at ? { started_at_ms: rec.started_at * 1000 } : {}),
    ...(rec.args_preview ? { args_preview: rec.args_preview } : {}),
    display,
    ...(rec.diff ? { diff: rec.diff } : {}),
    ...(isFinished ? {
      result: {
        ok,
        summary: rec.error_summary || (ok ? '完成' : rec.status === 'cancelled' ? '已取消' : '失败'),
        result: rec.result_preview || rec.error_summary || '',
        ...(rec.duration_ms !== null && rec.duration_ms !== undefined ? { duration_ms: rec.duration_ms } : {}),
        display,
      },
    } : {}),
  };
}

function cleanThinkLine(line: string, fromTail: boolean): string {
  const plain = line
    .replace(/^\s*```[A-Za-z0-9_+-]*/, '')
    .replace(/^[\s>#*+\-–—\d.、:：)）]+/, '')
    .replace(/\*\*|__|`/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!plain) return '';
  if (plain.length <= 90) return plain;
  return fromTail ? `…${plain.slice(-90)}` : `${plain.slice(0, 90)}…`;
}

function thinkingHeadline(raw: string, live: boolean): string {
  if (!raw.trim()) return '';
  const lines = raw.split('\n');
  if (live) {
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const cleaned = cleanThinkLine(lines[i]!, true);
      if (cleaned) return cleaned;
    }
    return '';
  }
  for (let i = 0; i < lines.length; i += 1) {
    const cleaned = cleanThinkLine(lines[i]!, false);
    if (cleaned) return cleaned;
  }
  return '';
}

export function buildSubagentProcessSteps(params: {
  toolRecords?: SubagentToolRecord[];
  roundNotes?: SubagentRoundNote[];
  stages?: SubagentStage[];
  status?: string;
  stage?: string;
  rounds?: number;
  toolOk?: number;
  toolErr?: number;
  filterFailed?: boolean;
}): SubagentProcessModel {
  const records = Array.isArray(params.toolRecords) ? params.toolRecords : [];
  const notes = Array.isArray(params.roundNotes) ? params.roundNotes : [];
  const stages = Array.isArray(params.stages) ? params.stages : [];
  const flat = buildSubagentFlatView(stages);

  if (records.length === 0 && notes.length === 0) {
    return {
      hasStructuredProcess: false,
      steps: [],
      rounds: Math.max(params.rounds ?? 0, flat.rounds),
      toolOk: Math.max(params.toolOk ?? 0, flat.toolOk),
      toolErr: Math.max(params.toolErr ?? 0, flat.toolErr),
    };
  }

  let okCount = 0;
  let errCount = 0;
  interface RoundBucket {
    round: number;
    startedAt: number;
    thinking: string;
    text: string;
    calls: ToolExecutionCall[];
    milestones: SubagentMilestoneItem[];
  }
  const bucketMap = new Map<number, RoundBucket>();
  const ensureBucket = (roundNum: number, startedAt = 0): RoundBucket => {
    const key = Math.max(1, Number(roundNum) || 1);
    let b = bucketMap.get(key);
    if (!b) {
      b = { round: key, startedAt, thinking: '', text: '', calls: [], milestones: [] };
      bucketMap.set(key, b);
    } else if (startedAt && (!b.startedAt || startedAt < b.startedAt)) {
      b.startedAt = startedAt;
    }
    return b;
  };

  for (const note of notes) {
    const b = ensureBucket(note.round, Number(note.started_at) || 0);
    if (note.thinking && note.thinking.trim()) b.thinking = note.thinking.trim();
    if (note.text && note.text.trim()) b.text = note.text.trim();
  }

  for (const rec of records) {
    if (rec.status === 'ok') okCount += 1;
    else if (rec.status === 'err') errCount += 1;
    if (params.filterFailed && rec.status !== 'err') continue;
    const b = ensureBucket(rec.round, Number(rec.started_at) || 0);
    b.calls.push(subagentToolToExecutionCall(rec));
  }

  const buckets = Array.from(bucketMap.values()).sort((a, b) => a.round - b.round);
  if (!params.filterFailed && buckets.length > 0) {
    for (const stage of stages) {
      if (!MILESTONE_STAGES.has(stage.name)) continue;
      let target = buckets[0]!;
      for (const b of buckets) {
        if (b.startedAt <= 0 || stage.at >= b.startedAt) target = b;
        else break;
      }
      target.milestones.push({
        kind: 'milestone',
        name: stage.name,
        at: stage.at,
        detail: stage.detail || '',
      });
    }
  }

  const terminal = params.status === 'completed' || params.status === 'failed'
    || params.status === 'timed_out' || params.status === 'cancelled';
  const maxRound = buckets.length > 0 ? buckets[buckets.length - 1]!.round : 0;

  const steps: SubagentProcessStep[] = [];
  for (const b of buckets) {
    if (params.filterFailed && b.calls.length === 0) continue;
    const isThinkingLive = !terminal && params.stage === 'thinking' && b.round === maxRound && b.calls.length === 0;
    const prev = steps.at(-1);
    const isPureToolContinuation =
      !params.filterFailed &&
      !b.thinking &&
      !b.text &&
      b.milestones.length === 0 &&
      b.calls.length > 0 &&
      prev !== undefined &&
      prev.milestones.length === 0 &&
      prev.calls.length > 0;

    if (isPureToolContinuation && prev) {
      prev.calls.push(...b.calls);
      continue;
    }

    steps.push({
      id: `step-${b.round}`,
      round: b.round,
      thinking: params.filterFailed ? '' : b.thinking,
      thinkingSummary: params.filterFailed ? '' : thinkingHeadline(b.thinking, isThinkingLive),
      thinkingLive: isThinkingLive,
      text: params.filterFailed ? '' : b.text,
      calls: b.calls,
      milestones: b.milestones,
    });
  }

  return {
    hasStructuredProcess: true,
    steps,
    rounds: Math.max(params.rounds ?? 0, buckets.length),
    toolOk: Math.max(params.toolOk ?? 0, okCount),
    toolErr: Math.max(params.toolErr ?? 0, errCount),
  };
}

export function buildSubagentFileChanges(params: {
  fileDiffs?: SubagentFileDiff[];
  toolRecords?: SubagentToolRecord[];
  changedFiles?: string[];
}): FileChangeEntry[] {
  const byPath = new Map<string, FileChangeEntry>();
  const baseName = (p: string): string => {
    const norm = p.replace(/\\/g, '/');
    const i = norm.lastIndexOf('/');
    return i >= 0 ? norm.slice(i + 1) : norm;
  };
  const push = (diff: SubagentFileDiff) => {
    const p = String(diff.file_path || '').trim();
    if (!p) return;
    const diffText = String(diff.diff_text || '');
    const counts = diffCounts({ file_path: p, diff_text: diffText, added: diff.added, removed: diff.removed });
    const existing = byPath.get(p);
    if (existing) {
      if (diffText && !existing.diffText) existing.diffText = diffText;
      if (existing.added === 0 && existing.removed === 0) {
        existing.added = counts.added;
        existing.removed = counts.removed;
      }
    } else {
      byPath.set(p, {
        name: baseName(p),
        path: p,
        added: counts.added,
        removed: counts.removed,
        diffText,
      });
    }
  };

  for (const d of params.fileDiffs ?? []) push(d);
  for (const rec of params.toolRecords ?? []) {
    if (rec.diff) push(rec.diff);
  }
  for (const fp of params.changedFiles ?? []) {
    const p = String(fp || '').trim();
    if (p && !byPath.has(p)) {
      byPath.set(p, { name: baseName(p), path: p, added: 0, removed: 0, diffText: '' });
    }
  }
  return [...byPath.values()];
}
