import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SettingsPage from './SettingsPage.vue';
import { useAppStore } from '../stores/appStore';


describe('SettingsPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    const store = useAppStore();
    store.$patch({
      modelProtocols: [
        { id: 'anthropic', label: 'Anthropic Messages (/v1/messages)', path: '/v1/messages' },
        { id: 'openai', label: 'Chat Completions (/chat/completions)', path: '/chat/completions' },
        { id: 'responses', label: 'Responses (/responses)', path: '/responses' },
      ],
      modelPresets: [
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
    });
  });

  it('shows preset providers and only asks for an API key', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="preset-deepseek"]').trigger('click');

    expect(wrapper.find('[data-test="preset-api-key"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Responses (/responses)');
    expect(wrapper.find('[data-test="custom-name"]').exists()).toBe(false);
  });

  it('opens a model editor with the multimodal choice', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');

    expect(wrapper.get('[data-test="model-modal"]').isVisible()).toBe(true);
    expect(wrapper.get('[data-test="model-supports-vision"]').attributes('type')).toBe('checkbox');
    expect(wrapper.text()).toContain('是否支持多模态');
  });

  it('selects the first preset when the backend snapshot arrives after mount', async () => {
    const store = useAppStore();
    const presets = JSON.parse(JSON.stringify(store.modelPresets));
    const providers = JSON.parse(JSON.stringify(store.providers));
    store.modelPresets = [];
    store.providers = [];
    const wrapper = mount(SettingsPage);

    store.modelPresets = presets;
    store.providers = providers;
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('预置厂商信息由 CLI 共享目录维护');
    expect(wrapper.find('[data-test="preset-api-key"]').exists()).toBe(true);
  });

  it('keeps the model editor open when saving fails', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'addModel').mockResolvedValue(null);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('[data-test="model-modal"] input[placeholder="模型 ID"]').setValue('broken-model');
    await wrapper.get('[data-test="model-modal"]').trigger('submit');

    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(true);
  });
});
