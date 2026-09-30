import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FeishuSettings from './FeishuSettings.vue';

const snapshot = {
  enabled: true,
  app_id: 'cli_demo',
  has_secret: true,
  approvers: ['ou_a', 'ou_b'],
  notify_chat_id: 'oc_x',
  status: {
    state: 'connected',
    running: true,
    last_error: '',
    received_events: 7,
    duplicate_events: 1,
    uptime_seconds: 30,
  },
};

function mockApi(overrides: Record<string, any> = {}) {
  const api = {
    get_feishu_settings: vi.fn(async () => ({ ok: true, settings: { ...snapshot } })),
    set_feishu_settings: vi.fn(async () => ({ ok: true, settings: { ...snapshot } })),
    get_feishu_secret: vi.fn(async () => ({ ok: true, app_secret: 'super_secret' })),
    test_feishu_connection: vi.fn(async () => ({ ok: true, msg: '凭据有效' })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

function mountPage() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(FeishuSettings, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('FeishuSettings', () => {
  it('挂载时加载设置并回显字段与状态', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();

    expect((wrapper.find('[data-test="feishu-app-id"]').element as HTMLInputElement).value).toBe('cli_demo');
    expect((wrapper.find('[data-test="feishu-approvers"]').element as HTMLInputElement).value).toBe('ou_a, ou_b');
    expect((wrapper.find('[data-test="feishu-notify-chat"]').element as HTMLInputElement).value).toBe('oc_x');
    expect((wrapper.find('[data-test="feishu-enabled"]').element as HTMLInputElement).checked).toBe(true);
    expect(wrapper.find('[data-test="feishu-status"]').text()).toContain('已连接');
    expect(wrapper.find('[data-test="feishu-status"]').text()).toContain('已收事件 7');
  });

  it('密钥默认掩码，点显示才按需取明文', async () => {
    const api = mockApi();
    const wrapper = mountPage();
    await flushPromises();

    const secret = () => wrapper.find('[data-test="feishu-app-secret"]').element as HTMLInputElement;
    expect(secret().type).toBe('password');
    expect(api.get_feishu_secret).not.toHaveBeenCalled();

    await wrapper.find('[data-test="feishu-toggle-secret"]').trigger('click');
    await flushPromises();

    expect(api.get_feishu_secret).toHaveBeenCalledTimes(1);
    expect(secret().type).toBe('text');
    expect(secret().value).toBe('super_secret');
  });

  it('保存时把白名单按逗号拆成数组并保留留空的密钥', async () => {
    const api = mockApi();
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="feishu-approvers"]').setValue('ou_a，ou_c');
    await wrapper.find('[data-test="feishu-app-id"]').setValue('cli_changed');
    await wrapper.find('[data-test="feishu-save"]').trigger('click');
    await flushPromises();

    expect(api.set_feishu_settings).toHaveBeenCalledWith({
      enabled: true,
      app_id: 'cli_changed',
      app_secret: '',
      approvers: ['ou_a', 'ou_c'],
      notify_chat_id: 'oc_x',
    });
    expect(wrapper.find('[data-test="feishu-message"]').text()).toBe('已保存');
  });

  it('保存失败时展示错误信息', async () => {
    mockApi({ set_feishu_settings: vi.fn(async () => ({ ok: false, msg: '写盘失败' })) });
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="feishu-save"]').trigger('click');
    await flushPromises();

    const message = wrapper.find('[data-test="feishu-message"]');
    expect(message.text()).toContain('保存失败');
    expect(message.text()).toContain('写盘失败');
    expect(message.classes()).toContain('bad');
  });

  it('测试连接把当前草稿凭据传给后端并展示结果', async () => {
    const api = mockApi();
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="feishu-app-id"]').setValue('cli_probe')
    await wrapper.find('[data-test="feishu-test"]').trigger('click');
    await flushPromises();

    expect(api.test_feishu_connection).toHaveBeenCalledWith('cli_probe', '');
    expect(wrapper.find('[data-test="feishu-message"]').text()).toBe('凭据有效');
  });

  it('未启用时状态显示为未启用', async () => {
    mockApi({
      get_feishu_settings: vi.fn(async () => ({
        ok: true,
        settings: {
          ...snapshot,
          enabled: false,
          has_secret: false,
          approvers: [],
          status: { ...snapshot.status, state: 'disabled', running: false, received_events: 0 },
        },
      })),
    });
    const wrapper = mountPage();
    await flushPromises();

    expect(wrapper.find('[data-test="feishu-status"]').text()).toContain('未启用');
    expect((wrapper.find('[data-test="feishu-app-secret"]').element as HTMLInputElement).placeholder)
      .toContain('输入 App Secret');
  });

  it('界面内置完整配置说明（含发布应用、回调与 /whoami）', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();

    const guide = wrapper.find('[data-test="feishu-guide"]');
    expect(guide.exists()).toBe(true);
    const text = guide.text();
    // 六步里的关键点必须都在，尤其"发布应用"与"卡片回调"这两个最容易漏的步骤
    expect(text).toContain('创建企业自建应用');
    expect(text).toContain('im:message:send_as_bot');
    expect(text).toContain('使用长连接接收事件');
    expect(text).toContain('im.message.receive_v1');
    expect(text).toContain('card.action.trigger');
    expect(text).toContain('回调 / 卡片回调');
    expect(text).toContain('卡片按钮点了没反应');
    expect(text).toContain('发布版本');
    expect(text).toContain('/whoami');
    // 排错指引
    expect(text).toContain('收不到消息时依次查');
  });

  it('配置说明可折叠', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();

    const guide = () => wrapper.find('[data-test="feishu-guide"]');
    // 默认展开（用户要求说明直接可见）；点标题折叠
    expect(guide().attributes('style') || '').not.toContain('display: none');
    await wrapper.find('[data-test="feishu-guide-toggle"]').trigger('click');
    expect(guide().attributes('style')).toContain('display: none');
    await wrapper.find('[data-test="feishu-guide-toggle"]').trigger('click');
    expect(guide().attributes('style') || '').not.toContain('display: none');
  });

  it('配置检查清单反映当前完成度', async () => {
    mockApi({
      get_feishu_settings: vi.fn(async () => ({
        ok: true,
        settings: { ...snapshot, approvers: [], status: { ...snapshot.status, state: 'error' } },
      })),
    });
    const wrapper = mountPage();
    await flushPromises();

    const items = wrapper.findAll('[data-test="feishu-checklist"] li');
    expect(items).toHaveLength(5);
    // 前三项已满足（App ID / 密钥 / 已启用），连接异常与白名单未配置
    expect(items[0].classes()).toContain('ok');
    expect(items[1].classes()).toContain('ok');
    expect(items[2].classes()).toContain('ok');
    expect(items[3].classes()).not.toContain('ok');
    expect(items[3].text()).toContain('连接异常');
    expect(items[4].classes()).not.toContain('ok');
    expect(items[4].text()).toContain('/whoami');
  });
});
