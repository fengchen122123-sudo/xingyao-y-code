// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';

// EditorState.languageData 是静态 Facet：实例上只能用 facet()/languageDataAt() 读，
// 计划原文的 `state.languageData.length` 在实例上恒为 undefined（已用最小探针证实）
import { EditorState } from '@codemirror/state';

import { createEditorState, LANGUAGE_LOADERS } from './setup';
import {
  diagnosticsFor,
  dispatchLspEvent,
  lineOffsets,
  lspCompletionSource,
  lspExtensions,
  offsetToPosition,
  positionToOffset,
  resetClientState,
  severityOf,
  toCompletions,
  toDiagnostics,
} from './lsp';

it('行号与文档偏移互转', () => {
  const text = 'a\nbb\nccc';
  const offsets = lineOffsets(text);
  expect(offsets).toEqual([0, 2, 5]);
  expect(positionToOffset(offsets, 1, 1, text.length)).toBe(3);
  expect(offsetToPosition(offsets, 4)).toEqual({ line: 1, character: 2 });
});

it('越界的行号被夹到文档范围内，不抛异常', () => {
  const offsets = lineOffsets('a\n');
  expect(positionToOffset(offsets, 99, 0, 2)).toBe(2);
  expect(positionToOffset(offsets, 0, 999, 2)).toBe(2);
});

it('severity 映射：1 是 error，2 是 warning，3 是 info，4 是 hint', () => {
  expect(severityOf(1)).toBe('error');
  expect(severityOf(2)).toBe('warning');
  expect(severityOf(3)).toBe('info');
  expect(severityOf(4)).toBe('hint');
  expect(severityOf(undefined)).toBe('error');
});

it('toCompletions 把 LSP kind 映射成 CM6 类型并保留文档', () => {
  const items = toCompletions([
    { label: 'foo', kind: 3, detail: 'def foo()' },
    { label: 'Bar', kind: 7 },
    { label: 'baz', kind: 999 },
  ]);
  expect(items[0]).toMatchObject({ label: 'foo', type: 'function', detail: 'def foo()' });
  expect(items[1].type).toBe('class');
  expect(items[2].type).toBe('text');
});

it('toCompletions 保留 insertText 与 MarkupContent 文档', () => {
  const items = toCompletions([
    { label: 'foo', insertText: 'foo()', documentation: { value: '说明' } },
    { label: 'bar', documentation: '简写文档' },
  ]);
  expect(items[0].apply).toBe('foo()');
  expect(items[0].info).toBe('说明');
  expect(items[1].apply).toBe('bar');
  expect(items[1].info).toBe('简写文档');
});

it('toDiagnostics 把 LSP 范围换算成文档偏移', () => {
  const text = 'x = 1\nundefined_name\n';
  const offsets = lineOffsets(text);
  const diags = toDiagnostics(
    [
      {
        range: { start: { line: 1, character: 0 }, end: { line: 1, character: 5 } },
        severity: 1,
        message: 'undefined',
        source: 'jedi',
      },
    ],
    offsets,
    text.length,
  );
  expect(diags[0].from).toBe(offsets[1]);
  expect(diags[0].to).toBe(offsets[1] + 5);
  expect(diags[0].severity).toBe('error');
  expect(diags[0].source).toBe('jedi');
});

it('toDiagnostics 跳过缺 range 的畸形条目而不是抛异常', () => {
  const text = 'x = 1\n';
  expect(() => toDiagnostics([{ message: 'bad' } as never], lineOffsets(text), text.length)).not.toThrow();
  expect(toDiagnostics([{ message: 'bad' } as never], lineOffsets(text), text.length)).toEqual([]);
});

it('lspExtensions 返回补全、诊断与视图注册三个扩展', () => {
  resetClientState();
  const exts = lspExtensions({ path: 'E:\\p\\a.py', languageId: 'python' });
  expect(exts.length).toBe(3);
});

it('诊断事件不会在视图未登记时抛异常', () => {
  resetClientState();
  expect(() =>
    dispatchLspEvent('lsp_diagnostics', {
      path: 'E:\\p\\a.py',
      diagnostics: [
        {
          range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
          severity: 1,
          message: 'm',
        },
      ],
    }),
  ).not.toThrow();
});

it('createEditorState 接受额外扩展且不破坏文档', async () => {
  resetClientState();
  const state = await createEditorState(
    'x = 1\n',
    'python',
    lspExtensions({ path: 'E:\\p\\a.py', languageId: 'python' }),
  );
  expect(state.doc.toString()).toBe('x = 1\n');
  // 语言扩展仍在：python 语言数据可查（commentTokens 是 python 语言声明的 '#'）
  expect(state.facet(EditorState.languageData).length).toBeGreaterThan(0);
  expect(state.languageDataAt<{ line: string }>('commentTokens', 0)).toEqual([{ line: '#' }]);
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('python');

  // 额外扩展真的进了 state：用公开 Facet 当哨兵（计划原文只能断言「没抛异常」）
  resetClientState();
  const withSentinel = await createEditorState('x = 1\n', 'python', [
    EditorState.readOnly.of(true),
    ...lspExtensions({ path: 'E:\\p\\a.py', languageId: 'python' }),
  ]);
  expect(withSentinel.facet(EditorState.readOnly)).toBe(true);
  expect(withSentinel.doc.toString()).toBe('x = 1\n');
});

it('诊断落地时冻结缓存：外部数组被改写不影响缓存，缓存也不可被下游改动', () => {
  resetClientState();
  const diag: any = {
    range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
    severity: 1,
    message: 'first',
  };
  const payload = { path: 'E:\\p\\a.py', diagnostics: [diag] };
  dispatchLspEvent('lsp_diagnostics', payload);
  // 事件源数组被后续原地改写（后端复用 buffer / 监听器原地排序）不得污染缓存
  payload.diagnostics[0].message = 'tampered';
  payload.diagnostics.length = 0;
  const cached = diagnosticsFor('E:\\p\\a.py');
  expect(cached).toHaveLength(1);
  expect(cached[0].message).toBe('first');
  expect(Object.isFrozen(cached)).toBe(true);
  expect(Object.isFrozen(cached[0].range)).toBe(true);
});

it('补全源在桥不可用时静默返回 null（不抛异常、不弹提示、不打日志）', async () => {
  resetClientState();
  (window as any).pywebview = undefined;
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const source = lspCompletionSource('E:\\p\\a.py');
  const context = {
    explicit: true,
    matchBefore: () => ({ from: 0, to: 2, text: 'ab' }),
    state: { doc: { toString: () => 'ab\n' } },
  } as never;
  await expect(source(context)).resolves.toBeNull();
  // 桥整体缺失（浏览器预览 / 测试环境）是「非目标环境」，不打日志：否则每次触发补全都会刷屏
  expect(warn).not.toHaveBeenCalled();
  warn.mockRestore();
});

it('补全源在桥调用 reject 时静默返回 null（不产生 unhandled rejection）', async () => {
  resetClientState();
  (window as any).pywebview = {
    api: {
      lsp_request: vi.fn(async () => {
        throw new Error('bridge down');
      }),
    },
  };
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const source = lspCompletionSource('E:\\p\\a.py');
  const context = {
    explicit: true,
    matchBefore: () => ({ from: 0, to: 2, text: 'ab' }),
    state: { doc: { toString: () => 'ab\n' } },
  } as never;
  await expect(source(context)).resolves.toBeNull();
  warn.mockRestore();
  (window as any).pywebview = undefined;
});

