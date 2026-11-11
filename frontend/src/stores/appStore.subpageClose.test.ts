import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 子页面（技能/MCP/插件/自动化）打开时点侧栏"+"或会话条目：
// 会话创建/切换必须收起子页面回到聊天视图，否则状态已切换但界面
// 停在子页，看起来像"点了没反应"。

function mockBridgeApi(overrides: Record<string, unknown> = {}) {
  (window as any).pywebview = {
    api: {
      switch_session: vi.fn(async () => ({
        messages: [], workdir: 'GLOBAL', is_global: true, sessions: [],
      })),
      send_user_message: vi.fn(async () => ({ ok: true })),
      get_sessions: vi.fn(async () => []),
      get_file_tree: vi.fn(async () => ({ ok: true, file_tree: [] })),
      ...overrides,
    },
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  mockBridgeApi();
});

it('子页面打开时新建自由对话收起子页面', async () => {
  const store = useAppStore();
  store.subpage = { show: true, type: 'automations' };

  await store.createGlobalSession();

  expect(store.subpage.show).toBe(false);
  expect(store.pendingWorkdir).toBe('GLOBAL');
  expect(store.currentSessionName).toBe('');
});

it('子页面打开时在项目分组新建会话收起子页面', async () => {
  const store = useAppStore();
  store.subpage = { show: true, type: 'skills' };

  await store.createSessionInGroup('E:\\proj');

  expect(store.subpage.show).toBe(false);
  expect(store.pendingWorkdir).toBe('E:\\proj');
  expect(store.workdir).toBe('E:\\proj');
});

it('子页面打开时切换会话收起子页面', async () => {
  const store = useAppStore();
  store.subpage = { show: true, type: 'mcp' };

  await store.switchSession('session-a');

  expect(store.subpage.show).toBe(false);
  expect(store.currentSessionName).toBe('session-a');
});
