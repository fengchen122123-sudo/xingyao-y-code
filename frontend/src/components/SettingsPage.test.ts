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
              supports_function_calling: false,
              computer_protocol: '',
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

  it('switches to the legacy token statistics view', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'loadTokenStatistics').mockResolvedValue(null);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="token-statistics-legacy-nav"]').trigger('click');

    expect(wrapper.find('[data-test="token-statistics-page-legacy"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="token-statistics-page"]').exists()).toBe(false);
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

  it('opens the hooks page from settings navigation and refreshes the list', async () => {
    const store = useAppStore();
    const refreshSpy = vi.spyOn(store, 'refreshHooks').mockResolvedValue(undefined);
    const wrapper = mount(SettingsPage);

    // 默认落在模型配置分区，钩子分区未挂载
    expect(wrapper.find('[data-test="hook-new-btn"]').exists()).toBe(false);
    expect(refreshSpy).not.toHaveBeenCalled();

    await wrapper.get('[data-test="hooks-nav"]').trigger('click');

    expect(wrapper.text()).toContain('钩子 Hooks');
    expect(wrapper.get('[data-test="hook-new-btn"]').isVisible()).toBe(true);
    // 进入钩子分区挂载组件时自动刷新一次列表
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  it('opens a model editor with the multimodal choice', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');

    expect(wrapper.get('[data-test="model-modal"]').isVisible()).toBe(true);
    expect(wrapper.get('[data-test="model-supports-vision"]').attributes('type')).toBe('checkbox');
    expect(wrapper.text()).toContain('是否支持多模态');
  });

  it('opens a model editor with function calling and computer protocol fields', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');

    expect(wrapper.get('[data-test="model-supports-function-calling"]').attributes('type')).toBe('checkbox');
    const protocol = wrapper.get<HTMLSelectElement>('[data-test="model-computer-protocol"]');
    // 自动（缺省）：视觉+工具调用双勾的模型无需逐个配置协议即可电脑操控
    expect(protocol.element.value).toBe('');
    const optionValues = Array.from(protocol.element.options).map(option => option.value);
    expect(optionValues).toEqual(['', 'none', 'generic', 'native_openai']);
    expect(wrapper.text()).toContain('自动');
  });

  it('defaults legacy models to the auto protocol in the editor', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    const editButton = wrapper.findAll('.configured-model-row .row-actions button')
      .find(button => button.text() === '编辑');
    expect(editButton).toBeDefined();
    await editButton!.trigger('click');

    expect(wrapper.get<HTMLInputElement>('[data-test="model-supports-vision"]').element.checked).toBe(true);
    expect(wrapper.get<HTMLInputElement>('[data-test="model-supports-function-calling"]').element.checked).toBe(false);
    // 后端对缺键模型返回 ''（自动）；该模型 fc=false，自动判定不会赋予操控资格
    expect(wrapper.get<HTMLSelectElement>('[data-test="model-computer-protocol"]').element.value).toBe('');
  });

  it('saves a new model with the auto protocol as an empty string', async () => {
    const store = useAppStore();
    const addModel = vi.spyOn(store, 'addModel').mockResolvedValue({ ok: true } as never);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('[data-test="model-modal"] input[placeholder="模型 ID"]').setValue('model-auto');
    // 视觉+工具调用双勾 + 协议保持「自动」
    await wrapper.get('[data-test="model-supports-vision"]').setValue(true);
    await wrapper.get('[data-test="model-supports-function-calling"]').setValue(true);
    await wrapper.get('[data-test="model-modal"]').trigger('submit');

    expect(addModel).toHaveBeenCalledWith('p1', 'model-auto', 128000, true, true, '', 0);
  });

  it('blocks saving generic protocol without vision and function calling', async () => {
    const store = useAppStore();
    const addModel = vi.spyOn(store, 'addModel').mockResolvedValue({ ok: true } as never);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('[data-test="model-modal"] input[placeholder="模型 ID"]').setValue('bad-generic');
    // 视觉与工具调用均未开启，直接选 generic
    await wrapper.get('[data-test="model-computer-protocol"]').setValue('generic');

    expect(wrapper.get('[data-test="model-computer-protocol-error"]').text()).toBe(
      'computer_protocol=generic 要求声明 supports_vision，且 supports_function_calling 未被显式关闭',
    );
    await wrapper.get('[data-test="model-modal"]').trigger('submit');

    expect(addModel).not.toHaveBeenCalled();
    // 前端阻止保存，modal 保持打开
    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(true);
  });

  it('saves a generic model with vision and function calling enabled', async () => {
    const store = useAppStore();
    const addModel = vi.spyOn(store, 'addModel').mockResolvedValue({ ok: true } as never);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="add-model"]').trigger('click');
    await wrapper.get('[data-test="model-modal"] input[placeholder="模型 ID"]').setValue('vision-model');
    await wrapper.get('[data-test="model-supports-vision"]').setValue(true);
    await wrapper.get('[data-test="model-supports-function-calling"]').setValue(true);
    await wrapper.get('[data-test="model-computer-protocol"]').setValue('generic');

    expect(wrapper.find('[data-test="model-computer-protocol-error"]').exists()).toBe(false);
    await wrapper.get('[data-test="model-modal"]').trigger('submit');

    expect(addModel).toHaveBeenCalledWith('p1', 'vision-model', 128000, true, true, 'generic', 0);
    expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(false);
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

  it('adds and removes a command permission rule', async () => {
    const add = vi.fn(async () => ({ ok: true, status: 'added' }));
    const remove = vi.fn(async () => ({ ok: true, status: 'removed' }));
    (window as any).pywebview = {
      api: {
        get_permission_rules: vi.fn(async () => ({
          ok: true,
          commands: ['git status'],
          write_files: [],
          edit_files: [],
        })),
        add_permission_rule: add,
        remove_permission_rule: remove,
      },
    };
    const store = useAppStore();
    vi.spyOn(store, 'askConfirm').mockResolvedValue(true);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="permission-nav"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="add-permission-rule"]').trigger('click');
    await wrapper.get('[data-test="permission-rule-type"]').setValue('command');
    await wrapper.get('[data-test="permission-rule-value"]').setValue('git diff');
    await wrapper.get('[data-test="save-permission-rule"]').trigger('click');

    expect(add).toHaveBeenCalledWith('command', 'git diff');

    await wrapper.get('[data-test="remove-permission-command-0"]').trigger('click');
    expect(remove).toHaveBeenCalledWith('command', 'git status');
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

  it('renders the disclaimer document with all clauses intact', async () => {
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="disclaimer-nav"]').trigger('click');

    expect(wrapper.get('.settings-header h1').text()).toBe('免责条款');
    const doc = wrapper.get('[data-test="disclaimer-doc"]');
    // 8 个条款章节完整渲染
    expect(wrapper.findAll('.disclaimer-clause').length).toBe(8);
    // 条款版本与更新日期可见（修改条款正文后需同步更新）
    expect(wrapper.get('[data-test="disclaimer-version"]').text()).toBe('v1.0');
    // 关键法定免责边界必须存在，防止后续编辑误删
    const text = doc.text();
    expect(text).toContain('不提供、不运营任何生成式人工智能模型服务');
    expect(text).toContain('责任限制');
    expect(text).toContain('法律允许的最大范围内');
    expect(text).toContain('按「现状」提供');
  });

  it('displays the micro error banner (Option B) when model test fails and allows copying/dismissing', async () => {
    const store = useAppStore();
    const rawError = 'litellm.AuthenticationError: AuthenticationError: OpenAIException - Invalid Authentication';
    vi.spyOn(store, 'testModel').mockResolvedValue({ ok: false, msg: rawError } as never);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    const testBtn = wrapper.findAll('.row-actions button').find(b => b.text().includes('测试'));
    expect(testBtn).toBeDefined();
    await testBtn!.trigger('click');
    await flushPromises();

    // 方案 B：微型警告条展开
    const banner = wrapper.find('[data-test="model-error-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain('API Key 无效或未授权');
    expect(banner.text()).toContain('请检查该厂商配置的 API Key');

    // 点击复制报错
    await wrapper.get('[data-test="copy-test-error"]').trigger('click');
    expect(writeText).toHaveBeenCalledWith(rawError);

    // 点击关闭警告条
    await wrapper.get('[data-test="dismiss-test-error"]').trigger('click');
    expect(wrapper.find('[data-test="model-error-banner"]').exists()).toBe(false);
  });

  it('displays inline success indicator when model test succeeds without error banner', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'testModel').mockResolvedValue({ ok: true, msg: '连接成功' } as never);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    const testBtn = wrapper.findAll('.row-actions button').find(b => b.text().includes('测试'));
    await testBtn!.trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="model-error-banner"]').exists()).toBe(false);
    expect(wrapper.find('.test-result.ok').exists()).toBe(true);
    expect(wrapper.get('.test-result.ok').text()).toContain('连接成功');
  });
});
