import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DecisionRecordPopup from './DecisionRecordPopup.vue';
import { useAppStore } from '../stores/appStore';

// mock 决策桥方法：默认未启用 + 无记录；用例按需覆盖
function mockApi(overrides: Record<string, unknown> = {}) {
  const api = {
    get_decision_audit_summary: vi.fn(async () => ({
      ok: true,
      today_count: 0,
      today_shadow_count: 0,
      today_adopted_count: 0,
      today_fallback_count: 0,
      recent: [],
    })),
    get_decision_settings: vi.fn(async () => ({
      ok: true,
      settings: { enabled: false, model_id: 'NeoHorse-Jev-4B' },
      runtime: {},
    })),
    open_decision_audit_dir: vi.fn(async () => ({ ok: true })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

describe('DecisionRecordPopup', () => {
  it('未启用且无记录：显示未启用状态与空态占位', async () => {
    mockApi();
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('未启用');
    expect(wrapper.find('[data-test="decision-pop-empty"]').exists()).toBe(true);
  });

  it('有记录：渲染统计行与最近记录列表（含影子标记与延迟）', async () => {
    mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 2,
        today_shadow_count: 1,
        today_adopted_count: 1,
        today_fallback_count: 0,
        recent: [
          { ts: '2026-10-16T10:00:00', scene: 'probe', shadow: false, adopted: true, latency_ms: 326, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '' },
          { ts: '2026-10-16T10:01:00', scene: 'router', shadow: true, adopted: false, latency_ms: 210, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '' },
        ],
      })),
      get_decision_settings: vi.fn(async () => ({
        ok: true,
        settings: { enabled: true, model_id: 'NeoHorse-Jev-4B' },
        runtime: {},
      })),
    });
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('已启用');
    expect(wrapper.find('[data-test="decision-pop-stats"]').text()).toContain('今日 2 次决策');
    const list = wrapper.find('[data-test="decision-pop-list"]').text();
    expect(list).toContain('probe');
    expect(list).toContain('router');
    expect(list).toContain('影子');
    expect(list).toContain('326ms');
  });

  it('去设置：切到决策分区并关闭面板', async () => {
    mockApi();
    const store = useAppStore();
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    await wrapper.find('[data-test="decision-pop-open-settings"]').trigger('click');
    expect(store.settingsSection).toBe('decision');
    expect(store.settingsOpen).toBe(true);
    expect(wrapper.emitted('close')).toBeTruthy();
  });

  it('打开审计目录：调用桥方法', async () => {
    const api = mockApi();
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    await wrapper.find('[data-test="decision-pop-open-audit"]').trigger('click');
    await flushPromises();
    expect(api.open_decision_audit_dir).toHaveBeenCalledTimes(1);
  });

  it('面板开启期间收到新决策事件：自动刷新记录并保持已读', async () => {
    const api = mockApi();
    const store = useAppStore();
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();
    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);

    // 模拟后端 decision_recorded 事件（store 侧 tick 递增）
    store.decisionUnread = 3;
    store.decisionEventTick += 1;
    await flushPromises();

    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(2);
    expect(store.decisionUnread).toBe(0);
    wrapper.unmount();
  });
});
