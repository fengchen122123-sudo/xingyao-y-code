import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

// 模拟串行排队：A 占用后端锁，B 发消息后 turn_start 迟迟不到（只收到 turn_queued）
it('排队期间新会话立即显示用户消息，不等 A 完成', async () => {
  const store = useAppStore();

  // 构造一个可用后端：send_user_message 成功但暂不产生任何 turn 事件（模拟排队）
  const api = {
    send_user_message: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };

  // 进入 pending 新对话
  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  store.currentSessionDisplayName = '新对话';

  await store.sendMessage('帮我写个排序算法', []);

  // 用户消息应立即出现在该新会话里（乐观渲染），而不是空白
  const runtime = store.sessionRuntimeByName[store.currentSessionName];
  const contents = (runtime?.messages || []).map(m => `${m.role}:${m.content}`);
  expect(contents).toContain('user:帮我写个排序算法');
  // 末尾应有 running 占位的 assistant 气泡
  const last = runtime?.messages[runtime.messages.length - 1];
  expect(last?.role).toBe('assistant');
  expect(last?.execution_state).toBe('running');
});

it('turn_start 到达时不重复渲染用户消息与占位', async () => {
  const store = useAppStore();
  const api = {
    send_user_message: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };

  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  await store.sendMessage('你好', []);
  const sessionName = store.currentSessionName;
  const before = store.sessionRuntimeByName[sessionName].messages.length;

  // 后端拿到锁，turn_start 到来
  store.handleBridgeEvent({
    type: 'turn_start',
    data: { session_name: sessionName, user_text: '你好' },
  } as any);

  const after = store.sessionRuntimeByName[sessionName].messages.length;
  // 不重复添加：消息数不变（用户消息已有 + 占位复用）
  expect(after).toBe(before);
});
