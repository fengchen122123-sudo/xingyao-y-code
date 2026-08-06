import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';


const event = (type: string, data: any) => ({ type, data });
const startData = (id: string, command: string) => ({
  id, name: 'execute_command', label: `运行命令 ${command}`,
  display: {
    category: 'command', action: '运行命令', target: command, command,
    metrics: {}, error_summary: '', is_skill: false,
  },
});
const finishData = (id: string, ok: boolean, duration_ms: number) => ({
  ...startData(id, id === 'bad' ? 'pytest -q' : 'python -m pytest -q'),
  ok, duration_ms, summary: ok ? '完成' : '失败', result: ok ? 'passed' : '退出码 1',
});
const editStart = (id: string, path: string) => ({
  id, name: 'edit_file', label: `修改文件 ${path}`,
  display: {
    category: 'mutation', action: '修改文件', target: path, command: '',
    metrics: { added: 1, removed: 1 }, error_summary: '', is_skill: false,
  },
});

beforeEach(() => setActivePinia(createPinia()));

it('keeps a failed tool and the corrected active tool in one message', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
  store.handleBridgeEvent(event('tool_start', startData('bad', 'pytest -q')));
  store.handleBridgeEvent(event('tool_finish', finishData('bad', false, 3200)));
  store.handleBridgeEvent(event('tool_start', startData('good', 'python -m pytest -q')));

  const message = store.messages.at(-1)!;
  expect(message.execution_state).toBe('running');
  expect(message.tool_calls).toHaveLength(2);
  expect(message.tool_calls![0].result!.ok).toBe(false);
  expect(message.tool_calls![1].result).toBeUndefined();
  expect(store.running).toBe(true);
});

it('binds diff_ready only to the matching tool call', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '改代码' }));
  store.handleBridgeEvent(event('tool_start', editStart('edit-1', 'a.py')));
  store.handleBridgeEvent(event('tool_start', editStart('edit-2', 'b.py')));
  const bound = { tool_call_id: 'edit-2', file_path: 'b.py', diff_text: '--- b.py\n+++ b.py\n-old\n+new' };
  const orphan = { tool_call_id: '', file_path: 'c.py', diff_text: '--- c.py\n+++ c.py\n-x\n+y' };
  store.handleBridgeEvent(event('diff_ready', bound));
  store.handleBridgeEvent(event('diff_ready', orphan));

  const message = store.messages.at(-1)!;
  expect(message.tool_calls![0].diff).toBeUndefined();
  expect(message.tool_calls![1].diff).toEqual({ file_path: 'b.py', diff_text: bound.diff_text });
  expect(message.legacy_diffs).toEqual([{ file_path: 'c.py', diff_text: orphan.diff_text }]);
});

it('pairs old history tool messages by explicit id when switching sessions', async () => {
  const store = useAppStore();
  // 会话 1：实时回合产生 message-local 结果
  store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
  store.handleBridgeEvent(event('tool_start', startData('good', 'python -m pytest -q')));
  store.handleBridgeEvent(event('tool_finish', finishData('good', true, 1200)));
  store.handleBridgeEvent(event('turn_finish', {}));

  // 会话 2：历史消息（旧形状 tool_calls + 独立 role=tool 消息）
  const history = [
    { role: 'user', content: '看看 README' },
    {
      role: 'assistant', content: '已读取', reasoning_content: '',
      tool_calls: [
        { id: 'call-a', function: { name: 'read_file', arguments: { file_path: 'README.md' } } },
        { id: 'call-b', function: { name: 'search_code', arguments: { query: 'ycode' } } },
      ],
    },
    { role: 'tool', tool_call_id: 'call-a', name: 'read_file', content: '# Y-CODE' },
  ];
  (window as any).pywebview = {
    api: {
      switch_session: async () => ({ messages: history, is_global: true, workdir: 'GLOBAL' }),
    },
  };
  await store.switchSession('session-2', '历史会话');

  // 配对的 role=tool 消息被折叠进助手消息，不再单独出现在列表里
  expect(store.messages).toHaveLength(2);
  const assistant = store.messages.find(m => m.role === 'assistant')!;
  expect(assistant.execution_state).toBe('completed');
  const calls = assistant.tool_calls!;
  expect(calls).toHaveLength(2);
  expect(calls[0].name).toBe('read_file');
  expect(calls[0].label).toBe('read_file README.md');
  expect(calls[0].result?.result).toBe('# Y-CODE');
  expect(calls[1].name).toBe('search_code');
  expect(calls[1].result).toBeUndefined();

  // 切换会话后不得保留上一会话的实时结果
  expect(JSON.stringify(store.messages)).not.toContain('python -m pytest -q');

  // 清理 mock，避免后续用例的 turn_finish 触发 refreshSessions 刷错误日志
  (window as any).pywebview = undefined;
});

