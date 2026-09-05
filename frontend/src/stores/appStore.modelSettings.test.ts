import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


const snapshot = {
  protocols: [
    { id: 'anthropic', label: 'Anthropic Messages (/v1/messages)', path: '/v1/messages' },
    { id: 'openai', label: 'Chat Completions (/chat/completions)', path: '/chat/completions' },
    { id: 'responses', label: 'Responses (/responses)', path: '/responses' },
  ],
  presets: [
    {
      id: 'deepseek',
      name: '深度求索',
      base_url: 'https://api.deepseek.com',
      protocol: 'responses',
      models: [
        { name: 'deepseek-v4-pro', context_window: 1000000, supports_vision: false },
      ],
    },
  ],
  providers: [
    {
      id: 'p1',
      name: '测试厂商',
      base_url: 'https://api.example.com',
      protocol: 'responses',
      preset_id: '',
      has_api_key: true,
      models: [
        {
          id: 'm1',
          name: 'model-one',
          provider_id: 'p1',
          provider_name: '测试厂商',
          context_window: 200000,
          supports_vision: true,
          supports_function_calling: false,
          computer_protocol: 'none',
        },
      ],
    },
  ],
  models: [
    {
      id: 'm1',
      name: 'model-one',
      provider_id: 'p1',
      provider_name: '测试厂商',
      context_window: 200000,
      supports_vision: true,
      supports_function_calling: false,
      computer_protocol: 'none',
    },
  ],
  active_model_id: 'm1',
};


describe('model settings store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('opens settings and replaces local data with the backend snapshot', async () => {
    const getModelSettings = vi.fn().mockResolvedValue(snapshot);
    (window as any).pywebview = { api: { get_model_settings: getModelSettings } };
    const store = useAppStore();

    await store.openModelSettings();

    expect(store.settingsOpen).toBe(true);
    expect(getModelSettings).toHaveBeenCalledOnce();
    expect(store.modelProtocols.at(-1)?.id).toBe('responses');
    expect(store.modelPresets[0].id).toBe('deepseek');
    expect(store.models[0].supports_vision).toBe(true);
  });

  it('sends multimodal model data and applies the returned snapshot', async () => {
    const next = structuredClone(snapshot);
    next.models[0].name = 'model-two';
    next.providers[0].models[0].name = 'model-two';
    const addModel = vi.fn().mockResolvedValue({ ok: true, settings: next });
    (window as any).pywebview = { api: { add_model: addModel } };
    const store = useAppStore();

    const result = await store.addModel('p1', 'model-two', 300000, true);

    // 缺省协议 = ''（自动）：视觉+工具调用双勾的模型无需显式配置即可操控
    expect(addModel).toHaveBeenCalledWith('p1', 'model-two', 300000, true, false, '');
    expect(result?.ok).toBe(true);
    expect(store.models[0].name).toBe('model-two');
  });

  it('sends capability fields when adding a generic computer control model', async () => {
    const next = structuredClone(snapshot);
    next.models[0].name = 'model-generic';
    next.models[0].supports_function_calling = true;
    next.models[0].computer_protocol = 'generic';
    next.providers[0].models[0].name = 'model-generic';
    next.providers[0].models[0].supports_function_calling = true;
    next.providers[0].models[0].computer_protocol = 'generic';
    const addModel = vi.fn().mockResolvedValue({ ok: true, settings: next });
    (window as any).pywebview = { api: { add_model: addModel } };
    const store = useAppStore();

    const result = await store.addModel('p1', 'model-generic', 300000, true, true, 'generic');

    expect(addModel).toHaveBeenCalledWith('p1', 'model-generic', 300000, true, true, 'generic');
    expect(result?.ok).toBe(true);
    expect(store.models[0].computer_protocol).toBe('generic');
    expect(store.models[0].supports_function_calling).toBe(true);
  });

  it('sends capability fields when updating a model', async () => {
    const next = structuredClone(snapshot);
    next.models[0].computer_protocol = 'none';
    next.providers[0].models[0].computer_protocol = 'none';
    const updateModel = vi.fn().mockResolvedValue({ ok: true, settings: next });
    (window as any).pywebview = { api: { update_model: updateModel } };
    const store = useAppStore();

    const result = await store.updateModel('p1', 'm1', 'model-one', 200000, true, true, 'none');

    expect(updateModel).toHaveBeenCalledWith('p1', 'm1', 'model-one', 200000, true, true, 'none');
    expect(result?.ok).toBe(true);
    expect(store.models[0].computer_protocol).toBe('none');
  });

  it('keeps the session-bound active model when the snapshot global id differs', () => {
    // 回归：GUI 按会话绑定模型，快照里的 active_model_id 是全局默认值，
    // 可能属于另一厂商的同名模型；直接覆盖会让界面选中态跳厂商
    const store = useAppStore();
    const twoProviderSnapshot = {
      ...structuredClone(snapshot),
      models: [
        { id: 'm-openai', name: 'k3', provider_id: 'p1', provider_name: '厂商A(OpenAI)', context_window: 1000000, supports_vision: true },
        { id: 'm-anthropic', name: 'k3', provider_id: 'p2', provider_name: '厂商B(Anthropic)', context_window: 1000000, supports_vision: true },
      ],
      active_model_id: 'm-openai',
    };
    store.activeModelId = 'm-anthropic';

    store.applyModelSettings(twoProviderSnapshot as any);

    expect(store.activeModelId).toBe('m-anthropic');
  });

  it('falls back to the snapshot active model when the current one no longer exists', () => {
    // 当前模型已被删除（或尚未选择）时，仍用快照的全局默认值兜底
    const store = useAppStore();
    store.activeModelId = 'm-deleted';

    store.applyModelSettings(structuredClone(snapshot) as any);

    expect(store.activeModelId).toBe('m1');
  });
});
