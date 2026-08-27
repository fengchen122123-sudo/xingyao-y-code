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
  // 但目标会话 runtime 已记录新模型，切回去时直接采用
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
