import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 红点导航全量索引：store navItems 状态 + refreshNavIndex + firstLoadedMsgId。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

it('refreshNavIndex 拉取全量索引写入 navItems', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  const getNav = vi.fn().mockResolvedValue({
    ok: true,
    items: [
      { id: 1, preview: '第一条' },
      { id: 5, preview: '第二条' },
    ],
    total: 2,
  });
  (window as any).pywebview = { api: { get_session_nav_index: getNav } };

  await store.refreshNavIndex();

  expect(getNav).toHaveBeenCalledWith('s1');
  expect(store.navItems).toHaveLength(2);
  expect(store.navItems[0]).toEqual({ id: 1, preview: '第一条' });
});

it('refreshNavIndex 竞态丢弃：在途切会话不写入', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  let resolveReq: ((v: any) => void) | null = null;
  (window as any).pywebview = {
    api: {
      get_session_nav_index: vi.fn().mockImplementation(
        () => new Promise(resolve => { resolveReq = resolve; }),
      ),
    },
  };

  const p = store.refreshNavIndex();
  store.currentSessionName = 's2';
  resolveReq!({ ok: true, items: [{ id: 1, preview: '旧会话' }], total: 1 });
  await p;

  expect(store.navItems).toHaveLength(0);
});

it('refreshNavIndex 后端不可用/接口缺失时静默不炸', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  (window as any).pywebview = undefined;
  await expect(store.refreshNavIndex()).resolves.toBeUndefined();

  (window as any).pywebview = { api: {} }; // 旧后端无此接口
  await expect(store.refreshNavIndex()).resolves.toBeUndefined();
});

it('firstLoadedMsgId 返回消息流中首条带 _msg_id 的消息 id', () => {
  const store = useAppStore();
  store.messages = [
    { role: 'user', content: '无 id' },
    { role: 'assistant', content: '有 id', _msg_id: 42 },
    { role: 'user', content: '后面的', _msg_id: 43 },
  ] as any;
  expect(store.firstLoadedMsgId).toBe(42);

  store.messages = [{ role: 'user', content: '全无 id' }] as any;
  expect(store.firstLoadedMsgId).toBeNull();
});
