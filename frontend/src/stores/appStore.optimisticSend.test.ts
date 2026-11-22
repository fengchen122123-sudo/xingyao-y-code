import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

it('排队期间新会话立即显示用户消息，不等 A 完成', async () => {
  const store = useAppStore();

  const api = {
    send_user_message: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };

  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  store.currentSessionDisplayName = '新对话';

  await store.sendMessage('帮我写个排序算法', []);

  const runtime = store.sessionRuntimeByName[store.currentSessionName];
  const contents = (runtime?.messages || []).map(m => `${m.role}:${m.content}`);
  expect(contents).toContain('user:帮我写个排序算法');
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

  store.handleBridgeEvent({
    type: 'turn_start',
    data: { session_name: sessionName, user_text: '你好' },
  } as any);

  const after = store.sessionRuntimeByName[sessionName].messages.length;
  expect(after).toBe(before);
});

it('send_user_message 返回 need_login 时拉起基元律动登录弹窗', async () => {
  const store = useAppStore();
  const api = {
    send_user_message: vi.fn().mockResolvedValue({
      ok: false,
      msg: '基元律动登录已失效，请重新登录后再使用该模型',
      need_login: true,
    }),
    get_sessions: vi.fn().mockResolvedValue([]),
    tr_auth_status: vi.fn().mockResolvedValue({ ok: true, logged_in: false }),
  };
  (window as any).pywebview = { api };

  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  expect(store.trLoginModal.show).toBe(false);

  await store.sendMessage('你好', []);

  expect(store.trLoginModal.show).toBe(true);
  expect(api.tr_auth_status).toHaveBeenCalledTimes(1);
});
