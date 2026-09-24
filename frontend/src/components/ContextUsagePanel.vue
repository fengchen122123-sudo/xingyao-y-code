<template>
  <!-- Teleport 到 body：与 ModelCascader 同一模式——fixed 浮层须逃离输入区
       isolation 层叠上下文，否则向上弹出时被上方分区遮挡 -->
  <Teleport to="body">
    <div
      class="ctx-usage-panel"
      data-test="context-usage-panel"
      :class="{ show: visible }"
      :style="panelStyle"
      @mouseenter="emit('hover-enter')"
      @mouseleave="emit('hover-leave')"
    >
      <!-- 标题行：左中文标题、右等宽摘要（完整千分位，百分比 1 位小数） -->
      <div class="cxp-header">
        <span class="cxp-title">上下文容量</span>
        <span class="cxp-summary" data-test="ctx-usage-summary">{{ summaryText }}</span>
      </div>

      <!-- 进度条：fill 表示已用/窗口；有来源数据时按占比分段着色（同色系深浅），否则单色 -->
      <div class="cxp-bar">
        <div class="cxp-bar-fill" data-test="ctx-usage-bar-fill" :style="{ width: fillWidth }">
          <span
            v-for="seg in barSegments"
            :key="seg.source"
            class="cxp-bar-seg"
            :style="{ width: seg.width, background: seg.color }"
          ></span>
        </div>
      </div>

      <!-- 来源占比列表：按 chars 降序，仅显示 chars>0；Σchars<=0 时整段隐藏 -->
      <div v-if="rows.length" class="cxp-rows" data-test="ctx-usage-breakdown">
        <div v-for="row in rows" :key="row.source" class="cxp-row" data-test="ctx-usage-row">
          <span class="cxp-swatch" :style="{ background: row.color }"></span>
          <span class="cxp-row-name">{{ row.label }}</span>
          <span class="cxp-row-pct">{{ row.pctText }}</span>
        </div>
      </div>

      <!-- 缓存命中率：cache_available=false 或无值时隐藏 -->
      <template v-if="showCacheRow">
        <div class="cxp-sep"></div>
        <div class="cxp-cache" data-test="ctx-usage-cache">
          <span class="cxp-cache-label">缓存命中率</span>
          <span class="cxp-cache-value">{{ cachePctText }}</span>
        </div>
      </template>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { ContextBreakdownSource } from '../types/pywebview';
import { formatTokenCount } from '../utils/tokenFormat';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits<{
  (e: 'hover-enter'): void;
  (e: 'hover-leave'): void;
}>();

const store = useAppStore();

// 中文名映射（后端来源白名单；键序即校验集合，不参与渲染顺序）
// 2026-09-23 对齐 ZCode：工具拆分为系统工具 / MCP 工具，标签同步
const SOURCE_LABELS: Record<ContextBreakdownSource, string> = {
  messages: '消息',
  system_prompt: '系统提示词',
  skills: '技能',
  runtime_context: '其他',
  system_tools: '系统工具',
  mcp_tools: 'MCP 工具',
};

// 同一色系五档深浅：色相固定为 accent，按列表顺序递减混合强度
const SEGMENT_INTENSITY = [100, 78, 60, 44, 30];

function segmentColor(index: number): string {
  const pct = SEGMENT_INTENSITY[Math.min(index, SEGMENT_INTENSITY.length - 1)];
  return `color-mix(in srgb, var(--accent) ${pct}%, var(--bg2))`;
}

// 防御性过滤：store 已归一化，面板对任意来源数据仍保持健壮（非法项静默丢弃）
const breakdown = computed(() =>
  (store.tokenUsage.context_breakdown || [])
    .filter(item => !!item && SOURCE_LABELS[item.source] !== undefined && Number(item.chars) > 0)
    .map(item => ({ source: item.source, chars: Number(item.chars) }))
    .sort((a, b) => b.chars - a.chars));

const breakdownTotal = computed(() => breakdown.value.reduce((sum, item) => sum + item.chars, 0));

// 每行：色块 + 中文名 + 1 位小数百分比；色块与进度条分段同色
const breakdownRows = computed(() => {
  const total = breakdownTotal.value;
  if (total <= 0) return [];
  return breakdown.value.map((item, index) => {
    const pct = (item.chars / total) * 100;
    return {
      source: item.source,
      label: SOURCE_LABELS[item.source],
      pct,
      color: segmentColor(index),
    };
  });
});

