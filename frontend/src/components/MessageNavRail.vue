<template>
  <div
    v-if="dots.length"
    class="nav-rail"
    data-test="nav-rail"
    :style="{ '--dot-size': `${dotSize}px`, '--slot-height': `${slotHeight}px` }"
  >
    <button
      v-for="(_, i) in dots"
      :key="i"
      type="button"
      class="rail-dot"
      :class="{ active: i === activeIndex, dim: hasNavIndex && i < loadedFirstIndex }"
      :aria-label="`跳转到第 ${i + 1} 条我的消息`"
      data-test="rail-dot"
      @click="jumpTo(i)"
      @mouseenter="onDotHover(i, $event)"
      @mouseleave="onDotLeave"
    ></button>

    <!-- 悬停预览浮层：Teleport 到 body，脱离 .nav-rail 的 overflow:hidden 裁剪；
         定位于红点左侧，max-width 20em 约 20 字一行自动换行，不溢出窗口 -->
    <Teleport to="body">
      <div
        v-if="hoverIndex >= 0"
        class="rail-tip"
        data-test="rail-tip"
        :style="tipStyle"
      >{{ msgTexts[hoverIndex] || `跳转到第 ${hoverIndex + 1} 条我的消息` }}</div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

const props = defineProps<{
  /** 滚动容器（#chat-scroll），点击红点时执行 scrollTo */
  getScrollEl: () => HTMLElement | null;
  /** 全部「我发的消息」DOM 元素，顺序与对话流一致 */
  getUserMsgElements: () => HTMLElement[];
  /** 全量 user 消息索引（分页懒加载下红点仍全量）；缺省回退 DOM 驱动 */
  navItems?: Array<{ id: number; preview: string }>;
  /** 已加载消息流首条 _msg_id：索引中小于它的红点为未加载（淡化） */
  loadedFirstId?: number | null;
  /** 按消息 id 在已加载 DOM 中精确定位 user 气泡（App 注入，限定在 #chat-scroll
      内查找）。命中即权威定位；缺省时退回索引换算；
      未命中的降级路径由调用方决定（点击入口不落邻近、false 交跨页） */
  getUserMsgElementById?: (id: number) => HTMLElement | null;
}>();

const emit = defineEmits<{
  /** 点击未加载区域红点：父组件负责跨页翻页后回调 jumpToLoaded */
  (e: 'jump', payload: { index: number; id: number }): void;
  /** 跳转发起（任意红点点击）：父组件据此显式退出「跟随底部」——
     跳转后内容变化不再自动吸底，直到用户主动滚回底部或点回底按钮 */
  (e: 'jumpStart'): void;
}>();

const hasNavIndex = computed(() => Array.isArray(props.navItems) && props.navItems.length > 0);
// 已加载分界：已加载区首条 user 消息在 navItems 中的索引。
// 注意后端分页只对页首做「回合对齐」，且吞并上限为页大小 + 30 条——工具调用密集的
// 回合会撑破上限，此时已加载区首条消息是 assistant/tool，它的 id 不在 user 索引里。
// 这种情况必须退回到「其后第一条 user 消息」，否则分界会退化成 0：轨道的
// 已加载/未加载判断、点击定位、（滚动时）高亮反查会整体错位。
const loadedFirstIndex = computed(() => {
  if (!hasNavIndex.value) return 0;
  const first = props.loadedFirstId;
  if (first == null) return 0;
  const exact = props.navItems!.findIndex(it => it.id === first);
  if (exact >= 0) return exact;
  const nextUser = props.navItems!.findIndex(it => it.id > first);
  // 没有比页首更新的 user 消息：已加载区不含任何 user，全部按未加载处理
  return nextUser >= 0 ? nextUser : props.navItems!.length;
});

// 红点数量 = 用户消息条数。用户消息在任何形态下都完整渲染（折叠只收中间
// 过程），因此点数随消息增加而增加，上下文压缩也不会丢点。
const msgCount = ref(0);
// 每条用户消息的悬停预览文本（与红点一一对应，超长截断）
const msgTexts = ref<string[]>([]);
const dots = computed(() => Array.from({ length: msgCount.value }));

// 悬停预览最大字符数：消息过长时不显示完整内容
const MAX_PREVIEW_LEN = 100;

// 提取用户消息的纯文本预览：折叠换行/连续空白为单空格，超长截断加省略号；
// 纯图片消息（无文字）退回「[图片]」占位。
function buildPreview(el: HTMLElement): string {
  const textEl = el.querySelector('.user-text');
  const text = textEl?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  if (text) {
    return text.length > MAX_PREVIEW_LEN ? `${text.slice(0, MAX_PREVIEW_LEN)}…` : text;
  }
  if (el.querySelector('.msg-img')) return '[图片]';
  return '';
}

