import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import TokenUsageSettingsLegacy from './TokenUsageSettingsLegacy.vue';
import { useAppStore } from '../stores/appStore';


const metrics = (total: number, requests: number) => ({
  prompt: Math.max(0, total - 100),
  completion: total > 0 ? 100 : 0,
  cached: Math.max(0, total - 300),
  cache_prompt: Math.max(0, total - 100),
  total,
  requests,
  cache_hit_rate: total > 300 ? 80 : null,
});

const m1 = {
  key: 'openai/deepseek-v4', provider: 'openai', model: 'deepseek-v4',
  ...metrics(900, 2),
};
const m2 = {
  key: 'moonshot/kimi-k2', provider: 'moonshot', model: 'kimi-k2',
  ...metrics(300, 1),
};
const snapshot = {
  ok: true as const,
  range: { days: 30 as const, start_date: '2026-07-11', end_date: '2026-08-09' },
  summary: metrics(1200, 3),
  models: [m1, m2],
  days: [
    { date: '2026-08-08', models: [m1, m2], ...metrics(1200, 3) },
    { date: '2026-08-09', models: [], ...metrics(0, 0) },
  ],
};


describe('TokenUsageSettingsLegacy', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('渲染改版前布局：摘要卡片、每日圆环与模型表格', async () => {
    const store = useAppStore() as any;
    store.tokenStatistics = structuredClone(snapshot);
    store.tokenStatisticsDays = 30;
    vi.spyOn(store, 'loadTokenStatistics').mockResolvedValue(snapshot as any);

    const wrapper = mount(TokenUsageSettingsLegacy);
    await flushPromises();

    expect(wrapper.find('[data-test="token-statistics-page-legacy"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test="token-summary-card"]')).toHaveLength(4);
    expect(wrapper.findAll('[role="img"]')).toHaveLength(2);
    expect(wrapper.find('table').exists()).toBe(true);
    expect(wrapper.findAll('[data-test="model-usage-row"]')).toHaveLength(2);
    expect(wrapper.text()).toContain('deepseek-v4');
  });

  it('无用量时渲染空态占位', async () => {
    const store = useAppStore() as any;
    store.tokenStatistics = {
      ...structuredClone(snapshot),
      summary: metrics(0, 0),
      models: [],
      days: [{ date: '2026-08-09', models: [], ...metrics(0, 0) }],
    };
    vi.spyOn(store, 'loadTokenStatistics').mockResolvedValue(store.tokenStatistics);

    const wrapper = mount(TokenUsageSettingsLegacy);
    await flushPromises();

    expect(wrapper.text()).toContain('暂无 Token 用量记录');
    expect(wrapper.findAll('[role="img"]')).toHaveLength(1);
  });
});
