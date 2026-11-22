import { getCurrentInstance, onMounted, ref, type Ref } from 'vue';

export const COMPOSER_HEIGHT_KEY = 'ycode-composer-height';

// textarea 默认 rows=2、字号 14、行高 1.65 ≈ 46px；
// jsdom / 隐藏状态下 offsetHeight 量不到，用该常量兜底
export const DEFAULT_MIN_TEXTAREA_HEIGHT = 46;
export const MAX_VIEWPORT_RATIO = 0.6;
export const MIN_EXPAND_DELTA = 70;

export function clampComposerHeight(height: number, min: number, max: number): number {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return Math.min(Math.max(Math.round(height), lo), hi);
}

export function dragTargetHeight(
  startClientY: number,
  currentClientY: number,
  startHeight: number,
  min: number,
  max: number,
): number {
  return clampComposerHeight(startHeight - (currentClientY - startClientY), min, max);
}

export function composerMaxHeight(min: number, viewportHeight = window.innerHeight): number {
  return Math.max(min + MIN_EXPAND_DELTA, Math.round(viewportHeight * MAX_VIEWPORT_RATIO));
}

export function computeRestoredHeight(
  stored: string | null,
  min: number,
  viewportHeight = window.innerHeight,
): number | null {
  if (stored == null) return null;
  const value = Number(stored);
  if (!Number.isFinite(value) || value <= min) return null;
  return clampComposerHeight(value, min, composerMaxHeight(min, viewportHeight));
}

export interface ComposerResizeApi {
  textareaHeight: Ref<number | null>;
  isResizing: Ref<boolean>;
  onHandlePointerdown: (e: PointerEvent) => void;
  resetHeight: () => void;
}

export function useComposerResize(getTextarea: () => HTMLElement | null): ComposerResizeApi {
  const textareaHeight = ref<number | null>(null);
  const isResizing = ref(false);
  let measuredMin: number | null = null;

  function minLimit(): number {
    return measuredMin ?? DEFAULT_MIN_TEXTAREA_HEIGHT;
  }

  function measureMinOnce() {
    if (measuredMin != null) return;
    const el = getTextarea();
    if (el && el.offsetHeight > 0) measuredMin = el.offsetHeight;
  }

  function persist(height: number | null) {
    try {
      if (height == null || height <= minLimit()) {
        localStorage.removeItem(COMPOSER_HEIGHT_KEY);
      } else {
        localStorage.setItem(COMPOSER_HEIGHT_KEY, String(height));
      }
    } catch { /* 隐私模式等存储不可用时静默跳过，仅本次会话生效 */ }
  }

  function resetHeight() {
    textareaHeight.value = null;
    persist(null);
  }

  function onHandlePointerdown(e: PointerEvent) {
    const el = getTextarea();
    if (!el) return;
    measureMinOnce();
    const minH = minLimit();
    const maxH = composerMaxHeight(minH);
    const startY = e.clientY;
    const startHeight = el.offsetHeight > 0 ? el.offsetHeight : minH;
    const handle = e.currentTarget as HTMLElement;
    try { handle.setPointerCapture?.(e.pointerId); } catch { }
    isResizing.value = true;

    const onMove = (ev: PointerEvent) => {
      textareaHeight.value = dragTargetHeight(startY, ev.clientY, startHeight, minH, maxH);
    };
    const onUp = (ev: PointerEvent) => {
      try { handle.releasePointerCapture?.(ev.pointerId); } catch { }
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      isResizing.value = false;
      persist(textareaHeight.value);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    e.preventDefault();
  }

  // 仅在组件 setup 上下文中注册恢复钩子；单测直接调用 composable 时
  // 无组件实例，跳过注册，避免 Vue 报"无活动实例"警告（同 useChatScroll）
  const instance = getCurrentInstance();
  if (instance) {
    // 挂载时恢复上次高度：必须在施加恢复高度之前测量默认高度
    //（此时 textarea 还没有内联 height，offsetHeight 才是真正的默认值）
    onMounted(() => {
      measureMinOnce();
      let stored: string | null = null;
      try {
        stored = localStorage.getItem(COMPOSER_HEIGHT_KEY);
      } catch { /* 存储不可用时按默认高度 */ }
      textareaHeight.value = computeRestoredHeight(stored, minLimit());
    });
  }

  return { textareaHeight, isResizing, onHandlePointerdown, resetHeight };
}
