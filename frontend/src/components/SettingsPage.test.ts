import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
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

    await wrapper.get('[data-test="add-provider-toggle"]').trigger('click');
    await wrapper.get('[data-test="preset-deepseek"]').trigger('click');

    expect(wrapper.find('[data-test="preset-api-key"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Responses (/responses)');
    expect(wrapper.find('[data-test="custom-name"]').exists()).toBe(false);
  });

  it('shows configured providers first and expands add-provider choices on demand', async () => {
    const wrapper = mount(SettingsPage);

    expect(wrapper.find('[data-test="provider-p1"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="preset-deepseek"]').exists()).toBe(false);

    await wrapper.get('[data-test="add-provider-toggle"]').trigger('click');

    const orderedActions = wrapper.findAll('[data-test]').map(item => item.attributes('data-test'));
    expect(orderedActions.indexOf('provider-p1')).toBeLessThan(orderedActions.indexOf('add-provider-toggle'));
    expect(orderedActions.indexOf('add-provider-toggle')).toBeLessThan(orderedActions.indexOf('preset-deepseek'));
    expect(orderedActions.indexOf('preset-deepseek')).toBeLessThan(orderedActions.indexOf('add-custom-provider'));
  });

  it('switches from model configuration to token statistics', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'loadTokenStatistics').mockResolvedValue(null);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="token-statistics-nav"]').trigger('click');

    expect(wrapper.find('.model-settings-card').exists()).toBe(false);
    expect(wrapper.find('[data-test="token-statistics-page"]').exists()).toBe(true);
  });

  it('opens the GUI appearance page from settings navigation', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="appearance-nav"]').trigger('click');

    expect(wrapper.get('[data-test="gui-theme-settings"]').isVisible()).toBe(true);
  });

  it('opens the extensible system settings page from settings navigation', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="system-settings-nav"]').trigger('click');

    expect(wrapper.get('[data-test="system-settings-page"]').isVisible()).toBe(true);
  });

  it('opens a model editor with the multimodal choice', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');

    expect(wrapper.get('[data-test="model-modal"]').isVisible()).toBe(true);
    expect(wrapper.get('[data-test="model-supports-vision"]').attributes('type')).toBe('checkbox');
    expect(wrapper.text()).toContain('是否支持多模态');
  });

  it('keeps the model editor open when the backdrop is clicked', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('.modal-mask').trigger('click');

    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(true);
  });

  it.each([
    ['close', '.modal-head button'],
    ['cancel', '.modal-actions .secondary-button'],
  ])('closes the model editor through the %s action', async (_action, selector) => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get(selector).trigger('click');

    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(false);
  });

  it('closes the model editor after saving succeeds', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'addModel').mockResolvedValue({ ok: true } as never);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('[data-test="model-modal"] input[placeholder="模型 ID"]').setValue('saved-model');
    await wrapper.get('[data-test="model-modal"]').trigger('submit');

    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(false);
  });

  it('selects the first configured provider when the backend snapshot arrives after mount', async () => {
    const store = useAppStore();
    const presets = JSON.parse(JSON.stringify(store.modelPresets));
    const providers = JSON.parse(JSON.stringify(store.providers));
    store.modelPresets = [];
    store.providers = [];
    const wrapper = mount(SettingsPage);

    store.modelPresets = presets;
    store.providers = providers;
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="provider-p1"].active').exists()).toBe(true);
    expect(wrapper.find('[data-test="preset-api-key"]').exists()).toBe(false);
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

  it('reveals the configured API key when clicking 显示', async () => {
    const store = useAppStore();
    const spy = vi.spyOn(store, 'getProviderApiKey').mockResolvedValue('sk-real-key');
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    const input = wrapper.get('[data-test="provider-api-key"]');
    expect(input.attributes('type')).toBe('password');

    await wrapper.get('[data-test="toggle-provider-key"]').trigger('click');
    await flushPromises();

    expect(spy).toHaveBeenCalledWith('p1');
    expect((input.element as HTMLInputElement).value).toBe('sk-real-key');
    expect(input.attributes('type')).toBe('text');

    await wrapper.get('[data-test="toggle-provider-key"]').trigger('click');
    await flushPromises();
    expect(input.attributes('type')).toBe('password');
  });

  it('does not reveal when the key cannot be loaded', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'getProviderApiKey').mockResolvedValue(null);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="toggle-provider-key"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="provider-api-key"]').attributes('type')).toBe('password');
  });

  it('copies the API key through the copy button', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'getProviderApiKey').mockResolvedValue('sk-real-key');
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="copy-provider-key"]').trigger('click');
    await flushPromises();

    expect(writeText).toHaveBeenCalledWith('sk-real-key');
    // 复制后输入框仍为掩码态，仅剪贴板拿到明文
    expect(wrapper.get('[data-test="provider-api-key"]').attributes('type')).toBe('password');
  });
});
