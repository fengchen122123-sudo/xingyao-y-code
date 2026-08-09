import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';


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
