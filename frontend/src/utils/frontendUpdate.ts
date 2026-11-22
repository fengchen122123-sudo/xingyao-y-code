
export const FRONTEND_AUTO_RELOAD_TS_KEY = 'ycode:frontend-auto-reload-ts';
export const FRONTEND_AUTO_RELOAD_COUNT_KEY = 'ycode:frontend-auto-reload-count';
export const FRONTEND_AUTO_RELOAD_FP_KEY = 'ycode:frontend-auto-reload-fp';

const MIN_AUTO_RELOAD_INTERVAL_MS = 10_000;
const USER_ACTIVITY_GRACE_MS = 5_000;
const PENDING_RETRY_INTERVAL_MS = 1_000;
/** 本次会话连续自动刷新次数上限：触顶即停止自动刷新并告警（dist 持续损坏兜底） */
const MAX_AUTO_RELOADS_PER_SESSION = 3;

export interface FrontendUpdateStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export interface ReloadSchedulerOptions {
  reload: () => void;
  now: () => number;
  canReload: () => boolean;
  storage: FrontendUpdateStorage;
  minIntervalMs?: number;
  activityGraceMs?: number;
  retryIntervalMs?: number;
  /** 本次会话连续自动刷新次数上限（默认 3）：触顶停止自动刷新，避免无限重载 */
  maxReloadsPerSession?: number;
  onExhausted?: (count: number) => void;
}

export interface ReloadScheduler {
  request(reason: string, fingerprint?: string): void;
  tick(): void;
  notifyActivity(): void;
  reset(): void;
  isPending(): boolean;
}

export function createReloadScheduler(options: ReloadSchedulerOptions): ReloadScheduler {
  const reload = options.reload;
  const now = options.now;
  const canReload = options.canReload;
  const storage = options.storage;
  const minIntervalMs = options.minIntervalMs ?? MIN_AUTO_RELOAD_INTERVAL_MS;
  const activityGraceMs = options.activityGraceMs ?? USER_ACTIVITY_GRACE_MS;
  const retryIntervalMs = options.retryIntervalMs ?? PENDING_RETRY_INTERVAL_MS;
  const maxReloads = options.maxReloadsPerSession ?? MAX_AUTO_RELOADS_PER_SESSION;
  const onExhausted = options.onExhausted ?? (() => { });

  let pending = false;
  let pendingReason = '';
  let pendingFingerprint: string | undefined;
  let timer: ReturnType<typeof setInterval> | null = null;
  let exhaustedNotified = false;
  let lastActivityAt = now();

  const readStoredNumber = (key: string): number => {
    try {
      const raw = storage.getItem(key);
      if (raw === null) return 0;
      const value = Number(raw);
      return Number.isFinite(value) && value > 0 ? value : 0;
    } catch {
      // 存储不可用（隐私模式等）：按「从未自动刷新」处理——宁多刷一次也不卡死
      return 0;
    }
  };

  const readStoredString = (key: string): string => {
    try {
      return storage.getItem(key) ?? '';
    } catch {
      return '';
    }
  };

  const writeStored = (key: string, value: string): void => {
    try {
      storage.setItem(key, value);
    } catch {
    }
  };

  const readLastReloadAt = (): number => readStoredNumber(FRONTEND_AUTO_RELOAD_TS_KEY);
  const readReloadCount = (): number => readStoredNumber(FRONTEND_AUTO_RELOAD_COUNT_KEY);
  // 本次会话连续自愈次数是否已触顶：触顶后不再刷新（避免 dist 持续损坏时无限重载）
  const isExhausted = (): boolean => readReloadCount() >= maxReloads;

  const notifyExhausted = (): void => {
    if (exhaustedNotified) return;
    exhaustedNotified = true;
    try {
      onExhausted(readReloadCount());
    } catch {
    }
  };

  const stopTimer = (): void => {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };

  function tick(): void {
    if (!pending) return;
    const current = now();
    let allowed = false;
    try {
      allowed = !!canReload();
    } catch {
      allowed = false;
    }
    if (!allowed) return;
    const lastReloadAt = readLastReloadAt();
    if (lastReloadAt > 0 && current - lastReloadAt < minIntervalMs) return;
    // 条件 c：距最后用户活动 >= 5 秒（界面空闲才刷新，避免打断输入/拖拽）
    if (current - lastActivityAt < activityGraceMs) return;
    // 条件 d：指纹未变化——与上次触发刷新的 dist 指纹相同，说明构建并未真正
    // 更替（同一事件被重复推送 / 页面刚刷新后又收到旧事件），不必再刷；
    // 清 pending 收工，避免同一指纹反复整页重载
    if (pendingFingerprint && pendingFingerprint === readStoredString(FRONTEND_AUTO_RELOAD_FP_KEY)) {
      pending = false;
      pendingReason = '';
      pendingFingerprint = undefined;
      stopTimer();
      return;
    }
    // 条件 e：连续自愈次数触顶——dist 持续损坏（chunk 永久 404）时再刷也是
    // 同样的坏页面，停止自动刷新并告警，杜绝「每 ≥10 秒整页重载一次」的死循环
    if (isExhausted()) {
      pending = false;
      pendingReason = '';
      pendingFingerprint = undefined;
      stopTimer();
      notifyExhausted();
      return;
    }
    pending = false;
    const reason = pendingReason;
    pendingReason = '';
    const fingerprint = pendingFingerprint;
    pendingFingerprint = undefined;
    stopTimer();
    writeStored(FRONTEND_AUTO_RELOAD_TS_KEY, String(current));
    writeStored(FRONTEND_AUTO_RELOAD_COUNT_KEY, String(readReloadCount() + 1));
    if (fingerprint) writeStored(FRONTEND_AUTO_RELOAD_FP_KEY, fingerprint);
    console.info(`[前端自愈] 构建资源已更新（${reason || 'unknown'}），空闲窗口内自动刷新到最新版本`);
    reload();
  }

  function ensureTimer(): void {
    if (timer !== null) return;
    timer = setInterval(tick, retryIntervalMs);
  }

  function request(reason: string, fingerprint?: string): void {
    if (isExhausted()) {
      pendingFingerprint = fingerprint;
      notifyExhausted();
      return;
    }
    pending = true;
    pendingReason = reason;
    // 最新一次触发源的指纹生效（preload-error 无指纹会清空，避免误用旧指纹拦截）
    pendingFingerprint = fingerprint;
    ensureTimer();
    tick();
  }

  function notifyActivity(): void {
    lastActivityAt = now();
  }

  function reset(): void {
    pending = false;
    pendingReason = '';
    pendingFingerprint = undefined;
    stopTimer();
    lastActivityAt = now();
    exhaustedNotified = false;
    for (const key of [FRONTEND_AUTO_RELOAD_COUNT_KEY, FRONTEND_AUTO_RELOAD_FP_KEY]) {
      try { storage.removeItem?.(key); } catch { }
    }
  }

  return { request, tick, notifyActivity, reset, isPending: () => pending };
}

