import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
});

it('subagentDetailId 随会话隔离：A 查看后切 B 不残留，切回 A 恢复', () => {
  const store = useAppStore();
  store.currentSessionName = 'A';
  store.subagentDetailId = 'sub-1';
  store._captureRuntime('A');

  store.currentSessionName = 'B';
  store._bindSessionRuntime('B');
  // B 未看过子代理详情：切过来不残留 A 的查看对象（否则会用 B 会话名去查 A 的任务 id）
  expect(store.subagentDetailId).toBe('');

  store.currentSessionName = 'A';
  store._bindSessionRuntime('A');
  expect(store.subagentDetailId).toBe('sub-1');
});

it('切会话时退出画笔截图模态：rightPanelSnipActive 置 false', () => {
  const store = useAppStore();
  store.currentSessionName = 'A';
  store.rightPanelSnipActive = true;
  store._captureRuntime('A');

  store.currentSessionName = 'B';
  store._bindSessionRuntime('B');
  expect(store.rightPanelSnipActive).toBe(false);
});

it('恢复页签集时过滤过期 id，避免 PANE_LABELS 取到 undefined', () => {
  const store = useAppStore();
  store.currentSessionName = 'A';
  (store as any)._getSessionRuntime('A').rightPanelTabs = ['files', 'legacy-tab'];
  (store as any)._getSessionRuntime('A').rightPanelActiveTab = 'files';
  store._bindSessionRuntime('A');
  expect(store.rightPanelTabs).toEqual(['files']);

  // 仅剩过期 id：过滤后回选择页，激活页签不得落在非法值上
  (store as any)._getSessionRuntime('A').rightPanelTabs = ['legacy-tab'];
  (store as any)._getSessionRuntime('A').rightPanelActiveTab = 'legacy-tab';
  store._bindSessionRuntime('A');
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelActiveTab).toBe('');
});

it('关闭页签清理对应瞬态：关 tasks 清 bgTaskDetailId、关 subagent 清 subagentDetailId', () => {
  const store = useAppStore();
  store.bgTaskDetailId = 't1';
  store.subagentDetailId = 'sub-1';
  store.openRightPanelTab('tasks');
  store.openRightPanelTab('subagent');
  expect(store.bgTaskDetailId).toBe('t1');
  expect(store.subagentDetailId).toBe('sub-1');

  store.closeRightPanelTab('tasks');
  expect(store.bgTaskDetailId).toBe('');

  store.closeRightPanelTab('subagent');
  expect(store.subagentDetailId).toBe('');
});