// 悬停红点索引（-1 = 无悬停），驱动浮层显隐
const hoverIndex = ref(-1);
// 浮层定位样式：垂直对齐红点中心，水平贴红点左侧 12px
const tipStyle = ref<Record<string, string>>({});

function onDotHover(i: number, e: MouseEvent) {
  hoverIndex.value = i;
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
  tipStyle.value = {
    top: `${rect.top + rect.height / 2}px`,
    right: `${window.innerWidth - rect.left + 12}px`,
  };
}

function onDotLeave() {
  hoverIndex.value = -1;
}

// 渐进式紧凑策略：对话少时松散，随数量增加逐步收紧，最后才缩点径。
// 三级：宽松(22px槽位) → 适中(16px) → 紧凑(12px) → 极限(点径缩小)
const BASE_DOT_SIZE = 8;
const MIN_DOT_SIZE = 3;
const BASE_SLOT = 22;      // 宽松：点 + 大间距
const MID_SLOT = 16;       // 适中：点 + 中等间距
const COMPACT_SLOT = 12;   // 紧凑：点 + 小间距
const MIN_SLOT = 9;        // 极限：点几乎贴在一起
const RAIL_VERTICAL_PADDING = 8;
const RAIL_VIEWPORT_MARGIN = 24;
const dotSize = ref(BASE_DOT_SIZE);
const slotHeight = ref(BASE_SLOT);

function refresh() {
  if (hasNavIndex.value) {
    msgCount.value = props.navItems!.length;
    msgTexts.value = props.navItems!.map(it => it.preview);
  } else {
    const els = props.getUserMsgElements();
    msgCount.value = els.length;
    msgTexts.value = els.map(buildPreview);
  }
  const scrollEl = props.getScrollEl();
  if (!scrollEl || !msgCount.value) {
    dotSize.value = BASE_DOT_SIZE;
    slotHeight.value = BASE_SLOT;
    return;
  }
  const avail = scrollEl.clientHeight - RAIL_VIEWPORT_MARGIN - RAIL_VERTICAL_PADDING;
  const n = msgCount.value;

  // 逐级尝试：宽松 → 适中 → 紧凑 → 极限，取第一个能装下的
  if (n * BASE_SLOT <= avail) {
    dotSize.value = BASE_DOT_SIZE;
    slotHeight.value = BASE_SLOT;
  } else if (n * MID_SLOT <= avail) {
    dotSize.value = BASE_DOT_SIZE;
    slotHeight.value = MID_SLOT;
  } else if (n * COMPACT_SLOT <= avail) {
    dotSize.value = BASE_DOT_SIZE;
    slotHeight.value = COMPACT_SLOT;
  } else if (n * MIN_SLOT <= avail) {
    dotSize.value = BASE_DOT_SIZE;
    slotHeight.value = MIN_SLOT;
  } else {
    // 极限：槽位=点径+1px，点径缩小到能装下
    const fit = Math.floor(avail / n);
    dotSize.value = Math.max(MIN_DOT_SIZE, Math.min(BASE_DOT_SIZE, fit - 1));
    slotHeight.value = Math.max(MIN_DOT_SIZE + 1, fit);
  }
}

// 当前"正在显示"的用户消息索引：滚动时按视口中线实时判定；
// 点击红点立即置为目标索引并"钉住"，直到用户手动滚动才交还给几何判定。
const activeIndex = ref(-1);
// 点击跳转的目标索引；用户手动滚动/显式回底时清除
let pinnedIndex = -1;

function updateActive() {
  const scrollEl = props.getScrollEl();
  const els = props.getUserMsgElements();
  if (!scrollEl || !els.length) {
    activeIndex.value = -1;
    return;
  }
  // 点击跳转后高亮被钉住，updateActive 不覆盖，直到用户手动滚动解除
  if (pinnedIndex >= 0) {
    activeIndex.value = Math.min(pinnedIndex, msgCount.value - 1);
    return;
  }
  // 已滚到底部：用户在看最新一条，直接高亮最后一条（中线判定在"看最后
  // 一条"这个最常见场景会错判给上一条——最后一条顶部通常没越过中线）
  const distToBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
  if (distToBottom < 80) {
    activeIndex.value = (hasNavIndex.value ? loadedFirstIndex.value : 0) + els.length - 1;
    return;
  }
  // 浏览历史中：以视口中线为判定线，顶部已越过中线的最后一条算"正在显示"
  const rect = scrollEl.getBoundingClientRect();
  const mid = rect.top + rect.height / 2;
  let active = 0;
  for (let i = 0; i < els.length; i++) {
    if (els[i].getBoundingClientRect().top <= mid) active = i;
    else break;
  }
  activeIndex.value = (hasNavIndex.value ? loadedFirstIndex.value : 0) + active;
}

