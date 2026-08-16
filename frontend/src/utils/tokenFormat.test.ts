import { describe, expect, it } from 'vitest';
import { buildTokenSummary, formatTokenCount } from './tokenFormat';

describe('formatTokenCount（与 CLI format_num 口径一致）', () => {
  it('小于 1000 原样显示', () => {
    expect(formatTokenCount(0)).toBe('0');
    expect(formatTokenCount(999)).toBe('999');
  });

  it('千级显示 K，整数不带小数点', () => {
    expect(formatTokenCount(1000)).toBe('1K');
    expect(formatTokenCount(52000)).toBe('52K');
    expect(formatTokenCount(52900)).toBe('52.9K');
    expect(formatTokenCount(1600)).toBe('1.6K');
  });

  it('百万级显示 M', () => {
    expect(formatTokenCount(1000000)).toBe('1M');
    expect(formatTokenCount(2500000)).toBe('2.5M');
  });
});

describe('buildTokenSummary（与 CLI 状态栏一致：输入/输出/命中）', () => {
  it('按 CLI 格式组装，命中率分母为输入', () => {
    expect(buildTokenSummary({
      total_prompt: 52900,
      total_completion: 1600,
      total_cached: 40700,
    })).toBe('输入 52.9K · 输出 1.6K · 命中 40.7K (77%)');
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
    })).toBe('输入 1K · 输出 100 · 命中 766 (77%)');
  });
});
