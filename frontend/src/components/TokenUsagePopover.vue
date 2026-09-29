<template>
  <!-- Teleport 到 body：与 ContextUsagePanel 同一模式——fixed 浮层须逃离输入区
       isolation 层叠上下文，否则向上弹出时被上方分区遮挡 -->
  <Teleport to="body">
    <div
      v-if="visible"
      class="token-usage-popover"
      data-test="token-usage-popover"
      :style="panelStyle"
      @click.stop
    >
      <!-- 本会话块：数字与状态行同源（落库聚合，重启不清零），额外给请求数/最近活跃 -->
      <div class="tup-section">
        <div class="tup-title-row">
          <span class="tup-title">本会话</span>
          <span class="tup-session-name" :title="currentSessionName">{{ currentSessionName }}</span>
        </div>
        <div class="tup-metrics" data-test="tup-session-metrics">
          <span class="tup-metric"><i>输入</i>{{ formatTokenCount(sessionTotals.prompt) }}</span>
          <span class="tup-metric"><i>输出</i>{{ formatTokenCount(sessionTotals.completion) }}</span>
          <span class="tup-metric">
            <i>命中</i>{{ formatTokenCount(sessionTotals.cached) }}
            <em>{{ sessionRateText }}</em>
          </span>
          <span class="tup-metric"><i>请求</i>{{ formatTokenCount(sessionTotals.requests) }} 次</span>
        </div>
        <div class="tup-sub">最近活跃 {{ lastActiveText }}</div>
        <!-- 保活烧损单列：只在确有保活轮次时出现，不与对话口径混算 -->
        <div v-if="sessionTotals.keepalive_requests > 0" class="tup-keepalive" data-test="tup-keepalive">
          保活烧损 {{ formatTokenCount(sessionTotals.keepalive_prompt) }} tokens
          （{{ sessionTotals.keepalive_requests }} 次保活请求）
        </div>
      </div>

      <div class="tup-sep"></div>

      <!-- 全部会话合计：走落库口径，重启不清零（与状态行旧进程累计口径区分） -->
      <div class="tup-section">
        <div class="tup-title-row">
          <span class="tup-title">全部会话</span>
          <span v-if="overviewTotals" class="tup-requests">{{ formatTokenCount(overviewTotals.requests) }} 次请求</span>
        </div>
        <div v-if="overviewTotals" class="tup-metrics" data-test="tup-all-metrics">
          <span class="tup-metric"><i>输入</i>{{ formatTokenCount(overviewTotals.prompt) }}</span>
          <span class="tup-metric"><i>输出</i>{{ formatTokenCount(overviewTotals.completion) }}</span>
          <span class="tup-metric">
            <i>命中</i>{{ formatTokenCount(overviewTotals.cached) }}
            <em>{{ allRateText }}</em>
          </span>
        </div>
        <div v-else class="tup-empty">{{ loading ? '加载中…' : '暂无数据' }}</div>
      </div>

      <template v-if="sessionRows.length">
        <div class="tup-sep"></div>
        <!-- 会话列表：按合计 token 降序，点击行切换到对应会话 -->
        <div class="tup-list" data-test="tup-session-list">
          <div
            v-for="row in sessionRows"
            :key="row.session_id"
            class="tup-row"
            :class="{ current: row.session_id === currentSessionName }"
            :title="row.session_id"
            @click="onPickSession(row.session_id)"
          >
            <span class="tup-row-name">{{ row.session_id }}</span>
            <span class="tup-row-num">{{ formatTokenCount(row.prompt) }}</span>
            <span class="tup-row-num">{{ formatTokenCount(row.completion) }}</span>
            <span class="tup-row-rate">{{ row.rateText }}</span>
          </div>
        </div>
        <div class="tup-list-head">
          <span>会话（输入 / 输出 / 命中）</span>
          <span>仅对话口径，不含保活</span>
        </div>
      </template>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatTokenCount, sessionHitRate } from '../utils/tokenFormat';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const store = useAppStore();

