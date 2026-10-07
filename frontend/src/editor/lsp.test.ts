// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import {
  changeDocument,
  closeDocument,
  diagnosticsFor,
  dispatchLspEvent,
  fileUri,
  lspRequest,
  normKey,
  onDiagnostics,
  onServerStatus,
  openDocument,
  resetClientState,
  setActiveWorkspace,
  uriToPath,
  type LspDiagnostic,
  type LspServerStatus,
} from './lsp';

const DIAG = {
  range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
  severity: 1 as const,
  message: 'undefined name x',
  source: 'fake',
};

function installBridge(overrides: Record<string, unknown> = {}) {
  const api = {
    lsp_request: vi.fn(async () => ({ ok: true, result: { items: [{ label: 'x' }] } })),
    // 显式标注返回类型：失败分支的用例会传 error 字段（与 pywebview.d.ts 的 lsp_notify
    // 声明一致），否则 vi.fn 推断出的 { ok: boolean } 会让 mockResolvedValue 报 TS2353
    lsp_notify: vi.fn(
      async (): Promise<{ ok: boolean; error?: { code: number; message: string }; msg?: string }> => ({
        ok: true,
      }),
    ),
    lsp_server_status: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', servers: [] })),
    lsp_set_server_path: vi.fn(async () => ({ ok: true })),
    lsp_sync_workspace: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', recycled: [] })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

let warnSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  (window as any).pywebview = undefined;
  // 失败分支按 Minor-1 会打日志：测试里静音，需要断言时用 warnSpy
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  resetClientState();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it('fileUri 与 uriToPath 往返一致（含中文目录）', () => {
  const path = 'E:\\proj\\子目录\\a.py';
  expect(uriToPath(fileUri(path))).toBe(path);
});

it('normKey 归一化斜杠与大小写，便于诊断按路径命中', () => {
  expect(normKey('E:/Proj/A.py')).toBe(normKey('E:\\proj\\a.py'));
});

it('openDocument 发送 didOpen 并登记文档', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  expect(api.lsp_notify).toHaveBeenCalledWith('E:\\p\\a.py', 'textDocument/didOpen', {
    textDocument: {
      uri: fileUri('E:\\p\\a.py'),
      languageId: 'python',
      version: 1,
      text: 'x = 1\n',
    },
  });
});

it('changeDocument 防抖：连续改动只发一次 didChange 且版本递增', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  changeDocument('E:\\p\\a.py', 'x = 2\n');
  changeDocument('E:\\p\\a.py', 'x = 3\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(150);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
  const second = api.lsp_notify.mock.calls[1] as unknown[];
  expect(second[1]).toBe('textDocument/didChange');
  expect((second[2] as any).textDocument.version).toBe(2);
  expect((second[2] as any).contentChanges).toEqual([{ text: 'x = 3\n' }]);
});

it('未打开的文档收到 changeDocument 时被忽略', async () => {
  const api = installBridge();
  changeDocument('E:\\p\\ghost.py', 'x = 1\n');
  await vi.advanceTimersByTimeAsync(200);
  expect(api.lsp_notify).not.toHaveBeenCalled();
});

it('closeDocument 发送 didClose 并清掉本地诊断', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  expect(diagnosticsFor('E:/p/a.py')).toHaveLength(1);
  await closeDocument('E:\\p\\a.py');
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
  expect(api.lsp_notify).toHaveBeenLastCalledWith('E:\\p\\a.py', 'textDocument/didClose', {
    textDocument: { uri: fileUri('E:\\p\\a.py') },
  });
});

it('lspRequest 在桥返回失败时返回 null', async () => {
  installBridge({
    lsp_request: vi.fn(async () => ({ ok: false, error: { code: -32000, message: 'no server' } })),
  });
  expect(await lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).toBeNull();
});

it('桥不可用时全部请求安全降级', async () => {
  expect(await lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).toBeNull();
  await expect(openDocument('E:\\p\\a.py', 'python', 'x')).resolves.toBeUndefined();
  await expect(closeDocument('E:\\p\\a.py')).resolves.toBeUndefined();
});

