import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('loads files when entering a pending project conversation', async () => {
  // 新会话秒开：不同步等 get_workspace_state 扫树，
  // 文件树由 get_file_tree 后台异步加载后补上
  const getFileTree = vi.fn().mockResolvedValue({
    ok: true,
    file_tree: [{ n: 'README.md', d: 0 }],
  });
  const getWorkspaceState = vi.fn();
  (window as any).pywebview = {
    api: { get_file_tree: getFileTree, get_workspace_state: getWorkspaceState },
  };
  const store = useAppStore();

  await store.createSessionInGroup('E:\\projects\\demo');

  // 切换动作本身不触碰后端（秒开）
  expect(getWorkspaceState).not.toHaveBeenCalled();
  expect(store.pendingWorkdir).toBe('E:\\projects\\demo');
  expect(store.workdir).toBe('E:\\projects\\demo');
  expect(store.currentWorkdirLabel).toBe('demo');
  expect(store.currentSessionName).toBe('');

  // 文件树后台加载完成后补上
  await vi.waitFor(() => {
    expect(store.fileTree).toEqual([{ n: 'README.md', d: 0 }]);
  });
  expect(getFileTree).toHaveBeenCalledWith('E:\\projects\\demo');
});

it('keeps the loaded tree when creating a session in the current workdir', async () => {
  // 当前就在该工作区：新会话保留已加载的树，完全不触发后端调用
  const getFileTree = vi.fn();
  (window as any).pywebview = {
    api: { get_file_tree: getFileTree },
  };
  const store = useAppStore();
  store.workdir = 'E:\\projects\\demo';
  store.currentWorkdirLabel = 'demo';
  store.fileTree = [{ n: 'README.md', d: 0 }];

  await store.createSessionInGroup('E:\\projects\\demo');

  expect(getFileTree).not.toHaveBeenCalled();
  expect(store.fileTree).toEqual([{ n: 'README.md', d: 0 }]);
  expect(store.pendingWorkdir).toBe('E:\\projects\\demo');
  expect(store.currentSessionName).toBe('');
});

it('does not inherit another session running state when creating a new conversation', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.handleBridgeEvent({
    type: 'turn_start',
    data: { session_name: 'session-a', user_text: '正在执行' },
  });

  await store.createGlobalSession();

  expect(store.currentSessionName).toBe('');
  expect(store.running).toBe(false);
  expect(store.runningSessions).toEqual(['session-a']);
});

it('reports running only for the currently selected session', () => {
  const store = useAppStore();
  // M30 修复后判定源统一为 runningSessions（与 sendMessage 拦截判定一致），
  // 不再读 sessionRuntimeByName[key].running
  store.currentSessionName = 'session-b';
  store.runningSessions = ['session-a'];

  expect(store.currentSessionRunning).toBe(false);
  store.currentSessionName = 'session-a';
  expect(store.currentSessionRunning).toBe(true);
});
