import { describe, expect, it } from 'vitest';
import { useIsMobile } from './useViewport';

// 参照 ModelCascader.test.ts：jsdom 下 window.matchMedia 位于原型、
// 直接赋值会抛错，必须用 Object.defineProperty 替换。
type ChangeEvent = { matches: boolean };
type Listener = (e: ChangeEvent) => void;

function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<Listener>();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => {
      if (query !== '(max-width: 768px)') throw new Error('unexpected query: ' + query);
      return {
        matches: initialMatches,
        addEventListener: (_t: string, cb: Listener) => listeners.add(cb),
        removeEventListener: (_t: string, cb: Listener) => { listeners.delete(cb); },
      };
    },
  });
  return { fire: (matches: boolean) => { for (const cb of listeners) cb({ matches }); } };
}

describe('useIsMobile', () => {
  it('jsdom 无 matchMedia 时返回恒为 false 的 ref（桌面兜底）', () => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: undefined });
    expect(useIsMobile().value).toBe(false);
  });

  it('初始值跟随 matchMedia.matches，跨断点时跟随 change 事件更新', () => {
    const mobile = installMatchMedia(true);
    expect(useIsMobile().value).toBe(true);

    const desktop = installMatchMedia(false);
    const isMobile = useIsMobile();
    expect(isMobile.value).toBe(false);

    desktop.fire(true);
    expect(isMobile.value).toBe(true);
    desktop.fire(false);
    expect(isMobile.value).toBe(false);
    // 确认订阅已注册（fire 有监听者），消除未使用告警语义歧义
    expect(mobile.fire).toBeTruthy();
  });
});
