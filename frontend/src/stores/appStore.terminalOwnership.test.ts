import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      get_sessions: vi.fn().mockResolvedValue([]),
      get_token_usage: vi.fn().mockResolvedValue(null),
      get_nav_index: vi.fn().mockResolvedValue([]),
    },
  };
});

it('终态事件无 session_name 且运行表有多个会话：不得误清当前会话的运行态', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = ['s1', 's2'];

  store.handleBridgeEvent({ type: 'turn_finish', data: {} } as any);

  expect(store.running).toBe(true);
  expect(store.runningSessions).toEqual(['s1', 's2']);
});

it('终态事件无 session_name、运行表为空但界面仍在运行：按当前会话收尾自愈', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = [];

  store.handleBridgeEvent({ type: 'turn_finish', data: {} } as any);

  expect(store.running).toBe(false);
});