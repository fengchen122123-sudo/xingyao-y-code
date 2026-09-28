import { describe, expect, it } from 'vitest';

import {
  buildSubagentFlatView,
  buildToolTraceRows,
  countDiffChanges,
  reduceExecutionState,
  type ToolDisplay,
  type ToolExecutionCall,
} from './toolTrace';


function readCall(id: string, path: string, durationMs: number, lines: number): ToolExecutionCall {
  const display: ToolDisplay = {
    category: 'read',
    action: '读取文件',
    target: path,
    command: '',
    metrics: { line_count: lines },
    error_summary: '',
    is_skill: false,
  };
  return {
    id,
    name: 'read_file',
    label: `读取文件 ${path}`,
    display,
    result: {
      ok: true,
      summary: '读取完成',
      result: '',
      duration_ms: durationMs,
      display,
    },
  };
}

function legacyReadCall(id: string, path: string): ToolExecutionCall {
  return {
    id,
    name: 'read_file',
    label: `读取文件 ${path}`,
    result: { ok: true, summary: '读取完成', result: '' },
  };
}

function commandCall(
  id: string,
  command: string,
  ok: boolean,
  durationMs: number,
): ToolExecutionCall {
  const display: ToolDisplay = {
    category: 'command',
    action: '运行命令',
    target: command,
    command,
    metrics: {},
    error_summary: ok ? '' : 'ModuleNotFoundError: pytest',
    is_skill: false,
  };
  return {
    id,
    name: 'execute_command',
    label: `运行命令 ${command}`,
    display,
    result: {
      ok,
      summary: ok ? '完成' : '失败',
      result: '',
      duration_ms: durationMs,
      display,
    },
  };
}

function displayedCall(
  id: string,
  name: string,
  category: ToolDisplay['category'],
  ok?: boolean,
): ToolExecutionCall {
  const display: ToolDisplay = {
    category,
    action: `action:${name}`,
    target: `target:${id}`,
    command: '',
    metrics: {},
    error_summary: ok === false ? `error:${id}` : '',
    is_skill: false,
  };
  return {
    id,
    name,
    label: `label:${id}`,
    display,
    ...(ok === undefined
      ? {}
      : { result: { ok, summary: `summary:${id}`, result: `raw:${id}`, display } }),
  };
}

