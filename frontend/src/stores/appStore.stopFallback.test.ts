import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

it('停止后 10s 内重发消息：兜底定时器不得清掉新回合的运行态', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.runningSessions = ['s1'];
  store.running = true;
  const api = {
    stop_current_turn: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
    get_token_usage: vi.fn().mockResolvedValue(null),
    get_nav_index: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };

  // 用户点「结束任务」
  await store.stopCurrentTurn();
  expect(api.stop_current_turn).toHaveBeenCalledWith('s1');

  // 后端 turn_cancelled 到达：本轮解锁，界面进入 done 态
  store.handleBridgeEvent({ type: 'turn_cancelled', data: { session_name: 's1' } } as any);
  expect(store.running).toBe(false);

  // 用户立刻重发：新回合真正开始
  store.handleBridgeEvent({
    type: 'turn_start',
    data: { session_name: 's1', user_text: '重新发一次任务' },
  } as any);
  expect(store.running).toBe(true);
  expect(store.runningSessions).toContain('s1');

  // 停止时挂的 10s 兜底定时器到这里触发：新回合正在跑，绝不能解锁
  vi.advanceTimersByTime(10000);

  expect(store.running).toBe(true);
  expect(store.runningSessions).toContain('s1');
});

it('turn_cancelled 丢失时兜底仍然生效：10s 后本地解锁会话', async () => {
  const store = useAppStore();
  store.currentSessionName = 's2';
  store.runningSessions = ['s2'];
  store.running = true;
  (window as any).pywebview = {
    api: {
      stop_current_turn: vi.fn().mockResolvedValue({ ok: true }),
      get_sessions: vi.fn().mockResolvedValue([]),
    },
  };

  await store.stopCurrentTurn();
  // 后端没回 turn_cancelled：会话一直停在运行态
  expect(store.runningSessions).toContain('s2');

  vi.advanceTimersByTime(9999);
  expect(store.runningSessions).toContain('s2');

  vi.advanceTimersByTime(1);
  expect(store.runningSessions).not.toContain('s2');
  expect(store.running).toBe(false);
});