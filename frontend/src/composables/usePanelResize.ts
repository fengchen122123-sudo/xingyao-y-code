import { ref, type Ref } from 'vue';

export const SIDEBAR_WIDTH_KEY = 'ycode-sidebar-width';
export const RIGHT_PANEL_WIDTH_KEY = 'ycode-rightpanel-width';

// 侧栏默认 240px，只允许向右拉宽到 480；不可收窄（侧栏菜单/会话项在
// 240px 以下会挤压错位，最小宽度即默认宽度）
export const SIDEBAR_DEFAULT_WIDTH = 240;
export const SIDEBAR_MIN_WIDTH = 240;
export const SIDEBAR_MAX_WIDTH = 480;
export const RIGHT_PANEL_DEFAULT_WIDTH = 330;
export const RIGHT_PANEL_MIN_WIDTH = 260;
export const RIGHT_PANEL_MAX_WIDTH = 1280;

// 单侧面板最宽不超过窗口宽度的 50%，保证中间对话区至少剩一半；
// 固定上限只兜底超宽窗口，比例守卫才是真正的挤压保护
export const MAX_VIEWPORT_RATIO = 0.5;

export function clampPanelWidth(width: number, min: number, max: number): number {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return Math.min(Math.max(Math.round(width), lo), hi);
}

export function panelMaxWidth(max: number, viewportWidth = window.innerWidth): number {
  return Math.min(max, Math.round(viewportWidth * MAX_VIEWPORT_RATIO));
}

export function dragTargetWidth(
  direction: 'left' | 'right',
  startClientX: number,
  currentClientX: number,
  startWidth: number,
  min: number,
  max: number,
): number {
  const delta = currentClientX - startClientX;
  const raw = direction === 'left' ? startWidth + delta : startWidth - delta;
  return clampPanelWidth(raw, min, max);
}

/**
 * 恢复记忆的宽度：非数字一律按未自定义处理（返回 null，用默认宽度）；
 * 合法数字夹取到 [min, max]，兼容旧版本更宽上限留下的越界存储。
 */
export function computeRestoredWidth(stored: string | null, min: number, max: number): number | null {
  if (stored == null) return null;
  const value = Number(stored);
  if (!Number.isFinite(value)) return null;
  return clampPanelWidth(value, min, max);
}

// ── 视口比例记忆（右面板专用）────────────────────────────────
// 宽度按“占视口宽度的比例”记忆：窗口放大/缩小时等比跟随，拖到 50% 就一直保持 50%。
// 存储值 <= 1 视为比例；> 1 视为旧版像素值（按当前视口换算成比例，兼容老记忆）。

export function parseStoredRatio(stored: string | null, viewportWidth = window.innerWidth): number | null {
  if (stored == null) return null;
  const value = Number(stored);
  if (!Number.isFinite(value) || value <= 0) return null;
  const ratio = value > 1 ? value / viewportWidth : value;
  return Math.min(ratio, MAX_VIEWPORT_RATIO);
}

export function ratioToWidth(ratio: number, min: number, max: number, viewportWidth = window.innerWidth): number {
  return clampPanelWidth(ratio * viewportWidth, min, panelMaxWidth(max, viewportWidth));
}

export function computeRestoredRatioWidth(
  stored: string | null,
  min: number,
  max: number,
  viewportWidth = window.innerWidth,
): number | null {
  const ratio = parseStoredRatio(stored, viewportWidth);
  return ratio == null ? null : ratioToWidth(ratio, min, max, viewportWidth);
}

export interface PanelResizeOptions {
  direction: 'left' | 'right';
  storageKey: string;
  minWidth: number;
  maxWidth: number;
  defaultWidth: number;
  getWidth: () => number;
  setWidth: (w: number) => void;
  viewportRatio?: boolean;
}

export interface PanelResizeApi {
  isResizing: Ref<boolean>;
  onHandlePointerdown: (e: PointerEvent) => void;
  resetWidth: () => void;
}

export function usePanelResize(opts: PanelResizeOptions): PanelResizeApi {
  const isResizing = ref(false);
  let ratio: number | null = null;
  if (opts.viewportRatio) {
    try { ratio = parseStoredRatio(localStorage.getItem(opts.storageKey)); } catch { ratio = null; }
  }

  function persist(width: number) {
    const customized = width !== opts.defaultWidth;
    if (opts.viewportRatio) ratio = customized ? width / window.innerWidth : null;
    try {
      if (!customized) {
        localStorage.removeItem(opts.storageKey);
      } else if (opts.viewportRatio) {
        localStorage.setItem(opts.storageKey, String(Number((width / window.innerWidth).toFixed(4))));
      } else {
        localStorage.setItem(opts.storageKey, String(width));
      }
    } catch { /* 隐私模式等存储不可用时静默跳过，仅本次会话生效 */ }
  }

  function resetWidth() {
    opts.setWidth(opts.defaultWidth);
    if (opts.viewportRatio) ratio = null;
    try { localStorage.removeItem(opts.storageKey); } catch { }
  }

  // 视口比例联动：窗口缩放时按记忆比例重算像素宽度（拖到 50% 就一直是 50%）；
  // 拖拽进行中不介入，避免和指针位移抢同一份宽度
  if (opts.viewportRatio) {
    window.addEventListener('resize', () => {
      if (ratio == null || isResizing.value) return;
      opts.setWidth(ratioToWidth(ratio, opts.minWidth, opts.maxWidth));
    });
  }

  function onHandlePointerdown(e: PointerEvent) {
    const startX = e.clientX;
    const startWidth = opts.getWidth();
    // 上限在按下时按当时视口锁定一次，避免拖拽中窗口变化导致忽宽忽窄
    const max = panelMaxWidth(opts.maxWidth);
    const handle = e.currentTarget as HTMLElement;
    try { handle.setPointerCapture?.(e.pointerId); } catch { }
    isResizing.value = true;

    const onMove = (ev: PointerEvent) => {
      opts.setWidth(dragTargetWidth(opts.direction, startX, ev.clientX, startWidth, opts.minWidth, max));
    };
    const onUp = (ev: PointerEvent) => {
      try { handle.releasePointerCapture?.(ev.pointerId); } catch { }
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      isResizing.value = false;
      persist(opts.getWidth());
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    e.preventDefault();
  }

  return { isResizing, onHandlePointerdown, resetWidth };
}
