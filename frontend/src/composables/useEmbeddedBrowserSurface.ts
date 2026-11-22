
import { nextTick, onMounted, onUnmounted, ref, watch, type Ref } from 'vue';

export interface BrowserRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface UseEmbeddedBrowserSurfaceOptions {
  isActive: () => boolean;
  shouldShowSurface: () => boolean;
  getSurfaceEl: () => HTMLElement | null;
  /**
   * 包含区元素 getter（可选）：所有上报矩形都会被夹紧（交集）到该元素
   * 矩形内。原生控件绘制在 DOM 之上、不受 overflow:hidden 裁剪，测量源
   * 一旦给出越出包含区的矩形，控件就会盖住界面其它区域——这个夹紧是
   * 「原生表面绝不逃出面板」的最后防线。
   */
  getClampEl?: () => HTMLElement | null;
  getZoom?: () => number;
  getSessionName?: () => string;
  /** 平台不支持时返回 true：完全不发桥调用，避免无谓报错 */
  isUnsupported: () => boolean;
  onUnsupported?: () => void;
}

export type EmbeddedSurfacePhase = 'idle' | 'opening' | 'ready' | 'hidden' | 'error';

export interface UseEmbeddedBrowserSurfaceApi {
  surfaceVisible: Ref<boolean>;
  occluded: Ref<boolean>;
  surfacePhase: Ref<EmbeddedSurfacePhase>;
  surfaceGeneration: Ref<number>;
  syncNow: () => Promise<any> | null;
  navigate: (url: string) => void;
  request: (method: string, ...args: unknown[]) => void;
}

const MIN_SIDE = 2;
const POLL_MS = 400;
const BOUNDS_HEARTBEAT_MS = 2000;

function readApi(): any {
  try {
    return (window as any).pywebview?.api ?? null;
  } catch {
    return null;
  }
}

function currentZoom(opts: UseEmbeddedBrowserSurfaceOptions): number {
  try {
    const z = opts.getZoom?.();
    return typeof z === 'number' && Number.isFinite(z) && z > 0 ? z : 1;
  } catch {
    return 1;
  }
}

/** 派发一次桥调用；返回 Promise（失败/缺方法都归一为 null，绝不抛出） */
function callApi(method: string, ...args: unknown[]): Promise<any> {
  try {
    const api = readApi();
    const fn = api?.[method];
    if (typeof fn !== 'function') return Promise.resolve(null);
    const result = fn.apply(api, args);
    return result && typeof result.then === 'function'
      ? result.catch(() => null)
      : Promise.resolve(result);
  } catch {
    return Promise.resolve(null);
  }
}

