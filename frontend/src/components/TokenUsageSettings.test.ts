import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import TokenUsageSettings from './TokenUsageSettings.vue';
import { useAppStore } from '../stores/appStore';

const DAY_MS = 86_400_000;
const RANGE_END = '2026-08-09';

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

function dateKeysEndingAt(endDate: string, count: number): string[] {
  const end = new Date(`${endDate}T00:00:00.000Z`).getTime();
  return Array.from({ length: count }, (_, index) => (
    new Date(end - (count - 1 - index) * DAY_MS).toISOString().slice(0, 10)
  ));
}

function makeDay(date: string, total: number, requests = 1, models: unknown[] = []) {
  return { date, models, ...metrics(total, requests) };
}

// 7 天范围快照：2 个模型，只有 2 天有真实用量。
const sevenDayDates = dateKeysEndingAt(RANGE_END, 7);
const statistics7 = {
  ok: true as const,
  range: { days: 7 as const, start_date: sevenDayDates[0], end_date: RANGE_END },
  summary: metrics(1200, 3),
  models: [m1, m2],
  days: sevenDayDates.map((date, index) => {
    if (index === 4) return makeDay(date, 900, 2, [m1]);
    if (index === 5) return makeDay(date, 300, 1, [m2]);
    return makeDay(date, 0, 0, []);
  }),
};

// 365 天 lifetime 快照：3 个活跃日，最后两天连续。
const lifetimeDates = dateKeysEndingAt(RANGE_END, 365);
const lifetime = {
  ok: true as const,
  range: { days: 365 as const, start_date: lifetimeDates[0], end_date: RANGE_END },
  summary: metrics(800, 3),
  models: [m1],
  days: lifetimeDates.map((date, index) => {
    if (index === 10) return makeDay(date, 100, 1, [m1]);
    if (index === 363) return makeDay(date, 500, 1, [m1]);
    if (index === 364) return makeDay(date, 200, 1, [m1]);
    return makeDay(date, 0, 0, []);
  }),
};

function mountPage(options: {
  statistics?: unknown;
  lifetime?: unknown;
  days?: 1 | 7 | 30;
} = {}) {
  const store = useAppStore() as any;
  store.tokenStatistics = options.statistics === undefined
    ? structuredClone(statistics7)
    : options.statistics;
  store.tokenStatisticsDays = options.days ?? 7;
  store.tokenStatisticsLifetime = options.lifetime === undefined
    ? structuredClone(lifetime)
    : options.lifetime;
  const load = vi
    .spyOn(store, 'loadTokenStatistics')
    .mockResolvedValue(store.tokenStatistics);
  const loadLifetime = vi
    .spyOn(store, 'loadTokenStatisticsLifetime')
    .mockResolvedValue(store.tokenStatisticsLifetime);
  return { store, load, loadLifetime, wrapper: mount(TokenUsageSettings) };
}

