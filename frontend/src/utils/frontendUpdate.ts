// 界面资源版本失配自愈（stale build chunk → auto reload）。
//
// 背景：运行中重建/更新前端 dist 后，已打开的页面仍引用旧构建哈希的 chunk，
// 本地静态服务器上这些文件已 404（实证：mermaid 懒加载失败，气泡里出现
// 「渲染器加载失败」）。两个触发源：
//   1) Python 侧 dist watchdog 感知 index.html 变化后经事件管线推送 bridge
//      事件 frontend_dist_updated（payload: {"fingerprint": "mtime:size"}），
//      appStore.handleBridgeEvent 转发到 requestFrontendReload('dist-updated')；
//   2) 构建产物动态 import 失败时 Vite 在 window 派发 'vite:preloadError'
//      （cancelable）——本模块监听它并请求自愈刷新，但**不** preventDefault：
//      保留调用方（mermaidRenderer 的源码降级等）既有行为，先降级、后自愈。
//
// 刷新策略（防打断 + 防风暴，全部满足才真正 reload）：
//   a) 无进行中回合：canReload 由 main.ts 注入（runningSessions.length === 0），
//      未注入时默认允许；
//   b) 距上次自动刷新 >= 10 秒：时间戳写 sessionStorage（整页刷新后仍在，
//      新页面加载后立刻再收到更新事件也不会连环刷新）；
//   c) 距最后用户活动 >= 5 秒：keydown/pointerdown 打点，界面空闲才刷新，
//      正在输入/拖拽/点击时自动顺延。
// 条件不满足时保持 pending：用 1 秒轻量定时器周期性复检（仅在 pending 期间
// 运行，刷新/重置前清理），条件满足即写时间戳并调用 reload。
//
// 依赖全部注入（reload/now/canReload/storage），本模块不 import appStore——
// 避免 appStore -> frontendUpdate -> appStore 循环依赖；核心逻辑抽成
// createReloadScheduler 工厂，生产用模块级默认单例，测试用
// __setFrontendUpdateDepsForTest 替换依赖、__resetFrontendUpdateForTest 清状态
// （与 mermaidRenderer 的 __setMermaidImporterForTest / __reset 同一模式）。

/** 距上次自动刷新时间戳的 sessionStorage key（毫秒时间戳字符串） */
export const FRONTEND_AUTO_RELOAD_TS_KEY = 'ycode:frontend-auto-reload-ts';
/** 本次会话「连续自动刷新次数」的 sessionStorage key（整页刷新后仍在） */
export const FRONTEND_AUTO_RELOAD_COUNT_KEY = 'ycode:frontend-auto-reload-count';
/** 上次触发刷新的 dist 指纹的 sessionStorage key（与后端 frontend_dist_updated 对齐） */
export const FRONTEND_AUTO_RELOAD_FP_KEY = 'ycode:frontend-auto-reload-fp';

/** 距上次自动刷新至少 10 秒，防「刷新风暴」 */
const MIN_AUTO_RELOAD_INTERVAL_MS = 10_000;
/** 距最后用户活动至少 5 秒，界面空闲才刷新 */
const USER_ACTIVITY_GRACE_MS = 5_000;
/** pending 期间复检定时器的间隔 */
const PENDING_RETRY_INTERVAL_MS = 1_000;
/** 本次会话连续自动刷新次数上限：触顶即停止自动刷新并告警（dist 持续损坏兜底） */
const MAX_AUTO_RELOADS_PER_SESSION = 3;

/** 存储抽象（生产注入 sessionStorage 的安全包装；测试可注入内存实现） */
export interface FrontendUpdateStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export interface ReloadSchedulerOptions {
  /** 真正执行刷新（生产默认 () => location.reload()） */
  reload: () => void;
  /** 时钟（默认 Date.now；测试可注入假时钟，或配合 vi.setSystemTime 使用） */
  now: () => number;
  /** 是否允许刷新（默认 true；main.ts 注入「无进行中回合」判定） */
  canReload: () => boolean;
  /** 防风暴时间戳存储 */
  storage: FrontendUpdateStorage;
  minIntervalMs?: number;
  activityGraceMs?: number;
  retryIntervalMs?: number;
  /** 本次会话连续自动刷新次数上限（默认 3）：触顶停止自动刷新，避免无限重载 */
  maxReloadsPerSession?: number;
  /** 触顶回调（main.ts 注入：给出可关闭的告警提示）；默认 no-op */
  onExhausted?: (count: number) => void;
}