it('keeps cancelled and error states when turn_finish arrives later', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '任务一' }));
  store.handleBridgeEvent(event('turn_cancelled', {}));
  expect(store.messages.at(-1)!.execution_state).toBe('cancelled');
  store.handleBridgeEvent(event('turn_finish', {}));
  expect(store.messages.at(-1)!.execution_state).toBe('cancelled');

  store.handleBridgeEvent(event('turn_start', { user_text: '任务二' }));
  store.handleBridgeEvent(event('turn_error', { message: 'boom' }));
  expect(store.messages.at(-1)!.execution_state).toBe('error');
  store.handleBridgeEvent(event('turn_finish', {}));
  expect(store.messages.at(-1)!.execution_state).toBe('error');
});

it('moves subagent snapshots into the running run_subagent call and freezes on completion', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '并行调研' }));
  store.handleBridgeEvent(event('tool_start', { id: 'sub-1', name: 'run_subagent', label: '运行子代理', display: {} }));
  store.handleBridgeEvent(event('subagent_progress', {
    tasks: [
      { id: 't1', description: '调研代码结构', status: 'running', stage: 'tool', stage_detail: '', elapsed: 3, type: 'foreground', background: false },
    ],
  }));
  const message = store.messages.at(-1)!;
  expect(message.tool_calls![0].subagent_tasks).toHaveLength(1);
  expect(message.tool_calls![0].subagent_tasks![0].description).toBe('调研代码结构');

  // 完成后快照冻结：后续进度事件不再覆盖已完成的调用
  store.handleBridgeEvent(event('tool_finish', {
    id: 'sub-1', name: 'run_subagent', label: '运行子代理',
    ok: true, summary: '完成', result: 'done', duration_ms: 5000, display: {},
  }));
  store.handleBridgeEvent(event('subagent_progress', { tasks: [] }));
  expect(message.tool_calls![0].subagent_tasks).toHaveLength(1);
});

it('creates an orphan completion call when tool_finish has no matching start', () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
  store.handleBridgeEvent(event('tool_finish', finishData('ghost', true, 100)));

  const message = store.messages.at(-1)!;
  expect(message.tool_calls).toHaveLength(1);
  expect(message.tool_calls![0].id).toBe('ghost');
  expect(message.tool_calls![0].result?.ok).toBe(true);
  expect(consoleError).toHaveBeenCalled();
  consoleError.mockRestore();
});

it('assigns a UI-only key and other category to tool_start events with empty ids', () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
  store.handleBridgeEvent(event('tool_start', startData('', 'npm test')));

  const message = store.messages.at(-1)!;
  const call = message.tool_calls![0];
  expect(call.id).toBe('contract-error-1');
  expect(call.display?.category).toBe('other');
  expect(typeof call.started_at_ms).toBe('number');
  expect(consoleError).toHaveBeenCalled();
  consoleError.mockRestore();
});

it('upserts tool_start by id without resetting the original start time', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
  store.handleBridgeEvent(event('tool_start', startData('dup', 'npm test')));
  const message = store.messages.at(-1)!;
  const startedAt = message.tool_calls![0].started_at_ms;
  store.handleBridgeEvent(event('tool_start', startData('dup', 'npm test')));
  expect(message.tool_calls).toHaveLength(1);
  expect(message.tool_calls![0].started_at_ms).toBe(startedAt);
});

it('raises an explicit contract error for tool events without an assistant message', () => {
  const store = useAppStore();
  expect(() => store.handleBridgeEvent(event('tool_start', startData('x', 'npm test')))).toThrow();
  expect(() => store.handleBridgeEvent(event('diff_ready', { tool_call_id: '', file_path: 'a', diff_text: '' }))).toThrow();
});