it('dispatchLspEvent 只认识 lsp_diagnostics / lsp_status', () => {
  const statuses: LspServerStatus[] = [];
  const diags: Array<[string, LspDiagnostic[]]> = [];
  const offStatus = onServerStatus((status) => statuses.push(status));
  const offDiag = onDiagnostics((path, list) => diags.push([path, list]));

  dispatchLspEvent('lsp_status', {
    server: 'only-two-events',
    language: 'python',
    workspace: 'E:\\w',
    state: 'running',
  });
  // status 监听器真的收到了（旧用例的恒真断言让这条从未被验证）
  expect(statuses.map((status) => status.state)).toEqual(['running']);

  dispatchLspEvent('some_other_event', { server: 'x', state: 'running', path: 'E:\\p\\a.py' });
  // 未知事件既不进任何监听器，也不落诊断缓存
  expect(statuses).toHaveLength(1);
  expect(diags).toHaveLength(0);
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);

  // 注销函数真的摘掉监听器
  offStatus();
  offDiag();
  dispatchLspEvent('lsp_status', {
    server: 'only-two-events',
    language: 'python',
    workspace: 'E:\\w',
    state: 'stopped',
  });
  expect(statuses).toHaveLength(1);
  expect(diags).toHaveLength(0);
});

it('onDiagnostics 投递 path 与诊断，注销后不再投递', () => {
  const seen: Array<[string, LspDiagnostic[]]> = [];
  const off = onDiagnostics((path, list) => seen.push([path, list]));
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG, DIAG] });
  expect(seen).toHaveLength(1);
  expect(seen[0][0]).toBe('E:\\p\\a.py');
  expect(seen[0][1]).toHaveLength(2);
  // 监听器拿到的是缓存里的同一份（冻结的）快照
  expect(seen[0][1]).toBe(diagnosticsFor('E:/p/a.py'));
  off();
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  expect(seen).toHaveLength(1);
});

it('onServerStatus 投递状态、注销后不再投递、畸形状态被忽略', () => {
  const seen: LspServerStatus[] = [];
  const off = onServerStatus((status) => seen.push(status));
  dispatchLspEvent('lsp_status', {
    server: 'on-status-a',
    language: 'python',
    workspace: 'E:\\w',
    state: 'running',
  });
  expect(seen).toHaveLength(1);
  expect(seen[0].server).toBe('on-status-a');
  // 缺 server 的畸形载荷与空载荷都不得投递
  dispatchLspEvent('lsp_status', { language: 'python', state: 'running' });
  dispatchLspEvent('lsp_status', null);
  expect(seen).toHaveLength(1);
  off();
  dispatchLspEvent('lsp_status', {
    server: 'on-status-a',
    language: 'python',
    workspace: 'E:\\w',
    state: 'stopped',
  });
  expect(seen).toHaveLength(1);
});

it('resetClientState 不重复注册监听（回归：清状态不能把监听器复制一份）', () => {
  const diagCalls: number[] = [];
  const statusCalls: number[] = [];
  const offDiag = onDiagnostics(() => diagCalls.push(1));
  const offStatus = onServerStatus(() => statusCalls.push(1));
  resetClientState();
  resetClientState();
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  dispatchLspEvent('lsp_status', {
    server: 'no-dup',
    language: 'python',
    workspace: 'E:\\w',
    state: 'running',
  });
  expect(diagCalls).toHaveLength(1);
  expect(statusCalls).toHaveLength(1);
  offDiag();
  offStatus();
});

it('resetClientState 清掉文档与诊断，重新打开会再发 didOpen', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  resetClientState();
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

// ---------------------------------------------------------------------------
// Task 8 评审 I-3 / I-4 / Minor-1：传输层兜底与通知返回值
// ---------------------------------------------------------------------------

it('didOpen 返回 ok:false（未配置服务器 / 越权）时不登记，后续击键不再发桥调用', async () => {
  const api = installBridge();
  api.lsp_notify.mockResolvedValue({
    ok: false,
    error: { code: -32000, message: '该文件类型未配置语言服务器，已降级为纯文本编辑' },
  });
  await openDocument('E:\\p\\notes.txt', '', 'hello\n');
  // 可诊断的痕迹（Minor-1：全链路零日志）
  expect(warnSpy).toHaveBeenCalledWith(
    expect.stringContaining('didOpen 未生效'),
    'E:\\p\\notes.txt',
    -32000,
    expect.anything(),
  );

  changeDocument('E:\\p\\notes.txt', 'hello world\n');
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1); // 只有那次失败的 didOpen

  // 不登记不等于拉黑：再次打开（例如用户切回标签）仍会重试
  await openDocument('E:\\p\\notes.txt', '', 'hello world\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

it('didOpen 传输 reject 时既不抛给调用方，也不登记幽灵文档', async () => {
  const api = installBridge({
    lsp_notify: vi.fn(async () => {
      throw new Error('transport down');
    }),
  });
  await expect(openDocument('E:\\p\\a.py', 'python', 'x = 1\n')).resolves.toBeUndefined();
  changeDocument('E:\\p\\a.py', 'x = 2\n');
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);
  // 未登记 → didClose 也不发
  await expect(closeDocument('E:\\p\\a.py')).resolves.toBeUndefined();
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);
});

it('didChange 桥调用 reject 被吞掉：异常不逃逸，本地版本与前一次变更不倒退', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  api.lsp_notify.mockImplementationOnce(async () => {
    throw new Error('transport down');
  });
  changeDocument('E:\\p\\a.py', 'x = 2\n');
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);

  changeDocument('E:\\p\\a.py', 'x = 3\n');
  await vi.advanceTimersByTimeAsync(300);
  const last = api.lsp_notify.mock.calls[2] as unknown[];
  expect(last[1]).toBe('textDocument/didChange');
  expect((last[2] as any).contentChanges).toEqual([{ text: 'x = 3\n' }]);
  // 版本单调：失败的那次也推进了版本，不会回退去重发旧版本
  expect((last[2] as any).textDocument.version).toBe(3);
});

