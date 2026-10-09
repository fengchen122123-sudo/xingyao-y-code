import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';

// 会话级 UI 偏好（IDE 模式 / 对话模式）的后端持久化与恢复。
// mock 方式与 appStore.workspace.test.ts 一致：直接挂 window.pywebview.api。

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('toggleIdeMode 把新模式立即写回后端（按会话名分桶）', async () => {
  const saveSessionUi = vi.fn().mockResolvedValue({ ok: true, ui: { version: 1, ide_mode: true } });
  (window as any).pywebview = { api: { save_session_ui: saveSessionUi } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';

  store.toggleIdeMode();

  expect(store.ideMode()).toBe(true);
  expect(saveSessionUi).toHaveBeenCalledWith('session_abc', { version: 1, ide_mode: true });

  // 切回对话模式同样落盘（否则重开软件会恢复成上一次的 IDE 模式）
  saveSessionUi.mockClear();
  store.toggleIdeMode();

  expect(store.ideMode()).toBe(false);
  expect(saveSessionUi).toHaveBeenCalledWith('session_abc', { version: 1, ide_mode: false });
});

it('启动初始化：后端 ide_mode=true 时当前会话恢复为 IDE 模式', async () => {
  const fakeApi = {
    get_initial_state: async () => ({
      models: [],
      providers: [],
      sessions: [],
      workdir: 'GLOBAL',
      current_session: 'session_abc',
      messages: [],
    }),
    get_session_ui: async () => ({ ok: true, ui: { version: 1, ide_mode: true } }),
    get_system_settings: async () => ({}),
    get_token_usage: async () => ({}),
    get_sessions: async () => [],
    check_update: async () => ({}),
  };
  (window as any).pywebview = { api: fakeApi };
  const store = useAppStore();

  await store.initFromBackend();

  // 恢复动作不阻塞启动流程（void），等它落地后断言
  await vi.waitFor(() => expect(store.ideMode()).toBe(true));
  expect(store.currentSessionName).toBe('session_abc');
});

it('启动初始化：后端 ide_mode=false 时保持对话模式，且不写本地记录', async () => {
  const fakeApi = {
    get_initial_state: async () => ({
      models: [],
      providers: [],
      sessions: [],
      workdir: 'GLOBAL',
      current_session: 'session_chat',
      messages: [],
    }),
    get_session_ui: async () => ({ ok: true, ui: { version: 1, ide_mode: false } }),
    get_system_settings: async () => ({}),
    get_token_usage: async () => ({}),
    get_sessions: async () => [],
    check_update: async () => ({}),
  };
  (window as any).pywebview = { api: fakeApi };
  const store = useAppStore();

  await store.initFromBackend();

  expect(store.ideMode()).toBe(false);
  // 只在「为真」时落记录：不写入才能让后续会话切换的权威同步仍能读到后端值
  expect('session_chat' in store.ideModeBySession).toBe(false);
});

it('切会话后按后端值恢复该会话的 IDE 模式', async () => {
  const getSessionUi = vi.fn().mockResolvedValue({ ok: true, ui: { version: 1, ide_mode: true } });
  const switchSession = vi.fn().mockResolvedValue({ messages: [] });
  (window as any).pywebview = { api: { switch_session: switchSession, get_session_ui: getSessionUi } };
  const store = useAppStore();

  await store.switchSession('B', '会话B');

  expect(getSessionUi).toHaveBeenCalledWith('B');
  await vi.waitFor(() => expect(store.ideMode()).toBe(true));
  expect(store.currentSessionName).toBe('B');
});

it('在途的恢复响应不覆盖用户已经手动切过的模式', async () => {
  let resolveGet: ((v: any) => void) | null = null;
  const getSessionUi = vi.fn().mockImplementation(
    () => new Promise(resolve => { resolveGet = resolve; }),
  );
  (window as any).pywebview = { api: { get_session_ui: getSessionUi } };
  const store = useAppStore();
  store.currentSessionName = 'A';

  const p = store.restoreIdeMode('A');
  // 请求在途期间用户手动切到 IDE 模式又切回对话模式：本地 false 是用户意图
  store.toggleIdeMode();
  store.toggleIdeMode();

  // 后端返回的是切换前的旧值 true
  resolveGet!({ ok: true, ui: { version: 1, ide_mode: true } });
  await p;

  // 旧值不得把用户的实时切换覆盖掉
  expect(store.ideMode()).toBe(false);
});

it('桥缺失时静默降级：不抛错，模式切换仍可用', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';

  expect(() => store.toggleIdeMode()).not.toThrow();
  expect(store.ideMode()).toBe(true);

  await expect(store.restoreIdeMode('s2')).resolves.toBeUndefined();
  // 无桥即无后端值：不为该会话写记录，按本地状态（对话模式）呈现
  expect('s2' in store.ideModeBySession).toBe(false);
  store.currentSessionName = 's2';
  expect(store.ideMode()).toBe(false);
});