function jumpTo(i: number) {
  // 新跳转接管：静默取消上一次跳转的定位检查（高亮/定位由本次负责）
  cancelJumpChecks();
  // 先通知父组件显式退出「跟随底部」：跳转后内容变化不再自动吸底
  emit('jumpStart');
  pinnedIndex = i;
  activeIndex.value = i;
  if (hasNavIndex.value) {
    const item = props.navItems![i];
    if (!item) return;
    if (i >= loadedFirstIndex.value) {
      // 已加载分支：先做目标存在性校验（id 优先 + 旧索引换算兜底）。换算出的
      // 元素不存在时降级走跨页通道——旧实现 clamp 到已加载区顶部后静默滑动，
      // 正是用户反馈的"点了没反应/落点不对"
      if (!scrollToLoadedIndex(i, item.id)) {
        emit('jump', { index: i, id: item.id });
      }
    } else {
      // 未加载区域：父组件跨页翻页后回调 jumpToLoaded
      emit('jump', { index: i, id: item.id });
    }
    return;
  }
  scrollToLocalIndex(i);
}

/** 已加载（含刚翻页加载到）的红点定位：id 权威优先，索引换算兜底。
    返回 false = 目标不在 DOM（调用方据此降级：点击入口走跨页通道，
    翻页回调退索引兜底）——旧实现 clamp 到已加载区顶部后静默滑动，
    正是用户反馈的"点了没反应/落点不对"。 */
function scrollToLoadedIndex(globalIdx: number, id?: number): boolean {
  // id 权威通道：命中即滚；未命中不落邻近元素（那是"点错消息"的来源），
  // 直接返回 false 交调用方决定降级路径
  if (id != null && props.getUserMsgElementById) {
    return scrollToElement(props.getUserMsgElementById(id));
  }
  const base = hasNavIndex.value ? loadedFirstIndex.value : 0;
  // clamp 到 0：翻页护栏中断导致目标仍超出已加载范围时，退到已加载区最上面，
  // 避免负索引取不到元素；此时返回的存在性布尔仍为 false，交由调用方降级
  return scrollToLocalIndex(Math.max(0, globalIdx - base));
}

/** 目标滚动位置：目标消息顶对齐视口顶部（留 16px 呼吸空间），并双端 clamp。
    clamp 上界（maxScroll）是关键：内容收缩后目标可能超过可滚动范围，不 clamp
    的话每次校正都写不进去、形成"稳定但偏差永远存在"的死循环。 */
function computeJumpTarget(scrollEl: HTMLElement, el: HTMLElement): number {
  const rect = el.getBoundingClientRect();
  const scrollRect = scrollEl.getBoundingClientRect();
  const raw = rect.top - scrollRect.top + scrollEl.scrollTop - 16;
  const max = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight);
  return Math.min(Math.max(0, raw), max);
}

// —— 跳转定位的「有界校正」——
// 一次性 scrollTo 的终点可能过时：目标上方/附近内容（图片解码、代码高亮、折叠体）
// 会在动画期间继续撑开。这里在发起平滑定位后按固定检查点做有限校正：
//   检查点 [800ms, 1500ms] + 兜底 2500ms 无条件收尾；
//   校正只在「滚动已停止（距最后 scroll 事件 ≥200ms）」且「偏移 >10px」时执行；
//   校正次数上限 2 次，绝无每帧校正/无限循环；用户输入或显式回底立即取消。
// 与旧实现（每帧检查 + 即时校正）的区别：旧实现会在平滑动画启动初期误判
// "位置稳定"而打断动画，且目标不可达时反复校正形成死循环（视口高频闪动）。
const JUMP_ALIGN_TOLERANCE = 10;     // px：偏移在此内视为已对齐
const JUMP_CHECK_DELAYS_MS = [800, 1500];
const JUMP_FINALIZE_MS = 2500;       // 兜底：无论结果，彻底结束检查
const JUMP_CHECK_MAX_CORRECTIONS = 2;
const JUMP_SCROLL_IDLE_MS = 200;     // 距最后一次 scroll 事件多久算「滚动停止」
let jumpTimers: number[] = [];
let jumpChecking = false;
let jumpCorrections = 0;
let jumpLastScrollAt = 0;

