import { getCurrentInstance, onMounted, ref, type Ref } from 'vue';

// 持久化键：与 appStore 的 'ycode-*' 前缀约定一致
export const COMPOSER_HEIGHT_KEY = 'ycode-composer-height';

// textarea 默认 rows=2、字号 14、行高 1.65 ≈ 46px；
// jsdom / 隐藏状态下 offsetHeight 量不到，用该常量兜底
export const DEFAULT_MIN_TEXTAREA_HEIGHT = 46;
// 拉高后仍要给对话区留出空间：输入框最高约占据口高度的 60%
export const MAX_VIEWPORT_RATIO = 0.6;
// 小窗口下 60% 可能太矮，保证至少能再拉开约 3 行
export const MIN_EXPAND_DELTA = 70;

/** 限制在 [min, max] 区间内 */
export function clampComposerHeight(height: number, min: number, max: number): number {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return Math.min(Math.max(Math.round(height), lo), hi);
}

/**
 * 由拖拽起止点算出目标高度：向上拖（clientY 变小）抬高输入框，
 * 向下拖压回默认高度即为下限，不再更矮。
 */
export function dragTargetHeight(
  startClientY: number,
  currentClientY: number,
  startHeight: number,
  min: number,
  max: number,
): number {
  return clampComposerHeight(startHeight - (currentClientY - startClientY), min, max);
}

/** 拉伸上限：窗口高度的 60%，且至少比默认高 MIN_EXPAND_DELTA */
export function composerMaxHeight(min: number, viewportHeight = window.innerHeight): number {
  return Math.max(min + MIN_EXPAND_DELTA, Math.round(viewportHeight * MAX_VIEWPORT_RATIO));
}

/**
 * 恢复上次记忆的高度：小于默认视为未拉伸（默认即最低），超出当前窗口
 * 允许上限则夹回；无效存储一律按未拉伸处理。
 */
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
  /** textarea 显式高度（px）；null 表示未拉伸、走默认 rows=2 高度 */
  textareaHeight: Ref<number | null>;
  /** 拖拽进行中（手柄据此高亮，并给根节点加 user-select 保护） */
  isResizing: Ref<boolean>;
  /** 绑定到手柄的 pointerdown */
  onHandlePointerdown: (e: PointerEvent) => void;
  /** 绑定到手柄的 dblclick：恢复默认高度并清除记忆 */
  resetHeight: () => void;
}

/**
 * 输入框高度拖拽调节：
 * - 最小高度 = textarea 当前默认渲染高度（即"现在的设置"，不可压得更矮）
 * - 最大高度 = 窗口高度的 60%（小窗口保底默认+70px）
 * - 松手后写入 localStorage，下次进入自动恢复（按当时窗口重新夹取）
 */
export function useComposerResize(getTextarea: () => HTMLTextAreaElement | null): ComposerResizeApi {
  const textareaHeight = ref<number | null>(null);
  const isResizing = ref(false);
  // 下限（默认未拉伸高度）只在首次测量成功时锁定一次。拉伸后 textarea 的
  // offsetHeight 就是当前拉伸值，若每次按下都重新测量，下限会跟着抬高，
  // 表现为"拉上去之后就再也拖不下来了"。
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
    // 起始高度取真实渲染值：无论此前是默认态还是已拉伸态都可继续拖
    const startHeight = el.offsetHeight > 0 ? el.offsetHeight : minH;
    const handle = e.currentTarget as HTMLElement;
    // pointer capture 让指针移出手柄/窗口后仍持续收到 move（jsdom 无此实现）
    try { handle.setPointerCapture?.(e.pointerId); } catch { /* 忽略 */ }
    isResizing.value = true;

    const onMove = (ev: PointerEvent) => {
      textareaHeight.value = dragTargetHeight(startY, ev.clientY, startHeight, minH, maxH);
    };
    const onUp = (ev: PointerEvent) => {
      try { handle.releasePointerCapture?.(ev.pointerId); } catch { /* 忽略 */ }
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      isResizing.value = false;
      persist(textareaHeight.value);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    // 阻止手柄上的拖拽演变为文本选择
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
