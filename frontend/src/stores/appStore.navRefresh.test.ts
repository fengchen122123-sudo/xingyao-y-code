import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

const tick = () => new Promise(resolve => window.setTimeout(resolve, 30));

function apiWith(index: Record<string, Array<{ id: number; preview: string }>>) {
  return {
    get_session_nav_index: vi.fn(async (name: string) => {
      const list = index[name] || [];
      return { ok: true, items: list, total: list.length };
    }),
    switch_session: vi.fn(async () => ({
      ok: true,
      messages: [],
      history_has_more: false,
      history_oldest_id: null,
      is_global: true,
      workdir: 'GLOBAL',
    })),
    get_sessions: vi.fn(async () => ({ ok: true, groups: [] })),
    get_token_usage: vi.fn(async () => ({ ok: true })),
  };
}

it('turn_finish 后红点索引刷新，包含刚发出的最后一条消息', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  const index = { A: [{ id: 1, preview: '第一条' }] };
  (window as any).pywebview = { api: apiWith(index) };

  await store.refreshNavIndex();
  expect(store.navItems).toHaveLength(1);

  index.A = [...index.A, { id: 2, preview: '修改更新记录' }];
  store.handleBridgeEvent({ type: 'turn_finish', data: { session_name: 'A' } } as any);
  await tick();

  expect(store.navItems).toHaveLength(2);
  expect(store.navItems[1].preview).toBe('修改更新记录');
});

it('后台会话 turn_finish：不在该会话时，其自身红点索引也要刷新', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  const index: Record<string, Array<{ id: number; preview: string }>> = {
    A: [{ id: 1, preview: 'A1' }],
    B: [{ id: 11, preview: 'B1' }],
  };
  (window as any).pywebview = { api: apiWith(index) };
  await store.refreshNavIndex();

  const rtB = (store as any)._getSessionRuntime('B');
  rtB.navItems = [{ id: 11, preview: 'B1' }];
  rtB.messages = [{ role: 'user', content: 'B 的历史' }];
  index.B = [...index.B, { id: 12, preview: 'B 的最后一条' }];

  store.handleBridgeEvent({ type: 'turn_finish', data: { session_name: 'B' } } as any);
  await tick();

  expect(rtB.navItems).toHaveLength(2);
  expect(store.navItems).toHaveLength(1);
});

it('切回已有内存缓存的会话时，红点索引也要刷新（避免缺最后一条）', async () => {
  const store = useAppStore();
  store.initialized = true;
  const index: Record<string, Array<{ id: number; preview: string }>> = {
    A: [{ id: 1, preview: 'A1' }],
    B: [{ id: 11, preview: 'B1' }],
  };
  (window as any).pywebview = { api: apiWith(index) };

  store.currentSessionName = 'A';
  store.messages = [{ role: 'user', content: 'A 的历史' }] as any;
  const rtA = (store as any)._getSessionRuntime('A');
  rtA.messages = store.messages;
  rtA.navItems = [{ id: 1, preview: 'A1' }];

  await store.switchSession('B');
  index.A = [...index.A, { id: 2, preview: 'A 的最后一条' }];
  await store.switchSession('A');
  await tick();

  expect(store.navItems).toHaveLength(2);
  expect(store.navItems[1].preview).toBe('A 的最后一条');
});

it('turn_cancelled 后红点索引也应刷新（新消息已落库）', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  const index = { A: [{ id: 1, preview: '第一条' }] };
  (window as any).pywebview = { api: apiWith(index) };

  await store.refreshNavIndex();
  index.A = [...index.A, { id: 2, preview: '被取消的提问' }];
  store.handleBridgeEvent({ type: 'turn_cancelled', data: { session_name: 'A' } } as any);
  await tick();

  expect(store.navItems).toHaveLength(2);
});

it('turn_error 后红点索引也应刷新（新消息已落库）', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  const index = { A: [{ id: 1, preview: '第一条' }] };
  (window as any).pywebview = { api: apiWith(index) };

  await store.refreshNavIndex();
  index.A = [...index.A, { id: 2, preview: '报错前的提问' }];
  store.handleBridgeEvent({ type: 'turn_error', data: { session_name: 'A', message: 'boom' } } as any);
  await tick();

  expect(store.navItems).toHaveLength(2);
});
