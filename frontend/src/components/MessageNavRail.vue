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
      :class="{ active: i === activeIndex }"
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
import { computed, onMounted, onUnmounted, ref } from 'vue';

const props = defineProps<{
  /** 滚动容器（#chat-scroll），点击红点时执行 scrollTo */
  getScrollEl: () => HTMLElement | null;
  /** 全部「我发的消息」DOM 元素，顺序与对话流一致 */
  getUserMsgElements: () => HTMLElement[];
}>();

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
  const els = props.getUserMsgElements();
  msgCount.value = els.length;
  msgTexts.value = els.map(buildPreview);
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
// 点击红点立即置为目标索引并"钉住"，直到用户手动滚动才交还给几何判定
//（否则点击后 updateActive 的几何计算会立即把高亮算回去）。
const activeIndex = ref(-1);
// 点击跳转的目标索引；用户手动滚动时清除
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
    activeIndex.value = Math.min(pinnedIndex, els.length - 1);
    return;
  }
  // 已滚到底部：用户在看最新一条，直接高亮最后一条（中线判定在"看最后
  // 一条"这个最常见场景会错判给上一条——最后一条顶部通常没越过中线）
  const distToBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
  if (distToBottom < 80) {
    activeIndex.value = els.length - 1;
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
  activeIndex.value = active;
}

function jumpTo(i: number) {
  const scrollEl = props.getScrollEl();
  const el = props.getUserMsgElements()[i];
  if (!scrollEl || !el) return;
  pinnedIndex = i;
  activeIndex.value = i;
  const rect = el.getBoundingClientRect();
  const scrollRect = scrollEl.getBoundingClientRect();
  // 让目标消息滚动到距离滚动容器顶部 16px 的位置（留一点呼吸空间）
  const target = rect.top - scrollRect.top + scrollEl.scrollTop - 16;
  scrollEl.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
}

let resizeObserver: ResizeObserver | undefined;
let boundScrollEl: HTMLElement | null = null;
const onScroll = () => updateActive();
// 用户主动滚动（滚轮/触摸/键盘）时解除点击高亮的钉住，交还给几何判定
const unpin = () => { pinnedIndex = -1; };

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
  resizeObserver?.disconnect();
  boundScrollEl?.removeEventListener('scroll', onScroll);
  boundScrollEl?.removeEventListener('wheel', unpin);
  boundScrollEl?.removeEventListener('touchmove', unpin);
});

defineExpose({ refresh });
</script>

<style scoped>
/* 固定钉在对话区右侧：相对 .chat-viewport（不滚动的外壳）定位，
   垂直居中、不随内容滚动。点贴右侧边缘（gutter 内），不压消息文字。 */
.nav-rail {
  position: absolute;
  right: 4px;
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