// 本会话数字直接用推送快照（每次请求记账后都刷新），弹层无需等待；
// 会话名回退链：推送快照 > 概览拉取 > store 当前会话
const sessionTotals = computed(() => store.tokenUsage.session_totals);
const overview = computed(() => store.tokenUsageOverview);
const overviewTotals = computed(() => (overview.value && overview.value.ok ? overview.value.totals : null));
const loading = computed(() => store.tokenUsageOverviewLoading);
const currentSessionName = computed(() => (
  sessionTotals.value.session_id
  || (overview.value && overview.value.ok ? overview.value.current : '')
  || store.currentSessionName
  || ''
));

const sessionRateText = computed(() => {
  const rate = sessionHitRate(sessionTotals.value);
  return rate === null ? '' : `${Math.round(rate)}%`;
});

const allRateText = computed(() => {
  if (!overviewTotals.value) return '';
  const rate = sessionHitRate(overviewTotals.value);
  return rate === null ? '' : `${Math.round(rate)}%`;
});

// 相对时间：与侧栏"5天/17小时"同风格的粗粒度表达，够定位即可
const lastActiveText = computed(() => {
  const ts = sessionTotals.value.last_ts;
  if (!ts) return '暂无记录';
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
});

const sessionRows = computed(() => {
  if (!overview.value || !overview.value.ok) return [];
  return overview.value.sessions.map(row => {
    const rate = sessionHitRate(row);
    return { ...row, rateText: rate === null ? '--' : `${Math.round(rate)}%` };
  });
});

function onPickSession(sessionId: string) {
  if (!sessionId || sessionId === store.currentSessionName) return;
  emit('close');
  store.switchSession(sessionId);
}

// 打开即拉取列表与合计（每次打开都刷新，不做后台轮询）
watch(() => props.visible, (visible) => {
  if (visible) {
    positionPanel();
    store.loadTokenUsageOverview();
    document.addEventListener('keydown', onKeydown);
  } else {
    document.removeEventListener('keydown', onKeydown);
  }
});

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close');
}

// 点击面板与锚点之外关闭（与 PermDropdown 同款全局点击约定）
function onGlobalClick(e: MouseEvent) {
  if (props.visible && props.anchorEl && !props.anchorEl.contains(e.target as Node)) {
    emit('close');
  }
}

// —— 定位：向上弹出（输入框在底部）、水平对齐锚点并在视口内夹取 ——
const PANEL_WIDTH = 380;
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

function onViewportResize() {
  if (props.visible) positionPanel();
}

onMounted(() => {
  document.addEventListener('click', onGlobalClick);
  window.addEventListener('resize', onViewportResize);
});
onUnmounted(() => {
  document.removeEventListener('click', onGlobalClick);
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('resize', onViewportResize);
});
</script>

<style scoped>
.token-usage-popover {
  position: fixed;
  z-index: 999;
  box-sizing: border-box;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--theme-float-background) 80%, transparent);
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(14px);
  color: var(--text);
  animation: tup-fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes tup-fadein {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.tup-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tup-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.tup-title {
  font-size: 12px;
  color: var(--text-dim);
  flex-shrink: 0;
}
.tup-session-name {
  font-size: 12px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tup-requests {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}

.tup-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-family: var(--mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.tup-metric i {
  font-style: normal;
  font-size: 11px;
  color: var(--text-faint);
  margin-right: 5px;
}
.tup-metric em {
  font-style: normal;
  font-size: 11px;
  color: var(--accent);
  margin-left: 4px;
}

.tup-sub {
  font-size: 11px;
  color: var(--text-faint);
}
.tup-keepalive {
  font-size: 11px;
  color: var(--orange);
}
.tup-empty {
  font-size: 12px;
  color: var(--text-faint);
}

.tup-sep {
  margin: 10px 0;
  height: 1px;
  background: var(--border);
}

.tup-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 180px;
  overflow-y: auto;
}
.tup-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 6px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
}
.tup-row:hover {
  background: var(--bg3);
}
.tup-row.current {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.tup-row-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tup-row-num {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--text-dim);
  white-space: nowrap;
}
.tup-row-rate {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--accent);
  width: 38px;
  text-align: right;
  white-space: nowrap;
}
.tup-list-head {
  margin-top: 6px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 10.5px;
  color: var(--text-faint);
}
</style>
