import { describe, expect, it } from 'vitest';
import { SHORT_LABEL_MAX, shortLabel } from './shortLabel';

describe('shortLabel', () => {
  it('默认保留前 6 个字符并加省略号', () => {
    expect(SHORT_LABEL_MAX).toBe(6);
    expect(shortLabel('基元律动请送我token')).toBe('基元律动请送…');
  });

  it('不超过上限时原样返回', () => {
    expect(shortLabel('星瑶Code')).toBe('星瑶Code');
    expect(shortLabel('123456')).toBe('123456');
  });

  it('去除首尾空白后再判断长度', () => {
    expect(shortLabel('  星瑶Code  ')).toBe('星瑶Code');
  });

  it('空值返回空串，不抛错', () => {
    expect(shortLabel(undefined)).toBe('');
    expect(shortLabel(null)).toBe('');
    expect(shortLabel('')).toBe('');
  });

  it('按码点截断，不劈开 emoji', () => {
    expect(shortLabel('😀😀😀😀😀😀😀', 6)).toBe('😀😀😀😀😀😀…');
  });

  it('自定义上限按整数字符数生效', () => {
    expect(shortLabel('abcdefgh', 3)).toBe('abc…');
    expect(shortLabel('abcdefgh', 0)).toBe('a…');
  });
});
