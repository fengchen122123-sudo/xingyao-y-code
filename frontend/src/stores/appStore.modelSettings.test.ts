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

    expect(addModel).toHaveBeenCalledWith('p1', 'model-two', 300000, true);
    expect(result?.ok).toBe(true);
    expect(store.models[0].name).toBe('model-two');
  });
});