export interface ReloadScheduler {
  /** 标记 pending 并立即尝试刷新（条件不满足则留给定时器复检）。
   *  fingerprint 为触发源携带的 dist 指纹（preload-error 无指纹时省略）。 */
  request(reason: string, fingerprint?: string): void;
  /** 单次复检：条件全部满足才执行刷新（测试可手动推进） */
  tick(): void;
  /** 记录一次用户活动（每次活动都顺延刷新宽限） */
  notifyActivity(): void;
  /** 清 pending / 定时器 / 活动时刻（不碰存储里的时间戳） */
  reset(): void;
  isPending(): boolean;
}

/**
 * 自愈调度器工厂：把「何时可以刷新」的核心逻辑与副作用（reload/时钟/存储）
 * 解耦，便于单测——所有外部依赖走注入项。
 */
export function createReloadScheduler(options: ReloadSchedulerOptions): ReloadScheduler {
  const reload = options.reload;
  const now = options.now;
  const canReload = options.canReload;
  const storage = options.storage;
  const minIntervalMs = options.minIntervalMs ?? MIN_AUTO_RELOAD_INTERVAL_MS;
  const activityGraceMs = options.activityGraceMs ?? USER_ACTIVITY_GRACE_MS;
  const retryIntervalMs = options.retryIntervalMs ?? PENDING_RETRY_INTERVAL_MS;
  const maxReloads = options.maxReloadsPerSession ?? MAX_AUTO_RELOADS_PER_SESSION;
  const onExhausted = options.onExhausted ?? (() => { /* 默认无告警出口 */ });

  let pending = false;
  let pendingReason = '';
  // 本次 pending 对应的 dist 指纹（preload-error 无指纹时为空）
  let pendingFingerprint: string | undefined;
  let timer: ReturnType<typeof setInterval> | null = null;
  // 触顶告警只发一次（同一次触顶反复触发 event 不刷屏）
  let exhaustedNotified = false;
  // 创建即视为「刚有活动」（页面加载瞬间不抢刷）；此后随用户事件顺延
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
      // 写不进去只意味着本次会话失去防风暴/指纹记忆，不影响本次刷新
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
      // 告警出口异常不应影响调度器收尾
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
    // 条件 a：无进行中回合（main.ts 注入 runningSessions.length === 0）。
    // 注入回调抛错按「不允许」处理（保持 pending 等下一次复检），不冒进刷新。
    let allowed = false;
    try {
      allowed = !!canReload();
    } catch {
      allowed = false;
    }
    if (!allowed) return;
    // 条件 b：距上次自动刷新 >= 10 秒（时间戳跨整页刷新存活，防刷新风暴）
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
    // 全部满足：先清 pending、停表、写时间戳/计数/指纹，再执行刷新——
    // 即使刷新过程中又触发一次 request，新页面也会被这些记录拦住
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
    // 已触顶：不再注册复检定时器、不再进入 pending——继续重载也只会是坏页面，
    // 只补发一次告警（新 dist 事件到达也拦在这里，需用户手动刷新恢复）
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
    // 立即尝试：多数场景触发时界面已空闲，无需等下一次复检
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
      try { storage.removeItem?.(key); } catch { /* 忽略 */ }
    }
  }

  return { request, tick, notifyActivity, reset, isPending: () => pending };
}

// ── 模块级默认单例（生产路径） ─────────────────────────────────────────────

