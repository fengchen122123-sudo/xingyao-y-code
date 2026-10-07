import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useIdeDebugStore } from './ideDebugStore';
import { useIdeStore } from './ideStore';
import { onDapOutput } from '../utils/dapBus';

const api = {
  ide_debug_start: vi.fn(),
  ide_debug_set_breakpoints: vi.fn(),
  ide_debug_command: vi.fn(),
  ide_debug_stack_trace: vi.fn(),
  ide_debug_scopes: vi.fn(),
  ide_debug_variables: vi.fn(),
  ide_debug_evaluate: vi.fn(),
  ide_debug_stop: vi.fn(),
  ide_run_file: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

describe('ideDebugStore 事件归约', () => {
  it('marks paused state and remembers the stop position', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_stopped', {
      session_id: 'dap-1', reason: 'breakpoint', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7,
    });

    expect(store.debugState).toBe('paused');
    expect(store.paused).toBe(true);
    expect(store.threadId).toBe(1);
    expect(store.selectedFrameId).toBe(1001);
    expect(store.stopPath).toBe('E:/p/app.py');
    expect(store.stopLine).toBe(7);
  });

  it('clears pause info on continue and terminated', () => {
    const store = useIdeDebugStore();
    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 1001, line: 7 });
    store.reduceDapEvent('dap_continued', { thread_id: 1 });
    expect(store.debugState).toBe('running');
    expect(store.frames).toEqual([]);

    store.reduceDapEvent('dap_terminated', { session_id: 'dap-1' });
    expect(store.debugState).toBe('stopped');
    expect(store.active).toBe(false);
  });

  it('records breakpoint verification per path and line', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_breakpoint_changed', { path: 'E:/p/app.py', line: 7, verified: true });

    expect(store.breakpointStates['E:/p/app.py:7']).toBe(true);
  });

  it('reports adapter state messages from dap_status', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_status', { state: 'running', msg: '' });
    expect(store.debugState).toBe('running');
    expect(store.active).toBe(false); // 还没有会话 id：界面据此区分「启动中」与「真在跑」

    store.reduceDapEvent('dap_status', { state: 'stopped', msg: '适配器退出' });
    expect(store.debugState).toBe('stopped');
    expect(store.lastError).toBe('适配器退出');
  });
});

describe('ideDebugStore 事件幂等', () => {
  it('重复 dap_stopped 不产生数组抖动（同一事件两遍不换引用）', () => {
    const store = useIdeDebugStore();
    const payload = { session_id: 'dap-1', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7 };

    store.reduceDapEvent('dap_stopped', payload);
    const frames = store.frames;
    const scopes = store.scopes;
    const variables = store.variables;

    store.reduceDapEvent('dap_stopped', payload);

    expect(store.frames).toBe(frames);
    expect(store.scopes).toBe(scopes);
    expect(store.variables).toBe(variables);
    expect(store.debugState).toBe('paused');
    expect(store.stopLine).toBe(7);
  });

  it('重复 dap_terminated / dap_continued 幂等', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 1, line: 3 });
    store.reduceDapEvent('dap_terminated', { session_id: 'dap-1' });
    const frames = store.frames;

    store.reduceDapEvent('dap_terminated', { session_id: 'dap-1' });

    expect(store.debugState).toBe('stopped');
    expect(store.sessionId).toBe('');
    expect(store.frames).toBe(frames);

    store.reduceDapEvent('dap_continued', { thread_id: 1 });
    store.reduceDapEvent('dap_continued', { thread_id: 1 });

    expect(store.debugState).toBe('running');
    expect(store.frames).toBe(frames);
  });

  it('同一断点验证事件重复到达不改变结果', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_breakpoint_changed', { path: 'E:/p/app.py', line: 7, verified: false });
    store.reduceDapEvent('dap_breakpoint_changed', { path: 'E:/p/app.py', line: 7, verified: false });

    expect(store.breakpointStates).toEqual({ 'E:/p/app.py:7': false });
  });
});

