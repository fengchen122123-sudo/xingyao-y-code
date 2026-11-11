import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 秘书调度台 open_session 桥事件：后端切换当前会话后推事件，前端必须复用
// switchSession 那条视图切换路径（切当前会话、拉历史、刷新生效名）。
// mock 方式与 appStore.switchLoading.test.ts 一致：直接挂 window.pywebview.api。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

/** 挂一个最小可用桥：switch_session + get_active_agent_name（生效名来源） */
function mockApi(switchImpl: (name: string, workdir?: string) => Promise<unknown>) {
  const switchSession = vi.fn().mockImplementation(switchImpl);
  const getActiveAgentName = vi.fn().mockResolvedValue({ agent_name: '妖妖' });
  (window as any).pywebview = {
    api: { switch_session: switchSession, get_active_agent_name: getActiveAgentName },
  };
  return { switchSession, getActiveAgentName };
}

it('open_session：复用 switchSession 切到目标会话、拉历史并刷新生效名', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_old';
  store.currentSessionDisplayName = '旧会话';
  const { switchSession } = mockApi(async () => ({
    messages: [{ role: 'user', content: '目标会话历史' }],
    workdir: 'D:\\proj\\imom',
  }));

  store.handleBridgeEvent({
    type: 'open_session',
    data: { session_name: 'session_ab12cd', display_name: '妖妖', workdir: 'D:\\proj\\imom' },
  } as any);

  // 走的就是用户点会话那条路：目标会话名 + 无 workdir（归属目录以后端为准）
  await vi.waitFor(() => expect(switchSession).toHaveBeenCalledWith('session_ab12cd', undefined));
  await vi.waitFor(() => expect(store.agentName).toBe('妖妖'));

  expect(store.currentSessionName).toBe('session_ab12cd');
  expect(store.currentSessionDisplayName).toBe('妖妖');
  // 消息区被目标会话历史重置（不是另写一套切换逻辑的空壳状态）
  expect(store.messages.map(m => m.content)).toEqual(['目标会话历史']);
  expect(store.sessionSwitching).toBe(false);
});

it('open_session：payload 缺 session_name 时不发切换请求、不抛错', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_old';
  const { switchSession } = mockApi(async () => ({ messages: [] }));

  expect(() => {
    store.handleBridgeEvent({ type: 'open_session', data: { display_name: '妖妖' } } as any);
  }).not.toThrow();
  // 等一轮微任务：确认没有被 void 出去的切换请求悄悄发出
  await Promise.resolve();
  await Promise.resolve();

  expect(switchSession).not.toHaveBeenCalled();
  expect(store.currentSessionName).toBe('session_old');
});

it('open_session：切换抛错时不崩、只留一条静默日志', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_old';
  // 极端情况（切换逻辑本身抛错）：桥事件管线不得被异常打断
  const boom = vi.spyOn(store as any, 'switchSession').mockRejectedValue(new Error('切换炸了'));
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

  expect(() => {
    store.handleBridgeEvent({ type: 'open_session', data: { session_name: 'session_x' } } as any);
  }).not.toThrow();

  await vi.waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
  expect(String(warn.mock.calls[0][0])).toContain('open_session');
  boom.mockRestore();
  warn.mockRestore();
});
