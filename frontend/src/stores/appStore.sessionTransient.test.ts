import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

// 会话级瞬态隔离与页签清理：
//   2) subagentDetailId 与 bgTaskDetailId 同口径随会话保存/恢复；
//   3) 画笔截图模态（rightPanelSnipActive）切会话时退出，不把浏览器表面按住；
//   6) 恢复页签集时按合法白名单过滤，避免过期 id 让 PANE_LABELS 取到 undefined；
//   7) 关闭页签时同步清理该页签的瞬态（详情查看对象）。

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
  // 模态是会话瞬态：残留会把新会话浏览器原生表面持续压住
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
  // 已设查看对象时开页签不会覆盖
  expect(store.bgTaskDetailId).toBe('t1');
  expect(store.subagentDetailId).toBe('sub-1');

  store.closeRightPanelTab('tasks');
  expect(store.bgTaskDetailId).toBe('');

  store.closeRightPanelTab('subagent');
  expect(store.subagentDetailId).toBe('');
});