it('closeDocument 在桥 reject 时吞掉传输异常，且本地状态先清干净', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  api.lsp_notify.mockImplementationOnce(async () => {
    throw new Error('transport down');
  });
  await expect(closeDocument('E:\\p\\a.py')).resolves.toBeUndefined();
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
  // 已关闭：didChange 不会打到已关闭文档上
  changeDocument('E:\\p\\a.py', 'x = 9\n');
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

it('lspRequest 失败时打日志说明原因（服务器缺失 / 不支持 / 越权）', async () => {
  installBridge({
    lsp_request: vi.fn(async () => ({
      ok: false,
      error: { code: -32601, message: 'method not supported' },
      msg: 'method not supported',
    })),
  });
  expect(await lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).toBeNull();
  expect(warnSpy).toHaveBeenCalledWith(
    expect.stringContaining('textDocument/hover 失败'),
    'E:\\p\\a.py',
    -32601,
    'method not supported',
  );
});

it('lspRequest 在桥 reject 时静默回落为 null（不打断调用方）', async () => {
  installBridge({
    lsp_request: vi.fn(async () => {
      throw new Error('transport down');
    }),
  });
  await expect(lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).resolves.toBeNull();
});

// ---------------------------------------------------------------------------
// 端到端缺口：服务器崩溃重启后重发 didOpen
// ---------------------------------------------------------------------------

function statusOf(server: string, workspace: string, language: string, state: string) {
  return { server, workspace, language, state };
}

it('服务器重启（restarting → starting → running）后对已打开文档重发 didOpen', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);

  dispatchLspEvent('lsp_status', statusOf('jedi-restart-1', 'E:\\ws1', 'python', 'running'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-1', 'E:\\ws1', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-1', 'E:\\ws1', 'python', 'starting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-1', 'E:\\ws1', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);

  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
  const reopen = api.lsp_notify.mock.calls[1] as unknown[];
  expect(reopen[0]).toBe('E:\\p\\a.py');
  expect(reopen[1]).toBe('textDocument/didOpen');
  expect((reopen[2] as any).textDocument.text).toBe('x = 1\n');
  expect((reopen[2] as any).textDocument.version).toBe(2);
});

