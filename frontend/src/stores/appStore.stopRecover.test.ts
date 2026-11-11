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

function mockApi(session: string) {
  const api = {
    stop_current_turn: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
    get_token_usage: vi.fn().mockResolvedValue(null),
    get_nav_index: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };
  return { store: useAppStore(), api, session };
}

it('运行态与 runningSessions 不一致时，点停止后兜底仍必须解锁界面', async () => {
  // 复现路径：顶层 running 被置真，但运行会话表里没有它（历史见过的事件错序/
  // 终态事件缺 session_name 等边界都会留下这种半残状态）。
  // 旧实现在这种状态下点停止：后端受理（ok:true），前端 running 不变，
  // 10s 兜底只看 runningSessions → 不解锁，表现为「一直点停止也停不下来」。
  const { store } = mockApi('s1');
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = [];

  await store.stopCurrentTurn();

  // 停止请求受理后，与服务端真正在跑的会话表对齐：本会话不在里面就是本地状态失真
  expect(store.running).toBe(false);

  // 兜底定时器同样必须兜住这种不一致：即使前面没自愈，10s 也要恢复可发送
  store.running = true;
  store.runningSessions = [];
  await store.stopCurrentTurn();
  vi.advanceTimersByTime(10000);
  expect(store.running).toBe(false);
});

it('正常停止（会话确实在运行表里）不提前解锁：等后端终态事件', async () => {
  const { store } = mockApi('s1');
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = ['s1'];

  await store.stopCurrentTurn();

  // 后端回合可能还在收尾（工具在途），不能点完就本地强制结束
  expect(store.running).toBe(true);
  expect(store.runningSessions).toContain('s1');

  // 真终态到达后才解锁
  store.handleBridgeEvent({ type: 'turn_cancelled', data: { session_name: 's1' } } as any);
  expect(store.running).toBe(false);
  expect(store.runningSessions).not.toContain('s1');
});

it('后端回报 idle（该会话已无活跃回合）：运行表里的残留条目也要立刻清掉', async () => {
  // 回合早已正常收尾、但终态事件在前端丢了：后端已无活跃回合，
  // 停止请求回 idle=true。此时不能再等事件（永远不会有），点一次就得恢复。
  const { store, api } = mockApi('s1');
  api.stop_current_turn.mockResolvedValue({ ok: true, idle: true });
  store.currentSessionName = 's1';
  store.running = true;
  store.runningSessions = ['s1'];

  await store.stopCurrentTurn();

  expect(store.running).toBe(false);
  expect(store.runningSessions).not.toContain('s1');
});