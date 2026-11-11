import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


const snapshot = {
  ok: true,
  range: { days: 7, start_date: '2026-08-03', end_date: '2026-08-09' },
  summary: {
    prompt: 1000,
    completion: 100,
    cached: 800,
    cache_prompt: 1000,
    total: 1100,
    requests: 3,
    cache_hit_rate: 80,
  },
  models: [
    {
      key: 'openai/m1', provider: 'openai', model: 'm1', prompt: 1000,
      completion: 100, cached: 800, cache_prompt: 1000, total: 1100,
      requests: 3, cache_hit_rate: 80,
    },
  ],
  days: [],
};


describe('token statistics store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('applies a successful snapshot and selected range together', async () => {
    const getStatistics = vi.fn().mockResolvedValue(structuredClone(snapshot));
    (window as any).pywebview = { api: { get_token_usage_statistics: getStatistics } };
    const store = useAppStore() as any;

    const result = await store.loadTokenStatistics(7, true);

    expect(result?.ok).toBe(true);
    expect(getStatistics).toHaveBeenCalledWith(7);
    expect(store.tokenStatisticsDays).toBe(7);
    expect(store.tokenStatistics.summary.total).toBe(1100);
    expect(store.tokenStatisticsLoading).toBe(false);
  });

  it('keeps the previous successful snapshot when refresh fails', async () => {
    const getStatistics = vi.fn().mockResolvedValue({ ok: false, msg: 'database busy' });
    (window as any).pywebview = { api: { get_token_usage_statistics: getStatistics } };
    const store = useAppStore() as any;
    store.tokenStatistics = structuredClone(snapshot);
    store.tokenStatisticsDays = 7;

    const result = await store.loadTokenStatistics(30, true);

    expect(result?.ok).toBe(false);
    expect(store.tokenStatisticsDays).toBe(7);
    expect(store.tokenStatistics.summary.total).toBe(1100);
    expect(store.tokenStatisticsLoading).toBe(false);
  });

  it('loads the lifetime (all history) snapshot', async () => {
    const lifetimeSnapshot = structuredClone(snapshot);
    // 累计范围下后端回填实际覆盖天数，而不是固定 365
    lifetimeSnapshot.range.days = 120;
    const getStatistics = vi.fn().mockResolvedValue(lifetimeSnapshot);
    (window as any).pywebview = { api: { get_token_usage_statistics: getStatistics } };
    const store = useAppStore() as any;

    const result = await store.loadTokenStatisticsLifetime(true);

    expect(result?.ok).toBe(true);
    expect(getStatistics).toHaveBeenCalledWith(0);
    expect(store.tokenStatisticsLifetime.range.days).toBe(120);
    expect(store.tokenStatisticsLifetimeLoading).toBe(false);
  });

  it('keeps the previous lifetime snapshot when refresh fails', async () => {
    const getStatistics = vi.fn().mockResolvedValue({ ok: false, msg: 'database busy' });
    (window as any).pywebview = { api: { get_token_usage_statistics: getStatistics } };
    const store = useAppStore() as any;
    const oldLifetime = structuredClone(snapshot);
    oldLifetime.range.days = 365;
    store.tokenStatisticsLifetime = oldLifetime;

    const result = await store.loadTokenStatisticsLifetime(true);

    expect(result?.ok).toBe(false);
    expect(store.tokenStatisticsLifetime.range.days).toBe(365);
    expect(store.tokenStatisticsLifetime.summary.total).toBe(1100);
    expect(store.tokenStatisticsLifetimeLoading).toBe(false);
  });
});
