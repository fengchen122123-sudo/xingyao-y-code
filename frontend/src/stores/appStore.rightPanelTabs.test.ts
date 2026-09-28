import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 右面板多页签的会话级隔离：页签集/激活页签/面板展开态随会话保存恢复，
// A 会话打开的浏览器页签不串到 B 会话；浏览器自动化事件按会话归属。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      switch_session: vi.fn().mockResolvedValue({ messages: [], is_global: true }),
    },
  };
});

it('openRightPanelTab 追加页签并激活；重复打开只激活不重复追加', () => {
  const store = useAppStore();
  store.openRightPanelTab('browser');
  expect(store.rightPanelTabs).toEqual(['browser']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);

  store.openRightPanelTab('git');
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);

  store.activateRightPanelTab('browser');
  store.openRightPanelTab('git');
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  expect(store.rightPanelActiveTab).toBe('git');
});

it('closeRightPanelTab 关闭激活页签时优先切右侧邻居，没有则取最后一个', () => {
  const store = useAppStore();
  store.openRightPanelTab('files');
  store.openRightPanelTab('git');
  store.openRightPanelTab('browser');
  store.activateRightPanelTab('git');

  store.closeRightPanelTab('git');
  expect(store.rightPanelTabs).toEqual(['files', 'browser']);
  expect(store.rightPanelActiveTab).toBe('browser');

  // 关闭尾部页签：右侧无邻居，取最后一个
  store.closeRightPanelTab('browser');
  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.rightPanelActiveTab).toBe('files');

  // 关闭非激活页签：激活页签不变
  store.openRightPanelTab('git');
  store.closeRightPanelTab('git');
  expect(store.rightPanelActiveTab).toBe('files');

  // 全部关完：回选择页
  store.closeRightPanelTab('files');
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelActiveTab).toBe('');
  expect(store.rightPanelPicker).toBe(true);
  expect(store.rightPanelCollapsed).toBe(false);
});

it('页签随会话隔离：A 开浏览器页签，切到 B 收起且无页签，切回 A 恢复', async () => {
  const store = useAppStore();
  await store.switchSession('A');
  store.openRightPanelTab('browser');
  store.openRightPanelTab('git');
  store.activateRightPanelTab('browser');

  await store.switchSession('B');
  // B 从未开过右面板：收起、无页签（浏览器不串台）
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelActiveTab).toBe('');
  expect(store.rightPanelCollapsed).toBe(true);

  // B 自己开的页签在切回 A 后不丢失
  store.openRightPanelTab('tasks');
  await store.switchSession('A');
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
});

it('浏览器自动化事件按会话归属：不抢当前会话的屏，切回发起会话时页签已开', () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.openRightPanelTab('files');
  store.rightPanelCollapsed = false;

  // A 会话发起自动化：当前查看的 B 不被展开/切换
  store.handleBridgeEvent({
    type: 'embedded_browser_automation',
    data: { width: 1280, height: 800, session_name: 'A' },
  } as any);
  expect(store.embeddedBrowser.automationViewport).toEqual([1280, 800]);
  expect(store.rightPanelActiveTab).toBe('files');
  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.rightPanelCollapsed).toBe(false);

  // 发起会话 A 的 runtime 已记下浏览器页签（含展开态）
  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toEqual(['browser']);
  expect(runtimeA.rightPanelActiveTab).toBe('browser');
  expect(runtimeA.rightPanelExpanded).toBe(true);
});

it('浏览器自动化事件缺 session_name 时归属当前会话（兼容旧后端/视口变更推送）', () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = true;

  store.handleBridgeEvent({
    type: 'embedded_browser_automation',
    data: { width: 375, height: 667 },
  } as any);
  expect(store.rightPanelTabs).toEqual(['browser']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
});

it('openRightPanelTab 打开详情页签时缺省选中本会话第一个任务，不跨会话代选', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.backgroundTasks = [
    { task_id: 't1', command: 'sleep 30', status: 'running', session_id: 's1' },
    { task_id: 't2', command: 'sleep 60', status: 'running', session_id: 's1' },
    { task_id: 't9', command: 'other session', status: 'running', session_id: 's2' },
  ];
  store.openRightPanelTab('tasks');
  expect(store.bgTaskDetailId).toBe('t1');

  // 本会话没有后台任务时不代选其他会话的任务（与执行条的会话过滤口径一致）
  const other = useAppStore();
  other.currentSessionName = 's2';
  other.backgroundTasks = [
    { task_id: 't1', command: 'sleep 30', status: 'running', session_id: 's1' },
  ];
  other.bgTaskDetailId = '';
  other.openRightPanelTab('tasks');
  expect(other.bgTaskDetailId).toBe('');
});

it('切会话时丢弃失去 run 的残留操控页签（终态收场发生在其他会话上的场景）', async () => {
  const store = useAppStore();
  await store.switchSession('A');
  // 模拟 A 会话的操控 run 自动开页签后，run 已被清场但页签残留在 A 的页签集
  await store.switchSession('B');
  (store as any)._getSessionRuntime('A').rightPanelTabs = ['computer', 'git'];
  (store as any)._getSessionRuntime('A').rightPanelActiveTab = 'computer';

  await store.switchSession('A');
  // run 已不在：操控页签是空白页签，恢复时被丢弃，落到 git 页签
  expect(store.rightPanelTabs).toEqual(['git']);
  expect(store.rightPanelActiveTab).toBe('git');

  // run 仍在时操控页签正常保留
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-9', event_seq: 1, status: 'running' });
  store.openRightPanelTab('computer', { expand: false });
  (store as any)._captureCurrentRuntime();
  await store.switchSession('B');
  await store.switchSession('A');
  expect(store.rightPanelTabs).toContain('computer');
  expect(store.rightPanelActiveTab).toBe('computer');
});
