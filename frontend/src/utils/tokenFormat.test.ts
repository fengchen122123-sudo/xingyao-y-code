import { describe, expect, it } from 'vitest';
import { buildSessionTokenSummary, formatTokenCount, sessionHitRate } from './tokenFormat';

describe('formatTokenCount（精确数值，千分位分隔）', () => {
  it('小于 1000 原样显示', () => {
    expect(formatTokenCount(0)).toBe('0');
    expect(formatTokenCount(999)).toBe('999');
  });

  it('千级及以上显示千分位完整数值', () => {
    expect(formatTokenCount(1000)).toBe('1,000');
    expect(formatTokenCount(52000)).toBe('52,000');
    expect(formatTokenCount(52900)).toBe('52,900');
    expect(formatTokenCount(803834)).toBe('803,834');
  });

  it('百万级同样显示完整数值', () => {
    expect(formatTokenCount(1000000)).toBe('1,000,000');
    expect(formatTokenCount(2500000)).toBe('2,500,000');
  });
});

describe('sessionHitRate / buildSessionTokenSummary（本会话口径）', () => {
  it('命中率分母只取已报告缓存明细的轮次（cache_prompt）', () => {
    expect(sessionHitRate({ cached: 740, cache_prompt: 1000 })).toBeCloseTo(74);
    // 未报告轮次不进分母：prompt 更大也不稀释命中率
    expect(sessionHitRate({ cached: 740, cache_prompt: 1000 })).toBeLessThan(74.001);
  });

  it('无缓存样本（cache_prompt=0）返回 null，不冒充 0%', () => {
    expect(sessionHitRate({ cached: 0, cache_prompt: 0 })).toBeNull();
  });

  it('本会话摘要组装：带"本会话"前缀 + 千分位', () => {
    expect(buildSessionTokenSummary({
      session_id: 's', prompt: 12480, completion: 3206, cached: 9876,
      cache_prompt: 12480, total: 15686, requests: 23, last_ts: null,
      keepalive_prompt: 0, keepalive_cached: 0, keepalive_requests: 0,
    })).toBe('本会话 输入 12,480 · 输出 3,206 · 命中 9,876 (79%)');
  });

  it('无缓存样本时省略百分比，不显示 NaN', () => {
    expect(buildSessionTokenSummary({
      session_id: 's', prompt: 0, completion: 0, cached: 0,
      cache_prompt: 0, total: 0, requests: 0, last_ts: null,
      keepalive_prompt: 0, keepalive_cached: 0, keepalive_requests: 0,
    })).toBe('本会话 输入 0 · 输出 0 · 命中 0');
  });
});
