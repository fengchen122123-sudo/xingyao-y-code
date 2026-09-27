// 内嵌浏览器「原生表面」位置同步（右侧面板浏览器页签专用）。
//
// 真实网页由宿主（Windows 桌面端 GUI）叠加的原生 WebView2 控件渲染：原生控件
// 不是 DOM 元素、不参与浏览器布局。前端能做的只有：持续把 DOM 占位区的
// viewport 矩形（CSS px）上报给宿主，让宿主把控件摆到该矩形上。本组合式函数
// 把这套「上报」逻辑收口：矩形测量、可见性判定、遮挡检测、变更去重与心跳兜底。
//
// 关键设计（WHY）：
// - 去重：open/hide 是重操作（创建/显隐 WebView2），只在可见性翻转时各调一次；
//   set_bounds 只在矩形真正变化时调用，避免每帧刷屏。
// - 心跳（2s）：缩放/滚动/DPI 变化等异常路径可能让某次变化没被观察到，
//   定期重报矩形做兜底自愈。
// - 遮挡检测：原生控件永远绘制在 DOM 之上。若有 DOM 覆盖层（弹窗、选择页）
//   压住占位区，必须主动 hide，否则控件会「穿」过覆盖层显示出来。
// - 所有桥调用 try/catch 吞掉：桥未就绪 / 平台不支持不应打断 UI。

import { nextTick, onMounted, onUnmounted, ref, watch, type Ref } from 'vue';

export interface BrowserRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface UseEmbeddedBrowserSurfaceOptions {
  /** 页签是否处于可见工作台（active prop 且非选择页/未折叠） */
  isActive: () => boolean;
  /**
   * 是否「希望」显示原生表面。起始页 / 不支持态返回 false →
   * 前端主动 hide（原生空白页会盖住 DOM 起始页）。
   */
  shouldShowSurface: () => boolean;
  /** 表面占位元素 getter（用于测量矩形与遮挡检测中心点） */
  getSurfaceEl: () => HTMLElement | null;
  /** 平台不支持时返回 true：完全不发桥调用，避免无谓报错 */
  isUnsupported: () => boolean;
  /** 任一次桥调用返回 unsupported_platform 时回调（界面切不支持态） */
  onUnsupported?: () => void;
}

export interface UseEmbeddedBrowserSurfaceApi {
  /** 原生表面当前应处于可见态（供 UI 判断是否让出占位区） */
  surfaceVisible: Ref<boolean>;
  /** 表面被其他 DOM 元素遮挡 */
  occluded: Ref<boolean>;
  /** 立即同步一次（矩形 + 可见性）；触发「隐藏→显示」翻转时返回 open 的 Promise */
  syncNow: () => Promise<any> | null;
  /** 导航到目标地址（未创建时先 open 再 navigate，规避 not_open） */
  navigate: (url: string) => void;
  /** 直通一个 browser_pane_* 桥方法（后退/前进/刷新/停止等） */
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

  // 去重状态：lastVisible=宿主当前是否已显示原生控件；lastRectKey=上次上报的
  // 矩形（用于 set_bounds 去重）
  let lastVisible = false;
  let lastRectKey = '';

  function markUnsupported(): void {
    try { opts.onUnsupported?.(); } catch { /* 忽略 */ }
  }

  /** 派发并顺带识别 unsupported_platform（界面据此切不支持态） */
  function dispatch(method: string, ...args: unknown[]): void {
    void callApi(method, ...args).then((res) => {
      if (res && res.ok === false && res.error === 'unsupported_platform') markUnsupported();
    });
  }

  function measure(el: HTMLElement): BrowserRect {
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.left),
      y: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  }

  function rectKey(rect: BrowserRect): string {
    return `${rect.x},${rect.y},${rect.width},${rect.height}`;
  }

  // 表面中心是否被别的 DOM 元素压住：命中 null（点不在视口内）也算被遮挡。
  // jsdom / 极端环境下 elementFromPoint 不可用时保守认为不遮挡。
  function detectOccluded(el: HTMLElement, rect: BrowserRect): boolean {
    try {
      if (typeof document === 'undefined' || typeof document.elementFromPoint !== 'function') return false;
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return !(hit && (hit === el || el.contains(hit)));
    } catch {
      return false;
    }
  }

  /** 是否满足「可显示原生表面」的全部条件 */
  function canShow(rect: BrowserRect): boolean {
    if (!opts.isActive() || !opts.shouldShowSurface() || opts.isUnsupported()) return false;
    return rect.width >= MIN_SIDE && rect.height >= MIN_SIDE;
  }

  function hide(): void {
    if (!lastVisible) return;
    lastVisible = false;
    surfaceVisible.value = false;
    lastRectKey = '';
    dispatch('browser_pane_hide');
  }

  /** 显示原生表面；返回 open 桥调用的 Promise（navigate 串行化依赖它） */
  function show(rect: BrowserRect, url = ''): Promise<any> {
    lastVisible = true;
    surfaceVisible.value = true;
    lastRectKey = rectKey(rect);
    // open 幂等（后端保活，重复调用只重定位）；首次带 url 可一步「创建 + 导航」
    return callApi('browser_pane_open', rect, url).then((res) => {
      if (res && res.ok === false && res.error === 'unsupported_platform') markUnsupported();
      return res;
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
        dispatch('browser_pane_set_bounds', rect);
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
    // Promise 之后；后端另有 pending-url 补导航兜底（双保险）。
    const opening = !lastVisible ? syncNow() : null;
    if (opening && typeof opening.then === 'function') {
      void opening.then(() => dispatch('browser_pane_navigate', url));
      return;
    }
    dispatch('browser_pane_navigate', url);
  }

  function request(method: string, ...args: unknown[]): void {
    if (opts.isUnsupported()) return;
    dispatch(method, ...args);
  }

  // 触发源一：尺寸变化（typeof 守卫：jsdom 无 ResizeObserver）
  let ro: ResizeObserver | null = null;
  // 触发源二：viewport 变化（resize / 任意容器滚动，capture 捕获滚动）
  const onViewportChange = () => syncNow();
  // 触发源三：400ms 轮询（仅 active：遮挡随时可能因弹窗/拖拽出现）
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
      // 心跳：可见时强制重报矩形，兜底丢失的尺寸/位置事件
      if (!opts.isActive() || !lastVisible) return;
      const target = opts.getSurfaceEl();
      if (target) dispatch('browser_pane_set_bounds', measure(target));
    }, BOUNDS_HEARTBEAT_MS);
    // 首帧后校准一次（此时 DOM 布局与 props 都已就位）
    void nextTick(() => syncNow());
  });

  onUnmounted(() => {
    if (ro) { try { ro.disconnect(); } catch { /* 忽略 */ } ro = null; }
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
    if (pollTimer !== undefined) { clearInterval(pollTimer); pollTimer = undefined; }
    if (beatTimer !== undefined) { clearInterval(beatTimer); beatTimer = undefined; }
    // 组件销毁（面板整块卸载）时让宿主收起原生表面，避免控件残留在窗口中
    hide();
  });

  // 触发源四：页签可见性与「覆盖层」态变化。flush:post 让回调在 DOM 更新后
  // 执行，测到的矩形才是最终布局值。
  watch(() => opts.isActive(), () => syncNow(), { flush: 'post' });
  watch(() => opts.shouldShowSurface(), () => syncNow(), { flush: 'post' });

  return { surfaceVisible, occluded, syncNow, navigate, request };
}
