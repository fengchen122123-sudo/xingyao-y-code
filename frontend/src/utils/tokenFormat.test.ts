import { describe, expect, it } from 'vitest';
import { buildTokenSummary, formatTokenCount } from './tokenFormat';

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

describe('buildTokenSummary（输入/输出/命中，完整数值）', () => {
  it('完整数值组装，命中率分母为输入', () => {
    expect(buildTokenSummary({
      total_prompt: 803834,
      total_completion: 6745,
      total_cached: 740967,
    })).toBe('输入 803,834 · 输出 6,745 · 命中 740,967 (92%)');
  });

  it('输入为 0 时命中率为 0%', () => {
    expect(buildTokenSummary({
      total_prompt: 0,
      total_completion: 0,
      total_cached: 0,
    })).toBe('输入 0 · 输出 0 · 命中 0 (0%)');
  });

  it('命中率四舍五入取整', () => {
    expect(buildTokenSummary({
      total_prompt: 1000,
      total_completion: 100,
      total_cached: 766,
    })).toBe('输入 1,000 · 输出 100 · 命中 766 (77%)');
  });
});
