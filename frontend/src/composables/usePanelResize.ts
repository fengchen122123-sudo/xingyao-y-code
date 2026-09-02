import { ref, type Ref } from 'vue';

// 持久化键：与 appStore 的 'ycode-*' 前缀约定一致（同 useComposerResize）
export const SIDEBAR_WIDTH_KEY = 'ycode-sidebar-width';
export const RIGHT_PANEL_WIDTH_KEY = 'ycode-rightpanel-width';

// 侧栏默认 240px，只允许向右拉宽到 480；不可收窄（侧栏菜单/会话项在
// 240px 以下会挤压错位，最小宽度即默认宽度）
export const SIDEBAR_DEFAULT_WIDTH = 240;
export const SIDEBAR_MIN_WIDTH = 240;
export const SIDEBAR_MAX_WIDTH = 480;
// 右面板默认 330px，可在 260~640 之间拖拽
export const RIGHT_PANEL_DEFAULT_WIDTH = 330;
export const RIGHT_PANEL_MIN_WIDTH = 260;
export const RIGHT_PANEL_MAX_WIDTH = 640;

// 单侧面板最宽不超过窗口宽度的 45%，避免小窗口下两侧把中间对话区挤没
export const MAX_VIEWPORT_RATIO = 0.45;

/** 限制在 [min, max] 区间内并取整（min > max 时按交换后的区间夹取） */
export function clampPanelWidth(width: number, min: number, max: number): number {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return Math.min(Math.max(Math.round(width), lo), hi);
}

/** 视口感知上限：min(配置上限, 视口宽度 * 45%) */
export function panelMaxWidth(max: number, viewportWidth = window.innerWidth): number {
  return Math.min(max, Math.round(viewportWidth * MAX_VIEWPORT_RATIO));
}

/**
 * 由拖拽起止点算出目标宽度：
 * - direction='left'（手柄在左侧面板右缘）：向右拖（clientX 变大）面板变宽
 * - direction='right'（手柄在右侧面板左缘）：向左拖（clientX 变小）面板变宽
 */
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

export interface PanelResizeOptions {
  /** 手柄在面板的哪一侧：'left' 手柄于左栏右缘，'right' 手柄于右栏左缘 */
  direction: 'left' | 'right';
  /** localStorage 持久化键 */
  storageKey: string;
  minWidth: number;
  maxWidth: number;
  /** 双击手柄恢复的默认宽度；拖回该值时清除记忆 */
  defaultWidth: number;
  /** 读取当前宽度（通常取自 appStore） */
  getWidth: () => number;
  /** 拖拽中实时回写宽度（通常写入 appStore） */
  setWidth: (w: number) => void;
}

export interface PanelResizeApi {
  /** 拖拽进行中（手柄据此高亮，宿主据此给根容器加 user-select 保护） */
  isResizing: Ref<boolean>;
  /** 绑定到手柄的 pointerdown */
  onHandlePointerdown: (e: PointerEvent) => void;
  /** 绑定到手柄的 dblclick：恢复默认宽度并清除记忆 */
  resetWidth: () => void;
}

/**
 * 面板宽度拖拽调节（桌面端左右分栏手柄）：
 * - 拖拽中实时跟随，松手后写入 localStorage，下次启动自动恢复
 * - 上限随视口动态夹取（不超过窗口 45%）
 * - 拖回默认宽度视为未自定义，清除记忆；双击手柄直接复位
 */
export function usePanelResize(opts: PanelResizeOptions): PanelResizeApi {
  const isResizing = ref(false);

  function persist(width: number) {
    try {
      if (width === opts.defaultWidth) {
        localStorage.removeItem(opts.storageKey);
      } else {
        localStorage.setItem(opts.storageKey, String(width));
      }
    } catch { /* 隐私模式等存储不可用时静默跳过，仅本次会话生效 */ }
  }

  function resetWidth() {
    opts.setWidth(opts.defaultWidth);
    try { localStorage.removeItem(opts.storageKey); } catch { /* 忽略 */ }
  }

  function onHandlePointerdown(e: PointerEvent) {
    const startX = e.clientX;
    const startWidth = opts.getWidth();
    // 上限在按下时按当时视口锁定一次，避免拖拽中窗口变化导致忽宽忽窄
    const max = panelMaxWidth(opts.maxWidth);
    const handle = e.currentTarget as HTMLElement;
    // pointer capture 让指针移出手柄/窗口后仍持续收到 move（jsdom 无此实现）
    try { handle.setPointerCapture?.(e.pointerId); } catch { /* 忽略 */ }
    isResizing.value = true;

    const onMove = (ev: PointerEvent) => {
      opts.setWidth(dragTargetWidth(opts.direction, startX, ev.clientX, startWidth, opts.minWidth, max));
    };
    const onUp = (ev: PointerEvent) => {
      try { handle.releasePointerCapture?.(ev.pointerId); } catch { /* 忽略 */ }
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      isResizing.value = false;
      persist(opts.getWidth());
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    // 阻止手柄上的拖拽演变为文本选择
    e.preventDefault();
  }

  return { isResizing, onHandlePointerdown, resetWidth };
}
