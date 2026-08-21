import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

it('message_queue 事件全量替换该会话排队队列', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';

  store.handleBridgeEvent({
    type: 'message_queue',
    data: { session_name: 's1', queue: [{ id: 'q1', text: '第一条', attachments: ['d:/a.txt'] }] },
  } as any);

  expect(store.currentMessageQueue).toHaveLength(1);
  expect(store.currentMessageQueue[0]).toEqual({ id: 'q1', text: '第一条', attachments: ['d:/a.txt'] });

  // 空快照清空队列（补发完成后）
  store.handleBridgeEvent({ type: 'message_queue', data: { session_name: 's1', queue: [] } } as any);
  expect(store.currentMessageQueue).toHaveLength(0);
  expect('s1' in store.messageQueues).toBe(false);
});

it('运行中发送消息进入排队：不乐观渲染气泡、不清运行态', async () => {
  const store = useAppStore();
  const api = {
    send_user_message: vi.fn().mockResolvedValue({ ok: true, queued: true }),
  };
  (window as any).pywebview = { api };
  store.currentSessionName = 's1';
  store.runningSessions = ['s1'];
  store.sessionRuntimeByName['s1'] = {
    messages: [{ role: 'user', content: '上一条' }],
    streamingText: '', thinkingText: '', running: true, subagentTasks: [],
  };

  await store.sendMessage('排队消息', []);

  // 未乐观渲染用户气泡 / running 占位（等补发回合的 turn_start 再渲染）
  const msgs = store.sessionRuntimeByName['s1'].messages;
  expect(msgs).toHaveLength(1);
  expect(msgs.some(m => m.content === '排队消息')).toBe(false);
  // 原回合仍在运行：运行态不被清除（停止按钮不消失、后续发送继续入队）
  expect(store.runningSessions).toContain('s1');
  expect(store.sessionRuntimeByName['s1'].running).toBe(true);
});

it('取消排队与插队调用对应桥方法', async () => {
  const store = useAppStore();
  const api = {
    cancel_queued_message: vi.fn().mockResolvedValue({ ok: true, removed: 1 }),
    jump_queue: vi.fn().mockResolvedValue({ ok: true }),
  };
  (window as any).pywebview = { api };

  await store.cancelQueuedMessage('s1', 'q1');
  expect(api.cancel_queued_message).toHaveBeenCalledWith('s1', 'q1');

  await store.jumpQueue('s1', 'q1');
  expect(api.jump_queue).toHaveBeenCalledWith('s1', 'q1');
});