describe('TokenUsageSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('渲染摘要条、52 列热力图、范围切换、趋势与环图', async () => {
    const { load, loadLifetime, wrapper } = mountPage();
    await flushPromises();

    // 累计摘要条：5 格，含 lifetime 汇总值。
    const strip = wrapper.get('[data-test="token-summary-strip"]');
    expect(wrapper.findAll('[data-test="token-summary-item"]')).toHaveLength(5);
    expect(strip.text()).toContain('总 Token');
    expect(strip.text()).toContain('800');
    expect(strip.text()).toContain('活跃天数');
    expect(strip.text()).toContain('3 天');
    expect(strip.text()).toContain('当前连续');
    expect(strip.text()).toContain('最长连续');

    // 热力图：52 列 × 7 格 = 364 格。
    expect(wrapper.findAll('[data-test="heatmap-column"]')).toHaveLength(52);
    expect(wrapper.findAll('[data-test="heatmap-cell"]')).toHaveLength(364);
    expect(wrapper.findAll('[data-test="heatmap-cell"].has-usage')).toHaveLength(3);
    expect(wrapper.get('[data-test="heatmap-cell"]').attributes('title')).toContain('Token');
    expect(wrapper.get('[data-test="heatmap-mode-daily"]').attributes('aria-pressed')).toBe('true');
    await wrapper.get('[data-test="heatmap-mode-weekly"]').trigger('click');
    expect(wrapper.get('[data-test="heatmap-mode-weekly"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.findAll('[data-test="heatmap-cell"]')).toHaveLength(364);
    expect(wrapper.get('[data-test="heatmap-column"]').attributes('title')).toContain('Token');

    // 范围 tabs：选中态来自 store，点击强制刷新对应范围；UI 不再提供「今日」入口。
    expect(wrapper.find('[data-test="range-1"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="range-7"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.get('[data-test="range-30"]').attributes('aria-pressed')).toBe('false');
    await wrapper.get('[data-test="range-7"]').trigger('click');
    expect(load).toHaveBeenLastCalledWith(7, true);

    // 趋势图：前 2 个模型各一条折线，7 天各一个 hover 列。
    expect(wrapper.findAll('[data-test="trend-legend-item"]')).toHaveLength(2);
    expect(wrapper.findAll('[data-test="trend-line"]')).toHaveLength(2);
    expect(wrapper.findAll('[data-test="trend-col"]')).toHaveLength(7);
    expect(wrapper.text()).toContain('deepseek-v4');
    expect(wrapper.text()).toContain('kimi-k2');
    expect(wrapper.get('[data-test="trend-col"]').find('title').text()).toContain('总计');

    // 环图列表：每行含占比与「总量 · 输入 · 输出 · 缓存 · 请求」副行。
    const rows = wrapper.findAll('[data-test="model-usage-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain('deepseek-v4');
    expect(rows[0].text()).toContain('75%');
    expect(rows[0].text()).toContain('900');
    expect(rows[0].text()).toContain('输入');
    expect(rows[0].text()).toContain('输出');
    expect(rows[0].text()).toContain('缓存');
    expect(rows[0].text()).toContain('次请求');
    expect(rows[1].text()).toContain('300');

    // 刷新按钮：同时强制刷新范围与 lifetime 两份数据。
    await wrapper.get('[data-test="token-refresh"]').trigger('click');
    expect(load).toHaveBeenLastCalledWith(7, true);
    expect(loadLifetime).toHaveBeenLastCalledWith(true);
  });

  it('首次加载时只显示 loading 视图', async () => {
    const store = useAppStore() as any;
    store.tokenStatistics = null;
    store.tokenStatisticsLifetime = null;
    store.tokenStatisticsLoading = true;
    vi.spyOn(store, 'loadTokenStatistics').mockReturnValue(new Promise(() => {}));
    vi.spyOn(store, 'loadTokenStatisticsLifetime').mockReturnValue(new Promise(() => {}));

    const wrapper = mount(TokenUsageSettings);
    await flushPromises();

    expect(wrapper.get('[data-test="token-statistics-page"]').attributes('aria-busy')).toBe('true');
    expect(wrapper.find('[role="status"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('正在读取本地 Token 统计');
    expect(wrapper.findAll('[data-test="token-summary-item"]')).toHaveLength(0);
    expect(wrapper.findAll('[data-test="heatmap-cell"]')).toHaveLength(0);
  });

  it('无 lifetime 与无范围用量时渲染占位与虚线空态', async () => {
    const { wrapper } = mountPage({
      statistics: {
        ok: true,
        range: { days: 7, start_date: sevenDayDates[0], end_date: RANGE_END },
        summary: metrics(0, 0),
        models: [],
        days: [makeDay(RANGE_END, 0, 0)],
      },
      lifetime: null,
    });
    await flushPromises();

    const strip = wrapper.get('[data-test="token-summary-strip"]');
    expect(wrapper.findAll('[data-test="token-summary-item"]')).toHaveLength(5);
    expect(strip.text()).toContain('--');
    expect(wrapper.find('[data-test="heatmap-cell"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('暂无用量数据');
    expect(wrapper.text()).toContain('暂无模型用量');
    expect(wrapper.find('table').exists()).toBe(false);
  });

  it('30 天范围完整渲染 30 个趋势列', async () => {
    const thirtyDayDates = dateKeysEndingAt(RANGE_END, 30);
    const { wrapper } = mountPage({
      days: 30,
      statistics: {
        ok: true,
        range: { days: 30, start_date: thirtyDayDates[0], end_date: RANGE_END },
        summary: metrics(1200, 3),
        models: [m1, m2],
        days: thirtyDayDates.map((date, index) => (
          index === 29 ? makeDay(date, 1200, 3, [m1, m2]) : makeDay(date, 0, 0, [])
        )),
      },
    });
    await flushPromises();

    expect(wrapper.get('[data-test="range-30"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.findAll('[data-test="trend-col"]')).toHaveLength(30);
    expect(wrapper.findAll('[data-test="trend-line"]')).toHaveLength(2);
  });
});