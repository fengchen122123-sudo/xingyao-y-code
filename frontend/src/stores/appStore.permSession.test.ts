import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 权限档位与会话的绑定关系：在对话页选择档位只对当前会话生效（含尚未
// 落库的 pending 草稿），切换会话时恢复各自的档位；全局默认只在未绑定
// 会话兜底，不再被单会话的选择覆盖。

function mockBridgeApi(overrides: Record<string, unknown> = {}) {
  (window as any).pywebview = {
    api: {
      set_permission_mode: vi.fn(async (mode: string, _sessionName?: string) => ({
        ok: true,
        permission_mode: mode,
      })),
      switch_session: vi.fn(async () => ({
        messages: [], workdir: 'GLOBAL', is_global: true, sessions: [],
      })),
      send_user_message: vi.fn(async () => ({ ok: true })),
      get_sessions: vi.fn(async () => []),
      ...overrides,
    },
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  mockBridgeApi();
});

it('权限选择只绑定当前会话并写入该会话 runtime', async () => {
  const setPerm = vi.fn(async (mode: string, _sessionName?: string) => ({
    ok: true,
    permission_mode: mode,
  }));
  mockBridgeApi({ set_permission_mode: setPerm });
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  await store.setPermissionMode('plan');

  expect(setPerm).toHaveBeenCalledWith('plan', 'session-a');
  expect(store.permissionMode).toBe('plan');
  expect(store._getSessionRuntime('session-a').permMode).toBe('plan');
});

it('权限档位随会话 runtime 保存与恢复，切会话立即生效（不等后端响应）', async () => {
  const store = useAppStore();
  // 会话 A 选完全访问
  store.currentSessionName = 'session-a';
  store.permissionMode = 'full';
  store._captureRuntime('session-a');

  // 切到 B：B 从未选过档位 → 保持当前值，由后端 switch_session 响应校正
  store.currentSessionName = 'session-b';
  store._bindSessionRuntime('session-b');
  expect(store.permissionMode).toBe('full');

  // 在 B 选计划模式并捕获
  store.permissionMode = 'plan';
  store._captureRuntime('session-b');

  // 切回 A：恢复完全访问，不受 B 影响
  store.currentSessionName = 'session-a';
  store._bindSessionRuntime('session-a');
  expect(store.permissionMode).toBe('full');

  // 再切回 B：恢复计划模式
  store.currentSessionName = 'session-b';
  store._bindSessionRuntime('session-b');
  expect(store.permissionMode).toBe('plan');
});

it('switchSession 恢复后端返回的会话权限档位', async () => {
  mockBridgeApi({
    switch_session: vi.fn(async () => ({
      messages: [], workdir: 'GLOBAL', is_global: true, sessions: [],
      permission_mode: 'accept_edits',
    })),
  });
  const store = useAppStore();

  await store.switchSession('session-a');

  expect(store.permissionMode).toBe('accept_edits');
  expect(store._getSessionRuntime('session-a').permMode).toBe('accept_edits');
});

it('项目新会话首条消息把会话权限档位提交给后端', async () => {
  const send = vi.fn(async (..._args: unknown[]) => ({ ok: true }));
  mockBridgeApi({ send_user_message: send });
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.activeModelId = 'm-current';
  store.workdir = 'E:\\project';
  store.currentWorkdirLabel = 'project';

  await store.createSessionInGroup('E:\\project');
  store._getSessionRuntime().permMode = 'plan';
  await store.sendMessage('第一条消息');

  expect(send).toHaveBeenCalledTimes(1);
  expect(send.mock.calls[0][5]).toBe('plan');
});