describe('ideDebugStore 与桥的协作', () => {
  it('groups stored breakpoints by file when starting a debug session', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: true, session_id: 'dap-9', adapter: 'debugpy' });
    const ide = useIdeStore();
    const store = useIdeDebugStore();
    ide.toggleBreakpoint('E:/p/app.py', 7);
    ide.toggleBreakpoint('E:/p/app.py', 11);
    ide.toggleBreakpoint('E:/p/other.py', 3);

    await store.startDebug('E:/p/app.py');

    expect(api.ide_debug_start).toHaveBeenCalledWith('E:/p/app.py', [
      { path: 'E:/p/app.py', lines: [7, 11] },
      { path: 'E:/p/other.py', lines: [3] },
    ], '', []);
    expect(store.sessionId).toBe('dap-9');
    expect(store.adapter).toBe('debugpy');
  });

  it('surfaces adapter hints from a failed start', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: false, msg: '未安装 debugpy（Python 调试适配器）' });
    const store = useIdeDebugStore();

    const res = await store.startDebug('E:/p/app.py');

    expect(res.ok).toBe(false);
    expect(store.lastError).toContain('debugpy');
  });

  it('forwards breakpoint toggles to the adapter when a session is running', async () => {
    api.ide_debug_set_breakpoints.mockResolvedValue({ ok: true, pending: false, verified: [{ line: 7, verified: true }] });
    const store = useIdeDebugStore();
    store.sessionId = 'dap-9';
    store.debugState = 'paused';

    await store.toggleBreakpoint('E:/p/app.py', 7);

    expect(api.ide_debug_set_breakpoints).toHaveBeenCalledWith('E:/p/app.py', [7]);
    expect(store.breakpointStates['E:/p/app.py:7']).toBe(true);
  });

  it('evaluates a watch expression on the selected frame', async () => {
    api.ide_debug_evaluate.mockResolvedValue({ ok: true, result: 'eval:x+1', type: 'int' });
    const store = useIdeDebugStore();
    store.sessionId = 'dap-9';
    store.selectedFrameId = 1001;

    await store.addWatch('x+1');

    expect(api.ide_debug_evaluate).toHaveBeenCalledWith('x+1', 1001);
    expect(store.watchResults).toEqual([{ expression: 'x+1', result: 'eval:x+1', type: 'int', error: '' }]);
  });

  it('routes debug adapter output through the bus, not reactive state', () => {
    const store = useIdeDebugStore();
    const seen: string[] = [];
    const off = onDapOutput((payload) => seen.push(payload.text));

    store.reduceDapEvent('dap_output', { session_id: 'dap-1', category: 'stdout', text: 'line\n' });

    expect(seen).toEqual(['line\n']);
    off();
  });

  it('停止调试后复位全部状态，允许重新开始', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: true, session_id: 'dap-9', adapter: 'debugpy' });
    api.ide_debug_stop.mockResolvedValue({ ok: true });
    const store = useIdeDebugStore();

    await store.startDebug('E:/p/app.py');
    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 1001, line: 7 });
    await store.stopDebug();

    expect(api.ide_debug_stop).toHaveBeenCalledTimes(1);
    expect(store.sessionId).toBe('');
    expect(store.debugState).toBe('stopped');
    expect(store.active).toBe(false);
    expect(store.frames).toEqual([]);
    expect(store.stopLine).toBe(0);
  });

  it('loadScopes 顺带展开首个作用域的变量（调试面板一次 IPC 就够）', async () => {
    api.ide_debug_scopes.mockResolvedValue({ ok: true, scopes: [{ name: 'Locals', variablesReference: 2001, expensive: false }] });
    api.ide_debug_variables.mockResolvedValue({
      ok: true, variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }],
    });
    const store = useIdeDebugStore();

    await store.loadScopes(1001);

    expect(api.ide_debug_scopes).toHaveBeenCalledWith(1001);
    expect(api.ide_debug_variables).toHaveBeenCalledWith(2001);
    expect(store.variables[0].value).toBe('42');
  });
});

