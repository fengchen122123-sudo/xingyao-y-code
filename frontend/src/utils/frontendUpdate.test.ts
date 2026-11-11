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

// 界面资源版本失配自愈：验证「dist 更新事件 / vite:preloadError → 界面空闲时
// 自动刷新」的调度逻辑与三道闸门（无进行中回合 / 10 秒防风暴 / 5 秒活动宽限）。
//
// 说明：window 事件监听是幂等的，本文件内多次调用 register 不会叠加监听器，
// canReload 注入以最后一次为准；每个用例前用 __reset 清 pending/时间戳/定时器，
// 并用 __setFrontendUpdateDepsForTest 注入 reload spy（默认单例 reload 是
// location.reload，jsdom 中不可导航，必须替换）。时间用 vi.useFakeTimers 控制：
// Date.now 被伪造（调度器默认 now = Date.now），1 秒复检定时器也由 fake timers 驱动。

const TS_KEY = FRONTEND_AUTO_RELOAD_TS_KEY;
// 假时钟基准（与 sessionStorage 中存的时间戳同量纲：Date.now() 毫秒）
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
    // 调度器创建于 T0（活动基线），把时钟推过 5 秒宽限，模拟界面已空闲
    vi.setSystemTime(T0 + 6000);

    const ev = new Event('vite:preloadError', { cancelable: true });
    const notCanceled = window.dispatchEvent(ev);

    expect(reload).toHaveBeenCalledTimes(1);
    // 未 preventDefault：dispatchEvent 返回 true 表示事件未被取消，
    // 调用方（mermaidRenderer 源码降级等）的既有路径保持原样
    expect(notCanceled).toBe(true);
    expect(sessionStorage.getItem(TS_KEY)).toBe(String(T0 + 6000));

    // 刚刷过：同一会话 10 秒内再收到事件不重复刷新（防风暴，见下一条用例细节）
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

    // 回合仍在进行：定时器多次复检也不放行
    vi.advanceTimersByTime(3000);
    expect(reload).not.toHaveBeenCalled();

    // 回合结束（runningSessions 清空）：下一次复检完成刷新
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
    // 3 秒前刚发生过一次自动刷新（整页刷新后时间戳仍在 sessionStorage）
    sessionStorage.setItem(TS_KEY, String(T0 + 3000));

    requestFrontendReload('dist-updated');
    expect(reload).not.toHaveBeenCalled();

    // 复检到「距上次刷新满 10 秒」的那一刻才刷新
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

    // keydown 计入活动：正在打字时不刷新
    window.dispatchEvent(new Event('keydown'));
    requestFrontendReload('preload-error');
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(4000);
    expect(reload).not.toHaveBeenCalled();

    // pointerdown 同样计入活动：宽限重新计时
    window.dispatchEvent(new Event('pointerdown'));
    vi.advanceTimersByTime(4999);
    expect(reload).not.toHaveBeenCalled();

    // 活动静默满 5 秒：下一次复检放行
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
    // 旧的 1 秒定时器已随 reset 清理：推进时间不再触发任何刷新
    vi.advanceTimersByTime(5000);
    expect(first).toHaveBeenCalledTimes(1);

    // 重置后注入新依赖可重测（时间戳已清、活动基线为重置时刻）
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

    // 调度器创建即记录活动基线（1000）：5 秒内请求保持 pending 不刷新
    scheduler.request('preload-error');
    expect(scheduler.isPending()).toBe(true);
    expect(reload).not.toHaveBeenCalled();

    clock = 6000;
    scheduler.tick();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(scheduler.isPending()).toBe(false);
    expect(store.get(TS_KEY)).toBe('6000');

    // 10 秒防风暴：短时间内再请求不刷新，满足间隔后 tick 才放行
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

    // 前三次（每次间隔满 10 秒）各自成功刷新，累计次数达到上限
    for (let i = 0; i < 3; i++) {
      vi.setSystemTime(T0 + 6000 + i * 11000);
      requestFrontendReload('dist-updated', `fp-${i}`);
    }
    expect(reload).toHaveBeenCalledTimes(3);
    expect(sessionStorage.getItem(FRONTEND_AUTO_RELOAD_COUNT_KEY)).toBe('3');

    // 第四次：触顶后不再刷新，改为告警，且不进入 pending（不再注册复检定时器）
    vi.setSystemTime(T0 + 60000);
    requestFrontendReload('dist-updated', 'fp-3');
    expect(reload).toHaveBeenCalledTimes(3);
    expect(onExhausted).toHaveBeenCalledTimes(1);
    expect(__getFrontendUpdateSchedulerForTest().isPending()).toBe(false);

    // 复检节奏推进也不再触发刷新（定时器已停）
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

    // 同一指纹：即便早已超过 10 秒防风暴窗口，也不重复刷新
    vi.setSystemTime(T0 + 40000);
    requestFrontendReload('dist-updated', 'mtime:100:2048');
    expect(reload).toHaveBeenCalledTimes(1);

    // 指纹更替（dist 真正更新）：正常刷新
    vi.setSystemTime(T0 + 50000);
    requestFrontendReload('dist-updated', 'mtime:200:4096');
    expect(reload).toHaveBeenCalledTimes(2);
  });
});