let canReloadImpl: () => boolean = () => true;
let onExhaustedImpl: (count: number) => void = () => { };
let handlersRegistered = false;

function safeSessionStorage(): FrontendUpdateStorage {
  return {
    getItem: (key) => {
      try { return sessionStorage.getItem(key); } catch { return null; }
    },
    setItem: (key, value) => {
      try { sessionStorage.setItem(key, value); } catch { }
    },
    removeItem: (key) => {
      try { sessionStorage.removeItem(key); } catch { }
    },
  };
}

function defaultOptions(): ReloadSchedulerOptions {
  return {
    reload: () => location.reload(),
    now: () => Date.now(),
    canReload: () => canReloadImpl(),
    onExhausted: (count) => onExhaustedImpl(count),
    storage: safeSessionStorage(),
    minIntervalMs: MIN_AUTO_RELOAD_INTERVAL_MS,
    activityGraceMs: USER_ACTIVITY_GRACE_MS,
    retryIntervalMs: PENDING_RETRY_INTERVAL_MS,
  };
}

let singletonOptions: ReloadSchedulerOptions = defaultOptions();
let scheduler: ReloadScheduler = createReloadScheduler(singletonOptions);

// 构建产物动态 import 失败（Vite 派发的 cancelable 事件）：只请求自愈刷新，
// 不 preventDefault——调用方（mermaidRenderer 降级显示源码等）的既有行为
// 保持原样，先降级、后自愈，两不耽误。
function onPreloadError(): void {
  requestFrontendReload('preload-error');
}

function onUserActivity(): void {
  scheduler.notifyActivity();
}

export function registerFrontendUpdateHandlers(options: { canReload?: () => boolean; onExhausted?: (count: number) => void } = {}): void {
  if (options.canReload) canReloadImpl = options.canReload;
  if (options.onExhausted) onExhaustedImpl = options.onExhausted;
  if (handlersRegistered) return;
  handlersRegistered = true;
  window.addEventListener('vite:preloadError', onPreloadError);
  window.addEventListener('keydown', onUserActivity, { capture: true, passive: true });
  window.addEventListener('pointerdown', onUserActivity, { capture: true, passive: true });
}

export function requestFrontendReload(reason: string, fingerprint?: string): void {
  scheduler.request(reason, fingerprint);
}

export function __getFrontendUpdateSchedulerForTest(): ReloadScheduler {
  return scheduler;
}

export function __resetFrontendUpdateForTest(): void {
  const previousStorage = singletonOptions.storage;
  scheduler.reset();
  singletonOptions = defaultOptions();
  scheduler = createReloadScheduler(singletonOptions);
  for (const store of [previousStorage, singletonOptions.storage]) {
    for (const key of [FRONTEND_AUTO_RELOAD_TS_KEY, FRONTEND_AUTO_RELOAD_COUNT_KEY, FRONTEND_AUTO_RELOAD_FP_KEY]) {
      try { store.removeItem?.(key); } catch { }
    }
  }
}

export function __setFrontendUpdateDepsForTest(overrides: Partial<ReloadSchedulerOptions>): void {
  singletonOptions = { ...defaultOptions(), ...overrides };
  scheduler = createReloadScheduler(singletonOptions);
}