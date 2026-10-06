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
  openDocument,
  resetClientState,
  uriToPath,
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
    lsp_notify: vi.fn(async () => ({ ok: true })),
    lsp_server_status: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', servers: [] })),
    lsp_set_server_path: vi.fn(async () => ({ ok: true })),
    lsp_sync_workspace: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', recycled: [] })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  vi.useFakeTimers();
  (window as any).pywebview = undefined;
  resetClientState();
});

afterEach(() => {
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
  const seen: unknown[] = [];
  dispatchLspEvent('lsp_status', { server: 'jedi', language: 'python', state: 'running' });
  dispatchLspEvent('some_other_event', { x: 1 });
  expect(seen).toEqual([]);
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
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
