import {
  computed,
  getCurrentInstance,
  onUnmounted,
  ref,
  type ComputedRef,
  type Ref,
} from 'vue';

// 距离底部小于该值视为"停留在底部"（进入跟随模式），单位 px
export const BOTTOM_THRESHOLD = 80;

/** 元素内容底部与可视区底部的距离（未滚完的内容量，越接近 0 越贴底） */
export function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

/** 是否停留在底部附近 */
export function isNearBottom(el: HTMLElement, threshold = BOTTOM_THRESHOLD): boolean {
  return distanceFromBottom(el) < threshold;
}

export interface ChatScrollApi {
  /** 跟随模式：停留在底部时自动吸底，用户上滑后关闭 */
  stickToBottom: Ref<boolean>;
  /** 是否显示"回到底部"按钮 */
  showJumpButton: ComputedRef<boolean>;
  /** 滚动到底部；force=true 忽略跟随模式强制吸底并恢复跟随 */
  scrollToBottom: (force?: boolean) => void;
  /** 用户滚动时同步跟随模式（绑定到容器 scroll 事件） */
  handleScroll: () => void;
  /** 立即按当前 scrollTop 重新判定跟随模式（DOM 重建后调用） */
  syncStick: () => void;
}

// ---------------------------------------------------------------------------
// 对话区智能滚动：跟随模式 vs 自由浏览
//
// 核心诉求：流式输出期间不再强制把用户拉回底部——只要用户向上翻历史，
// 就冻结当前位置；停留在底部时才自动跟随新内容。force 场景（发新消息、
// 切会话、从设置页返回、权限询问）仍会立即回底并恢复跟随。
// ---------------------------------------------------------------------------
export function useChatScroll(getScrollEl: () => HTMLElement | null): ChatScrollApi {
  const stickToBottom = ref(true);
  let scrollRaf: number | undefined;
  // 同一帧内若既有普通滚动又有 force 滚动，force 需升级覆盖（否则会被吞掉）
  let pendingForce = false;

  function syncStick() {
    const el = getScrollEl();
    stickToBottom.value = el ? isNearBottom(el) : true;
  }

  function scrollToBottom(force = false) {
    if (force) pendingForce = true;
    // 合并到下一帧：流式输出期间会被高频调用，逐帧滚动足够顺滑且避免布局抖动
    if (scrollRaf !== undefined) return;
    scrollRaf = window.requestAnimationFrame(() => {
      scrollRaf = undefined;
      const f = pendingForce;
      pendingForce = false;
      const el = getScrollEl();
      if (!el) return;
      if (f) {
        el.scrollTop = el.scrollHeight;
        stickToBottom.value = true;
      } else if (stickToBottom.value) {
        el.scrollTop = el.scrollHeight;
      }
    });
  }

  function handleScroll() {
    syncStick();
  }

  // 仅在组件 setup 上下文中注册清理钩子；单测直接调用 composable 时
  // 无组件实例，跳过注册，避免 Vue 报"无活动实例"警告（rAF 已在测试中 flush）
  const instance = getCurrentInstance();
  if (instance) {
    onUnmounted(() => {
      if (scrollRaf !== undefined) {
        window.cancelAnimationFrame(scrollRaf);
        scrollRaf = undefined;
      }
    });
  }

  return {
    stickToBottom,
    showJumpButton: computed(() => !stickToBottom.value),
    scrollToBottom,
    handleScroll,
    syncStick,
  };
}
