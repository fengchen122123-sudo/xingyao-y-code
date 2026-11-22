import {
  computed,
  getCurrentInstance,
  onUnmounted,
  ref,
  type ComputedRef,
  type Ref,
} from 'vue';

export const BOTTOM_THRESHOLD = 80;

export function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

export function isNearBottom(el: HTMLElement, threshold = BOTTOM_THRESHOLD): boolean {
  return distanceFromBottom(el) < threshold;
}

export type UserScrollIntent = 'none' | 'awayFromBottom' | 'towardBottom' | 'unknown';

const USER_INTENT_TTL_MS = 300;

export interface ChatScrollApi {
  stickToBottom: Ref<boolean>;
  showJumpButton: ComputedRef<boolean>;
  scrollToBottom: (force?: boolean) => void;
  handleScroll: () => boolean;
  syncStick: () => void;
  /** 登记用户滚动输入意图（wheel/touch/keydown/滚动条拖动）。
      awayFromBottom 会立即退出跟随（不等 scroll 事件派发），覆盖
      「用户上滚瞬间、内容变化 commit 先到」的竞态 */
  noteUserScrollIntent: (intent: UserScrollIntent) => void;
  commitFollowing: (following: boolean) => void;
}

export function useChatScroll(
  getScrollEl: () => HTMLElement | null,
): ChatScrollApi {
  const stickToBottom = ref(true);
  let scrollRaf: number | undefined;
  // 同一帧内若既有普通滚动又有 force 滚动，force 需升级覆盖（否则会被吞掉）
  let pendingForce = false;
  let userIntent: { intent: UserScrollIntent; at: number } = { intent: 'none', at: 0 };

  function commitFollowing(following: boolean) {
    stickToBottom.value = following;
  }

  function getActiveIntent(): UserScrollIntent {
    return Date.now() - userIntent.at <= USER_INTENT_TTL_MS ? userIntent.intent : 'none';
  }

  function noteUserScrollIntent(intent: UserScrollIntent) {
    if (intent === 'none') return;
    userIntent = { intent, at: Date.now() };
    // 明确的向上意图：立即退出跟随（不等 scroll 事件；覆盖输入后、
    // 事件派发前的内容变化 commit 竞态——否则会把用户阅读位置拽回底部）
    if (intent === 'awayFromBottom') commitFollowing(false);
  }

  function syncStick() {
    const el = getScrollEl();
    stickToBottom.value = el ? isNearBottom(el) : true;
  }

  function scrollToBottom(force = false) {
    if (force) {
      pendingForce = true;
      // 显式动作（发送/回底/切会话）覆盖一切输入意图：随后必须允许吸底
      userIntent = { intent: 'none', at: 0 };
    }
    // 合并到下一帧：流式输出期间会被高频调用，逐帧滚动足够顺滑且避免布局抖动
    if (scrollRaf !== undefined) return;
    scrollRaf = window.requestAnimationFrame(() => {
      scrollRaf = undefined;
      const f = pendingForce;
      pendingForce = false;
      const el = getScrollEl();
      if (!el) {
        // 容器暂不可用（DOM 重建瞬间）：保留 force 意图，交由下一次帧执行
        if (f) pendingForce = true;
        return;
      }
      if (f) {
        el.scrollTop = el.scrollHeight;
        stickToBottom.value = true;
      } else if (stickToBottom.value && getActiveIntent() !== 'awayFromBottom') {
        el.scrollTop = el.scrollHeight;
      }
    });
  }

  function handleScroll(): boolean {
    const el = getScrollEl();
    if (!el) return false;
    const intent = getActiveIntent();
    if (intent === 'none') return false;
    stickToBottom.value = isNearBottom(el);
    return true;
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
    noteUserScrollIntent,
    commitFollowing,
  };
}
