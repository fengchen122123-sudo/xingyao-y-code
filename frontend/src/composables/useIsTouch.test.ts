import { describe, expect, it, vi, afterEach } from 'vitest';
import { useIsTouch } from './useIsTouch';

function mockMatchMedia(matches: boolean) {
  (window as unknown as { matchMedia: unknown }).matchMedia = vi
    .fn()
    .mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
}

describe('useIsTouch', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('pointer: coarse 匹配时为 true（手机/平板）', () => {
    mockMatchMedia(true);
    expect(useIsTouch().value).toBe(true);
  });

  it('pointer: coarse 不匹配时为 false（桌面）', () => {
    mockMatchMedia(false);
    expect(useIsTouch().value).toBe(false);
  });

  it('matchMedia 不可用时按桌面 false 处理', () => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia;
    expect(useIsTouch().value).toBe(false);
  });
});
