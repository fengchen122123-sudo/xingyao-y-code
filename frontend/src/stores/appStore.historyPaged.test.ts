import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 会话历史分页懒加载：store 分页状态 + loadOlderMessages action。
// mock 方式与 appStore.toolEvents.test.ts 一致：直接挂 window.pywebview.api。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

function seedSession(store: ReturnType<typeof useAppStore>) {
  store.currentSessionName = 's1';
  store.messages = [
    { role: 'user', content: '第 51 条' },
    { role: 'assistant', content: '回答 51', tool_calls: [], execution_state: 'completed' },
  ] as any;
  store.historyHasMore = true;
  store.historyOldestId = 100;
}

it('switchSession 成功分支写入历史分页状态', async () => {
  const store = useAppStore();
  (window as any).pywebview = {
    api: {
      switch_session: vi.fn().mockResolvedValue({
        messages: [{ role: 'user', content: '历史' }],
        is_global: true,
        workdir: 'GLOBAL',
        history_has_more: true,
        history_oldest_id: 42,
      }),
    },
  };
  await store.switchSession('s1', '会话1');
  expect(store.historyHasMore).toBe(true);
  expect(store.historyOldestId).toBe(42);
  expect(store.historyLoading).toBe(false);
});

it('switchSession 响应缺分页字段时回退默认值', async () => {
  const store = useAppStore();
  store.historyHasMore = true;
  store.historyOldestId = 7;
  (window as any).pywebview = {
    api: {
      switch_session: vi.fn().mockResolvedValue({
        messages: [], is_global: true, workdir: 'GLOBAL',
      }),
    },
  };
  await store.switchSession('s1', '会话1');
  expect(store.historyHasMore).toBe(false);
  expect(store.historyOldestId).toBeNull();
});

it('loadOlderMessages prepend 更早历史到头部且保持顺序', async () => {
  const store = useAppStore();
  seedSession(store);
  const loadOlder = vi.fn().mockResolvedValue({
    ok: true,
    messages: [
      { role: 'user', content: '第 49 条' },
      { role: 'assistant', content: '回答 49', tool_calls: [] },
      { role: 'user', content: '第 50 条' },
      { role: 'assistant', content: '回答 50', tool_calls: [] },
    ],
    has_more: true,
    oldest_id: 60,
  });
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  await store.loadOlderMessages();

  expect(loadOlder).toHaveBeenCalledTimes(1);
  expect(loadOlder).toHaveBeenCalledWith('s1', 100);
  expect(store.messages.map(m => m.content)).toEqual([
    '第 49 条', '回答 49', '第 50 条', '回答 50', '第 51 条', '回答 51',
  ]);
  expect(store.historyHasMore).toBe(true);
  expect(store.historyOldestId).toBe(60);
  expect(store.historyLoading).toBe(false);
});

it('prepend 的旧历史走与 switchSession 相同的 tool_call 配对管线', async () => {
  const store = useAppStore();
  seedSession(store);
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '看看 README' },
          {
            role: 'assistant', content: '已读取',
            tool_calls: [
              { id: 'call-a', function: { name: 'read_file', arguments: { file_path: 'README.md' } } },
            ],
          },
          { role: 'tool', tool_call_id: 'call-a', name: 'read_file', content: '# Y-CODE' },
        ],
        has_more: false,
        oldest_id: null,
      }),
    },
  };

  await store.loadOlderMessages();

  // role=tool 消息被折叠进助手消息，不单独出现在列表里
  expect(store.messages).toHaveLength(4);
  const assistant = store.messages[1];
  expect(assistant.role).toBe('assistant');
  expect(assistant.tool_calls).toHaveLength(1);
  expect(assistant.tool_calls![0].name).toBe('read_file');
  expect(assistant.tool_calls![0].result?.result).toBe('# Y-CODE');
  expect(assistant.execution_state).toBe('completed');
});

it('单飞守卫：并发两次调用只发一次请求', async () => {
  const store = useAppStore();
  seedSession(store);
  let resolveReq: ((v: any) => void) | null = null;
  const loadOlder = vi.fn().mockImplementation(
    () => new Promise(resolve => { resolveReq = resolve; }),
  );
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  const p1 = store.loadOlderMessages();
  const p2 = store.loadOlderMessages();
  expect(store.historyLoading).toBe(true);

  resolveReq!({ ok: true, messages: [{ role: 'user', content: '更早' }], has_more: false, oldest_id: null });
  await Promise.all([p1, p2]);

  expect(loadOlder).toHaveBeenCalledTimes(1);
  expect(store.messages).toHaveLength(3);
  expect(store.historyLoading).toBe(false);
});

it('竞态丢弃：请求期间切换会话，响应不污染新会话', async () => {
  const store = useAppStore();
  seedSession(store);
  let resolveReq: ((v: any) => void) | null = null;
  const loadOlder = vi.fn().mockImplementation(
    () => new Promise(resolve => { resolveReq = resolve; }),
  );
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  const p = store.loadOlderMessages();
  // 请求在途期间用户切到别的会话
  store.currentSessionName = 's2';
  store.messages = [{ role: 'user', content: '新会话消息' }] as any;

  resolveReq!({ ok: true, messages: [{ role: 'user', content: '旧会话更早历史' }], has_more: true, oldest_id: 10 });
  await p;

  expect(store.messages.map(m => m.content)).toEqual(['新会话消息']);
  // 分页游标不被旧会话响应覆盖
  expect(store.historyOldestId).toBe(100);
  expect(store.historyHasMore).toBe(true);
});

it('has_more=false 后再次调用不再发请求', async () => {
  const store = useAppStore();
  seedSession(store);
  const loadOlder = vi.fn().mockResolvedValue({
    ok: true,
    messages: [{ role: 'user', content: '最早一条' }],
    has_more: false,
    oldest_id: null,
  });
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  await store.loadOlderMessages();
  expect(store.historyHasMore).toBe(false);
  expect(store.historyOldestId).toBeNull();

  await store.loadOlderMessages();
  expect(loadOlder).toHaveBeenCalledTimes(1);
});

it('无游标或后端不可用时直接返回不发请求', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.historyHasMore = true;
  store.historyOldestId = null;
  const loadOlder = vi.fn();
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };
  await store.loadOlderMessages();
  expect(loadOlder).not.toHaveBeenCalled();

  // 后端不可用
  store.historyOldestId = 5;
  (window as any).pywebview = undefined;
  await expect(store.loadOlderMessages()).resolves.toBeUndefined();
  expect(store.historyLoading).toBe(false);
});