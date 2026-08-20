import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 模型与会话的绑定关系：每个会话（含尚未落库的 pending 草稿）都有自己
// 独立的模型选择，互不影响；切换会话时即时恢复该会话上次使用的模型。

function mockBridgeApi(overrides: Record<string, unknown> = {}) {
  (window as any).pywebview = {
    api: {
      set_model: vi.fn(async (modelId: string) => ({
        ok: true,
        active_model_id: modelId,
        active_model_name: `模型-${modelId}`,
        reasoning_effort: 'default',
      })),
      ...overrides,
    },
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  mockBridgeApi();
});

it('pending 新会话中切换模型不产生提示消息（空状态页不跳转）', async () => {
  const store = useAppStore();
  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';
  store.messages = [];

  await store.setModel('m-kimi');

  expect(store.activeModelId).toBe('m-kimi');
  expect(store.activeModelName).toBe('模型-m-kimi');
  // 没有任何真实对话内容时，不得插入"已切换活动模型"提示，
  // 否则 messages 非空会让空状态页（KimiEmptyState）消失，表现为页面跳转
  expect(store.messages.length).toBe(0);
});

it('pending 新会话切换模型写入 pending runtime，不回写其它会话', async () => {
  const store = useAppStore();
  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';

  await store.setModel('m-kimi');

  const runtime = store.sessionRuntimeByName['__pending__:GLOBAL'];
  expect(runtime.modelId).toBe('m-kimi');
  expect(runtime.modelName).toBe('模型-m-kimi');
});

it('已有真实对话的会话切换模型仍显示切换提示', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.messages = [
    { role: 'user', content: '你好' },
    { role: 'assistant', content: '你好呀', execution_state: 'completed' },
  ];

  await store.setModel('m-kimi');

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(String(last.content)).toContain('已切换活动模型');
});

it('模型选择随会话 runtime 保存与恢复，切会话立即生效（不等后端响应）', async () => {
  const store = useAppStore();
  // 会话 A 绑定 deepseek
  store.currentSessionName = 'session-a';
  store.activeModelId = 'm-deepseek';
  store.activeModelName = '模型-m-deepseek';
  await store.setModel('m-deepseek-2');
  expect(store.activeModelId).toBe('m-deepseek-2');

  // 手动捕获并切换到会话 B（B 从未选过模型 → 保持全局默认）
  store._captureRuntime('session-a');
  store.activeModelId = 'm-global';
  store.activeModelName = '模型-m-global';
  store.currentSessionName = 'session-b';
  store._bindSessionRuntime('session-b');
  expect(store.activeModelId).toBe('m-global');

  // 在 B 换成 kimi 并捕获
  await store.setModel('m-kimi');
  store._captureRuntime('session-b');

  // 切回 A：立即恢复 deepseek-2，不受 B 影响
  store.currentSessionName = 'session-a';
  store._bindSessionRuntime('session-a');
  expect(store.activeModelId).toBe('m-deepseek-2');
  expect(store.activeModelName).toBe('模型-m-deepseek-2');

  // 再切回 B：恢复 kimi
  store.currentSessionName = 'session-b';
  store._bindSessionRuntime('session-b');
  expect(store.activeModelId).toBe('m-kimi');
});

it('新建会话沿用当前会话正在使用的模型', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.activeModelId = 'm-deepseek';
  store.activeModelName = '模型-m-deepseek';

  await store.createGlobalSession();

  expect(store.currentSessionName).toBe('');
  expect(store.pendingWorkdir).toBe('GLOBAL');
  expect(store.activeModelId).toBe('m-deepseek');
  expect(store.activeModelName).toBe('模型-m-deepseek');
});

it('会话模型被删除后回落到后端配置模型', async () => {
  const store = useAppStore();
  // A 绑定的模型（runtime 中的 m-gone）已在模型管理里被删除
  store.sessionRuntimeByName['session-a'] = {
    messages: [],
    streamingText: '',
    thinkingText: '',
    running: false,
    subagentTasks: [],
    modelId: 'm-gone',
    modelName: '已删除模型',
  };
  store.currentSessionName = 'session-a';
  store.models = [{ id: 'm-new' } as any];
  store.activeModelId = 'm-global';
  store.activeModelName = '模型-m-global';

  store._bindSessionRuntime('session-a');

  expect(store.activeModelId).toBe('m-global');
});