it('重发 didOpen 带上尚未提交的最新文本，并消费掉那次待发变更', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  changeDocument('E:\\p\\a.py', 'x = 2\n'); // 防抖窗口内，尚未提交

  dispatchLspEvent('lsp_status', statusOf('jedi-restart-4', 'E:\\ws4', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-4', 'E:\\ws4', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);

  const reopen = api.lsp_notify.mock.calls[1] as unknown[];
  expect(reopen[1]).toBe('textDocument/didOpen');
  expect((reopen[2] as any).textDocument.text).toBe('x = 2\n');
  // 全文已在 didOpen 里送达：那次 didChange 被消费，不再重复发送
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

it('重启重发只覆盖该服务器语言的文档', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  await openDocument('E:\\p\\a.json', 'json', '{}\n');

  dispatchLspEvent('lsp_status', statusOf('jedi-restart-5', 'E:\\ws5', 'json', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-5', 'E:\\ws5', 'json', 'running'));
  await vi.advanceTimersByTimeAsync(0);

  expect(api.lsp_notify).toHaveBeenCalledTimes(3);
  const reopen = api.lsp_notify.mock.calls[2] as unknown[];
  expect(reopen[0]).toBe('E:\\p\\a.json');
  expect(reopen[1]).toBe('textDocument/didOpen');
});

it('服务器首次启动（starting → running）不重发 didOpen（避免协议错误）', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\b.py', 'python', 'y = 1\n');
  dispatchLspEvent('lsp_status', statusOf('jedi-fresh', 'E:\\ws6', 'python', 'starting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-fresh', 'E:\\ws6', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);
});

it('重启重发失败时撤销本地登记，之后不再向空文档发 didChange', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  api.lsp_notify.mockResolvedValue({
    ok: false,
    error: { code: -32000, message: '该文件类型未配置语言服务器，已降级为纯文本编辑' },
  });

  dispatchLspEvent('lsp_status', statusOf('jedi-restart-6', 'E:\\ws7', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-restart-6', 'E:\\ws7', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  const before = api.lsp_notify.mock.calls.length;

  changeDocument('E:\\p\\a.py', 'x = 3\n');
  await vi.advanceTimersByTimeAsync(300);
  expect(api.lsp_notify.mock.calls.length).toBe(before);
});

it('running → stopped 后再次 running 也会重发 didOpen（进程被回收过）', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_status', statusOf('jedi-recycled', 'E:\\ws8', 'python', 'running'));
  dispatchLspEvent('lsp_status', statusOf('jedi-recycled', 'E:\\ws8', 'python', 'stopped'));
  dispatchLspEvent('lsp_status', statusOf('jedi-recycled', 'E:\\ws8', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

// ---------------------------------------------------------------------------
// Task 12 评审 B：重启重发不得跨工作区误发
// ---------------------------------------------------------------------------

it('切工作区清状态后，旧工作区迟到的 running 不会对新工作区文档重发 didOpen', async () => {
  const api = installBridge();
  await openDocument('E:\\wsA\\a.py', 'python', 'x = 1\n');
  // 旧工作区（wsA）服务器崩溃：留下「待重发」标记
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));

  // 用户切到另一个工作区：本地 LSP 状态整体清空（生产入口是 ideStore.syncWorkspace）
  resetClientState();
  await openDocument('E:\\wsB\\b.py', 'python', 'y = 1\n');
  const before = api.lsp_notify.mock.calls.length;

  // wsA 那个键上迟到的 running：如果只清了 openDocs 而留下旧的重启记账，
  // 这里会把 wsB 的文档当成「刚重启的服务器仍打开的文档」重发 didOpen —— 跨区误发
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  // 唯一允许的 didOpen 是本用例自己 openDocument 发出的那条（mock.calls[1]）
  expect(api.lsp_notify.mock.calls.length).toBe(before);
  expect((api.lsp_notify.mock.calls[1] as unknown[])[0]).toBe('E:\\wsB\\b.py');
});

it('resetClientState 清掉按 (workspace, server) 的重启记账', async () => {
  const api = installBridge();
  await openDocument('E:\\wsA\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));
  resetClientState();
  // 重启记账清空后，旧键上的 running 既不是「回收后再次 running」也不是「重启完成」：
  // 不对任何人重发（此时也没有登记在册的文档）
  await openDocument('E:\\wsA\\b.py', 'python', 'z = 1\n');
  const before = api.lsp_notify.mock.calls.length;
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify.mock.calls.length).toBe(before);
});

it('切换工作区时回收语言服务器并清空本地 LSP 状态', async () => {
  const { createPinia, setActivePinia } = await import('pinia');
  const { useIdeStore } = await import('../stores/ideStore');
  setActivePinia(createPinia());
  const api = installBridge({
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true,
      path,
      content: 'x = 1\n',
      encoding: 'utf-8',
      bom: false,
      eol: 'lf',
      hash: 'h1',
    })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
  });
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('E:\\ws\\a.py');
  await openDocument('E:\\ws\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\ws\\a.py', diagnostics: [DIAG] });
  expect(diagnosticsFor('E:\\ws\\a.py')).toHaveLength(1);

  await store.syncWorkspace();

  expect(api.lsp_sync_workspace).toHaveBeenCalledTimes(1);
  expect(diagnosticsFor('E:\\ws\\a.py')).toEqual([]);
});

it('桥不支持 lsp_sync_workspace 时 syncWorkspace 仍然清本地状态（不抛异常）', async () => {
  const { createPinia, setActivePinia } = await import('pinia');
  const { useIdeStore } = await import('../stores/ideStore');
  setActivePinia(createPinia());
  const api = installBridge();
  delete (api as any).lsp_sync_workspace;
  const store = useIdeStore();
  store.setSession('s1');
  await openDocument('E:\\ws\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\ws\\a.py', diagnostics: [DIAG] });
  await expect(store.syncWorkspace()).resolves.toBeUndefined();
  expect(diagnosticsFor('E:\\ws\\a.py')).toEqual([]);
});

// ---------------------------------------------------------------------------
// S2 验收欠账 4：重启重发的过滤条件补齐 workspace 维度
// ---------------------------------------------------------------------------

it('重启重发按文档归属工作区过滤，不把 A 区文档重发给 B 区的同语言服务器', async () => {
  const api = installBridge();
  setActiveWorkspace('E:\\wsA');
  await openDocument('E:\\wsA\\a.py', 'python', 'x = 1\n');

  // B 区同语言的服务器重启：与 a.py 无关，重发就是跨区误发（重复 didOpen 是协议错误）
  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);

  // 自己的服务器重启：必须重发
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
  expect((api.lsp_notify.mock.calls[1] as unknown[])[0]).toBe('E:\\wsA\\a.py');
  expect((api.lsp_notify.mock.calls[1] as unknown[])[1]).toBe('textDocument/didOpen');
});

it('工作区比较对斜杠方向与盘符大小写不敏感（后端 normcase 口径）', async () => {
  const api = installBridge();
  setActiveWorkspace('E:\\wsA');
  await openDocument('E:\\wsA\\a.py', 'python', 'x = 1\n');

  // 后端 status() 报的是 os.path.normcase 归一化后的路径：小写 + 反斜杠
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'e:\\wsa', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'e:\\wsa', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);

  // 另一个方向：事件里是正斜杠，登记时是反斜杠
  dispatchLspEvent('lsp_status', statusOf('jedi-a2', 'E:/wsA/', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a2', 'E:/wsA/', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(3);
});

it('openDocument 的 workspace 参数优先于当前工作区', async () => {
  const api = installBridge();
  setActiveWorkspace('E:\\wsA');
  await openDocument('E:\\wsB\\b.py', 'python', 'y = 1\n', 'E:\\wsB');

  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);

  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
  expect((api.lsp_notify.mock.calls[1] as unknown[])[0]).toBe('E:\\wsB\\b.py');
});

it('归属未知的文档仍参与重发（不因过滤而漏发）', async () => {
  const api = installBridge();
  // 没设过工作区（挂载早期打开的文件）：归属未知按参与处理，交回三态同清兜底
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');

  dispatchLspEvent('lsp_status', statusOf('jedi-x', 'E:\\wsX', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-x', 'E:\\wsX', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

it('resetClientState 一并清掉当前工作区（宁可未知也不记错归属）', async () => {
  const api = installBridge();
  setActiveWorkspace('E:\\wsA');
  resetClientState();
  await openDocument('E:\\wsB\\b.py', 'python', 'y = 1\n');

  // 旧工作区（wsA）的服务器重启：新文档不是它的，不得把旧归属当成它的
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  // 归属未知 → 参与（唯一那次 didOpen 由本用例自己发出）
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});

it('syncWorkspace 记录当前工作区，供后续打开文档的归属过滤使用', async () => {
  const { createPinia, setActivePinia } = await import('pinia');
  const { useIdeStore } = await import('../stores/ideStore');
  setActivePinia(createPinia());
  const api = installBridge({
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true,
      path,
      content: 'x = 1\n',
      encoding: 'utf-8',
      bom: false,
      eol: 'lf',
      hash: 'h1',
    })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    lsp_sync_workspace: vi.fn(async () => ({ ok: true, workspace: 'E:\\wsA', recycled: [] })),
  });
  const store = useIdeStore();
  store.setSession('s1');
  await store.syncWorkspace();
  expect(api.lsp_sync_workspace).toHaveBeenCalledTimes(1);
  await openDocument('E:\\wsA\\a.py', 'python', 'x = 1\n');

  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-b', 'E:\\wsB', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);

  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'restarting'));
  dispatchLspEvent('lsp_status', statusOf('jedi-a', 'E:\\wsA', 'python', 'running'));
  await vi.advanceTimersByTimeAsync(0);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});
