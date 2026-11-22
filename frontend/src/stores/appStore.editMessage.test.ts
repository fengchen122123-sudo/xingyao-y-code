import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore, type ChatMessage } from './appStore';

const api = {
  edit_user_message: vi.fn(),
};

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
  api.edit_user_message.mockReset();
});

function seedHistory(store: ReturnType<typeof useAppStore>) {
  store.currentSessionName = 's1';
  store.messages = [
    { role: 'user', content: '第一条', _msg_id: 1 },
    { role: 'assistant', content: '回答一', _msg_id: 2 },
    { role: 'user', content: '第二条', _msg_id: 3 },
    { role: 'assistant', content: '回答二', _msg_id: 4 },
  ] as ChatMessage[];
}

it('rewinds local history and resends the edited text', async () => {
  const store = useAppStore();
  seedHistory(store);
  api.edit_user_message.mockResolvedValue({ ok: true, removed: 2 });
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const ok = await store.editUserMessage(store.messages[2], '改过的第二条');

  expect(ok).toBe(true);
  expect(api.edit_user_message).toHaveBeenCalledWith('s1', 3, '改过的第二条');
  expect(store.messages.map(m => m.content)).toEqual(['第一条', '回答一']);
  expect(sendSpy).toHaveBeenCalledWith('改过的第二条', []);
});

it('keeps local history untouched when backend rejects', async () => {
  const store = useAppStore();
  seedHistory(store);
  api.edit_user_message.mockResolvedValue({ ok: false, msg: '会话正在生成，请先停止或等待完成' });
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const ok = await store.editUserMessage(store.messages[2], '改过的第二条');

  expect(ok).toBe(false);
  // 历史必须原样保留；错误提示以独立气泡追加（不覆盖也不截断历史）
  expect(store.messages.slice(0, 4).map(m => m.content)).toEqual(
    ['第一条', '回答一', '第二条', '回答二'],
  );
  expect(store.messages.some(m => m.role === 'error')).toBe(true);
  expect(sendSpy).not.toHaveBeenCalled();
});

it('switching session during the edit await must not resend into the new session', async () => {
  const store = useAppStore();
  seedHistory(store);
  store._captureRuntime('s1');
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);
  api.edit_user_message.mockImplementation(async () => {
    store.currentSessionName = 's2';
    store.messages = [{ role: 'user', content: '另一个会话的历史', _msg_id: 9 }] as ChatMessage[];
    return { ok: true, removed: 2 };
  });
  const edited = store.messages[2];

  const ok = await store.editUserMessage(edited, '改过的第二条');

  expect(ok).toBe(false);
  expect(sendSpy).not.toHaveBeenCalled();
  // 截断必须落在发起编辑的 s1，而不是当前查看的 s2
  const rt = store._getSessionRuntime('s1');
  expect(rt.messages.filter(m => m.role !== 'error' && m.role !== 'notice').map(m => m.content))
    .toEqual(['第一条', '回答一']);
  expect(rt.messages.some(m => m.role === 'error')).toBe(true);
  expect(store.messages.map(m => m.content)).toEqual(['另一个会话的历史']);
});

it('refreshes the nav rail index after truncating locally', async () => {
  const store = useAppStore();
  seedHistory(store);
  api.edit_user_message.mockResolvedValue({ ok: true, removed: 2 });
  vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);
  const navSpy = vi.spyOn(store, 'refreshNavIndex').mockResolvedValue(undefined);

  const ok = await store.editUserMessage(store.messages[2], '改过的第二条');

  expect(ok).toBe(true);
  expect(navSpy).toHaveBeenCalledWith('s1');
});

it('keeps the edit and asks for a manual resend when the resend throws', async () => {
  const store = useAppStore();
  seedHistory(store);
  api.edit_user_message.mockResolvedValue({ ok: true, removed: 2 });
  vi.spyOn(store, 'sendMessage').mockRejectedValue(new Error('网络断了'));

  // 编辑已落库，重发失败不能整体算失败（否则用户会以为改动丢了）
  await expect(store.editUserMessage(store.messages[2], '改过的第二条')).resolves.toBe(false);

  expect(store.messages.filter(m => m.role !== 'error' && m.role !== 'notice').map(m => m.content))
    .toEqual(['第一条', '回答一']);
  const notices = store.messages.filter(m => m.role === 'error').map(m => m.content || '');
  expect(notices.some(c => c.includes('编辑已保存') && c.includes('手动重发'))).toBe(true);
});

it('blank text is rejected without calling backend', async () => {
  const store = useAppStore();
  seedHistory(store);
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const ok = await store.editUserMessage(store.messages[2], '   ');

  expect(ok).toBe(false);
  expect(api.edit_user_message).not.toHaveBeenCalled();
  expect(sendSpy).not.toHaveBeenCalled();
});

it('reports an unsupported hint when the backend lacks edit_user_message', async () => {
  const store = useAppStore();
  seedHistory(store);
  (window as any).pywebview = { api: {} };

  const ok = await store.editUserMessage(store.messages[2], '改过的第二条');

  expect(ok).toBe(false);
  const notices = store.messages.filter(m => m.role === 'error').map(m => m.content || '');
  expect(notices.some(c => c.includes('不支持编辑'))).toBe(true);
});

it('reports a failure notice when the backend call throws', async () => {
  const store = useAppStore();
  seedHistory(store);
  api.edit_user_message.mockRejectedValue(new Error('桥断了'));
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const ok = await store.editUserMessage(store.messages[2], '改过的第二条');

  expect(ok).toBe(false);
  expect(sendSpy).not.toHaveBeenCalled();
  // 截断未获后端确认：本地历史必须原样保留
  expect(store.messages.slice(0, 4).map(m => m.content)).toEqual(
    ['第一条', '回答一', '第二条', '回答二'],
  );
  const notices = store.messages.filter(m => m.role === 'error').map(m => m.content || '');
  expect(notices.some(c => c.includes('编辑失败'))).toBe(true);
});

it('message without _msg_id is rejected without calling backend', async () => {
  const store = useAppStore();
  seedHistory(store);
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const ok = await store.editUserMessage({ role: 'user', content: '未入库' }, '改过的内容');

  expect(ok).toBe(false);
  expect(api.edit_user_message).not.toHaveBeenCalled();
  expect(sendSpy).not.toHaveBeenCalled();
});

it('turn_start backfills the database id onto the newest user bubble', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.messages = [
    { role: 'user', content: '新提问' },
    {
      role: 'assistant', content: '', reasoning_content: '',
      tool_calls: [], execution_state: 'running',
    },
  ] as ChatMessage[];

  store.handleBridgeEvent({
    type: 'turn_start',
    data: {
      session_name: 's1',
      session_id: 3,
      turn_id: 't1',
      user_text: '新提问',
      user_msg_id: 77,
    },
  } as any);

  expect(store.messages[0]._msg_id).toBe(77);
});
