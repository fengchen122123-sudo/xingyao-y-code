import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';

// 「切到 IDE 模式后文件夹要打开」：toggleIdeMode 需要确保当前会话的工作区
// 文件树已加载。mock 方式与 appStore.workspace.test.ts 一致：直接挂
// window.pywebview.api。

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

// 冲掉 toggleIdeMode 里 `void this.ensureIdeWorkspace()` 的微任务链，
// 让"不该发的请求"真的有机会发出来后再断言（否则断言是假阳性）
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

it('无工作区时切到 IDE：不弹目录选择器、不请求文件树', async () => {
  const selectWorkspaceFolder = vi.fn();
  const getFileTree = vi.fn();
  (window as any).pywebview = {
    api: { select_workspace_folder: selectWorkspaceFolder, get_file_tree: getFileTree },
  };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.workdir = '';

  store.toggleIdeMode();
  await flush();

  // 用户确认的行为：未绑定工作区时不自动弹选择器，界面留欢迎页让用户自己点
  expect(store.ideMode()).toBe(true);
  expect(selectWorkspaceFolder).not.toHaveBeenCalled();
  expect(getFileTree).not.toHaveBeenCalled();
});

it('有工作区且文件树为空时切到 IDE：拉一次文件树并写入 fileTree', async () => {
  const getFileTree = vi.fn().mockResolvedValue({
    ok: true,
    file_tree: [{ n: 'README.md', d: 0 }, { n: 'src', d: 1 }],
  });
  (window as any).pywebview = { api: { get_file_tree: getFileTree } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.workdir = 'E:\\projects\\demo';
  store.fileTree = [];

  store.toggleIdeMode();

  await vi.waitFor(() => expect(store.fileTree.length).toBe(2));
  expect(getFileTree).toHaveBeenCalledTimes(1);
  expect(getFileTree).toHaveBeenCalledWith('E:\\projects\\demo');
  expect(store.fileTree.map(n => n.n)).toEqual(['README.md', 'src']);
  expect(store.fileTreeRefreshing).toBe(false);
});

it('有工作区且文件树已加载时切到 IDE：不重复请求文件树', async () => {
  const getFileTree = vi.fn();
  (window as any).pywebview = { api: { get_file_tree: getFileTree } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.workdir = 'E:\\projects\\demo';
  store.fileTree = [{ n: 'README.md', d: 0 }];

  store.toggleIdeMode();
  await flush();

  expect(store.ideMode()).toBe(true);
  expect(getFileTree).not.toHaveBeenCalled();
  expect(store.fileTree).toEqual([{ n: 'README.md', d: 0 }]);
});

it('从 IDE 切回对话模式不触发文件树请求', async () => {
  const getFileTree = vi.fn();
  (window as any).pywebview = { api: { get_file_tree: getFileTree } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.workdir = 'E:\\projects\\demo';
  store.fileTree = []; // 故意留空：切回对话模式不该补拉树
  store.ideModeBySession['session_abc'] = true;

  store.toggleIdeMode();
  await flush();

  expect(store.ideMode()).toBe(false);
  expect(getFileTree).not.toHaveBeenCalled();
});

it('工作区存在但 get_file_tree 失败：不抛错、fileTree 保持原值、刷新态复位', async () => {
  const getFileTree = vi.fn().mockRejectedValue(new Error('后端挂了'));
  (window as any).pywebview = { api: { get_file_tree: getFileTree } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.workdir = 'E:\\projects\\demo';
  store.fileTree = [];

  // 不 reject：ensureIdeWorkspace 吞掉错误，toggleIdeMode 的 void 调用不会炸
  store.toggleIdeMode();
  await expect(flush()).resolves.toBeUndefined();

  await vi.waitFor(() => expect(getFileTree).toHaveBeenCalledTimes(1));
  expect(store.fileTree).toEqual([]); // 失败保留原值，不清空也不塞脏数据
  expect(store.fileTreeRefreshing).toBe(false); // 刷新态必须复位，不能卡在 true
  expect(store.ideMode()).toBe(true); // 模式切换本身不受拉树失败影响
});
