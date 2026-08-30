import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 模型与会话的绑定关系：每个会话（含尚未落库的 pending 草稿）都有自己
// 独立的模型选择，互不影响；切换会话时即时恢复该会话上次使用的模型。

function reasoningSnapshot(choice = 'default') {
  return {
    kind: 'effort' as const,
    visible: true,
    disabled: false,
    disabled_reason: '',
    choice,
    options: [
      { value: 'default', label: '默认' },
      { value: 'off', label: '关闭' },
      { value: 'medium', label: '中' },
      { value: 'high', label: '高' },
    ],
  };
}

function mockBridgeApi(overrides: Record<string, unknown> = {}) {
  (window as any).pywebview = {
    api: {
      set_model: vi.fn(async (modelId: string) => ({
        ok: true,
        active_model_id: modelId,
        active_model_name: `模型-${modelId}`,
        reasoning: reasoningSnapshot(),
      })),
      switch_session: vi.fn(async () => ({
        messages: [],
        workdir: 'GLOBAL',
        is_global: true,
        sessions: [],
        active_model_id: 'm-global',
        active_model_name: '模型-m-global',
        reasoning: reasoningSnapshot(),
      })),
      get_reasoning_snapshot: vi.fn(async () => ({
        ok: true,
        reasoning: reasoningSnapshot('medium'),
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

it('pending 新会话中切换模型不产生提示消息（空状态页不跳转）', async () => {
  const store = useAppStore();
  store.models = [{ id: 'm-kimi', name: '模型-m-kimi' } as any];
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
  store.models = [{ id: 'm-kimi', name: '模型-m-kimi' } as any];
  store.pendingWorkdir = 'GLOBAL';
  store.currentSessionName = '';

  await store.setModel('m-kimi');

  const runtime = store.sessionRuntimeByName['__pending__:GLOBAL'];
  expect(runtime.modelId).toBe('m-kimi');
  expect(runtime.modelName).toBe('模型-m-kimi');
  expect(store.reasoning.choice).toBe('medium');
});

it('已有真实对话的会话切换模型仍显示切换提示', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.messages = [
    { role: 'user', content: '你好' },
    { role: 'assistant', content: '你好呀', execution_state: 'completed' },
  ];
  store._captureRuntime('session-a');

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
  expect(store.draftModelId).toBe('m-deepseek');
});

it('未落库新会话切到已有会话后再次新建会恢复文字和附件草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.activeModelId = 'm-deepseek';
  store.activeModelName = '模型-m-deepseek';

  await store.createGlobalSession();
  const pendingRuntime = store._getSessionRuntime();
  pendingRuntime.composerText = '尚未发送的汉字';
  pendingRuntime.composerAttachments = [
    { path: 'C:/tmp/draft.png', name: 'draft.png', ext: '.png', size: 12 },
  ];

  await store.switchSession('session-a', '会话 A', 'GLOBAL');
  expect(store._getSessionRuntime().composerText || '').toBe('');

  await store.createGlobalSession();
  expect(store._getSessionRuntime().composerText).toBe('尚未发送的汉字');
  expect(store._getSessionRuntime().composerAttachments?.map(a => a.path)).toEqual([
    'C:/tmp/draft.png',
  ]);
});

it('项目新会话首条消息把当前窗口模型提交给后端', async () => {
  const send = vi.fn(async (..._args: unknown[]) => ({ ok: true }));
  mockBridgeApi({ send_user_message: send });
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.activeModelId = 'm-current';
  store.activeModelName = '模型-m-current';
  store.workdir = 'E:\\project';
  store.currentWorkdirLabel = 'project';

  await store.createSessionInGroup('E:\\project');
  await store.sendMessage('第一条消息');

  expect(send).toHaveBeenCalledTimes(1);
  expect(send.mock.calls[0][4]).toBe('m-current');
});

it('switchSession 恢复后端返回的会话模型', async () => {
  mockBridgeApi({
    switch_session: vi.fn(async () => ({
      messages: [], workdir: 'GLOBAL', is_global: true, sessions: [],
      active_model_id: 'm-session', active_model_name: '模型-m-session',
      reasoning: reasoningSnapshot('high'),
    })),
  });
  const store = useAppStore();

  await store.switchSession('session-a');

  expect(store.activeModelId).toBe('m-session');
  expect(store.activeModelName).toBe('模型-m-session');
  expect(store.reasoning.choice).toBe('high');
});

it('switchSession 在途期间 setModel 后旧响应不覆盖本地选择', async () => {
  let release!: (value: any) => void;
  const response = new Promise<any>((resolve) => { release = resolve; });
  mockBridgeApi({ switch_session: vi.fn(() => response) });
  const store = useAppStore();
  store.currentSessionName = 'session-a';
  store.messages = [];

  const switching = store.switchSession('session-a');
  await Promise.resolve();
  await store.setModel('m-new');
  release({
    messages: [], workdir: 'GLOBAL', is_global: true, sessions: [],
    active_model_id: 'm-old', active_model_name: '模型-m-old',
    reasoning: reasoningSnapshot(),
  });
  await switching;

  expect(store.activeModelId).toBe('m-new');
  expect(store.activeModelName).toBe('模型-m-new');
});

it('setReasoningChoice 应用后端规范化后的完整快照', async () => {
  const setChoice = vi.fn(async () => ({
    ok: true,
    reasoning: reasoningSnapshot('medium'),
  }));
  mockBridgeApi({ set_reasoning_choice: setChoice });
  const store = useAppStore();
  store.activeModelId = 'm-qwen';

  await store.setReasoningChoice('medium');

  expect(setChoice).toHaveBeenCalledWith('m-qwen', 'medium');
  expect(store.reasoning).toEqual(reasoningSnapshot('medium'));
});

it('setReasoningChoice 失败时恢复之前的完整快照', async () => {
  mockBridgeApi({
    set_reasoning_choice: vi.fn(async () => ({ ok: false, msg: '不支持' })),
  });
  const store = useAppStore();
  store.activeModelId = 'm-deepseek';
  store.reasoning = reasoningSnapshot('high') as any;

  await store.setReasoningChoice('off');

  expect(store.reasoning).toEqual(reasoningSnapshot('high'));
});

it('同一模型连续选择时旧成功响应不能覆盖最后一次选择', async () => {
  let resolveHigh!: (value: any) => void;
  let resolveMax!: (value: any) => void;
  mockBridgeApi({
    set_reasoning_choice: vi.fn((_modelId: string, choice: string) => new Promise(resolve => {
      if (choice === 'high') resolveHigh = resolve;
      else resolveMax = resolve;
    })),
  });
  const store = useAppStore();
  store.activeModelId = 'm1';
  store.reasoning = reasoningSnapshot('default') as any;

  const highRequest = store.setReasoningChoice('high');
  const maxRequest = store.setReasoningChoice('max');
  resolveMax({ ok: true, reasoning: reasoningSnapshot('max') });
  await maxRequest;
  resolveHigh({ ok: true, reasoning: reasoningSnapshot('high') });
  await highRequest;

  expect(store.reasoning.choice).toBe('max');
});

it('同一模型的旧失败响应不能回滚后续已成功选择', async () => {
  let rejectOld!: (value: any) => void;
  let resolveNew!: (value: any) => void;
  mockBridgeApi({
    set_reasoning_choice: vi.fn((_modelId: string, choice: string) => new Promise(resolve => {
      if (choice === 'high') rejectOld = resolve;
      else resolveNew = resolve;
    })),
  });
  const store = useAppStore();
  store.activeModelId = 'm1';
  store.reasoning = reasoningSnapshot('default') as any;

  const oldRequest = store.setReasoningChoice('high');
  const newRequest = store.setReasoningChoice('max');
  resolveNew({ ok: true, reasoning: reasoningSnapshot('max') });
  await newRequest;
  rejectOld({ ok: false, msg: '旧请求失败' });
  await oldRequest;

  expect(store.reasoning.choice).toBe('max');
});

it('同一模型的旧异常不能把最新成功状态标记为断线', async () => {
  let rejectOld!: (reason: any) => void;
  let resolveNew!: (value: any) => void;
  const notify = vi.fn();
  mockBridgeApi({
    set_reasoning_choice: vi.fn((_modelId: string, choice: string) => new Promise((resolve, reject) => {
      if (choice === 'high') rejectOld = reject;
      else resolveNew = resolve;
    })),
  });
  const store = useAppStore();
  store.activeModelId = 'm1';
  store.reasoning = reasoningSnapshot('default') as any;
  store.connected = true;
  store.notifyChat = notify as any;

  const oldRequest = store.setReasoningChoice('high');
  const newRequest = store.setReasoningChoice('max');
  resolveNew({ ok: true, reasoning: reasoningSnapshot('max') });
  await newRequest;
  rejectOld(new Error('旧请求异常'));
  await oldRequest;

  expect(store.reasoning.choice).toBe('max');
  expect(store.connected).toBe(true);
  expect(notify).not.toHaveBeenCalledWith(expect.stringContaining('旧请求异常'));
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