it('桥 reject 时静默降级：不抛错，读不到就不写记录', async () => {
  const saveSessionUi = vi.fn().mockRejectedValue(new Error('save boom'));
  const getSessionUi = vi.fn().mockRejectedValue(new Error('get boom'));
  (window as any).pywebview = { api: { save_session_ui: saveSessionUi, get_session_ui: getSessionUi } };
  const store = useAppStore();
  store.currentSessionName = 's1';

  expect(() => store.toggleIdeMode()).not.toThrow();
  expect(store.ideMode()).toBe(true);

  await expect(store.restoreIdeMode('s2')).resolves.toBeUndefined();

  expect(getSessionUi).toHaveBeenCalledWith('s2');
  expect('s2' in store.ideModeBySession).toBe(false);
});

it('草稿会话（空串桶）拿到真实会话名后把 IDE 模式迁移过去', async () => {
  const saveSessionUi = vi.fn().mockResolvedValue({ ok: true, ui: { version: 1, ide_mode: true } });
  const api = {
    save_session_ui: saveSessionUi,
    send_user_message: vi.fn().mockResolvedValue({ ok: true }),
    get_sessions: vi.fn().mockResolvedValue([]),
  };
  (window as any).pywebview = { api };
  const store = useAppStore();
  // 进入草稿会话（尚未落库，统一挂在 '' 桶）
  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  store.toggleIdeMode();
  expect(store.ideModeBySession['']).toBe(true);
  saveSessionUi.mockClear();

  // 首条消息落库：草稿拿到真实会话名
  await store.sendMessage('你好', []);

  const realName = store.currentSessionName;
  expect(realName).toMatch(/^session_[0-9a-f]{6}$/);
  // '' 桶必须清掉：否则下一个新会话一进来就"继承"这个模式
  expect(store.ideModeBySession['']).toBeUndefined();
  expect(store.ideModeBySession[realName]).toBe(true);
  expect(store.ideMode()).toBe(true);
  // 迁移后按新名落盘（旧名的后端记录删不掉，只能留着）
  expect(saveSessionUi).toHaveBeenCalledWith(realName, { version: 1, ide_mode: true });
});

it('重命名会话时 IDE 模式桶随后端最终会话名迁移', async () => {
  const saveSessionUi = vi.fn().mockResolvedValue({ ok: true, ui: { version: 1, ide_mode: true } });
  const renameSession = vi.fn().mockResolvedValue({ ok: true, name: '新标题', sessions: [] });
  (window as any).pywebview = { api: { rename_session: renameSession, save_session_ui: saveSessionUi } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.ideModeBySession['session_abc'] = true;

  await store.renameSession('session_abc', '新标题');

  expect(store.currentSessionName).toBe('新标题');
  expect(store.ideModeBySession['session_abc']).toBeUndefined();
  expect(store.ideModeBySession['新标题']).toBe(true);
  expect(store.ideMode()).toBe(true);
  expect(saveSessionUi).toHaveBeenCalledWith('新标题', { version: 1, ide_mode: true });
});

it('后端自动改名（session_renamed 事件）后 IDE 模式跟着新名走', async () => {
  // 首条消息后后端会把 session_xxxxxx 改成消息标题并推 session_renamed，
  // 不迁移的话模式留在旧名下，重开软件恢复不出来
  const saveSessionUi = vi.fn().mockResolvedValue({ ok: true, ui: { version: 1, ide_mode: true } });
  (window as any).pywebview = { api: { save_session_ui: saveSessionUi } };
  const store = useAppStore();
  store.currentSessionName = 'session_abc';
  store.ideModeBySession['session_abc'] = true;

  store.handleBridgeEvent({
    type: 'session_renamed',
    data: { old: 'session_abc', new: '帮我写个排序算法' },
  } as any);

  expect(store.currentSessionName).toBe('帮我写个排序算法');
  expect(store.ideModeBySession['session_abc']).toBeUndefined();
  expect(store.ideModeBySession['帮我写个排序算法']).toBe(true);
  expect(store.ideMode()).toBe(true);
  expect(saveSessionUi).toHaveBeenCalledWith('帮我写个排序算法', { version: 1, ide_mode: true });
});