export function useEmbeddedBrowserSurface(
  opts: UseEmbeddedBrowserSurfaceOptions,
): UseEmbeddedBrowserSurfaceApi {
  const surfaceVisible = ref(false);
  const occluded = ref(false);
  const surfacePhase = ref<EmbeddedSurfacePhase>('idle');
  const surfaceGeneration = ref(0);

  let disposed = false;
  let lastVisible = false;
  let lastRectKey = '';
  let desiredGeneration = 0;
  let hostChain: Promise<unknown> = Promise.resolve();

  function enqueueHost<T>(fn: () => Promise<T>): Promise<T> {
    const next = hostChain.then(fn, fn);
    hostChain = next.then(() => undefined, () => undefined);
    return next;
  }

  function currentSession(): string {
    try {
      return String(opts.getSessionName?.() || '');
    } catch {
      return '';
    }
  }

  function markUnsupported(): void {
    try { opts.onUnsupported?.(); } catch { }
  }

  function dispatch(method: string, ...args: unknown[]): void {
    void callApi(method, ...args).then((res) => {
      if (res && res.ok === false && res.error === 'unsupported_platform') markUnsupported();
    });
  }

  function measure(el: HTMLElement): BrowserRect {
    const r = el.getBoundingClientRect();
    return clampRect(el, {
      x: Math.round(r.left),
      y: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
    });
  }

  /** 把矩形夹紧（交集）到包含区内：测量源越出包含区的部分直接裁掉。
   *  量包含区自身时不夹紧（恒等交集）；包含区不可用（缺元素/零尺寸/
   *  异常）时不夹紧，维持原行为。 */
  function clampRect(measured: HTMLElement, rect: BrowserRect): BrowserRect {
    const clampEl = opts.getClampEl?.();
    if (!clampEl || clampEl === measured) return rect;
    try {
      const b = clampEl.getBoundingClientRect();
      if (b.width <= 0 || b.height <= 0) return rect;
      const left = Math.max(rect.x, Math.round(b.left));
      const top = Math.max(rect.y, Math.round(b.top));
      const right = Math.min(rect.x + rect.width, Math.round(b.right));
      const bottom = Math.min(rect.y + rect.height, Math.round(b.bottom));
      return {
        x: left,
        y: top,
        width: Math.max(0, right - left),
        height: Math.max(0, bottom - top),
      };
    } catch {
      return rect;
    }
  }

  function rectKey(rect: BrowserRect): string {
    return `${rect.x},${rect.y},${rect.width},${rect.height}`;
  }

  // 5 点 elementFromPoint 遮挡启发式检测（中心 + 四角内缩采样）：
  // 命中 null（不在视口内）或外部元素即判定被遮挡。
  // 注：CSS pointer-events:none 或恰好落在采样点之间的微小浮层可能漏检，
  // 因此已知弹窗、抽屉、画笔与窄面板退化预览均须叠加显式 shouldShowSurface 门控；
  // 未知系统/自定义覆盖层按 best-effort 兜底处理。
  function detectOccluded(el: HTMLElement, rect: BrowserRect): boolean {
    try {
      if (typeof document === 'undefined' || typeof document.elementFromPoint !== 'function') return false;
      const dx = Math.min(8, Math.max(1, rect.width * 0.1));
      const dy = Math.min(8, Math.max(1, rect.height * 0.1));
      const points: Array<[number, number]> = [
        [rect.x + rect.width / 2, rect.y + rect.height / 2],
        [rect.x + dx, rect.y + dy],
        [rect.x + Math.max(dx, rect.width - dx), rect.y + dy],
        [rect.x + dx, rect.y + Math.max(dy, rect.height - dy)],
        [rect.x + Math.max(dx, rect.width - dx), rect.y + Math.max(dy, rect.height - dy)],
      ];
      for (const [px, py] of points) {
        const hit = document.elementFromPoint(px, py);
        if (!(hit && (hit === el || el.contains(hit)))) return true;
      }
      return false;
    } catch {
      return false;
    }
  }

  function canShow(rect: BrowserRect): boolean {
    if (!opts.isActive() || !opts.shouldShowSurface() || opts.isUnsupported()) return false;
    return rect.width >= MIN_SIDE && rect.height >= MIN_SIDE;
  }

  function hide(): void {
    const wasActive = lastVisible || surfacePhase.value === 'opening' || surfacePhase.value === 'ready';
    if (!wasActive) return;
    lastVisible = false;
    surfaceVisible.value = false;
    lastRectKey = '';
    desiredGeneration += 1;
    const gen = desiredGeneration;
    surfaceGeneration.value = gen;
    surfacePhase.value = 'hidden';
    const session = currentSession();
    const hidePromise = callApi('browser_pane_hide', session, gen).then((res) => {
      if (res && res.ok === false && res.error === 'unsupported_platform') markUnsupported();
    });
    // hide 必须立刻到达宿主（不得被阻塞的 open 卡死），同时让后续 show/set_bounds 串行等待 hide 落地
    hostChain = Promise.all([hostChain, hidePromise]).then(() => undefined, () => undefined);
  }

  function show(rect: BrowserRect, url = ''): Promise<any> {
    lastVisible = true;
    surfaceVisible.value = true;
    lastRectKey = rectKey(rect);
    desiredGeneration += 1;
    const gen = desiredGeneration;
    surfaceGeneration.value = gen;
    surfacePhase.value = 'opening';
    const zoom = currentZoom(opts);
    const session = currentSession();
    return enqueueHost(() => {
      if (disposed || !lastVisible || gen !== desiredGeneration) {
        return Promise.resolve({ ok: false, stale: true });
      }
      return callApi('browser_pane_open', rect, url, zoom, session, gen).then((res) => {
        if (disposed || !lastVisible || gen !== desiredGeneration || res?.stale) {
          return res;
        }
        if (res && res.ok === false) {
          if (res.error === 'unsupported_platform') {
            markUnsupported();
          } else {
            surfacePhase.value = 'error';
          }
        } else {
          surfacePhase.value = res?.phase === 'opening' ? 'opening' : 'ready';
        }
        return res;
      });
    });
  }

  function move(rect: BrowserRect): void {
    const gen = desiredGeneration;
    const zoom = currentZoom(opts);
    const session = currentSession();
    void enqueueHost(() => {
      if (disposed || !lastVisible || gen !== desiredGeneration) {
        return Promise.resolve();
      }
      return callApi('browser_pane_set_bounds', rect, zoom, session, gen).then((res) => {
        if (disposed || !lastVisible || gen !== desiredGeneration || res?.stale) {
          return;
        }
        if (res && res.ok === false) {
          if (res.error === 'unsupported_platform') {
            markUnsupported();
          } else if (res.error !== 'not_open') {
            surfacePhase.value = 'error';
          }
        }
      });
    });
  }

  /** 同步一次；本次触发了「隐藏→显示」翻转时返回 open 的 Promise，否则 null */
  function syncNow(): Promise<any> | null {
    try {
      if (opts.isUnsupported()) { hide(); return null; }
      const el = opts.getSurfaceEl();
      if (!el) { hide(); return null; }
      const rect = measure(el);
      // occluded 只由检测结果赋值，hide 不再复位它（评审 S2：否则「因遮挡而
      // 隐藏」期间对外信号恒为 false，语义失真）
      occluded.value = detectOccluded(el, rect);
      if (!canShow(rect) || occluded.value) { hide(); return null; }
      if (!lastVisible) return show(rect, '');
      const key = rectKey(rect);
      if (key !== lastRectKey) {
        lastRectKey = key;
        move(rect);
      }
      return null;
    } catch {
      /* 布局不可用时忽略本次同步 */
      return null;
    }
  }

  function navigate(url: string): void {
    if (!url || opts.isUnsupported()) return;
    // 评审 B1：pywebview 对每个 api 调用独立派发，open 与 navigate 背靠背
    // 发出时到达顺序无保证，且 WebView2 内核初始化是异步的——首次导航极易
    // 撞上 not_open/not_ready 被静默丢弃。这里把 navigate 串到 open 的
    // 宿主串行队列之后；后端另有 pending-url 补导航兜底（双保险）。
    const opening = !lastVisible ? syncNow() : null;
    if (opening && typeof opening.then === 'function') {
      void opening.then(() => enqueueHost(() => callApi('browser_pane_navigate', url)));
      return;
    }
    void enqueueHost(() => callApi('browser_pane_navigate', url));
  }

  function request(method: string, ...args: unknown[]): void {
    if (opts.isUnsupported()) return;
    dispatch(method, ...args);
  }

  let ro: ResizeObserver | null = null;
  const onViewportChange = () => syncNow();
  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let beatTimer: ReturnType<typeof setInterval> | undefined;

  onMounted(() => {
    const el = opts.getSurfaceEl();
    if (el && typeof ResizeObserver !== 'undefined') {
      try {
        ro = new ResizeObserver(() => syncNow());
        ro.observe(el);
      } catch { ro = null; }
    }
    window.addEventListener('resize', onViewportChange);
    window.addEventListener('scroll', onViewportChange, true);
    pollTimer = setInterval(() => { if (opts.isActive()) syncNow(); }, POLL_MS);
    beatTimer = setInterval(() => {
      // 心跳：可见时按当前表面代次重报矩形，兜底丢失的尺寸/位置事件
      if (!opts.isActive() || !lastVisible) return;
      const target = opts.getSurfaceEl();
      if (target) move(measure(target));
    }, BOUNDS_HEARTBEAT_MS);
    void nextTick(() => syncNow());
  });

  onUnmounted(() => {
    if (ro) { try { ro.disconnect(); } catch { } ro = null; }
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
    if (pollTimer !== undefined) { clearInterval(pollTimer); pollTimer = undefined; }
    if (beatTimer !== undefined) { clearInterval(beatTimer); beatTimer = undefined; }
    // 组件销毁（面板整块卸载）时先让宿主 hide 原生表面再置 disposed，避免残留与误丢弃 hide
    hide();
    disposed = true;
  });

  watch(() => opts.isActive(), () => syncNow(), { flush: 'post' });
  watch(() => opts.shouldShowSurface(), () => syncNow(), { flush: 'post' });

  return {
    surfaceVisible,
    occluded,
    surfacePhase,
    surfaceGeneration,
    syncNow,
    navigate,
    request,
  };
}
