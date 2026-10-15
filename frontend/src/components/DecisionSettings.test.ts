import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DecisionSettings from './DecisionSettings.vue';

const baseSettings = {
  version: 1,
  enabled: false,
  provider_ref: 'auto',
  base_url: '',
  model_id: 'NeoHorse-Jev-4B',
  endpoint_style: 'decision',
  key_mode: 'auto',
  shadow_mode: true,
  timeout_ms: 800,
  scenes: {},
  audit_enabled: true,
};

const baseRuntime = {
  key_count: 2,
  active_key_index: null,
  denied_key_indexes: [],
  cooldown_key_indexes: [],
  last_probe: null,
};

// mock window.pywebview.api 的五个决策桥方法（参考 FeishuSettings.test.ts 的模式）
function mockApi(
  apiOverrides: Record<string, unknown> = {},
  settingsOverride: Record<string, unknown> = {},
) {
  const settings = { ...baseSettings, ...settingsOverride };
  const api = {
    get_decision_settings: vi.fn(async () => ({
      ok: true,
      settings: { ...settings },
      runtime: { ...baseRuntime },
    })),
    set_decision_settings: vi.fn(async () => ({
      ok: true,
      msg: '决策设置已保存',
      settings: { ...settings, enabled: true },
    })),
    probe_decision: vi.fn(async () => ({ ok: true, results: [], model_id: '', msg: '' })),
    list_decision_models: vi.fn(async () => ({ ok: true, models: [], msg: '' })),
    get_decision_audit_tail: vi.fn(async () => ({ ok: true, lines: [] })),
    ...apiOverrides,
  };
  (window as any).pywebview = { api };
  return api;
}

function mountPage() {
  return mount(DecisionSettings);
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('DecisionSettings', () => {
  it('默认关闭：总开关未勾选，其余控件全部禁用', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();

    expect(
      (wrapper.find('[data-test="decision-enabled"]').element as HTMLInputElement).checked,
    ).toBe(false);

    const disabledSelectors = [
      'decision-model-id',
      'decision-fetch-models',
      'decision-probe',
      'decision-endpoint-decision',
      'decision-endpoint-systemone',
      'decision-key-auto',
      'decision-key-index',
      'decision-shadow',
      'decision-timeout',
      'decision-audit-open',
    ];
    for (const selector of disabledSelectors) {
      const element = wrapper.find(`[data-test="${selector}"]`).element as HTMLInputElement | HTMLButtonElement;
      expect(element.disabled, selector).toBe(true);
    }

    // 保存按钮不受总开关约束：关闭开关时也必须能保存
    expect(
      (wrapper.find('[data-test="decision-save"]').element as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it('打开总开关并保存：调用 set_decision_settings 且 payload.enabled=true', async () => {
    const api = mockApi();
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="decision-enabled"]').setValue(true);
    // 开关打开后其余控件解除禁用
    expect(
      (wrapper.find('[data-test="decision-model-id"]').element as HTMLInputElement).disabled,
    ).toBe(false);

    await wrapper.find('[data-test="decision-save"]').trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenCalledTimes(1);
    expect(api.set_decision_settings).toHaveBeenCalledWith({
      enabled: true,
      model_id: 'NeoHorse-Jev-4B',
      endpoint_style: 'decision',
      key_mode: 'auto',
      shadow_mode: true,
      timeout_ms: 800,
    });
    expect(wrapper.find('[data-test="decision-saved"]').text()).toBe('决策设置已保存');
  });

  it('运行探针：调用 probe_decision 并渲染逐 Key 状态行', async () => {
    const api = mockApi({
      probe_decision: vi.fn(async () => ({
        ok: true,
        model_id: 'NeoHorse-Jev-4B',
        msg: '',
        results: [
          { key_index: 0, status: 'no_permission', latency_ms: 305, error: '上游 403' },
          { key_index: 1, status: 'available', latency_ms: 326, error: '' },
        ],
      })),
    });
    const wrapper = mountPage();
    await flushPromises();

    // 探针按钮受总开关约束：先打开开关
    await wrapper.find('[data-test="decision-enabled"]').setValue(true);
    await wrapper.find('[data-test="decision-probe"]').trigger('click');
    await flushPromises();

    expect(api.probe_decision).toHaveBeenCalledTimes(1);
    const results = wrapper.find('[data-test="decision-probe-results"]').text();
    expect(results).toContain('可用（326 ms）');
    expect(results).toContain('权限不足');
    expect(wrapper.find('[data-test="decision-status"]').text()).toContain('可用（Key#2）');
  });

  it('从上游拉取成功：下拉列出 decision 模型且可点选填充', async () => {
    const api = mockApi({
      list_decision_models: vi.fn(async () => ({
        ok: true,
        msg: '',
        models: [
          { id: 'NeoHorse-Jev-4B', name: 'NeoHorse-Jev-4B', context: 33000, price_text: '输入 0 / 输出 0' },
          { id: 'NeoHorse-Jev-8B', name: 'NeoHorse-Jev-8B', context: 64000, price_text: '' },
        ],
      })),
    });
    const wrapper = mountPage();
    await flushPromises();
    await wrapper.find('[data-test="decision-enabled"]').setValue(true);

    await wrapper.find('[data-test="decision-fetch-models"]').trigger('click');
    await flushPromises();

    expect(api.list_decision_models).toHaveBeenCalledTimes(1);
    const select = wrapper.find('[data-test="decision-model-select"]');
    expect(select.exists()).toBe(true);
    const options = select.findAll('option');
    expect(options.length).toBe(3); // 占位项 + 2 个模型
    expect(options[1].text()).toContain('NeoHorse-Jev-4B');
    expect(options[2].text()).toContain('NeoHorse-Jev-8B');

    await select.setValue('NeoHorse-Jev-8B');
    expect(
      (wrapper.find('[data-test="decision-model-id"]').element as HTMLInputElement).value,
    ).toBe('NeoHorse-Jev-8B');
  });

  it('从上游拉取失败：显示提示且模型 ID 仍可手动编辑', async () => {
    mockApi({
      list_decision_models: vi.fn(async () => ({ ok: false, models: [], msg: '拉取模型列表超时' })),
    });
    const wrapper = mountPage();
    await flushPromises();
    await wrapper.find('[data-test="decision-enabled"]').setValue(true);

    await wrapper.find('[data-test="decision-fetch-models"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="decision-fetch-hint"]').text()).toContain('拉取模型列表超时');
    expect(wrapper.find('[data-test="decision-model-select"]').exists()).toBe(false);
    // 失败后输入框仍可编辑
    const input = wrapper.find('[data-test="decision-model-id"]');
    expect((input.element as HTMLInputElement).disabled).toBe(false);
    await input.setValue('NeoHorse-Jev-Manual');
    expect((input.element as HTMLInputElement).value).toBe('NeoHorse-Jev-Manual');
  });
});