describe('buildToolTraceRows', () => {
  it('groups adjacent successful reads', () => {
    const rows = buildToolTraceRows([
      readCall('1', 'a.py', 100, 10),
      readCall('2', 'b.py', 200, 20),
      readCall('3', 'c.py', 300, 30),
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: 'read-group', count: 3, durationMs: 600 });
    expect(rows[0].metrics.line_count).toBe(60);
  });

  it('flushes a read group before a failure and keeps later work', () => {
    const rows = buildToolTraceRows([
      readCall('1', 'a.py', 100, 10),
      commandCall('bad', 'pytest -q', false, 3200),
      commandCall('good', 'python -m pytest -q', true, 100),
    ]);

    expect(rows.map((row) => row.kind)).toEqual(['read-group', 'command', 'command']);
    expect(rows[1].status).toBe('failed');
    expect(rows[2].status).toBe('completed');
  });

  it('does not invent a legacy duration', () => {
    const rows = buildToolTraceRows([legacyReadCall('1', 'a.py')]);

    expect(rows[0].durationMs).toBeUndefined();
  });

  it('keeps legacy diffs in an explicit final row', () => {
    const legacyDiffs = [{ file_path: 'a.py', diff_text: '+new' }];
    const rows = buildToolTraceRows([], legacyDiffs);

    expect(rows[0].kind).toBe('legacy-diffs');
    expect(rows[0].legacyDiffs).toEqual(legacyDiffs);
    expect(rows[0].calls).toEqual([]);
  });

  it('keeps duplicate call IDs as explicit rows with unique row IDs', () => {
    const rows = buildToolTraceRows([
      commandCall('duplicate', 'first', true, 10),
      commandCall('duplicate', 'second', true, 20),
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.id)).toEqual([
      'command:duplicate',
      'command:duplicate#2',
    ]);
    expect(rows.map((row) => row.command)).toEqual(['first', 'second']);
  });

  it('groups adjacent searches but not a failed search', () => {
    const rows = buildToolTraceRows([
      displayedCall('search-1', 'search_code', 'search', true),
      displayedCall('search-2', 'glob_files', 'search', true),
      displayedCall('search-bad', 'search_code', 'search', false),
      displayedCall('search-3', 'search_code', 'search', true),
    ]);

    expect(rows.map((row) => [row.kind, row.status, row.count])).toEqual([
      ['search-group', 'completed', 2],
      ['search-group', 'failed', 1],
      ['search-group', 'completed', 1],
    ]);
  });

  it('groups adjacent homogeneous running reads without mixing completed calls', () => {
    const rows = buildToolTraceRows([
      displayedCall('running-1', 'read_file', 'read'),
      displayedCall('running-2', 'read_file', 'read'),
      displayedCall('completed', 'read_file', 'read', true),
    ]);

    expect(rows.map((row) => [row.status, row.count])).toEqual([
      ['running', 2],
      ['completed', 1],
    ]);
  });

  it('terminalizes unresolved calls with the enclosing turn', () => {
    const call = displayedCall('legacy-empty-id', 'list_directory', 'read');

    expect(buildToolTraceRows([call], [], 'running')[0].status).toBe('running');
    expect(buildToolTraceRows([call], [], 'completed')[0].status).toBe('completed');
    expect(buildToolTraceRows([call], [], 'cancelled')[0].status).toBe('cancelled');
    expect(buildToolTraceRows([call], [], 'error')[0].status).toBe('failed');
  });

  it('keeps mutations and subagents as independent rows', () => {
    const rows = buildToolTraceRows([
      displayedCall('edit-1', 'edit_file', 'mutation', true),
      displayedCall('edit-2', 'write_file', 'mutation', true),
      displayedCall('agent-1', 'run_subagent', 'other', true),
      displayedCall('agent-2', 'run_subagent', 'other', true),
    ]);

    expect(rows.map((row) => row.kind)).toEqual(['mutation', 'mutation', 'tool', 'tool']);
    expect(rows.every((row) => row.count === 1)).toBe(true);
  });

  it('preserves a diff only on its explicitly bound call', () => {
    const unbound = displayedCall('edit-1', 'edit_file', 'mutation', true);
    const bound = {
      ...displayedCall('edit-2', 'edit_file', 'mutation', true),
      diff: { file_path: 'same.py', diff_text: '+bound' },
    };

    const rows = buildToolTraceRows([unbound, bound]);

    expect(rows[0].calls[0].diff).toBeUndefined();
    expect(rows[1].calls[0].diff).toEqual({ file_path: 'same.py', diff_text: '+bound' });
  });

  it('never infers display metadata from raw result text', () => {
    const rows = buildToolTraceRows([{
      id: 'unknown',
      name: 'unknown_tool',
      label: '安全标签',
      result: {
        ok: false,
        summary: '失败',
        result: 'execute_command --secret=raw\n+++ file\n+content',
      },
    }]);

    expect(rows[0]).toMatchObject({
      kind: 'tool',
      status: 'failed',
      action: '安全标签',
      detail: '',
      command: '',
      errorSummary: '',
      metrics: {},
    });
  });

  it('uses finish display as the authoritative safe presentation', () => {
    const call = displayedCall('command-1', 'execute_command', 'command', true);
    call.display = {
      ...call.display!,
      action: '开始动作',
      target: '开始目标',
    };
    call.result!.display = {
      ...call.result!.display!,
      action: '完成动作',
      target: '完成目标',
      metrics: { line_count: 7 },
    };

    expect(buildToolTraceRows([call])[0]).toMatchObject({
      action: '完成动作',
      detail: '完成目标',
      metrics: { line_count: 7 },
    });
  });

  it('is deterministic across JSON serialization and preserves input order', () => {
    const calls = [
      displayedCall('later-id', 'execute_command', 'command', true),
      displayedCall('earlier-id', 'edit_file', 'mutation', true),
    ];
    const restored = JSON.parse(JSON.stringify(calls)) as ToolExecutionCall[];

    expect(buildToolTraceRows(restored)).toEqual(buildToolTraceRows(calls));
    expect(buildToolTraceRows(restored).map((row) => row.calls[0].id)).toEqual([
      'later-id',
      'earlier-id',
    ]);
  });

  it('marks a missing ID explicitly without dropping the call', () => {
    const rows = buildToolTraceRows([displayedCall('', 'run_subagent', 'other')]);

    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe('tool:missing-1');
    expect(rows[0].calls[0].id).toBe('');
  });
});

describe('countDiffChanges', () => {
  it('counts only standard unified diff changes', () => {
    expect(countDiffChanges('--- a.py\n+++ a.py\n-old\n+new\n context')).toEqual({
      added: 1,
      removed: 1,
    });
  });

  it('counts changed content that begins with diff-header characters', () => {
    expect(countDiffChanges('--- a.py\n+++ b.py\n++++literal\n----literal')).toEqual({
      added: 1,
      removed: 1,
    });
  });
});

