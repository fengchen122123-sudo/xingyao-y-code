import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

/**
 * 回归：右面板开合被跨会话事件回滚。
 *
 * handleBridgeEvent 处理归属其他会话的事件时会临时 bind 目标会话、
 * 处理完在 finally 里 bind 回当前会话；_bindSessionRuntime 会按快照
 * 恢复 rightPanelCollapsed 等布局态 —— 用户刚点的开/合被旧快照覆盖，
 * 表现为"关了又自动打开 / 打开又自动关闭"。
 */

function bindCurrent(store: ReturnType<typeof useAppStore>, session = 'A') {
  store.currentSessionName = session;
  store._getSessionRuntime(session);
  store._bindSessionRuntime(session);
}

it('跨会话事件不回滚用户刚展开的右面板', () => {
  const store = useAppStore();
  bindCurrent(store);

  // 用户点"切换面板"展开；此时 A 的快照仍是默认收起（rightPanelExpanded 未定义）
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);

  // 后台会话 B 的回合事件到达：跨会话 → 临时 bind B 再 bind 回 A
  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'B', user_text: '后台任务' } } as any);

  // 面板开合不得被 A 的旧快照覆盖
  expect(store.rightPanelCollapsed).toBe(false);
});

it('跨会话事件不回滚用户刚收起的右面板', () => {
  const store = useAppStore();
  bindCurrent(store);
  // A 的快照此前捕获过"展开"（如展开状态下跑完一轮后被同会话事件捕获）
  store._getSessionRuntime('A').rightPanelExpanded = true;
  store._bindSessionRuntime(store.currentSessionName);
  expect(store.rightPanelCollapsed).toBe(false);

  // 用户收起面板
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

  // 当前会话自己的事件：finally 捕获实时布局进 A 的快照
  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'A', user_text: '当前会话' } } as any);
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store._getSessionRuntime('A').rightPanelExpanded).toBe(true);
});
