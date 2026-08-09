import { describe, expect, it } from 'vitest';

import { donutGradient, formatTokenCount, modelColor } from './tokenStatistics';


describe('token statistics view helpers', () => {
  it('builds proportional donut segments for multiple models', () => {
    const gradient = donutGradient([
      { key: 'openai/m1', total: 75 },
      { key: 'moonshot/m2', total: 25 },
    ] as any);

    expect(gradient).toContain('0.000% 75.000%');
    expect(gradient).toContain('75.000% 100.000%');
    expect(gradient.match(/#[0-9A-Fa-f]{6}/g)?.length).toBe(2);
  });

  it('uses an empty ring when the day has no tokens', () => {
    expect(donutGradient([])).toBe('conic-gradient(var(--bg3) 0% 100%)');
  });

  it('fills the whole ring for a single model', () => {
    expect(donutGradient([{ key: 'openai/m1', total: 42 }])).toContain(
      '0.000% 100.000%',
    );
  });

  it('keeps model colors stable and formats readable totals', () => {
    expect(modelColor('openai/m1')).toBe(modelColor('openai/m1'));
    expect(modelColor('openai/m1')).not.toBe(modelColor('moonshot/m2'));
    expect(formatTokenCount(999)).toBe('999');
    expect(formatTokenCount(1_250)).toBe('1.3K');
    expect(formatTokenCount(3_400_000)).toBe('3.4M');
  });
});