describe('ideDebugStore 会话归属（S3-T10 评审）', () => {
  it('旧会话迟到的事件不会改动新会话状态', async () => {
    api.ide_debug_start
      .mockResolvedValueOnce({ ok: true, session_id: 'dap-A', adapter: 'debugpy' })
      .mockResolvedValueOnce({ ok: true, session_id: 'dap-B', adapter: 'debugpy' });
    const store = useIdeDebugStore();

    await store.startDebug('E:/p/app.py');
    await store.stopDebug();
    await store.startDebug('E:/p/app.py');
    expect(store.sessionId).toBe('dap-B');
    expect(store.debugState).toBe('running');

    // 旧适配器临死前推的结束事件：绝不能把新会话清掉
    store.reduceDapEvent('dap_exited', { session_id: 'dap-A', exit_code: 0 });
    store.reduceDapEvent('dap_terminated', { session_id: 'dap-A' });

    expect(store.sessionId).toBe('dap-B');
    expect(store.debugState).toBe('running');
    expect(store.active).toBe(true);
  });

  it('旧会话的 dap_continued 不会把已停止的前端刷回 running', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: true, session_id: 'dap-A', adapter: 'debugpy' });
    const store = useIdeDebugStore();

    await store.startDebug('E:/p/app.py');
    await store.stopDebug();
    expect(store.debugState).toBe('stopped');

    store.reduceDapEvent('dap_continued', { session_id: 'dap-A', thread_id: 1 });

    expect(store.debugState).toBe('stopped');
    expect(store.sessionId).toBe('');
  });

  it('重启后当前会话 id 已知：外来 id 的事件一律丢弃', async () => {
    api.ide_debug_start
      .mockResolvedValueOnce({ ok: true, session_id: 'dap-A', adapter: 'debugpy' })
      .mockResolvedValueOnce({ ok: true, session_id: 'dap-B', adapter: 'debugpy' });
    const store = useIdeDebugStore();

    await store.startDebug('E:/p/app.py');
    // 未经 stopDebug 直接重启（启动路径会回收旧会话）
    await store.startDebug('E:/p/app.py');
    store.reduceDapEvent('dap_stopped', { session_id: 'dap-A', thread_id: 1, line: 3 });

    expect(store.debugState).toBe('running');
    expect(store.stopLine).toBe(0);
    // 属于自己的事件照常归约
    store.reduceDapEvent('dap_stopped', { session_id: 'dap-B', thread_id: 1, frame_id: 7, line: 3 });
    expect(store.debugState).toBe('paused');
    expect(store.stopLine).toBe(3);
  });

  it('不带 session_id 的事件保持既有语义（老桥兼容）', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 9, line: 4 });

    expect(store.debugState).toBe('paused');
    expect(store.stopLine).toBe(4);
  });
});

describe('ideDebugStore 变量按作用域归属与层级回退（S3-T10 评审）', () => {
  const twoScopes = [
    { name: 'Locals', variablesReference: 2001, expensive: false },
    { name: 'Globals', variablesReference: 3001, expensive: false },
  ];

  it('各作用域渲染自己的子节点，不会拿首个作用域的内容顶替', async () => {
    api.ide_debug_scopes.mockResolvedValue({ ok: true, scopes: twoScopes });
    api.ide_debug_variables.mockImplementation(async (reference: number) =>
      reference === 2001
        ? { ok: true, variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }] }
        : { ok: true, variables: [{ name: 'os', value: '<module os>', type: 'module', variablesReference: 0 }] },
    );
    const store = useIdeDebugStore();

    await store.loadScopes(1001);
    expect(store.variables.map((item) => item.name)).toEqual(['x']);
    expect(store.activeScopeRef).toBe(2001);

    await store.selectScope(3001);

    expect(api.ide_debug_variables).toHaveBeenLastCalledWith(3001);
    expect(store.variables.map((item) => item.name)).toEqual(['os']);
    expect(store.varPath.map((node) => node.name)).toEqual(['Globals']);
  });

  it('展开变量进入子层级，返回上一层恢复父层变量', async () => {
    api.ide_debug_scopes.mockResolvedValue({ ok: true, scopes: [twoScopes[0]] });
    api.ide_debug_variables.mockImplementation(async (reference: number) =>
      reference === 2001
        ? { ok: true, variables: [{ name: 'self', value: 'App', type: 'App', variablesReference: 4001 }] }
        : { ok: true, variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }] },
    );
    const store = useIdeDebugStore();

    await store.loadScopes(1001);
    await store.expandVariables(4001, 'self');

    expect(store.variables.map((item) => item.name)).toEqual(['x']);
    expect(store.varPath.map((node) => node.name)).toEqual(['Locals', 'self']);

    await store.backVariables();

    expect(store.variables.map((item) => item.name)).toEqual(['self']);
    expect(store.varPath.map((node) => node.name)).toEqual(['Locals']);
    // 回退走缓存：只发过两次 variables 请求（Locals 根 + self 子层）
    expect(api.ide_debug_variables).toHaveBeenCalledTimes(2);
  });

  it('继续执行后层级与缓存一起清空，不把上一次暂停的变量树留给下一次', async () => {
    api.ide_debug_scopes.mockResolvedValue({ ok: true, scopes: [twoScopes[0]] });
    api.ide_debug_variables.mockResolvedValue({
      ok: true, variables: [{ name: 'self', value: 'App', type: 'App', variablesReference: 4001 }],
    });
    const store = useIdeDebugStore();

    await store.loadScopes(1001);
    await store.expandVariables(4001, 'self');
    store.reduceDapEvent('dap_continued', { thread_id: 1 });

    expect(store.varPath).toEqual([]);
    expect(store.activeScopeRef).toBe(0);
    expect(store.varCache).toEqual({});

    // 下一次暂停会重新取，而不是吃上次的缓存
    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 7, line: 3 });
    await store.loadScopes(1001);
    expect(api.ide_debug_variables).toHaveBeenCalledTimes(3);
  });
});