// canReload 注入（main.ts 调用 registerFrontendUpdateHandlers 时传入）；
// 默认 true——未注入时不拦截刷新。
let canReloadImpl: () => boolean = () => true;
// 触顶告警出口注入（main.ts 传入 store.notifyModal 等）；默认 no-op。
let onExhaustedImpl: (count: number) => void = () => { /* 未注入时静默 */ };
// window 监听只注册一次（幂等防重复注册）
let handlersRegistered = false;

/** sessionStorage 的安全包装：隐私模式等场景下读返回 null、写失败静默 */
function safeSessionStorage(): FrontendUpdateStorage {
  return {
    getItem: (key) => {
      try { return sessionStorage.getItem(key); } catch { return null; }
    },
    setItem: (key, value) => {
      try { sessionStorage.setItem(key, value); } catch { /* 忽略 */ }
    },
    removeItem: (key) => {
      try { sessionStorage.removeItem(key); } catch { /* 忽略 */ }
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

// 用户活动打点：仅更新时间戳，每次活动都顺延刷新宽限。
function onUserActivity(): void {
  scheduler.notifyActivity();
}

/**
 * 在 main.ts 调用一次。
 * 幂等：window 监听只注册一次，重复调用不会叠加监听器；canReload 注入以
 * 最后一次传入为准（生产只调用一次；测试需要反复注入不同闸门）。
 */
export function registerFrontendUpdateHandlers(options: { canReload?: () => boolean; onExhausted?: (count: number) => void } = {}): void {
  if (options.canReload) canReloadImpl = options.canReload;
  if (options.onExhausted) onExhaustedImpl = options.onExhausted;
  if (handlersRegistered) return;
  handlersRegistered = true;
  // capture: true 保证子元素在冒泡阶段 stopPropagation 前也能观测到；
  // passive: true 不阻塞输入/点击的默认行为
  window.addEventListener('vite:preloadError', onPreloadError);
  window.addEventListener('keydown', onUserActivity, { capture: true, passive: true });
  window.addEventListener('pointerdown', onUserActivity, { capture: true, passive: true });
}

/**
 * 请求「刷新到最新构建」：标记 pending 并立即尝试；条件不满足则保持 pending，
 * 由内部 1 秒定时器周期性复检，条件满足即刷新。可被重复调用（合并为同一
 * pending，最新 reason 生效）。
 */
export function requestFrontendReload(reason: string, fingerprint?: string): void {
  scheduler.request(reason, fingerprint);
}

/**
 * 测试专用：取当前单例调度器（常用于断言 isPending()，验证触顶后不再 pending）。
 */
export function __getFrontendUpdateSchedulerForTest(): ReloadScheduler {
  return scheduler;
}

/**
 * 测试专用：清 pending / 定时器 / 活动时刻 / 时间戳，并把单例恢复为默认真实
 * 实现（配合 __setFrontendUpdateDepsForTest 使用）。
 */
export function __resetFrontendUpdateForTest(): void {
  const previousStorage = singletonOptions.storage;
  scheduler.reset();
  singletonOptions = defaultOptions();
  scheduler = createReloadScheduler(singletonOptions);
  // 时间戳/连续刷新计数/指纹可能落在默认真实 sessionStorage，也可能落在
  // 上一条用例注入的存储里
  for (const store of [previousStorage, singletonOptions.storage]) {
    for (const key of [FRONTEND_AUTO_RELOAD_TS_KEY, FRONTEND_AUTO_RELOAD_COUNT_KEY, FRONTEND_AUTO_RELOAD_FP_KEY]) {
      try { store.removeItem?.(key); } catch { /* 忽略 */ }
    }
  }
}

/**
 * 测试专用：替换单例依赖（reload/now/canReload/storage…）并重建单例状态
 * （pending/定时器/活动基线全部重置）。
 * 例：__setFrontendUpdateDepsForTest({ reload: vi.fn() })。
 */
export function __setFrontendUpdateDepsForTest(overrides: Partial<ReloadSchedulerOptions>): void {
  singletonOptions = { ...defaultOptions(), ...overrides };
  scheduler = createReloadScheduler(singletonOptions);
}