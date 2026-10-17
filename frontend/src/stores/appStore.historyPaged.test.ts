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

// ---------------------------------------------------------------------------
// 分页状态自愈：坏状态（has_more=false + 游标为空）必须能自己恢复
// 真实故障（2026-10-11）：读库失败被后端吞成「空页 + has_more=False」，前端写进
// 会话分页状态并随会话保存 → 滚到顶不加载更早消息、点最上面一个红点毫无反应，
// 切走再切回依然失效（用户原话「怎么都无法加载到最上面」）。
// ---------------------------------------------------------------------------

it('游标被清空时用已加载消息里最旧的 _msg_id 自愈，翻页重新可用', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  // 只剩尾部一页的坏状态：游标为 null、has_more 被写成 false
  store.messages = [
    { role: 'user', content: '尾部提问', _msg_id: 41221 },
    { role: 'assistant', content: '尾部回答', tool_calls: [], execution_state: 'completed', _msg_id: 41222 },
  ] as any;
  store.historyHasMore = false;
  store.historyOldestId = null;
  const loadOlder = vi.fn().mockResolvedValue({
    ok: true,
    messages: [
      { role: 'user', content: '更早提问', _msg_id: 41000 },
      { role: 'assistant', content: '更早回答', tool_calls: [], _msg_id: 41001 },
    ],
    has_more: true,
    oldest_id: 41000,
  });
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  await store.loadOlderMessages();

  // 不再被守卫短路：以最旧 _msg_id 当游标真发了一次请求，并 prepend 到头部
  expect(loadOlder).toHaveBeenCalledTimes(1);
  expect(loadOlder).toHaveBeenCalledWith('s1', 41221);
  expect(store.messages.map(m => m.content)).toEqual([
    '更早提问', '更早回答', '尾部提问', '尾部回答',
  ]);
  expect(store.historyHasMore).toBe(true);
  expect(store.historyOldestId).toBe(41000);
});

it('翻页失败（ok=false）保留原分页状态，不写成「没有更早历史」', async () => {
  const store = useAppStore();
  seedSession(store);
  const loadOlder = vi.fn().mockResolvedValue({
    ok: false, messages: [], has_more: false, oldest_id: null, msg: 'database is locked',
  });
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };

  await store.loadOlderMessages();

  // 状态原样保留：下次滚动/跳转仍会重试，而不是永久关掉翻页
  expect(store.historyHasMore).toBe(true);
  expect(store.historyOldestId).toBe(100);
  expect(store.historyLoading).toBe(false);
  await store.loadOlderMessages();
  expect(loadOlder).toHaveBeenCalledTimes(2);
});

// ---------------------------------------------------------------------------
// 回归：跨会话事件不得回滚当前会话刚翻页加载的历史与游标
// 真实故障（2026-10-15，用户第二次反馈）：长会话向上翻几页后，后台会话的
// 事件（token_usage / turn_start 等）到达——事件管线临时绑定目标会话时
// 没有先快照当前会话，finally 回绑拿的是旧快照，刚 prepend 的历史与推进的
// 游标被丢弃，表现为「向上翻一段就被弹回、再也加载不出来，点红点也上不去」。
// ---------------------------------------------------------------------------

it('跨会话事件不回滚当前会话刚翻页加载的历史与游标', async () => {
  const store = useAppStore();
  seedSession(store);          // 当前会话 s1：尾部一页，游标 100
  // 建立 s1 的 runtime 快照（等价于切进 s1 时的状态）
  store._getSessionRuntime('s1');
  store._captureRuntime('s1');

  // 用户向上翻页：prepend 一页更早历史，游标推进到 60（顶层实时进度，尚未入快照）
  const loadOlder = vi.fn().mockResolvedValue({
    ok: true,
    messages: [
      { role: 'user', content: '第 49 条', _msg_id: 60 },
      { role: 'assistant', content: '回答 49', tool_calls: [], _msg_id: 61 },
    ],
    has_more: true,
    oldest_id: 60,
  });
  (window as any).pywebview = { api: { load_older_messages: loadOlder } };
  await store.loadOlderMessages();
  expect(store.messages.map(m => m.content)[0]).toBe('第 49 条');
  expect(store.historyOldestId).toBe(60);

  // 后台会话 B 的回合事件到达：跨会话 → 临时绑定 B 再回绑 s1
  store.handleBridgeEvent({ type: 'turn_start', data: { session_name: 'B', user_text: '后台任务' } } as any);

  // 刚翻页加载的历史与推进的游标不得被 s1 的旧快照回滚
  expect(store.messages.map(m => m.content)).toEqual([
    '第 49 条', '回答 49', '第 51 条', '回答 51',
  ]);
  expect(store.historyOldestId).toBe(60);
  expect(store.historyHasMore).toBe(true);
});