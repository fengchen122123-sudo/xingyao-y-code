import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

it('model_changed 事件更新当前会话的模型状态与思考强度', () => {
  const store = useAppStore();
  store.currentSessionName = '会话一';

  store.handleBridgeEvent({
    type: 'model_changed',
    data: {
      session_name: '会话一',
      model_id: 'k3',
      model_name: 'k3',
      reasoning: {
        kind: 'effort', visible: true, disabled: false,
        disabled_reason: '', choice: 'max', options: [],
      },
    },
  });

  expect(store.activeModelId).toBe('k3');
  expect(store.activeModelName).toBe('k3');
  expect(store.reasoning.choice).toBe('max');
  // 事件同时落入会话 runtime，切走再切回时模型不回退
  expect(store._getSessionRuntime('会话一').modelId).toBe('k3');
  expect(store._getSessionRuntime('会话一').modelName).toBe('k3');
});

it('model_changed 不串会话：其他会话的切换不影响当前显示状态', () => {
  const store = useAppStore();
  store.currentSessionName = '会话一';
  store.activeModelId = 'glm-5.3-flash';
  store.activeModelName = 'glm-5.3-flash';

  store.handleBridgeEvent({
    type: 'model_changed',
    data: { session_name: '另一个会话', model_id: 'k3', model_name: 'k3' },
  });

  expect(store.activeModelId).toBe('glm-5.3-flash');
  expect(store.activeModelName).toBe('glm-5.3-flash');
  expect(store._getSessionRuntime('另一个会话').modelId).toBe('k3');
});

it('model_changed 递增会话模型版本号，使在途 switchSession 旧快照失效', () => {
  const store = useAppStore();
  store.currentSessionName = '会话一';
  const before = store._sessionModelVersion['会话一'] || 0;

  store.handleBridgeEvent({
    type: 'model_changed',
    data: { session_name: '会话一', model_id: 'k3', model_name: 'k3' },
  });

  expect(store._sessionModelVersion['会话一']).toBe(before + 1);
});

it('turn_start 固定本轮执行模型，回合中切换底部模型不影响本轮与后续轮次显示', () => {
  const store = useAppStore();
  store.currentSessionName = '会话一';
  store.activeModelId = 'm-ds';
  store.activeModelName = 'deepseek-v4.1-flash-1017';

  store.handleBridgeEvent({
    type: 'turn_start',
    data: {
      session_name: '会话一',
      user_text: '请检查',
      model_id: 'm-ds',
      model_name: 'deepseek-v4.1-flash-1017',
    },
  });

  // 回合执行中途用户在底部切换成另一个模型
  store.activeModelId = 'm-glm';
  store.activeModelName = 'glm-5.3-flash';

  store.handleBridgeEvent({
    type: 'token_delta',
    data: { session_name: '会话一', text: '第一步' },
  });
  store.handleBridgeEvent({
    type: 'round_started',
    data: { session_name: '会话一' },
  });
  store.handleBridgeEvent({
    type: 'message_final',
    data: {
      session_name: '会话一',
      content: '最终回答',
      model_id: 'm-ds',
      model_name: 'deepseek-v4.1-flash-1017',
    },
  });

  const assistants = store.messages.filter(m => m.role === 'assistant');
  expect(assistants).toHaveLength(2);
  expect(assistants[0].model_name).toBe('deepseek-v4.1-flash-1017');
  expect(assistants[1].model_name).toBe('deepseek-v4.1-flash-1017');
});
