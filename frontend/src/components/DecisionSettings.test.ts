import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DecisionSettings from './DecisionSettings.vue';
import { useAppStore } from '../stores/appStore';

const baseSettings = {
  version: 1,
  enabled: false,
  provider_ref: 'auto',
  base_url: '',
  model_id: 'NeoHorse-Jev-4B',
  endpoint_style: 'decision',
  key_mode: 'auto',
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

function mockApi(
  apiOverrides: Record<string, unknown> = {},
  settingsOverride: Record<string, unknown> = {},
) {
  let currentSettings = { ...baseSettings, ...settingsOverride };
  const api = {
    get_decision_settings: vi.fn(async () => ({
      ok: true,
      settings: { ...currentSettings },
      runtime: { ...baseRuntime },
    })),
    set_decision_settings: vi.fn(async (patch: Record<string, unknown>) => {
      currentSettings = { ...currentSettings, ...patch };
      return {
        ok: true,
        msg: '决策设置已保存',
        settings: { ...currentSettings },
      };
    }),
    probe_decision: vi.fn(async () => ({ ok: true, results: [], model_id: '', msg: '' })),
    list_decision_models: vi.fn(async () => ({ ok: true, models: [], msg: '' })),
    list_decision_providers: vi.fn(async () => ({ ok: true, providers: [], msg: '' })),
    get_decision_scenes: vi.fn(async () => ({ ok: true, scenes: [], enabled: currentSettings.enabled })),
    set_decision_scene: vi.fn(async () => ({ ok: true, msg: '已保存', scenes: [] })),
    get_decision_audit_tail: vi.fn(async () => ({ ok: true, lines: [] })),
    get_decision_audit_summary: vi.fn(async () => ({
      ok: true,
      today_count: 0,
      today_adopted_count: 0,
      today_fallback_count: 0,
      recent: [],
    })),
    get_decision_record_detail: vi.fn(async () => ({ ok: false, msg: '未找到' })),
    open_decision_audit_dir: vi.fn(async () => ({ ok: true })),
    ...apiOverrides,
  };
  (window as any).pywebview = { api };
  return api;
}

let activePinia = createPinia();

function mountPage() {
  return mount(DecisionSettings, {
    global: {
      plugins: [activePinia],
    },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  activePinia = createPinia();
  setActivePinia(activePinia);
});

describe('DecisionSettings', () => {
  it('默认关闭：总开关与所有开关均使用 switch 按钮，无原生 checkbox/radio，禁用连接控件', async () => {
    mockApi();
    const wrapper = mountPage();
    await flushPromises();

    // 所有开关均采用 switch 按钮形式，不存在原生 checkbox/radio
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0);
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(0);

    const masterSwitch = wrapper.get('[data-test="decision-enabled"]');
    expect(masterSwitch.attributes('role')).toBe('switch');
    expect(masterSwitch.attributes('aria-checked')).toBe('false');
    expect(wrapper.get('[data-test="decision-enabled-pill"]').text()).toBe('已关闭');
    expect(wrapper.find('[data-test="decision-scenes-standby"]').exists()).toBe(true);

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
      'decision-key-index-num',
      'decision-timeout',
    ];
    expect(wrapper.find('[data-test="decision-shadow"]').exists()).toBe(false);
    for (const selector of disabledSelectors) {
      const element = wrapper.find(`[data-test="${selector}"]`).element as HTMLInputElement | HTMLButtonElement;
      expect(element.disabled, selector).toBe(true);
    }

    // 保存、审计开关、打开审计目录不受总开关关闭约束
    expect((wrapper.find('[data-test="decision-save"]').element as HTMLButtonElement).disabled).toBe(false);
    expect((wrapper.find('[data-test="decision-audit-enabled"]').element as HTMLButtonElement).disabled).toBe(false);
    expect((wrapper.find('[data-test="decision-audit-open"]').element as HTMLButtonElement).disabled).toBe(false);
  });

  it('点击启用决策模型开关按钮：立即持久化并解锁配置，再次点击可立即关闭并回退自动决策权限', async () => {
    const api = mockApi();
    const store = useAppStore(activePinia);
    store.permissionMode = 'auto_decision';
    const wrapper = mountPage();
    await flushPromises();

    const masterSwitch = wrapper.get('[data-test="decision-enabled"]');
    await masterSwitch.trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenCalledWith({ enabled: true });
    expect(masterSwitch.attributes('aria-checked')).toBe('true');
    expect(wrapper.get('[data-test="decision-enabled-pill"]').text()).toBe('已启用');
    expect(wrapper.find('[data-test="decision-scenes-standby"]').exists()).toBe(false);
    expect((wrapper.find('[data-test="decision-model-id"]').element as HTMLInputElement).disabled).toBe(false);
    expect(store.decisionEnabled).toBe(true);

    // 再次点击关闭
    store.permissionMode = 'auto_decision';
    await masterSwitch.trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenLastCalledWith({ enabled: false });
    expect(masterSwitch.attributes('aria-checked')).toBe('false');
    expect(store.decisionEnabled).toBe(false);
    expect(store.permissionMode).toBe('confirm');
  });

  it('模型 ID 为空时点击总开关：解锁编辑栏并提示补充模型 ID 后保存', async () => {
    const api = mockApi({}, { model_id: '' });
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.get('[data-test="decision-enabled"]').trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).not.toHaveBeenCalled();
    expect(wrapper.get('[data-test="decision-enabled"]').attributes('aria-checked')).toBe('true');
    expect((wrapper.get('[data-test="decision-model-id"]').element as HTMLInputElement).disabled).toBe(false);
    expect(wrapper.get('[data-test="decision-error"]').text()).toContain('模型 ID');

    await wrapper.get('[data-test="decision-model-id"]').setValue('NeoHorse-Jev-Custom');
    await wrapper.get('[data-test="decision-save"]').trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: true, model_id: 'NeoHorse-Jev-Custom' }),
    );
  });

  it('修改接口形态、Key 策略、超时并保存或放弃修改', async () => {
    const api = mockApi({}, { enabled: true });
    const wrapper = mountPage();
    await flushPromises();

    expect(wrapper.find('[data-test="decision-dirty-pill"]').exists()).toBe(false);
    expect((wrapper.get('[data-test="decision-reload"]').element as HTMLButtonElement).disabled).toBe(true);

    await wrapper.get('[data-test="decision-endpoint-systemone"]').trigger('click');
    await wrapper.get('[data-test="decision-key-index"]').trigger('click');
    await wrapper.get('[data-test="decision-key-index-num"]').setValue(2);
    await wrapper.get('[data-test="decision-timeout"]').setValue(1500);

    expect(wrapper.find('[data-test="decision-dirty-pill"]').exists()).toBe(true);
    expect((wrapper.get('[data-test="decision-reload"]').element as HTMLButtonElement).disabled).toBe(false);

    // 放弃修改恢复到落盘状态
    await wrapper.get('[data-test="decision-reload"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="decision-endpoint-decision"]').attributes('aria-checked')).toBe('true');
    expect((wrapper.get('[data-test="decision-timeout"]').element as HTMLInputElement).value).toBe('800');

    // 重新修改并保存
    await wrapper.get('[data-test="decision-endpoint-systemone"]').trigger('click');
    await wrapper.get('[data-test="decision-key-index"]').trigger('click');
    await wrapper.get('[data-test="decision-key-index-num"]').setValue(2);
    await wrapper.get('[data-test="decision-timeout"]').setValue(1500);
    await wrapper.get('[data-test="decision-save"]').trigger('click');
    await flushPromises();

    expect(api.set_decision_settings).toHaveBeenCalledWith({
      enabled: true,
      model_id: 'NeoHorse-Jev-4B',
      endpoint_style: 'systemone',
      provider_ref: 'auto',
      base_url: '',
      key_mode: 'index:1',
      timeout_ms: 1500,
    });
    expect(wrapper.get('[data-test="decision-saved"]').text()).toBe('决策设置已保存');
  });

  it('运行探针：调用 probe_decision 并渲染逐 Key 状态芯片', async () => {
    const api = mockApi(
      {
        probe_decision: vi.fn(async () => ({
          ok: true,
          model_id: 'NeoHorse-Jev-4B',
          msg: '',
          results: [
            { key_index: 0, status: 'no_permission', latency_ms: 305, error: '上游 403' },
            { key_index: 1, status: 'available', latency_ms: 326, error: '' },
          ],
        })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="decision-probe"]').trigger('click');
    await flushPromises();

    expect(api.probe_decision).toHaveBeenCalledTimes(1);
    const results = wrapper.find('[data-test="decision-probe-results"]').text();
    expect(results).toContain('可用（326 ms）');
    expect(results).toContain('权限不足');
    expect(wrapper.find('[data-test="decision-status"]').text()).toContain('可用（Key#2）');
    expect(wrapper.find('[data-test="decision-probe-model"]').text()).toContain('NeoHorse-Jev-4B');
  });

  it('从上游拉取成功：下拉列出 decision 模型且可点选填充', async () => {
    const api = mockApi(
      {
        list_decision_models: vi.fn(async () => ({
          ok: true,
          msg: '',
          models: [
            { id: 'NeoHorse-Jev-4B', name: 'NeoHorse-Jev-4B', context: 33000, price_text: '输入 0 / 输出 0' },
            { id: 'NeoHorse-Jev-8B', name: 'NeoHorse-Jev-8B', context: 64000, price_text: '' },
          ],
        })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="decision-fetch-models"]').trigger('click');
    await flushPromises();

    expect(api.list_decision_models).toHaveBeenCalledTimes(1);
    const select = wrapper.find('[data-test="decision-model-select"]');
    expect(select.exists()).toBe(true);
    const options = select.findAll('option');
    expect(options.length).toBe(3);
    expect(options[1].text()).toContain('NeoHorse-Jev-4B');
    expect(options[2].text()).toContain('NeoHorse-Jev-8B');

    await select.setValue('NeoHorse-Jev-8B');
    expect(
      (wrapper.find('[data-test="decision-model-id"]').element as HTMLInputElement).value,
    ).toBe('NeoHorse-Jev-8B');
  });

  it('从上游拉取失败：显示提示且模型 ID 仍可手动编辑', async () => {
    mockApi(
      {
        list_decision_models: vi.fn(async () => ({ ok: false, models: [], msg: '拉取模型列表超时' })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    await wrapper.find('[data-test="decision-fetch-models"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="decision-fetch-hint"]').text()).toContain('拉取模型列表超时');
    expect(wrapper.find('[data-test="decision-model-select"]').exists()).toBe(false);
    const input = wrapper.find('[data-test="decision-model-id"]');
    expect((input.element as HTMLInputElement).disabled).toBe(false);
    await input.setValue('NeoHorse-Jev-Manual');
    expect((input.element as HTMLInputElement).value).toBe('NeoHorse-Jev-Manual');
  });

  it('调用通道：列出可选通道，选中后保存 provider_ref 与 Base URL 覆盖', async () => {
    const api = mockApi(
      {
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
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    const select = wrapper.find('[data-test="decision-channel-select"]');
    expect(select.exists()).toBe(true);
    const options = select.findAll('option');
    expect(options.length).toBe(3);
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

  it('业务场景：全部改用 switch 开关按钮，开启给警示且支持即时切换与阈值调整', async () => {
    const rows = [
      {
        scene: 'secretary',
        label: '秘书',
        description: '派发给哪个候选会话',
        mode: 'off',
        mode_text: '关闭',
        enabled: false,
        threshold: 0.6,
        timeout_ms: 1500,
      },
      {
        scene: 'search',
        label: '搜索',
        description: '一次取回几条结果',
        mode: 'live',
        mode_text: '开启',
        enabled: true,
        threshold: 0.5,
        timeout_ms: 1500,
      },
      {
        scene: 'safety',
        label: '安全',
        description: '高风险命令是否放行',
        mode: 'off',
        mode_text: '关闭',
        enabled: false,
        threshold: 0.9,
        timeout_ms: 1500,
      },
    ];
    const api = mockApi(
      {
        get_decision_scenes: vi.fn(async () => ({
          ok: true,
          scenes: rows,
          enabled: true,
        })),
        set_decision_scene: vi.fn(async (scene: string, mode: string, threshold?: number) => ({
          ok: true,
          msg: mode === 'off' ? `${scene === 'search' ? '搜索' : scene}场景已关闭` : '场景已开启',
          scenes: rows.map((row) =>
            row.scene === scene
              ? {
                  ...row,
                  mode,
                  mode_text: mode === 'live' ? '开启' : '关闭',
                  enabled: mode === 'live',
                  ...(typeof threshold === 'number' ? { threshold } : {}),
                }
              : row,
          ),
        })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    for (const scene of ['secretary', 'search', 'safety']) {
      expect(wrapper.find(`[data-test="decision-scene-${scene}"]`).exists()).toBe(true);
      const sw = wrapper.find(`[data-test="decision-scene-${scene}-switch"]`);
      expect(sw.exists()).toBe(true);
      expect(sw.attributes('role')).toBe('switch');
      expect(wrapper.find(`[data-test="decision-scene-${scene}-threshold"]`).exists()).toBe(true);
    }

    expect(wrapper.get('[data-test="decision-scene-search-switch"]').attributes('aria-checked')).toBe('true');
    expect(wrapper.get('[data-test="decision-scene-secretary-switch"]').attributes('aria-checked')).toBe('false');

    expect(wrapper.find('[data-test="decision-scene-search-live-warning"]').text()).toContain('真实影响任务行为');
    expect(wrapper.find('[data-test="decision-scene-secretary-live-warning"]').exists()).toBe(false);

    // 点击开关按钮关闭 search 场景
    await wrapper.get('[data-test="decision-scene-search-switch"]').trigger('click');
    await flushPromises();
    expect(api.set_decision_scene).toHaveBeenCalledWith('search', 'off');
    expect(wrapper.get('[data-test="decision-scene-search-switch"]').attributes('aria-checked')).toBe('false');
    expect(wrapper.find('[data-test="decision-scenes-msg"]').text()).toContain('搜索场景已关闭');

    // 修改阈值
    const thresholdInput = wrapper.get('[data-test="decision-scene-safety-threshold"]');
    await thresholdInput.setValue('0.85');
    await thresholdInput.trigger('change');
    await flushPromises();
    expect(api.set_decision_scene).toHaveBeenLastCalledWith('safety', 'off', 0.85);
  });

  it('业务场景：切换失败时回滚开关状态并回显后端原因', async () => {
    const rows = [
      {
        scene: 'secretary',
        label: '秘书',
        description: '',
        mode: 'off',
        mode_text: '关闭',
        enabled: false,
        threshold: 0.6,
        timeout_ms: 1500,
      },
    ];
    mockApi(
      {
        get_decision_scenes: vi.fn(async () => ({
          ok: true,
          scenes: rows,
          enabled: true,
        })),
        set_decision_scene: vi.fn(async () => ({ ok: false, msg: '未知场景「nope」', scenes: rows })),
      },
      { enabled: true },
    );
    const wrapper = mountPage();
    await flushPromises();

    const sw = wrapper.get('[data-test="decision-scene-secretary-switch"]');
    expect(sw.attributes('aria-checked')).toBe('false');
    await sw.trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="decision-scenes-msg"]').text()).toContain('未知场景');
    expect(sw.attributes('aria-checked')).toBe('false');
  });

  it('决策记录与审计：支持开关按钮控制审计日志、渲染统计与列表并打开目录', async () => {
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 3,
        today_adopted_count: 2,
        today_fallback_count: 1,
        recent: [
          {
            ts: '2026-10-16T10:00:00',
            scene: 'probe',
            adopted: true,
            latency_ms: 326,
            fallback_reason: '',
            model_id: 'NeoHorse-Jev-4B',
            answers_digest: '',
          },
          {
            ts: '2026-10-16T10:01:00',
            scene: 'router',
            adopted: true,
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

    const auditSwitch = wrapper.get('[data-test="decision-audit-enabled"]');
    expect(auditSwitch.attributes('role')).toBe('switch');
    expect(auditSwitch.attributes('aria-checked')).toBe('true');

    await auditSwitch.trigger('click');
    await flushPromises();
    expect(api.set_decision_settings).toHaveBeenCalledWith({ audit_enabled: false });
    expect(auditSwitch.attributes('aria-checked')).toBe('false');

    const summary = wrapper.find('[data-test="decision-audit-summary"]').text();
    expect(summary).toContain('今日 3 次决策');
    expect(summary).toContain('采纳 2');
    expect(summary).not.toContain('影子');

    const list = wrapper.find('[data-test="decision-audit-list"]').text();
    expect(list).toContain('probe');
    expect(list).toContain('router');
    expect(list).not.toContain('影子');
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

  it('决策记录：支持在「全部会话」与「当前会话」间切换，且可展开带 trace_id 的详情', async () => {
    const store = useAppStore(activePinia);
    store.currentSessionName = 'sess-A';

    const getSummary = vi.fn(async (_limit?: number, sessionId?: string, sessionOnly?: boolean) => {
      if (sessionOnly && sessionId === 'sess-A') {
        return {
          ok: true,
          today_count: 1,
          today_adopted_count: 1,
          today_fallback_count: 0,
          recent: [
            {
              ts: '2026-11-20T14:00:00',
              scene: 'subagent_review',
              session_id: 'sess-A',
              trace_id: 'trace-123',
              adopted: true,
              latency_ms: 420,
              fallback_reason: '',
              model_id: 'NeoHorse-Jev-4B',
              answers_digest: '',
            },
          ],
        };
      }
      return {
        ok: true,
        today_count: 2,
        today_adopted_count: 2,
        today_fallback_count: 0,
        recent: [
          {
            ts: '2026-11-20T13:38:36',
            scene: 'memory_filter',
            session_id: 'sess-B',
            adopted: true,
            latency_ms: 800,
            fallback_reason: '',
            model_id: 'NeoHorse-Jev-4B',
            answers_digest: '',
          },
          {
            ts: '2026-11-20T14:00:00',
            scene: 'subagent_review',
            session_id: 'sess-A',
            trace_id: 'trace-123',
            adopted: true,
            latency_ms: 420,
            fallback_reason: '',
            model_id: 'NeoHorse-Jev-4B',
            answers_digest: '',
          },
        ],
      };
    });

    const getDetail = vi.fn(async () => ({
      ok: true,
      record: {
        trace_id: 'trace-123',
        model_id: 'NeoHorse-Jev-4B',
        request: { model: 'NeoHorse-Jev-4B', state: '检查子代理汇报', questions: { q: {} } },
        answers: { q: { choice: 'accepted' } },
      },
    }));

    mockApi({
      get_decision_audit_summary: getSummary,
      get_decision_record_detail: getDetail,
    });
    const wrapper = mountPage();
    await flushPromises();

    expect(getSummary).toHaveBeenLastCalledWith(10);
    const listAll = wrapper.find('[data-test="decision-audit-list"]').text();
    expect(listAll).toContain('会话：sess-B');
    expect(listAll).toContain('会话：sess-A');

    await wrapper.find('[data-test="decision-audit-scope-session"]').trigger('click');
    await flushPromises();
    expect(getSummary).toHaveBeenLastCalledWith(10, 'sess-A', true);
    expect(wrapper.find('[data-test="decision-audit-summary"]').text()).toContain('本会话今日 1 次决策');
    const listScoped = wrapper.find('[data-test="decision-audit-list"]').text();
    expect(listScoped).toContain('subagent_review');
    expect(listScoped).not.toContain('memory_filter');

    // 点击记录项展开详情
    await wrapper.find('[data-test="decision-audit-list"] li').trigger('click');
    await flushPromises();
    expect(getDetail).toHaveBeenCalledWith('trace-123');
    expect(wrapper.find('[data-test="decision-audit-list"]').text()).toContain('检查子代理汇报');
  });
});
