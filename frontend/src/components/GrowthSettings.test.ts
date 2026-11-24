import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GrowthSettings from './GrowthSettings.vue';

function mockApi(overrides: Record<string, any> = {}) {
  let settings = {
    enabled: true,
    learning_enabled: true,
    user_key: 'fch',
  };
  const api = {
    get_growth_settings: vi.fn(async () => ({
      ok: true,
      settings: { ...settings },
      snapshot: {
        user_key: settings.user_key || 'fch',
        title: '冯晨昊',
        stage: 'awakening',
        lessons_count: 97,
        last_learning_at: '2026-11-22T20:43:13',
        changelog: [
          '2026-11-22 15:00 [self] 旧记录',
          '2026-11-22 20:43 [self] 新记录',
        ],
      },
    })),
    set_growth_settings: vi.fn(
      async (
        enabled: boolean | null,
        learning: boolean | null,
        userKey: string | null,
      ): Promise<{ ok: boolean; settings?: typeof settings; msg?: string }> => {
        if (enabled !== null) settings.enabled = enabled;
        if (learning !== null) settings.learning_enabled = learning;
        if (userKey !== null) settings.user_key = userKey;
        return { ok: true, settings: { ...settings } };
      },
    ),
    reset_growth: vi.fn(async () => ({ ok: true })),
    export_growth_snapshot: vi.fn(async () => ({ ok: true, snapshot: { hello: 'world' } })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('GrowthSettings', () => {
  it('使用开关按钮渲染自我演化与经验学习开关，且无原生 checkbox/radio', async () => {
    mockApi();
    const wrapper = mount(GrowthSettings);
    await flushPromises();

    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0);
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(0);

    const enabledSw = wrapper.get('[data-test="growth-enabled-toggle"]');
    const learningSw = wrapper.get('[data-test="growth-learning-toggle"]');
    expect(enabledSw.attributes('role')).toBe('switch');
    expect(enabledSw.attributes('aria-checked')).toBe('true');
    expect(learningSw.attributes('role')).toBe('switch');
    expect(learningSw.attributes('aria-checked')).toBe('true');

    expect(wrapper.get('[data-test="growth-current-user"]').text()).toBe('fch');
    expect(wrapper.get('[data-test="growth-current-title"]').text()).toBe('冯晨昊');
    expect(wrapper.get('[data-test="growth-current-stage"]').text()).toBe('觉醒');
    expect(wrapper.get('[data-test="growth-lessons-count"]').text()).toBe('97');
    expect(wrapper.get('[data-test="growth-last-learning"]').text()).toBe('2026-11-22 20:43:13');
    const logRows = wrapper.findAll('.growth-changelog-row');
    expect(logRows).toHaveLength(2);
    expect(logRows[0].text()).toContain('新记录');
  });

  it('点击开关按钮立即保存并支持失败回滚', async () => {
    const api = mockApi();
    const wrapper = mount(GrowthSettings);
    await flushPromises();

    const enabledSw = wrapper.get('[data-test="growth-enabled-toggle"]');
    await enabledSw.trigger('click');
    await flushPromises();

    expect(api.set_growth_settings).toHaveBeenCalledWith(false, null, null);
    expect(enabledSw.attributes('aria-checked')).toBe('false');
    expect(wrapper.get('[data-test="growth-tip"]').text()).toContain('已保存');

    // 失败时回滚开关状态
    api.set_growth_settings.mockResolvedValueOnce({ ok: false, msg: '写盘被拒' });
    const learningSw = wrapper.get('[data-test="growth-learning-toggle"]');
    await learningSw.trigger('click');
    await flushPromises();
    expect(learningSw.attributes('aria-checked')).toBe('true');
    expect(wrapper.get('[data-test="growth-error"]').text()).toContain('写盘被拒');
  });

  it('支持保存用户标识与二次确认重置操作', async () => {
    const api = mockApi();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = mount(GrowthSettings);
    await flushPromises();

    await wrapper.get('[data-test="growth-user-key"]').setValue('work_user');
    await wrapper.get('[data-test="growth-save-user-key"]').trigger('click');
    await flushPromises();
    expect(api.set_growth_settings).toHaveBeenCalledWith(null, null, 'work_user');

    await wrapper.get('[data-test="growth-reset-self"]').trigger('click');
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(api.reset_growth).toHaveBeenCalledWith('self');
  });
});
