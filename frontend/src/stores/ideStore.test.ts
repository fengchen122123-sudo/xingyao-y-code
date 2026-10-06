import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import { normKey, useIdeStore } from './ideStore';

function installBridge(overrides: Record<string, unknown> = {}) {
  const api = {
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true,
      path,
      content: 'x = 1\n',
      encoding: 'utf-8',
      bom: false,
      eol: 'lf',
      hash: 'h1',
    })),
    ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    ide_save_layout: vi.fn(async () => ({ ok: true, layout: null })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

it('openFile 读取内容并登记为标签', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');

  expect(api.ide_read_file).toHaveBeenCalledWith('E:\\p\\a.py');
  expect(store.tabs().map((t) => t.path)).toEqual(['E:\\p\\a.py']);
  expect(store.activePath()).toBe('E:\\p\\a.py');
  expect(store.tab('E:\\p\\a.py')?.dirty).toBe(false);
  // 打开后必须把打开集合同步给后端轮询
  expect(api.ide_set_open_files).toHaveBeenCalledWith(['E:\\p\\a.py']);
});

it('重复打开同一文件不会产生第二个标签', async () => {
  installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('a.py');
  expect(store.tabs().length).toBe(1);
});

it('updateContent 置脏，saveTab 用打开时的 hash 保存并清脏', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  expect(store.tab('a.py')?.dirty).toBe(true);

  await store.saveTab('a.py');
  expect(api.ide_write_file).toHaveBeenCalledWith('a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('a.py')?.dirty).toBe(false);
  expect(store.tab('a.py')?.hash).toBe('h2');
});

it('保存冲突时不覆盖本地内容，标记冲突状态', async () => {
  installBridge({
    ide_write_file: vi.fn(async () => ({ ok: false, conflict: { disk_hash: 'h9' }, msg: '文件已被外部修改' })),
  });
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  await store.saveTab('a.py');

  expect(store.tab('a.py')?.dirty).toBe(true);
  expect(store.tab('a.py')?.conflict).toBe(true);
  expect(store.tab('a.py')?.content).toBe('x = 2\n');
});

it('外部改动：文件干净时自动重载，脏时只标记不覆盖', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');

  // 干净：自动重载为新内容
  api.ide_read_file.mockResolvedValueOnce({
    ok: true, path: 'a.py', content: 'x = 99\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h3',
  } as any);
  await store.handleExternalChange('a.py');
  expect(store.tab('a.py')?.content).toBe('x = 99\n');
  expect(store.tab('a.py')?.externalChange).toBe(false);

  // 脏：保留用户内容，只标记
  store.updateContent('a.py', 'x = 100\n');
  await store.handleExternalChange('a.py');
  expect(store.tab('a.py')?.content).toBe('x = 100\n');
  expect(store.tab('a.py')?.externalChange).toBe(true);
});

it('会话之间标签互相隔离', async () => {
  installBridge();
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('a.py');
  store.setSession('s2');
  expect(store.tabs().length).toBe(0);
  store.setSession('s1');
  expect(store.tabs().length).toBe(1);
});

it('closeTab 关闭最后一个标签后活动路径为空', async () => {
  installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  store.closeTab('a.py');
  expect(store.tabs()).toEqual([]);
  expect(store.activePath()).toBe('');
});

// ---- A：后端 realpath 与调用方原始路径不一致时的查表口径 ----

it('后端返回 realpath 与调用方路径大小写/分隔符不同时，第二次 openFile 不新开标签', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');
  await store.openFile('E:\\P\\a.py');

  expect(api.ide_read_file).toHaveBeenCalledTimes(1);
  expect(store.tabs().length).toBe(1);
  // 标签路径仍是后端 realpath（权威路径，不得被规范化结果覆盖）
  expect(store.tabs()[0].path).toBe('e:\\p\\a.py');
  expect(store.activePath()).toBe('e:\\p\\a.py');
});

it('用原始路径 updateContent/saveTab 仍能命中 realpath 标签并真正写盘（不静默 return）', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');
  store.updateContent('E:\\P\\a.py', 'x = 2\n');
  expect(store.tab('E:\\P\\a.py')?.dirty).toBe(true);

  await store.saveTab('E:\\P\\a.py');
  expect(api.ide_write_file).toHaveBeenCalledTimes(1);
  expect(api.ide_write_file).toHaveBeenCalledWith('e:\\p\\a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('E:\\P\\a.py')?.dirty).toBe(false);
  expect(store.tab('E:\\P\\a.py')?.hash).toBe('h2');
});

