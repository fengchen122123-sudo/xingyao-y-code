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
  // H2 契约：归属无法唯一推断时「不更新任何会话运行态」。
  // 旧实现把 running=false 放在归属判断之外无条件执行，等于把正在跑的
  // 当前会话按成空闲，同时 runningSessions 里还留着它——随后的发送判定
  // 会走排队分支、界面却显示可发送，用户看到的就是状态对不上。
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = ['s1', 's2'];

  store.handleBridgeEvent({ type: 'turn_finish', data: {} } as any);

  expect(store.running).toBe(true);
  expect(store.runningSessions).toEqual(['s1', 's2']);
});

it('终态事件无 session_name、运行表为空但界面仍在运行：按当前会话收尾自愈', () => {
  // 反向不一致：运行表空了、界面还在转。这种残留没有任何事件能纠正，
  // 用户只能反复点停止，所以终态到达时按当前显示的会话收尾。
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = [];

  store.handleBridgeEvent({ type: 'turn_finish', data: {} } as any);

  expect(store.running).toBe(false);
});