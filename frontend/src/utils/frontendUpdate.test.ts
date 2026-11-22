import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  createReloadScheduler,
  registerFrontendUpdateHandlers,
  requestFrontendReload,
  __resetFrontendUpdateForTest,
  __setFrontendUpdateDepsForTest,
  __getFrontendUpdateSchedulerForTest,
  FRONTEND_AUTO_RELOAD_TS_KEY,
  FRONTEND_AUTO_RELOAD_COUNT_KEY,
} from './frontendUpdate';

const TS_KEY = FRONTEND_AUTO_RELOAD_TS_KEY;
const T0 = 1_700_000_000_000;

describe('frontendUpdate 自愈调度', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    __resetFrontendUpdateForTest();
  });

  afterEach(() => {
    __resetFrontendUpdateForTest();
    vi.useRealTimers();
  });

  it('vite:preloadError：条件满足立即刷新、写防风暴时间戳，且不阻断默认行为', () => {
    const reload = vi.fn();
    __setFrontendUpdateDepsForTest({ reload });
    registerFrontendUpdateHandlers({ canReload: () => true });
    vi.setSystemTime(T0 + 6000);

    const ev = new Event('vite:preloadError', { cancelable: true });
    const notCanceled = window.dispatchEvent(ev);

    expect(reload).toHaveBeenCalledTimes(1);
    // 未 preventDefault：dispatchEvent 返回 true 表示事件未被取消，
    // 调用方（mermaidRenderer 源码降级等）的既有路径保持原样
    expect(notCanceled).toBe(true);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 6000));

    window.dispatchEvent(new Event('vite:preloadError'));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('canReload=false：保持 pending 不刷新；闸门放开后由 1 秒复检定时器推进刷新', () => {
    const reload = vi.fn();
    let canReload = false;
    __setFrontendUpdateDepsForTest({ reload });
    registerFrontendUpdateHandlers({ canReload: () => canReload });
    vi.setSystemTime(T0 + 6000);

    requestFrontendReload('dist-updated');
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(3000);
    expect(reload).not.toHaveBeenCalled();

    canReload = true;
    vi.advanceTimersByTime(1000);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 10000));
  });

  it('距上次自动刷新不足 10 秒：不刷新，满 10 秒后的复检才放行', () => {
    const reload = vi.fn();
    __setFrontendUpdateDepsForTest({ reload });
    registerFrontendUpdateHandlers({ canReload: () => true });
    vi.setSystemTime(T0 + 6000);
    sessionStorage.setItem(TS_KEY, String(T0 + 3000));

    requestFrontendReload('dist-updated');
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(6999);
    expect(reload).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 13000));
  });

  it('距最后用户活动不足 5 秒：不刷新；keydown/pointerdown 活动过去后刷新', () => {
    const reload = vi.fn();
    __setFrontendUpdateDepsForTest({ reload });
    registerFrontendUpdateHandlers({ canReload: () => true });
    vi.setSystemTime(T0 + 6000);

    window.dispatchEvent(new Event('keydown'));
    requestFrontendReload('preload-error');
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(4000);
    expect(reload).not.toHaveBeenCalled();

    window.dispatchEvent(new Event('pointerdown'));
    vi.advanceTimersByTime(4999);
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 15000));
  });

  it('__resetFrontendUpdateForTest：清 pending/时间戳/定时器后可重新测试', () => {
    const first = vi.fn();
    __setFrontendUpdateDepsForTest({ reload: first });
    registerFrontendUpdateHandlers({ canReload: () => true });
    vi.setSystemTime(T0 + 6000);
    window.dispatchEvent(new Event('vite:preloadError'));
    expect(first).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 6000));

    __resetFrontendUpdateForTest();
    expect(sessionStorage.getItem(TS_KEY)).toBeNull();
    vi.advanceTimersByTime(5000);
    expect(first).toHaveBeenCalledTimes(1);

    const second = vi.fn();
    __setFrontendUpdateDepsForTest({ reload: second });
    vi.setSystemTime(T0 + 20000);
    requestFrontendReload('preload-error');
    expect(second).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 20000));
  });

  it('createReloadScheduler 工厂直用：手动 tick 推进，条件全满足才 reload', () => {
    let clock = 1000;
    const store = new Map<string, string>();
    const reload = vi.fn();
    const scheduler = createReloadScheduler({
      reload,
      now: () => clock,
      canReload: () => true,
      storage: {
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => { store.set(k, v); },
        removeItem: (k) => { store.delete(k); },
      },
      minIntervalMs: 10000,
      activityGraceMs: 5000,
    });

    scheduler.request('preload-error');
    expect(scheduler.isPending()).toBe(true);
    expect(reload).not.toHaveBeenCalled();

    clock = 6000;
    scheduler.tick();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(scheduler.isPending()).toBe(false);
    expect(store.get(TS_KEY)).toBe('6000');

    clock = 10000;
    scheduler.request('dist-updated');
    expect(reload).toHaveBeenCalledTimes(1);
    clock = 16000;
    scheduler.tick();
    expect(reload).toHaveBeenCalledTimes(2);
    expect(store.get(TS_KEY)).toBe('16000');

    scheduler.reset();
    expect(scheduler.isPending()).toBe(false);
  });

  it('连续自愈刷新达到上限后停止自动刷新并告警（dist 持续损坏不再无限重载）', () => {
    const reload = vi.fn();
    const onExhausted = vi.fn();
    __setFrontendUpdateDepsForTest({ reload, onExhausted, maxReloadsPerSession: 3 });
    registerFrontendUpdateHandlers({ canReload: () => true });

    for (let i = 0; i < 3; i++) {
      vi.setSystemTime(T0 + 6000 + i * 11000);
      requestFrontendReload('dist-updated', `fp-${i}`);
    }
    expect(reload).toHaveBeenCalledTimes(3);
    expect(sessionStorage.getItem(FRONTEND_AUTO_RELOAD_COUNT_KEY)).toBe('3');

    vi.setSystemTime(T0 + 60000);
    requestFrontendReload('dist-updated', 'fp-3');
    expect(reload).toHaveBeenCalledTimes(3);
    expect(onExhausted).toHaveBeenCalledTimes(1);
    expect(__getFrontendUpdateSchedulerForTest().isPending()).toBe(false);

    vi.advanceTimersByTime(5000);
    expect(reload).toHaveBeenCalledTimes(3);
  });

  it('fingerprint 未变化不重复刷新，指纹更替后才刷新', () => {
    const reload = vi.fn();
    __setFrontendUpdateDepsForTest({ reload });
    registerFrontendUpdateHandlers({ canReload: () => true });
    vi.setSystemTime(T0 + 6000);
    requestFrontendReload('dist-updated', 'mtime:100:2048');
    expect(reload).toHaveBeenCalledTimes(1);

    vi.setSystemTime(T0 + 40000);
    requestFrontendReload('dist-updated', 'mtime:100:2048');
    expect(reload).toHaveBeenCalledTimes(1);

    vi.setSystemTime(T0 + 50000);
    requestFrontendReload('dist-updated', 'mtime:200:4096');
    expect(reload).toHaveBeenCalledTimes(2);
  });
});