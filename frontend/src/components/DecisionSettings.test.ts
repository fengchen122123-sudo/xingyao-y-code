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
    list_decision_providers: vi.fn(async () => ({ ok: true, providers: [], msg: '' })),
    get_decision_audit_tail: vi.fn(async () => ({ ok: true, lines: [] })),
    get_decision_audit_summary: vi.fn(async () => ({
      ok: true,
      today_count: 0,
      today_shadow_count: 0,
      today_adopted_count: 0,
      today_fallback_count: 0,
      recent: [],
    })),
    open_decision_audit_dir: vi.fn(async () => ({ ok: true })),
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
      'decision-channel-select',
      'decision-base-url',
      'decision-key-auto',
      'decision-key-index',
      'decision-shadow',
      'decision-timeout',
    ];
    for (const selector of disabledSelectors) {
      const element = wrapper.find(`[data-test="${selector}"]`).element as HTMLInputElement | HTMLButtonElement;
      expect(element.disabled, selector).toBe(true);
    }

    // 保存按钮不受总开关约束：关闭开关时也必须能保存
    expect(
      (wrapper.find('[data-test="decision-save"]').element as HTMLButtonElement).disabled,
    ).toBe(false);

    // 打开审计目录不受总开关约束：审计记录随时可查看
    expect(
      (wrapper.find('[data-test="decision-audit-open"]').element as HTMLButtonElement).disabled,
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
      provider_ref: 'auto',
      base_url: '',
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

  it('调用通道：列出可选通道，选中后保存 provider_ref 与 Base URL 覆盖', async () => {
    const api = mockApi({
      list_decision_providers: vi.fn(async () => ({
        ok: true,
        msg: '',
        providers: [
          {
            ref: '61eb1545',
            name: '基元律动',
            base_url: 'https://tokenrhythm.studio/v1',
            key_count: 2,
            auto_match: true,
          },
          {
            ref: '9244d209',
            name: 'OpenCode Zen 免费通道',
            base_url: 'http://127.0.0.1:17863/v1',
            key_count: 1,
            auto_match: false,
          },
        ],
      })),
    });
    const wrapper = mountPage();
    await flushPromises();
    await wrapper.find('[data-test="decision-enabled"]').setValue(true);

    const select = wrapper.find('[data-test="decision-channel-select"]');
    expect(select.exists()).toBe(true);
    const options = select.findAll('option');
    expect(options.length).toBe(3); // 自动匹配 + 两条通道
    expect(options[1].text()).toContain('基元律动');
    expect(options[1].text()).toContain('2 把 Key');
    expect(options[2].text()).toContain('OpenCode Zen 免费通道');

    await select.setValue('9244d209');
    await wrapper.find('[data-test="decision-base-url"]').setValue('http://127.0.0.1:17863/v1');
    await wrapper.find('[data-test="decision-save"]').trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_ref: '9244d209',
        base_url: 'http://127.0.0.1:17863/v1',
      }),
    );
  });

  it('调用通道：配置里的通道不在列表时补兜底选项，不显示为空', async () => {
    mockApi({}, { provider_ref: 'legacy-provider' });
    const wrapper = mountPage();
    await flushPromises();

    const select = wrapper.find('[data-test="decision-channel-select"]');
    const values = select.findAll('option').map((item) => (item.element as HTMLOptionElement).value);
    expect(values).toContain('legacy-provider');
    expect((select.element as HTMLSelectElement).value).toBe('legacy-provider');
  });

  it('决策记录面板：渲染今日统计与最近记录，打开目录按钮可用', async () => {
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 3,
        today_shadow_count: 1,
        today_adopted_count: 1,
        today_fallback_count: 1,
        recent: [
          {
            ts: '2026-10-16T10:00:00',
            scene: 'probe',
            shadow: false,
            adopted: true,
            latency_ms: 326,
            fallback_reason: '',
            model_id: 'NeoHorse-Jev-4B',
            answers_digest: '',
          },
          {
            ts: '2026-10-16T10:01:00',
            scene: 'router',
            shadow: true,
            adopted: false,
            latency_ms: 210,
            fallback_reason: '',
            model_id: 'NeoHorse-Jev-4B',
            answers_digest: '{"tier":"c2"}',
          },
        ],
      })),
    });
    const wrapper = mountPage();
    await flushPromises();

    const summary = wrapper.find('[data-test="decision-audit-summary"]').text();
    expect(summary).toContain('今日 3 次决策');
    expect(summary).toContain('影子 1');

    const list = wrapper.find('[data-test="decision-audit-list"]').text();
    expect(list).toContain('probe');
    expect(list).toContain('router');
    expect(list).toContain('影子');
    expect(list).toContain('326ms');

    await wrapper.find('[data-test="decision-audit-open"]').trigger('click');
    await flushPromises();
    expect(api.open_decision_audit_dir).toHaveBeenCalledTimes(1);
  });

  it('决策记录为空时展示占位文案', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();
    expect(wrapper.find('[data-test="decision-audit-summary"]').text()).toContain('暂无决策记录');
  });

  it('业务场景：四行三态开关渲染，切正式给警示、全局影子压住时给提示', async () => {
    // 全局影子开着：router 已按「正式」落盘，但真实生效仍是 shadow
    const rows = [
      {
        scene: 'router',
        label: '路由',
        description: '本回合用主模型还是副模型',
        mode: 'shadow',
        mode_text: '影子',
        requested_mode: 'live',
        enabled: true,
        shadow: false,
        threshold: 0.5,
        timeout_ms: 250,
        global_shadow: true,
      },
      {
        scene: 'secretary',
        label: '秘书',
        description: '派发给哪个候选会话',
        mode: 'off',
        mode_text: '关闭',
        requested_mode: 'off',
        enabled: false,
        shadow: true,
        threshold: 0.6,
        timeout_ms: 250,
        global_shadow: true,
      },
      {
        scene: 'search',
        label: '搜索',
        description: '一次取回几条结果',
        mode: 'shadow',
        mode_text: '影子',
        requested_mode: 'shadow',
        enabled: true,
        shadow: true,
        threshold: 0.5,
        timeout_ms: 250,
        global_shadow: true,
      },
      {
        scene: 'safety',
        label: '安全',
        description: '高风险命令是否放行',
        mode: 'off',
        mode_text: '关闭',
        requested_mode: 'off',
        enabled: false,
        shadow: true,
        threshold: 0.9,
        timeout_ms: 250,
        global_shadow: true,
      },
    ];
    const api = mockApi(
      {
        get_decision_scenes: vi.fn(async () => ({
          ok: true,
          scenes: rows,
          shadow_mode: true,
          enabled: true,
        })),
        set_decision_scene: vi.fn(async () => ({
          ok: true,
          msg: '路由场景已关闭',
          scenes: rows.map((row) => (row.scene === 'router' ? { ...row, mode: 'off', mode_text: '关闭', requested_mode: 'off' } : row)),
        })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    // 四个场景行都在，且每行有三个三态单选项与阈值输入
    for (const scene of ['router', 'secretary', 'search', 'safety']) {
      expect(wrapper.find(`[data-test="decision-scene-${scene}"]`).exists()).toBe(true);
      for (const mode of ['off', 'shadow', 'live']) {
        expect(
          wrapper.find(`[data-test="decision-scene-${scene}-mode-${mode}"]`).exists(),
        ).toBe(true);
      }
      expect(wrapper.find(`[data-test="decision-scene-${scene}-threshold"]`).exists()).toBe(true);
    }

    // 选中态跟随 requested_mode（router 是 live，secretary 是 off）
    expect(
      (wrapper.find('[data-test="decision-scene-router-mode-live"]').element as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      (wrapper.find('[data-test="decision-scene-secretary-mode-off"]').element as HTMLInputElement).checked,
    ).toBe(true);

    // 正式模式的警示 + 全局影子压住的提示
    expect(wrapper.find('[data-test="decision-scene-router-live-warning"]').text()).toContain('真实影响任务行为');
    expect(wrapper.find('[data-test="decision-scene-router-effective-hint"]').text()).toContain('全局影子模式已开启');
    // 非 live 场景不出现这两类提示
    expect(wrapper.find('[data-test="decision-scene-secretary-live-warning"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="decision-scene-secretary-effective-hint"]').exists()).toBe(false);

    // 切换场景三态：立即调用 set_decision_scene 并回显提示
    const setScene = (api as unknown as { set_decision_scene: ReturnType<typeof vi.fn> }).set_decision_scene;
    await wrapper.find('[data-test="decision-scene-router-mode-off"]').setValue();
    await flushPromises();
    expect(setScene).toHaveBeenCalledWith('router', 'off');
    expect(wrapper.find('[data-test="decision-scenes-msg"]').text()).toContain('路由场景已关闭');
  });

  it('业务场景：切换失败时回显后端原因', async () => {
    mockApi(
      {
        get_decision_scenes: vi.fn(async () => ({
          ok: true,
          scenes: [
            {
              scene: 'router',
              label: '路由',
              description: '',
              mode: 'off',
              mode_text: '关闭',
              requested_mode: 'off',
              enabled: false,
              shadow: true,
              threshold: 0.5,
              timeout_ms: 250,
              global_shadow: true,
            },
          ],
          shadow_mode: true,
          enabled: true,
        })),
        set_decision_scene: vi.fn(async () => ({ ok: false, msg: '未知场景「nope」', scenes: [] })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="decision-scene-router-mode-shadow"]').setValue();
    await flushPromises();

    expect(wrapper.find('[data-test="decision-scenes-msg"]').text()).toContain('未知场景');
  });
});