function cancelJumpTimers() {
  for (const t of jumpTimers) window.clearTimeout(t);
  jumpTimers = [];
}

/** 静默取消跳转检查（新跳转接管/组件卸载用；不触碰高亮钉住） */
function cancelJumpChecks() {
  cancelJumpTimers();
  jumpChecking = false;
}

/** 公开取消（用户滚动接管/显式回底/发送消息）：结束检查并解除高亮钉住 */
function cancelJump() {
  cancelJumpChecks();
  pinnedIndex = -1;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** 滚到指定元素（跳转定位公共路径）；返回是否成功发起定位 */
function scrollToElement(el: HTMLElement | null | undefined): boolean {
  const scrollEl = props.getScrollEl();
  if (!scrollEl || !el) {
    // 目标缺失（索引与 DOM 短暂不同步）：结束检查，避免定时器泄漏；
    // 返回 false 不在此处静默吞掉——由调用方决定降级路径
    cancelJumpChecks();
    return false;
  }
  const target = computeJumpTarget(scrollEl, el);
  scrollEl.scrollTo({ top: target, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  startJumpChecks(scrollEl, el);
  return true;
}

function scrollToLocalIndex(localIdx: number): boolean {
  return scrollToElement(props.getUserMsgElements()[localIdx]);
}

function startJumpChecks(scrollEl: HTMLElement, el: HTMLElement) {
  cancelJumpTimers();
  jumpChecking = true;
  jumpCorrections = 0;
  jumpLastScrollAt = Date.now();
  for (const delay of JUMP_CHECK_DELAYS_MS) {
    jumpTimers.push(window.setTimeout(() => runJumpCheck(scrollEl, el), delay));
  }
  // 兜底：检查点全部被跳过（滚动迟迟不停）时做最后一次校正机会，然后无条件收尾
  jumpTimers.push(window.setTimeout(() => runJumpCheck(scrollEl, el, true), JUMP_FINALIZE_MS));
}

function runJumpCheck(scrollEl: HTMLElement, el: HTMLElement, final = false) {
  if (!jumpChecking) return;
  // 元素被卸载（切会话/消息流重排）或滚动容器已更换：直接收尾
  if (!el.isConnected || props.getScrollEl() !== scrollEl) {
    cancelJumpChecks();
    return;
  }
  // 滚动仍在进行（平滑动画/惯性中）：本次不校正，交给后续检查点。
  // 最终检查（final）不再等待——长动画必已结束；若此时仍有滚动活动，
  // 也必来自内容/惯性而非用户输入（用户输入会先取消整个检查）。
  if (!final && Date.now() - jumpLastScrollAt < JUMP_SCROLL_IDLE_MS) return;
  if (jumpCorrections >= JUMP_CHECK_MAX_CORRECTIONS) {
    cancelJumpChecks();
    return;
  }
  const target = computeJumpTarget(scrollEl, el);
  const delta = target - scrollEl.scrollTop;
  if (Math.abs(delta) <= JUMP_ALIGN_TOLERANCE) {
    // 已对齐：提前收尾
    cancelJumpChecks();
    return;
  }
  scrollEl.scrollTo({ top: target, behavior: 'auto' });
  jumpCorrections += 1;
  if (final || jumpCorrections >= JUMP_CHECK_MAX_CORRECTIONS) {
    cancelJumpChecks();
  }
}

/** 父组件跨页加载完成后回调：目标已进入已加载范围，执行定位。
    宽容兜底：id 命中优先；未命中退回索引换算（翻页已完成，索引此时期望可用）；
    两者都失败仅记 warn——不得再 emit jump（会重新进入翻页循环）。 */
function jumpToLoaded(globalIdx: number, id?: number) {
  cancelJumpChecks();
  pinnedIndex = globalIdx;
  activeIndex.value = globalIdx;
  if (id != null && props.getUserMsgElementById) {
    const el = props.getUserMsgElementById(id);
    if (el) {
      scrollToElement(el);
      return;
    }
  }
  if (!scrollToLoadedIndex(globalIdx)) {
    console.warn('[MessageNavRail] 跳转定位失败：目标既不在已加载 DOM、也未按 id 命中', globalIdx, id);
  }
}

let resizeObserver: ResizeObserver | undefined;
let boundScrollEl: HTMLElement | null = null;
const onScroll = () => {
  // 记录滚动活动：跳转检查点据此判定「滚动是否已停止」
  if (jumpChecking) jumpLastScrollAt = Date.now();
  updateActive();
};
// 用户主动滚动（滚轮/触摸/键盘）时解除高亮钉住并取消跳转检查，不再抢滚动控制权
const unpin = () => {
  cancelJump();
};

onMounted(() => {
  refresh();
  // 挂载后按当前滚动位置初始化高亮（refresh 只管数量，避免覆盖 jumpTo 的高亮）
  updateActive();
  const scrollEl = props.getScrollEl();
  if (scrollEl) {
    scrollEl.addEventListener('scroll', onScroll, { passive: true });
    scrollEl.addEventListener('wheel', unpin, { passive: true });
    scrollEl.addEventListener('touchmove', unpin, { passive: true });
    boundScrollEl = scrollEl;
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => refresh());
      resizeObserver.observe(scrollEl);
    }
  }
});

onUnmounted(() => {
  cancelJumpTimers();
  resizeObserver?.disconnect();
  boundScrollEl?.removeEventListener('scroll', onScroll);
  boundScrollEl?.removeEventListener('wheel', unpin);
  boundScrollEl?.removeEventListener('touchmove', unpin);
});

// 索引刷新（切会话/回合结束）时重算红点与槽位
watch(() => props.navItems, () => refresh());

defineExpose({ refresh, jumpToLoaded, cancelJump });
</script>

<style scoped>
/* 固定钉在对话区右侧：相对 .chat-viewport（不滚动的外壳）定位，
   垂直居中、不随内容滚动。点落在会话卡片内缘（外层 6px 白色衬边 + 1px 描边
   之外），不压消息文字、不骑在衬边与卡片的分界线上。 */
.nav-rail {
  position: absolute;
  right: 13px;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  /* 间距由槽位高度提供（不设 gap），点多时槽位整体压缩——
     gap 是固定值不参与 flex 收缩，会导致轨道超高溢出工作区 */
  gap: 0;
  padding: 4px;
  z-index: 40;
  /* 轨道永远不超出主工作区：限高在工作区内（上下各留 12px） */
  max-height: calc(100% - 24px);
  overflow: hidden;
}
.rail-dot {
  width: 22px;
  /* 槽位高度由 JS 按可用空间分级计算：宽松→适中→紧凑→极限，
     保证轨道绝不超出工作区 */
  height: var(--slot-height, 22px);
  min-height: 0;
  flex-shrink: 0; /* 不再由 CSS 压缩，改由 JS 统一计算 */
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  padding: 0;
  background: transparent;
  cursor: pointer;
}
.rail-dot::before {
  content: '';
  /* 点直径由 JS 按可用高度计算：间距压到底仍装不下时缩小点本体 */
  width: var(--dot-size, 8px);
  height: var(--dot-size, 8px);
  border-radius: 50%;
  background: var(--text-faint);
  opacity: 0.55;
  transition: transform 0.12s ease, opacity 0.12s ease, background 0.12s ease;
}
/* 当前正在显示的那条消息：深灰实心高亮，其余保持浅灰 */
.rail-dot.active::before {
  background: var(--text-dim);
  opacity: 1;
  transform: scale(1.25);
}
.rail-dot:hover::before {
  transform: scale(1.4);
  opacity: 1;
}
/* 未加载（历史分页未翻到的区域）：空心环样式——清晰可见（能看到有多少条、可点击），
   又与已加载的实心点明确区分（点击会触发跨页加载）。旧样式（实心、透明度 0.22）
   在浅色主题下几乎不可见，用户会误以为"红点没有渲染出来"。
   :not(.active)：跳转目标被钉住高亮时让 active 的实心样式优先。 */
.rail-dot.dim:not(.active)::before {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--text-faint);
  opacity: 0.6;
}
.rail-dot.dim:not(.active):hover::before {
  opacity: 0.9;
}

/* 悬停预览浮层：fixed 定位在红点左侧，约 20 字（20em）一行自动换行，
   长文本多行展示而不溢出窗口；复用主题浮窗色与毛玻璃质感 */
.rail-tip {
  position: fixed;
  transform: translateY(-50%);
  max-width: 20em;
  padding: 8px 12px;
  background: color-mix(in srgb, var(--theme-float-background) 92%, transparent);
  color: var(--theme-text-bright);
  border: 1px solid var(--theme-border);
  border-radius: var(--theme-radius-small);
  font-size: 12px;
  line-height: 1.55;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: break-word;
  box-shadow: 0 4px 16px var(--theme-shadow-color);
  z-index: 1000;
  pointer-events: none;
}
</style>
