import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

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

  store.closeRightPanelTab('browser');
  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.rightPanelActiveTab).toBe('files');

  store.openRightPanelTab('git');
  store.closeRightPanelTab('git');
  expect(store.rightPanelActiveTab).toBe('files');

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
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelActiveTab).toBe('');
  expect(store.rightPanelCollapsed).toBe(true);

  store.openRightPanelTab('tasks');
  await store.switchSession('A');
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
});

it('浏览器自动化事件按会话归属：不抢当前会话的屏，切回发起会话时页签已开，且 automationViewport 会话隔离', async () => {
  const store = useAppStore();
  await store.switchSession('B');
  store.openRightPanelTab('files');
  store.rightPanelCollapsed = false;

  store.handleBridgeEvent({
    type: 'embedded_browser_automation',
    data: { width: 1280, height: 800, session_name: 'A' },
  } as any);
  // 后台会话 A 的分辨率不得污染当前查看会话 B 的 automationViewport
  expect(store.embeddedBrowser.automationViewport).toBeNull();
  expect(store.rightPanelActiveTab).toBe('files');
  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.rightPanelCollapsed).toBe(false);

  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toEqual(['browser']);
  expect(runtimeA.rightPanelActiveTab).toBe('browser');
  expect(runtimeA.rightPanelExpanded).toBe(true);
  expect(runtimeA.automationViewport).toEqual([1280, 800]);

  await store.switchSession('A');
  expect(store.embeddedBrowser.automationViewport).toEqual([1280, 800]);
  await store.switchSession('B');
  expect(store.embeddedBrowser.automationViewport).toBeNull();
});

it('用户手动折叠右面板后，后台/前台自动开页签不覆盖折叠偏好，也不改变面板宽度', async () => {
  const store = useAppStore();
  await store.switchSession('A');
  store.rightPanelWidth = 340;
  store.rightPanelCollapsed = false;
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);

  store.openRightPanelTabForSession('A', 'computer', { expand: false });
  expect(store.rightPanelTabs).toContain('computer');
  expect(store.rightPanelActiveTab).toBe('computer');
  expect(store.rightPanelCollapsed).toBe(true);
  expect(store.rightPanelWidth).toBe(340);

  store.handleBridgeEvent({
    type: 'embedded_browser_automation',
    data: { width: 1440, height: 900, session_name: 'A' },
  } as any);
  expect(store.rightPanelCollapsed).toBe(true);
  expect(store.rightPanelWidth).toBe(340);
  expect(store.embeddedBrowser.automationViewport).toEqual([1440, 900]);
});

it('浏览器自动化事件缺 session_name 时只应用分辨率，不替任何会话开页签', () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = true;

  store.handleBridgeEvent({
    type: 'embedded_browser_automation',
    data: { width: 375, height: 667 },
  } as any);
  expect(store.embeddedBrowser.automationViewport).toEqual([375, 667]);
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelCollapsed).toBe(true);
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
  await store.switchSession('B');
  (store as any)._getSessionRuntime('A').rightPanelTabs = ['computer', 'git'];
  (store as any)._getSessionRuntime('A').rightPanelActiveTab = 'computer';

  await store.switchSession('A');
  expect(store.rightPanelTabs).toEqual(['git']);
  expect(store.rightPanelActiveTab).toBe('git');

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-9', event_seq: 1, status: 'running' });
  store.openRightPanelTab('computer', { expand: false });
  (store as any)._captureCurrentRuntime();
  await store.switchSession('B');
  await store.switchSession('A');
  expect(store.rightPanelTabs).toContain('computer');
  expect(store.rightPanelActiveTab).toBe('computer');
});

it('embedded_browser_assigned：后台会话只在自己的右栏登记，不抢当前会话', () => {
  const store = useAppStore();
  store.currentSessionName = 'A';
  store.openRightPanelTab('files');

  store.handleBridgeEvent({
    type: 'embedded_browser_assigned',
    data: { session_name: 'B', url: 'https://example.com' },
  } as any);

  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.browserNavRequest).toBeNull();
  const runtimeB = (store as any).sessionRuntimeByName['B'];
  expect(runtimeB.rightPanelTabs).toEqual(['browser']);
  expect(runtimeB.rightPanelActiveTab).toBe('browser');
});

it('embedded_browser_activation：当前会话收到回放地址时写入导航请求并开页签', () => {
  const store = useAppStore();
  store.currentSessionName = 'B';

  store.handleBridgeEvent({
    type: 'embedded_browser_activation',
    data: { session_name: 'B', url: 'https://example.com/page' },
  } as any);

  expect(store.rightPanelTabs).toContain('browser');
  expect(store.browserNavRequest).toMatchObject({
    url: 'https://example.com/page',
    session: 'B',
  });
});

it('子代理开关关闭：会话内外的子代理页签一并收掉，重新打开只恢复入口', async () => {
  const store = useAppStore();
  store.openRightPanelTab('files');
  store.openRightPanelTab('subagent');
  store.subagentDetailId = 'sub-1';
  (store as any).sessionRuntimeByName['B'] = {
    rightPanelTabs: ['subagent', 'git'],
    rightPanelActiveTab: 'subagent',
  };
  (window as any).pywebview.api.set_subagent_enabled = vi.fn().mockResolvedValue({
    ok: true,
    settings: { ...store.systemSettings, subagent_enabled: false },
  });

  expect(await store.setSubagentEnabled(false)).toBe(true);
  expect(store.systemSettings.subagent_enabled).toBe(false);
  // 当前会话：激活的子代理页签被清，回退到左侧邻居，查看对象一并清理
  expect(store.rightPanelTabs).toEqual(['files']);
  expect(store.rightPanelActiveTab).toBe('files');
  expect(store.subagentDetailId).toBe('');
  // 其它会话：页签清掉，激活页签同步回退
  const runtimeB = (store as any).sessionRuntimeByName['B'];
  expect(runtimeB.rightPanelTabs).toEqual(['git']);
  expect(runtimeB.rightPanelActiveTab).toBe('git');

  (window as any).pywebview.api.set_subagent_enabled = vi.fn().mockResolvedValue({
    ok: true,
    settings: { ...store.systemSettings, subagent_enabled: true },
  });
  expect(await store.setSubagentEnabled(true)).toBe(true);
  expect(store.rightPanelTabs).toEqual(['files']);

  store.systemSettings.subagent_enabled = false;
  store.sessionRuntimeByName['C'] = {
    ...((store as any)._getSessionRuntime('C') || {}),
    rightPanelTabs: ['subagent'],
    rightPanelActiveTab: 'subagent',
  } as any;
  await store.switchSession('C');
  expect(store.rightPanelTabs).not.toContain('subagent');
});
