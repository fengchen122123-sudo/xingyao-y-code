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

it('captures and restores tokenUsage per session runtime (无闪烁切换)', () => {
  const store = useAppStore();

  // 会话 A 收到一份窗口用量
  store.currentSessionName = 'session-a';
  store.handleBridgeEvent({
    type: 'token_usage',
    data: { used: 36800, window: 128000, total_prompt: 36800, total_completion: 6745, total_cached: 30000 },
  });
  expect(store.tokenUsage.used).toBe(36800);

  // 切走：捕获 A 的 runtime（含 tokenUsage）
  store._captureRuntime('session-a');

  // 切到会话 B（无暂存快照 → 保持当前值，等后端事件校正）
  store._bindSessionRuntime('session-b');
  expect(store.tokenUsage.used).toBe(36800);

  // B 收到自己的用量
  store.currentSessionName = 'session-b';
  store.handleBridgeEvent({
    type: 'token_usage',
    data: { used: 5000, window: 128000, total_prompt: 5000, total_completion: 100, total_cached: 2000 },
  });
  store._captureRuntime('session-b');
  expect(store.tokenUsage.used).toBe(5000);

  // 切回 A：立即恢复 A 上次快照，无需等后端事件
  store._bindSessionRuntime('session-a');
  expect(store.tokenUsage.used).toBe(36800);
  expect(store.tokenUsage.total_prompt).toBe(36800);
});

it('token_usage 事件透传 context_breakdown，非法来源与非正 chars 被过滤', () => {
  const store = useAppStore();

  store.handleBridgeEvent({
    type: 'token_usage',
    data: {
      used: 920,
      window: 128_000,
      context_breakdown: [
        { source: 'messages', chars: 620 },
        { source: 'unknown_source', chars: 99 },
        { source: 'skills', chars: 0 },
        { source: 'system_tools', chars: -3 },
        { source: 'tools', chars: 42 },
      ],
    },
  });

  expect(store.tokenUsage.context_breakdown).toEqual([{ source: 'messages', chars: 620 }]);
});

it('normalizeTokenUsage 对 context_breakdown 缺失/异常输入回落 []', () => {
  const usage = normalizeTokenUsage({
    context_breakdown: [
      { source: 'system_prompt', chars: 200 },
      null,
      'junk',
      { chars: 3 },
    ],
  });
  expect(usage.context_breakdown).toEqual([{ source: 'system_prompt', chars: 200 }]);

  // 旧后端无该字段 / 字段为 null → []
  expect(normalizeTokenUsage({ used: 1, window: 128000 }).context_breakdown).toEqual([]);
  expect(normalizeTokenUsage({ context_breakdown: null }).context_breakdown).toEqual([]);
});
