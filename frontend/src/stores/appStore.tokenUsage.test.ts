import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { normalizeTokenUsage, useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

it('preserves all explicit cache scopes from the bridge event', () => {
  const store = useAppStore();

  store.handleBridgeEvent({
    type: 'token_usage',
    data: {
      used: 36_800,
      window: 1_000_000,
      cumulative: 500_000,
      cached_pct: 91,
      cache_available: true,
      cache_latest_pct: 99,
      cache_hot_pct: 98.8,
      cache_session_pct: 91,
      cache_hot_turns: 8,
      cache_invalidation_reason: '',
      prompt_tokens: 420_000,
      cached_tokens: 382_200,
    },
  });

  expect(store.tokenUsage.cache_latest_pct).toBe(99);
  expect(store.tokenUsage.cache_hot_pct).toBe(98.8);
  expect(store.tokenUsage.cache_session_pct).toBe(91);
  expect(store.tokenUsage.cache_available).toBe(true);
  expect(store.tokenUsage.prompt_tokens).toBe(420_000);
  expect(store.tokenUsage.cached_tokens).toBe(382_200);
});

it('keeps unavailable cache rates as null instead of converting them to zero', () => {
  const store = useAppStore();

  store.handleBridgeEvent({
    type: 'token_usage',
    data: {
      used: 100,
      window: 128_000,
      cumulative: 120,
      cached_pct: 0,
      cache_available: false,
      cache_latest_pct: null,
      cache_hot_pct: null,
      cache_session_pct: null,
      cache_hot_turns: 0,
      cache_invalidation_reason: '服务端未报告缓存 token 明细',
      prompt_tokens: 0,
      cached_tokens: 0,
    },
  });

  expect(store.tokenUsage.cache_latest_pct).toBeNull();
  expect(store.tokenUsage.cache_hot_pct).toBeNull();
  expect(store.tokenUsage.cache_session_pct).toBeNull();
  expect(store.tokenUsage.cache_available).toBe(false);
});

it('normalizes CLI-style totals with zero fallback', () => {
  const usage = normalizeTokenUsage({
    total_prompt: 480000,
    total_completion: 20000,
    total_cached: 382200,
  });

  expect(usage.total_prompt).toBe(480000);
  expect(usage.total_completion).toBe(20000);
  expect(usage.total_cached).toBe(382200);

  // 旧后端 payload 无新字段时回落 0，不产生 NaN
  const legacy = normalizeTokenUsage({ used: 1, window: 128000 });
  expect(legacy.total_prompt).toBe(0);
  expect(legacy.total_completion).toBe(0);
  expect(legacy.total_cached).toBe(0);
});

it('normalizes a legacy backend payload without inventing cache rates', () => {
  const usage = normalizeTokenUsage({
    used: 12,
    window: 128_000,
    cached_pct: 0,
    cumulative: 15,
  });

  expect(usage.used).toBe(12);
  expect(usage.cache_latest_pct).toBeNull();
  expect(usage.cache_hot_pct).toBeNull();
  expect(usage.cache_session_pct).toBeNull();
  expect(usage.cache_available).toBe(false);
});
