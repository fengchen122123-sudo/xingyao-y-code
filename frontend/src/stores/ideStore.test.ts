import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import { useIdeStore } from './ideStore';

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