it('含 .. 相对段的路径与后端 realpath 归到同一标签，保存也命中', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'E:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\p\\sub\\..\\a.py');
  await store.openFile('E:\\p\\a.py');
  expect(api.ide_read_file).toHaveBeenCalledTimes(1);
  expect(store.tabs().length).toBe(1);

  store.updateContent('E:\\p\\sub\\..\\a.py', 'x = 3\n');
  await store.saveTab('E:\\p\\sub\\..\\a.py');
  expect(api.ide_write_file).toHaveBeenCalledTimes(1);
  expect(api.ide_write_file).toHaveBeenCalledWith('E:\\p\\a.py', 'x = 3\n', 'h1', 'utf-8', false, 'lf');
});

it('closeTab / handleExternalChange 同样经规范化键命中标签', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');
  // 脏：外部改动只标记不覆盖
  store.updateContent('E:\\P\\a.py', 'x = 5\n');
  await store.handleExternalChange('E:\\P\\a.py');
  expect(store.tab('E:\\P\\a.py')?.externalChange).toBe(true);

  store.closeTab('E:\\P\\a.py');
  expect(store.tabs()).toEqual([]);
  expect(api.ide_read_file).toHaveBeenCalledTimes(1);
});

it('normKey 折叠分隔符 / 盘符大小写 / 相对段，且保留相对路径语义', () => {
  expect(normKey('E:\\P\\a.py')).toBe(normKey('e:/p/a.py'));
  expect(normKey('E:\\p\\sub\\..\\a.py')).toBe(normKey('E:\\p\\a.py'));
  expect(normKey('E:\\p\\a.py')).not.toBe(normKey('E:\\p\\b.py'));
  // 大小写折叠只发生在 Windows 形态的路径上
  expect(normKey('src/A.py')).toBe('src/A.py');
});

// ---- B：toggleBreakpoint / persistLayout 的行为覆盖 ----

it('toggleBreakpoint 同一 path+line 再调一次删除断点，breakpoints 用 `${path}:${line}` 键', async () => {
  const api = installBridge();
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('a.py');
  store.toggleBreakpoint('a.py', 7);
  store.toggleBreakpoint('a.py', 3);

  expect(store.breakpointsFor('a.py')).toEqual([3, 7]);
  expect(store.breakpoints.has('a.py:3')).toBe(true);
  expect(store.breakpoints.has('a.py:7')).toBe(true);
  expect(store.breakpoints.size).toBe(2);

  store.toggleBreakpoint('a.py', 3);
  expect(store.breakpointsFor('a.py')).toEqual([7]);
  expect(store.breakpoints.has('a.py:3')).toBe(false);
  // 每次增删都会把布局（含断点）写回后端
  await Promise.resolve();
  expect(api.ide_save_layout).toHaveBeenCalled();
  const lastCall = api.ide_save_layout.mock.calls.at(-1) as unknown[] | undefined;
  const layout = lastCall?.[1] as { breakpoints: unknown } | undefined;
  expect(layout?.breakpoints).toEqual([{ path: 'a.py', line: 7 }]);
});

it('breakpointsFor 只返回该文件的断点并按行号升序', async () => {
  installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  store.toggleBreakpoint('a.py', 9);
  store.toggleBreakpoint('b.py', 1);
  store.toggleBreakpoint('a.py', 2);
  store.toggleBreakpoint('a.py', 5);

  expect(store.breakpointsFor('a.py')).toEqual([2, 5, 9]);
  expect(store.breakpointsFor('b.py')).toEqual([1]);
  expect(store.breakpointsFor('c.py')).toEqual([]);
});

it('toggleBreakpoint 落库用标签 realpath，gutter 的 `${path}:${line}` 能命中', async () => {
  installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');
  store.toggleBreakpoint('E:\\P\\a.py', 4);

  expect(store.breakpoints.has(`${store.tabs()[0].path}:4`)).toBe(true);
  expect(store.breakpointsFor('E:\\P\\a.py')).toEqual([4]);
});

it('persistLayout 把 tabs / active / breakpoints 写回 ide_save_layout', async () => {
  const api = installBridge();
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('a.py');
  await store.openFile('b.py');
  store.activate('a.py');
  store.toggleBreakpoint('b.py', 4);
  api.ide_save_layout.mockClear();

  await store.persistLayout();

  expect(api.ide_save_layout).toHaveBeenCalledTimes(1);
  expect(api.ide_save_layout).toHaveBeenCalledWith('s1', {
    version: 1,
    tabs: [
      { path: 'a.py', line: 1 },
      { path: 'b.py', line: 2 },
    ],
    active: 'a.py',
    breakpoints: [{ path: 'b.py', line: 4 }],
  });
});

it('persistLayout 无会话或无桥时安全返回，不抛异常', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  api.ide_save_layout.mockClear();
  await expect(store.persistLayout()).resolves.toBeUndefined();
  expect(api.ide_save_layout).not.toHaveBeenCalled();

  store.setSession('s1');
  (window as any).pywebview = undefined;
  await expect(store.persistLayout()).resolves.toBeUndefined();
});
