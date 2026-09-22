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

/** 用户滚动意图（wheel/touch/keyboard 输入的方向） */
export type UserScrollIntent = 'none' | 'awayFromBottom' | 'towardBottom' | 'unknown';

// 输入事件后，多久内的 scroll 事件视为「用户滚动」（其余为程序化/布局滚动）
const USER_INTENT_TTL_MS = 300;

export interface ChatScrollApi {
  /** 跟随模式（following）：停留在底部时自动吸底。它表达「滚动权」——只有真实
      用户输入或显式动作可以改变它；程序化/布局滚动只更新几何账目 */
  stickToBottom: Ref<boolean>;
  /** 是否显示"回到底部"按钮 */
  showJumpButton: ComputedRef<boolean>;
  /** 滚动到底部；force=true 忽略跟随模式强制吸底并恢复跟随（显式动作） */
  scrollToBottom: (force?: boolean) => void;
  /** 处理滚动事件：仅「用户滚动」（意图新鲜）时按落点更新跟随态。
      返回本次是否用户来源——调用方可据此决定是否触发自动翻页等用户动作 */
  handleScroll: () => boolean;
  /** 立即按当前 scrollTop 重新判定跟随模式（DOM 重建后调用） */
  syncStick: () => void;
  /** 登记用户滚动输入意图（wheel/touch/keydown/滚动条拖动）。
      awayFromBottom 会立即退出跟随（不等 scroll 事件派发），覆盖
      「用户上滚瞬间、内容变化 commit 先到」的竞态 */
  noteUserScrollIntent: (intent: UserScrollIntent) => void;
  /** 显式设置跟随态（跳转发起时 commitFollowing(false)；一般无需手动置 true） */
  commitFollowing: (following: boolean) => void;
}

// ---------------------------------------------------------------------------
// 对话区智能滚动：跟随模式 vs 自由浏览（滚动权状态机，借鉴 ZCode 做法）
//
// 核心语义：following 表达「滚动权」，不是瞬时几何快照——
//   - 只有真实用户输入（滚轮/触摸/键盘/滚动条）才按最终落点决定跟随态；
//   - 程序化滚动（跳转/吸底/翻页补偿）与布局变化不改跟随态，只更新几何账目；
//   - 跳转（红点导航）显式退出跟随：内容变化不再吸底，直到用户滚回底部；
//   - 内容变化时仅「仍持有跟随权」才吸底，且吸底前对账新鲜的向上意图
//     （用户上滚瞬间、scroll 事件尚未派发时，content commit 不得把视口拽回）。
// 与旧实现（抑制时间窗 + 落点无条件判定）相比，这里没有时间窗兜底、
// 没有"抑制中/恢复后"的模糊地带：跳转=following=false 本身就是全部保证。
// ---------------------------------------------------------------------------
export function useChatScroll(
  getScrollEl: () => HTMLElement | null,
): ChatScrollApi {
  const stickToBottom = ref(true);
  let scrollRaf: number | undefined;
  // 同一帧内若既有普通滚动又有 force 滚动，force 需升级覆盖（否则会被吞掉）
  let pendingForce = false;
  // 用户滚动意图（带时间戳；TTL 过期视为程序化来源）
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
        // 仅跟随中吸底；新鲜向上意图出现时让位（对账：吸底动作前的最后一道闸）
        el.scrollTop = el.scrollHeight;
      }
    });
  }

  function handleScroll(): boolean {
    const el = getScrollEl();
    if (!el) return false;
    const intent = getActiveIntent();
    if (intent === 'none') return false; // 程序化/布局滚动：不改跟随权
    // 用户滚动：按最终落点判定（上滚离底 → 冻结；滚回底部 → 恢复跟随）
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
