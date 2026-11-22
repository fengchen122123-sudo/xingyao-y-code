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
  getScrollEl: () => HTMLElement | null;
  /** 全部「我发的消息」DOM 元素，顺序与对话流一致 */
  getUserMsgElements: () => HTMLElement[];
  /** 全量 user 消息索引（分页懒加载下红点仍全量）；缺省回退 DOM 驱动 */
  navItems?: Array<{ id: number; preview: string }>;
  loadedFirstId?: number | null;
  /** 按消息 id 在已加载 DOM 中精确定位 user 气泡（App 注入，限定在 #chat-scroll
      内查找）。命中即权威定位；缺省时退回索引换算；
      未命中的降级路径由调用方决定（点击入口不落邻近、false 交跨页） */
  getUserMsgElementById?: (id: number) => HTMLElement | null;
}>();

const emit = defineEmits<{
  (e: 'jump', payload: { index: number; id: number }): void;
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
  return nextUser >= 0 ? nextUser : props.navItems!.length;
});

const msgCount = ref(0);
const msgTexts = ref<string[]>([]);
const dots = computed(() => Array.from({ length: msgCount.value }));

const MAX_PREVIEW_LEN = 100;

function buildPreview(el: HTMLElement): string {
  const textEl = el.querySelector('.user-text');
  const text = textEl?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  if (text) {
    return text.length > MAX_PREVIEW_LEN ? `${text.slice(0, MAX_PREVIEW_LEN)}…` : text;
  }
  if (el.querySelector('.msg-img')) return '[图片]';
  return '';
}

const hoverIndex = ref(-1);
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

const BASE_DOT_SIZE = 8;
const MIN_DOT_SIZE = 3;
const BASE_SLOT = 22;
const MID_SLOT = 16;
const COMPACT_SLOT = 12;
const MIN_SLOT = 9;
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
    const fit = Math.floor(avail / n);
    dotSize.value = Math.max(MIN_DOT_SIZE, Math.min(BASE_DOT_SIZE, fit - 1));
    slotHeight.value = Math.max(MIN_DOT_SIZE + 1, fit);
  }
}

const activeIndex = ref(-1);
let pinnedIndex = -1;

function updateActive() {
  const scrollEl = props.getScrollEl();
  const els = props.getUserMsgElements();
  if (!scrollEl || !els.length) {
    activeIndex.value = -1;
    return;
  }
  if (pinnedIndex >= 0) {
    activeIndex.value = Math.min(pinnedIndex, msgCount.value - 1);
    return;
  }
  const distToBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
  if (distToBottom < 80) {
    activeIndex.value = (hasNavIndex.value ? loadedFirstIndex.value : 0) + els.length - 1;
    return;
  }
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
  cancelJumpChecks();
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
      emit('jump', { index: i, id: item.id });
    }
    return;
  }
  scrollToLocalIndex(i);
}

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

function computeJumpTarget(scrollEl: HTMLElement, el: HTMLElement): number {
  const rect = el.getBoundingClientRect();
  const scrollRect = scrollEl.getBoundingClientRect();
  const raw = rect.top - scrollRect.top + scrollEl.scrollTop - 16;
  const max = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight);
  return Math.min(Math.max(0, raw), max);
}

const JUMP_ALIGN_TOLERANCE = 10;
const JUMP_CHECK_DELAYS_MS = [800, 1500];
const JUMP_FINALIZE_MS = 2500;       // 兜底：无论结果，彻底结束检查
const JUMP_CHECK_MAX_CORRECTIONS = 2;
const JUMP_SCROLL_IDLE_MS = 200;
let jumpTimers: number[] = [];
let jumpChecking = false;
let jumpCorrections = 0;
let jumpLastScrollAt = 0;

function cancelJumpTimers() {
  for (const t of jumpTimers) window.clearTimeout(t);
  jumpTimers = [];
}

function cancelJumpChecks() {
  cancelJumpTimers();
  jumpChecking = false;
}

function cancelJump() {
  cancelJumpChecks();
  pinnedIndex = -1;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

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
  if (!el.isConnected || props.getScrollEl() !== scrollEl) {
    cancelJumpChecks();
    return;
  }
  if (!final && Date.now() - jumpLastScrollAt < JUMP_SCROLL_IDLE_MS) return;
  if (jumpCorrections >= JUMP_CHECK_MAX_CORRECTIONS) {
    cancelJumpChecks();
    return;
  }
  const target = computeJumpTarget(scrollEl, el);
  const delta = target - scrollEl.scrollTop;
  if (Math.abs(delta) <= JUMP_ALIGN_TOLERANCE) {
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
  if (jumpChecking) jumpLastScrollAt = Date.now();
  updateActive();
};
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

watch(() => props.navItems, () => refresh());

defineExpose({ refresh, jumpToLoaded, cancelJump });
</script>

<style scoped>
.nav-rail {
  position: absolute;
  right: 13px;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
  padding: 4px;
  z-index: 40;
  max-height: calc(100% - 24px);
  overflow: hidden;
}
.rail-dot {
  width: 22px;
  /* 槽位高度由 JS 按可用空间分级计算：宽松→适中→紧凑→极限，
     保证轨道绝不超出工作区 */
  height: var(--slot-height, 22px);
  min-height: 0;
  flex-shrink: 0;
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
  width: var(--dot-size, 8px);
  height: var(--dot-size, 8px);
  border-radius: 50%;
  background: var(--text-faint);
  opacity: 0.55;
  transition: transform 0.12s ease, opacity 0.12s ease, background 0.12s ease;
}
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
