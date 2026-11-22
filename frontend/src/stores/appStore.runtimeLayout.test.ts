import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

function bindCurrent(store: ReturnType<typeof useAppStore>, session = 'A') {
  store.currentSessionName = session;
  store._getSessionRuntime(session);
  store._bindSessionRuntime(session);
}

it('跨会话事件不回滚用户刚展开的右面板', () => {
  const store = useAppStore();
  bindCurrent(store);

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);

  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'B', user_text: '后台任务' } } as any);

  // 面板开合不得被 A 的旧快照覆盖
  expect(store.rightPanelCollapsed).toBe(false);
});

it('跨会话事件不回滚用户刚收起的右面板', () => {
  const store = useAppStore();
  bindCurrent(store);
  store._getSessionRuntime('A').rightPanelExpanded = true;
  store._bindSessionRuntime(store.currentSessionName);
  expect(store.rightPanelCollapsed).toBe(false);

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);

  // 跨会话事件到达后不得自动展开
  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'B' } } as any);
  expect(store.rightPanelCollapsed).toBe(true);
});

it('同会话事件仍捕获面板开合快照（用户操作最终随会话持久）', () => {
  const store = useAppStore();
  bindCurrent(store);

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);

  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'A', user_text: '当前会话' } } as any);
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store._getSessionRuntime('A').rightPanelExpanded).toBe(true);
});