const rows = computed(() => breakdownRows.value.map(row => ({
  source: row.source,
  label: row.label,
  pctText: `${row.pct.toFixed(1)}%`,
  color: row.color,
})));

const barSegments = computed(() => breakdownRows.value.map(row => ({
  source: row.source,
  width: `${row.pct.toFixed(2)}%`,
  color: row.color,
})));

// 摘要：used / window（pct 1 位小数）；window<=0 时按 0% 处理，不产生 NaN
const summaryText = computed(() => {
  const used = store.tokenUsage.used || 0;
  const win = store.tokenUsage.window || 0;
  const pct = win > 0 ? (used / win) * 100 : 0;
  return `${formatTokenCount(used)} / ${formatTokenCount(win)}（${pct.toFixed(1)}%）`;
});

const fillWidth = computed(() => {
  const used = store.tokenUsage.used || 0;
  const win = store.tokenUsage.window || 0;
  const ratio = win > 0 ? Math.min(Math.max(used / win, 0), 1) : 0;
  return `${(ratio * 100).toFixed(2)}%`;
});

const showCacheRow = computed(() => {
  const usage = store.tokenUsage;
  const pct = usage.cache_session_pct;
  return usage.cache_available === true && typeof pct === 'number' && Number.isFinite(pct);
});

const cachePctText = computed(() => `${Number(store.tokenUsage.cache_session_pct).toFixed(1)}%`);

// 保活命中率已下线（与车道身份行同批）：store/后端字段保留，界面不再展示。

// —— 定位：向上弹出（输入框在底部）、水平对齐锚点并在视口内夹取 ——
const PANEL_WIDTH = 320;
const PANEL_GAP = 8;
const VIEWPORT_PADDING = 12;
const panelStyle = ref<Record<string, string>>({});

function positionPanel() {
  if (!props.anchorEl) return;
  const rect = props.anchorEl.getBoundingClientRect();
  const vw = window.innerWidth;
  const width = Math.min(PANEL_WIDTH, Math.max(0, vw - VIEWPORT_PADDING * 2));
  const left = Math.max(
    VIEWPORT_PADDING,
    Math.min(Math.floor(rect.left), vw - VIEWPORT_PADDING - width),
  );
  panelStyle.value = {
    position: 'fixed',
    bottom: `${Math.max(0, Math.floor(window.innerHeight - rect.top + PANEL_GAP))}px`,
    top: 'auto',
    left: `${left}px`,
    right: 'auto',
    width: `${width}px`,
  };
}

watch(() => props.visible, (visible) => {
  if (visible) positionPanel();
}, { immediate: true });

// 打开期间视口尺寸变化（窗口缩放/软键盘）重新夹取位置
function onViewportResize() {
  if (props.visible) positionPanel();
}

onMounted(() => window.addEventListener('resize', onViewportResize));
onUnmounted(() => window.removeEventListener('resize', onViewportResize));
</script>

<style scoped>
.ctx-usage-panel {
  position: fixed;
  z-index: 999;
  display: none;
  box-sizing: border-box;
  width: 320px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--theme-float-background) 80%, transparent);
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(14px);
  color: var(--text);
}
.ctx-usage-panel.show {
  display: block;
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}

.cxp-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.cxp-title {
  font-size: 12px;
  color: var(--text-dim);
}
.cxp-summary {
  font-family: var(--mono);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--text);
  white-space: nowrap;
}

/* 进度条：高 8px 圆角，fill 表示已用/窗口，内部分段按来源占比着色 */
.cxp-bar {
  margin-top: 10px;
  height: 8px;
  border-radius: 4px;
  background: var(--bg3);
  overflow: hidden;
}
.cxp-bar-fill {
  display: flex;
  height: 100%;
  border-radius: 4px;
  background: var(--accent);
  overflow: hidden;
  transition: width 0.3s;
}
.cxp-bar-seg {
  display: block;
  height: 100%;
  flex-shrink: 0;
}

.cxp-rows {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cxp-row {
  display: flex;
  align-items: center;
  font-size: 12px;
  color: var(--text-dim);
}
.cxp-swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 8px;
  flex-shrink: 0;
}
.cxp-row-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cxp-row-pct {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--text);
  margin-left: 8px;
}

.cxp-sep {
  height: 1px;
  background: var(--border);
  margin: 10px 0;
}
.cxp-cache {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
}
.cxp-cache-label {
  color: var(--text-dim);
}
.cxp-cache-value {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
</style>