describe('reduceExecutionState', () => {
  it('does not overwrite error or cancelled terminal states on finish events', () => {
    expect(reduceExecutionState('error', 'turn_finish')).toBe('error');
    expect(reduceExecutionState('error', 'turn_idle')).toBe('error');
    expect(reduceExecutionState('cancelled', 'turn_finish')).toBe('cancelled');
    expect(reduceExecutionState('cancelled', 'turn_idle')).toBe('cancelled');
  });

  it('starts a fresh serializable execution state for a new turn', () => {
    expect(reduceExecutionState('error', 'turn_start')).toBe('running');
    expect(reduceExecutionState(undefined, 'turn_start')).toBe('running');
    expect(reduceExecutionState('running', 'turn_error')).toBe('error');
    expect(reduceExecutionState('running', 'turn_cancelled')).toBe('cancelled');
    expect(reduceExecutionState('running', 'turn_finish')).toBe('completed');
  });
});

describe('buildSubagentFlatView', () => {
  const stage = (name: string, at: number, detail = '') => ({ name, at, detail });

  it('pairs tool start/finish into a single row anchored at the start time', () => {
    const view = buildSubagentFlatView([
      stage('model', 100),
      stage('tool', 110, 'read_file'),
      stage('tool', 111, 'read_file ✓ 0.4s'),
      stage('composing', 112),
    ]);
    expect(view.rows).toEqual([
      { kind: 'stage', name: 'model', at: 100 },
      { kind: 'tool', name: 'read_file', at: 110, status: 'ok', durationText: '0.4s' },
      { kind: 'stage', name: 'composing', at: 112 },
    ]);
    expect(view).toMatchObject({ rounds: 1, toolOk: 1, toolErr: 0 });
  });

  it('keeps an unfinished tool as running', () => {
    const view = buildSubagentFlatView([
      stage('tool', 100, 'bash'),
    ]);
    expect(view.rows[0]).toEqual({ kind: 'tool', name: 'bash', at: 100, status: 'running' });
    expect(view.toolOk).toBe(0);
  });

  it('captures the failure summary from a ✗ finish', () => {
    const view = buildSubagentFlatView([
      stage('tool', 100, 'bash'),
      stage('tool', 103, 'bash ✗ 2 errors no-unused-vars'),
    ]);
    expect(view.rows[0]).toMatchObject({
      kind: 'tool',
      name: 'bash',
      at: 100,
      status: 'err',
      errorText: '2 errors no-unused-vars',
    });
    expect(view.rows[0].durationText).toBeUndefined();
    expect(view.toolErr).toBe(1);
  });

  it('pairs sequential runs of the same tool LIFO', () => {
    const view = buildSubagentFlatView([
      stage('tool', 100, 'bash'),
      stage('tool', 101, 'bash ✓ 1.0s'),
      stage('tool', 102, 'bash'),
      stage('tool', 110, 'bash ✗ failed'),
    ]);
    expect(view.rows.map((r) => r.status)).toEqual(['ok', 'err']);
    expect(view.rows.map((r) => r.at)).toEqual([100, 102]);
    expect(view).toMatchObject({ toolOk: 1, toolErr: 1 });
  });

  it('emits a standalone row when the start entry was evicted by the 64-entry cap', () => {
    const view = buildSubagentFlatView([
      stage('model', 100),
      stage('tool', 200, 'grep ✓ 0.9s'),
    ]);
    expect(view.rows[1]).toEqual({
      kind: 'tool',
      name: 'grep',
      at: 200,
      status: 'ok',
      durationText: '0.9s',
    });
    expect(view.toolOk).toBe(1);
  });

  it('counts model stages as rounds and leaves other template stages intact', () => {
    const view = buildSubagentFlatView([
      stage('model', 100),
      stage('thinking', 101),
      stage('tool', 102, 'read_file'),
      stage('tool', 103, 'read_file ✓ 0.2s'),
      stage('model', 104),
      stage('waiting_approval', 105),
      stage('running', 106, 'approval resolved'),
    ]);
    expect(view.rounds).toBe(2);
    expect(view.rows.filter((r) => r.kind === 'stage').map((r) => r.name)).toEqual([
      'model',
      'thinking',
      'model',
      'waiting_approval',
      'running',
    ]);
  });

  it('returns an empty view for no stages', () => {
    expect(buildSubagentFlatView([])).toEqual({ rows: [], rounds: 0, toolOk: 0, toolErr: 0 });
  });
});
