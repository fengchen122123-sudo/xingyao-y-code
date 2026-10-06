import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import { pathKey, samePath, useIdeStore } from './ideStore';

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

// ---- A：以后端 realpath 为唯一判重权威（只统一分隔符，不折叠大小写与 `..`）----

it('调用方路径含 .. 而后端回规范化绝对路径：第二次 openFile 只产生一个标签，saveTab 仍命中', async () => {
  // 真实存在的形态差异：传入 E:\p\sub\..\a.py，后端回 realpath E:\p\a.py
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'E:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\p\\sub\\..\\a.py');
  await store.openFile('E:\\p\\sub\\..\\a.py');

  // (1) 判重只依据后端 realpath：绝不新建第二个标签
  expect(store.tabs().map((t) => t.path)).toEqual(['E:\\p\\a.py']);
  expect(store.activePath()).toBe('E:\\p\\a.py');

  // (2) 调用方拿原路径保存仍能命中标签，写盘用 realpath（不能静默 return）
  store.updateContent('E:\\p\\sub\\..\\a.py', 'x = 2\n');
  expect(store.tab('E:\\p\\a.py')?.dirty).toBe(true);
  await store.saveTab('E:\\p\\sub\\..\\a.py');
  expect(api.ide_write_file).toHaveBeenCalledTimes(1);
  expect(api.ide_write_file).toHaveBeenCalledWith('E:\\p\\a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('E:\\p\\a.py')?.dirty).toBe(false);
});

it('同一文件换一种写法打开：多读一次后按 realpath 精确命中，丢弃读取结果不新建标签', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'E:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');
  // 第二次换含 `..` 的写法：精确比对未命中 → 真读一次（可接受的重复读取）
  api.ide_read_file.mockResolvedValueOnce({
    ok: true, path: 'E:\\p\\a.py', content: 'x = 99\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h9',
  } as any);
  await store.openFile('E:\\p\\sub\\..\\a.py');

  expect(api.ide_read_file).toHaveBeenCalledTimes(2);
  expect(store.tabs().length).toBe(1);
  // 判定为同一标签后本次读取结果被丢弃：内容与 hash 都没被换掉
  expect(store.tabs()[0].path).toBe('E:\\p\\a.py');
  expect(store.tabs()[0].content).toBe('x = 1\n');
  expect(store.tabs()[0].hash).toBe('h1');
  // 这次调用方写法被记为查表别名，后续保存仍能命中并写 realpath
  store.updateContent('E:\\p\\sub\\..\\a.py', 'x = 2\n');
  await store.saveTab('E:\\p\\sub\\..\\a.py');
  expect(api.ide_write_file).toHaveBeenCalledWith('E:\\p\\a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
});

it('标签路径始终保存后端 realpath，不被调用方路径覆盖', async () => {
  installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');

  expect(store.tabs()[0].path).toBe('e:\\p\\a.py');
  expect(store.activePath()).toBe('e:\\p\\a.py');
  expect(store.tab('E:\\P\\a.py')?.path).toBe('e:\\p\\a.py');
});

it('大小写不同不做词法折叠：后端认同一文件时也只有（且必须只有）一个标签', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');
  await store.openFile('E:\\p\\A.py');

  // 不折叠大小写 → 第二次仍走一次真读，靠后端 realpath 相等收敛到一个标签
  expect(api.ide_read_file).toHaveBeenCalledTimes(2);
  expect(store.tabs().length).toBe(1);
  expect(store.tabs()[0].path).toBe('e:\\p\\a.py');
});

it('写类动作用调用方原路径命中标签：靠 openFile 记下的后端对答，而不是大小写折叠', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'e:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\P\\a.py');
  // 权威路径是后端 realpath，调用方原路径被记为查表别名（不折叠大小写）
  expect(store.tabs()[0].path).toBe('e:\\p\\a.py');
  expect(store.tabs()[0].requestedPaths).toEqual(['E:\\P\\a.py']);

  store.updateContent('E:\\P\\a.py', 'x = 2\n');
  expect(store.tab('E:\\P\\a.py')?.dirty).toBe(true);

  await store.saveTab('E:\\P\\a.py');
  expect(api.ide_write_file).toHaveBeenCalledTimes(1);
  expect(api.ide_write_file).toHaveBeenCalledWith('e:\\p\\a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('E:\\P\\a.py')?.dirty).toBe(false);
  expect(store.tab('E:\\P\\a.py')?.hash).toBe('h2');
});

it('closeTab / handleExternalChange 用调用方原路径同样命中标签', async () => {
  const api = installBridge({
    ide_read_file: vi.fn(async () => ({
      ok: true, path: 'E:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
  });
  const store = useIdeStore();
  await store.openFile('E:\\p\\sub\\..\\a.py');
  // 脏：外部改动只标记不覆盖
  store.updateContent('E:\\p\\sub\\..\\a.py', 'x = 5\n');
  await store.handleExternalChange('E:\\p\\sub\\..\\a.py');
  expect(store.tab('E:\\p\\a.py')?.externalChange).toBe(true);

  store.closeTab('E:\\p\\sub\\..\\a.py');
  expect(store.tabs()).toEqual([]);
  expect(api.ide_read_file).toHaveBeenCalledTimes(1);
});

it('pathKey/samePath 只统一分隔符：不折叠大小写、不折叠 ..（不猜路径）', () => {
  expect(pathKey('E:\\p\\a.py')).toBe('E:/p/a.py');
  expect(samePath('E:\\p\\a.py', 'E:/p/a.py')).toBe(true);
  expect(samePath('E:\\p\\a.py', 'E:\\p\\b.py')).toBe(false);
  // 大小写敏感目录里 A.py 与 a.py 是两个文件：不得无差别 toLowerCase
  expect(samePath('E:/p/A.py', 'E:/p/a.py')).toBe(false);
  // 中间段是 junction / 符号链接时，`..` 的 realpath 语义与词法折叠不同：不得折叠
  expect(samePath('E:/p/sub/../a.py', 'E:/p/a.py')).toBe(false);
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

// ---- C：'' 桶时序（Task 8 评审 Important）----

it("未设会话时打开的文件在 setSession 后迁移进新会话，不凭空消失且仍可保存", async () => {
  const api = installBridge();
  const store = useIdeStore();

  // 界面挂载前就打开文件：此时还没有会话名，标签只能落进 '' 桶
  await store.openFile('a.py');
  expect(store.tabs().map((t) => t.path)).toEqual(['a.py']);

  // 随后才拿到会话名
  store.setSession('s1');

  // 标签随迁移仍在（活动标签一并迁移），不会随 '' 桶变成孤儿
  expect(store.tabs().map((t) => t.path)).toEqual(['a.py']);
  expect(store.activePath()).toBe('a.py');

  // 迁移后的标签仍可正常保存（写盘用 realpath + 打开时的 hash）
  store.updateContent('a.py', 'x = 2\n');
  expect(store.tab('a.py')?.dirty).toBe(true);
  await store.saveTab('a.py');
  expect(api.ide_write_file).toHaveBeenCalledWith('a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('a.py')?.dirty).toBe(false);

  // '' 桶已清空：同一份标签不会同时挂在两个桶里
  store.setSession('');
  expect(store.tabs()).toEqual([]);
  store.setSession('s1');
  expect(store.tabs().map((t) => t.path)).toEqual(['a.py']);
});
