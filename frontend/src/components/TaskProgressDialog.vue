<template>
  <div class="plan-pop" data-test="task-dialog" @click.stop>
    <div class="pop-head">
      <div class="ph-ico"><AppIcon name="layers" :size="14" :stroke-width="2" /></div>
      <div class="ph-title">
        <h3>星链</h3>
        <p class="ph-sub">{{ headerSubtitle }}</p>
      </div>
      <button
        type="button"
        class="pop-close-btn"
        title="关闭"
        aria-label="关闭"
        data-test="task-pop-close"
        @click="$emit('close')"
      >
        <AppIcon name="x" :size="13" :stroke-width="2" />
      </button>
    </div>

    <div class="pop-overview">
      <div class="plan-title-row" data-test="task-dialog-plan">
        <div class="plan-title-left">
          <span class="dot" :class="progressTone"></span>
          <span class="plan-title-text">{{ planTitle }}</span>
        </div>
        <div class="plan-progress-nums">
          <span v-if="planItems.length > 0" class="plan-ratio">{{ doneCount }}/{{ planItems.length }}</span>
          <span class="progress-pct" :class="progressTone">{{ progressPct }}%</span>
        </div>
      </div>

      <div class="progress-bar" role="progressbar" :aria-valuenow="progressPct" aria-valuemin="0" aria-valuemax="100">
        <i :class="progressTone" :style="{ width: progressPct + '%' }"></i>
      </div>

      <div
        v-if="planItems.length > 0"
        class="stats-grid"
        :class="{ 'has-issues': issueCount > 0 }"
        data-test="task-dialog-stats"
      >
        <button
          v-for="cell in statCells"
          :key="cell.key"
          type="button"
          class="stat-cell"
          :class="[cell.tone, { active: statusFilter === cell.key }]"
          :disabled="cell.key !== 'all' && cell.count === 0"
          :data-test="'task-filter-' + cell.key"
          @click="toggleFilter(cell.key)"
        >
          <span class="k">{{ cell.label }}</span>
          <span class="v">{{ cell.count }}</span>
        </button>
      </div>
    </div>

    <div class="pop-body" @scroll="hideTipImmediate">
      <ul v-if="filteredItems.length > 0" class="plan-list" data-test="task-dialog-list">
        <li
          v-for="(item, index) in filteredItems"
          :key="item.id"
          class="plan-card"
          :class="[item.status, { expandable: hasDetails(item), open: isExpanded(item.id) }]"
          :data-test="'plan-item-' + item.status"
          @click="toggleItem(item, $event)"
          @mouseenter="onCardEnter(item, index, $event)"
          @mouseleave="scheduleHideTip"
        >
          <div class="p-row">
            <span class="p-status-ico" :class="item.status" :aria-label="statusLabel(item.status)">
              <AppIcon
                :name="statusIcon(item.status)"
                :size="11"
                :stroke-width="2.2"
                :class="{ 'tp-icon-spin': item.status === 'in_progress' }"
              />
            </span>

            <span class="p-id">{{ displayItemId(item.id, index) }}</span>

            <div class="p-content">
              <div class="p-text-line">
                <span
                  v-if="isIssueStatus(item.status)"
                  class="p-tag"
                  :class="'status-' + item.status"
                >{{ statusLabel(item.status) }}</span>
                <span
                  v-if="executionModeMeta(item.execution_mode, item.handoff_task_id)"
                  class="p-tag"
                  :class="executionModeMeta(item.execution_mode, item.handoff_task_id)?.tone"
                >{{ executionModeMeta(item.execution_mode, item.handoff_task_id)?.label }}</span>
                <span v-if="item.attempts && item.attempts > 1" class="p-tag tag-muted">
                  第{{ item.attempts }}次
                </span>
                <span class="p-text">{{ item.text }}</span>
              </div>

              <div v-if="item.handoff_note" class="p-callout warn" data-test="plan-item-handoff">
                <AppIcon name="triangle-alert" :size="11" :stroke-width="2" />
                <span>{{ item.handoff_note }}</span>
              </div>

              <div
                v-if="item.error && !isExpanded(item.id)"
                class="p-callout bad"
                data-test="plan-item-error"
              >
                <AppIcon name="info" :size="11" :stroke-width="2" />
                <span>{{ item.error }}</span>
              </div>

              <div
                v-if="hasDetails(item) && isExpanded(item.id)"
                class="p-detail"
                data-test="plan-item-detail"
                @click.stop
              >
                <div v-if="item.depends_on?.length" class="detail-block">
                  <div class="detail-title">前置依赖</div>
                  <div class="dep-list">
                    <span
                      v-for="depId in item.depends_on"
                      :key="depId"
                      class="dep-pill"
                      :class="depStatus(depId)"
                    >
                      <span class="dep-dot"></span>
                      <code>{{ formatDepRef(depId) }}</code>
                      <span v-if="depSubject(depId)" class="dep-subject">{{ depSubject(depId) }}</span>
                    </span>
                  </div>
                </div>

                <div v-if="item.files?.length" class="detail-block">
                  <div class="detail-title">关联文件</div>
                  <div class="file-list">
                    <code v-for="filePath in item.files" :key="filePath" class="file-chip">{{ filePath }}</code>
                  </div>
                </div>

                <div v-if="item.verification" class="detail-block">
                  <div class="detail-title">
                    {{ item.verification_type === 'command' ? '命令验收' : '结果验收' }}
                  </div>
                  <pre class="detail-pre">{{ item.verification }}</pre>
                </div>

                <div v-if="item.result" class="detail-block">
                  <div class="detail-title">执行结论</div>
                  <pre class="detail-pre good">{{ item.result }}</pre>
                </div>

                <div v-if="item.error" class="detail-block">
                  <div class="detail-title">异常原因</div>
                  <pre class="detail-pre bad">{{ item.error }}</pre>
                </div>

                <div
                  v-if="stageLabel(item.stage) || item.owner || item.handoff_task_id || item.handoff_reason"
                  class="detail-meta-strip"
                >
                  <span v-if="stageLabel(item.stage)">阶段: {{ stageLabel(item.stage) }}</span>
                  <span v-if="item.owner">执行者: {{ item.owner }}</span>
                  <span v-if="item.handoff_task_id">任务ID: {{ item.handoff_task_id }}</span>
                  <span v-if="item.handoff_reason">接管原因: {{ item.handoff_reason }}</span>
                </div>
              </div>
            </div>

            <div v-if="item.handoff_task_id || hasDetails(item)" class="p-row-actions">
              <button
                v-if="item.handoff_task_id"
                type="button"
                class="p-subagent-btn"
                data-test="plan-item-subagent"
                title="在右侧面板查看该子代理的执行过程"
                @click.stop="openSubagent(item.handoff_task_id)"
              >
                子代理
              </button>
              <span
                v-if="hasDetails(item)"
                class="chevron-ico"
                :class="{ rotated: isExpanded(item.id) }"
              >
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" />
              </span>
            </div>
          </div>
        </li>
      </ul>

      <div v-else-if="planItems.length > 0" class="pop-empty" data-test="task-dialog-filter-empty">
        <p>当前筛选条件下暂无计划条目</p>
        <button
          type="button"
          class="pop-btn pop-btn-secondary"
          data-test="task-filter-reset"
          @click="statusFilter = 'all'"
        >
          显示全部 ({{ planItems.length }})
        </button>
      </div>

      <div v-else class="pop-empty" data-test="task-dialog-empty">
        <div class="empty-ico"><AppIcon name="layers" :size="16" :stroke-width="1.7" /></div>
        <p>星链还没有条目。星瑶拆解任务后，各条目的执行进度会在这里实时更新。</p>
      </div>
    </div>

    <Teleport to="body">
      <div
        v-if="tipState.visible"
        class="plan-item-tip"
        data-test="plan-item-tooltip"
        :style="tipState.style"
        @mouseenter="cancelHideTip"
        @mouseleave="scheduleHideTip"
      >
        <div class="tip-head">
          <span class="tip-id">{{ tipState.idLabel }}</span>
          <span class="tip-status" :class="'status-' + tipState.status">{{ statusLabel(tipState.status) }}</span>
        </div>
        <div class="tip-body">{{ tipState.text }}</div>
      </div>
    </Teleport>

    <div class="pop-foot">
      <span class="foot-hint">{{ footHint }}</span>
      <div v-if="planItems.length > 0" class="foot-actions">
        <button
          v-if="anyHasDetails"
          type="button"
          class="pop-btn pop-btn-secondary"
          data-test="task-pop-toggle-all"
          @click="toggleExpandAll"
        >
          <span>{{ allExpanded ? '收起详情' : '展开详情' }}</span>
        </button>
        <button
          type="button"
          class="pop-btn pop-btn-accent"
          data-test="task-pop-copy"
          @click="copyPlanMarkdown"
        >
          <AppIcon :name="copyState === 'copied' ? 'check' : 'copy'" :size="12" :stroke-width="2" />
          <span>{{ copyState === 'copied' ? '已复制' : '复制清单' }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { PlanTaskItem, PlanTaskStatus } from '../types/pywebview';
import type { IconName } from './iconPaths';
import AppIcon from './AppIcon.vue';

const emit = defineEmits<{ close: [] }>();

const store = useAppStore();

type StatusFilter = 'all' | 'in_progress' | 'done' | 'todo' | 'issue';

const statusFilter = ref<StatusFilter>('all');
const expandedIds = ref<Record<string, boolean>>({});
const copyState = ref<'idle' | 'copied'>('idle');
let copyResetTimer: ReturnType<typeof setTimeout> | undefined;

const planTitle = computed(() => store.planData?.title || '当前任务计划');
const planItems = computed<PlanTaskItem[]>(() => store.planData?.items ?? []);
const progressPct = computed(() => {
  const raw = Number(store.planData?.progress ?? 0);
  if (!Number.isFinite(raw)) return 0;
  return Math.max(0, Math.min(100, Math.round(raw)));
});

const ISSUE_STATUSES = new Set<PlanTaskStatus>(['failed', 'blocked', 'cancelled']);

const doneCount = computed(() => planItems.value.filter((i) => i.status === 'done').length);
const inProgressCount = computed(() => planItems.value.filter((i) => i.status === 'in_progress').length);
const todoCount = computed(() => planItems.value.filter((i) => i.status === 'todo').length);
const issueCount = computed(() => planItems.value.filter((i) => ISSUE_STATUSES.has(i.status)).length);

const progressTone = computed(() => {
  if (issueCount.value > 0 && inProgressCount.value === 0) return 'bad';
  if (planItems.value.length > 0 && (progressPct.value >= 100 || doneCount.value === planItems.value.length)) {
    return 'good';
  }
  if (inProgressCount.value > 0) return 'active';
  return 'idle';
});

const statCells = computed(() => {
  const base: Array<{ key: StatusFilter; label: string; count: number; tone?: string }> = [
    { key: 'all', label: '全部', count: planItems.value.length },
    { key: 'in_progress', label: '进行中', count: inProgressCount.value, tone: inProgressCount.value > 0 ? 'accent' : undefined },
    { key: 'done', label: '已完成', count: doneCount.value, tone: doneCount.value > 0 ? 'green' : undefined },
    { key: 'todo', label: '待执行', count: todoCount.value },
  ];
  if (issueCount.value > 0) {
    base.push({ key: 'issue', label: '异常', count: issueCount.value, tone: 'rose' });
  }
  return base;
});

const filteredItems = computed(() => {
  const list = planItems.value;
  if (statusFilter.value === 'all') return list;
  if (statusFilter.value === 'issue') return list.filter((i) => ISSUE_STATUSES.has(i.status));
  return list.filter((i) => i.status === statusFilter.value);
});

const itemById = computed(() => {
  const map = new Map<string, PlanTaskItem>();
  for (const item of planItems.value) {
    map.set(item.id, item);
  }
  return map;
});

const anyHasDetails = computed(() => planItems.value.some((item) => hasDetails(item)));
const allExpanded = computed(() => {
  const expandable = filteredItems.value.filter((item) => hasDetails(item));
  return expandable.length > 0 && expandable.every((item) => Boolean(expandedIds.value[item.id]));
});

const headerSubtitle = computed(() => (
  anyHasDetails.value
    ? '星瑶编排复杂任务时的执行进度 · 点击条目展开详情'
    : '星瑶编排复杂任务时的执行进度'
));

const footHint = computed(() => {
  const total = planItems.value.length;
  if (total === 0) return '任务拆解后自动同步进度';
  if (issueCount.value > 0) return `共 ${total} 项 · ${issueCount.value} 项异常需关注`;
  if (inProgressCount.value > 0) return `共 ${total} 项 · ${inProgressCount.value} 项执行中`;
  if (doneCount.value === total) return `共 ${total} 项 · 已全部完成`;
  return `共 ${total} 项 · 实时同步推进`;
});

function toggleFilter(key: StatusFilter) {
  if (key !== 'all' && statusFilter.value === key) {
    statusFilter.value = 'all';
  } else {
    statusFilter.value = key;
  }
}

function isIssueStatus(status: PlanTaskStatus): boolean {
  return ISSUE_STATUSES.has(status);
}

function hasDetails(item: PlanTaskItem): boolean {
  return Boolean(
    item.depends_on?.length
    || item.files?.length
    || item.verification
    || item.result
    || item.error
    || item.owner
    || item.handoff_task_id
    || item.handoff_reason,
  );
}

function isExpanded(id: string): boolean {
  return Boolean(expandedIds.value[id]);
}

async function toggleItem(item: PlanTaskItem, event?: MouseEvent) {
  if (!hasDetails(item)) return;
  hideTipImmediate();
  const nextOpen = !expandedIds.value[item.id];
  const cardEl = event?.currentTarget as HTMLElement | null;
  expandedIds.value = {
    ...expandedIds.value,
    [item.id]: nextOpen,
  };
  if (nextOpen && cardEl) {
    await nextTick();
    cardEl.scrollIntoView?.({ block: 'nearest' });
  }
}

const tipState = ref<{
  visible: boolean;
  idLabel: string;
  status: PlanTaskStatus;
  text: string;
  style: Record<string, string>;
}>({
  visible: false,
  idLabel: '',
  status: 'todo',
  text: '',
  style: {},
});
let tipHideTimer: ReturnType<typeof setTimeout> | undefined;

function cancelHideTip() {
  if (tipHideTimer !== undefined) {
    clearTimeout(tipHideTimer);
    tipHideTimer = undefined;
  }
}

function hideTipImmediate() {
  cancelHideTip();
  if (tipState.value.visible) {
    tipState.value = { ...tipState.value, visible: false };
  }
}

function scheduleHideTip() {
  cancelHideTip();
  tipHideTimer = setTimeout(() => {
    tipState.value = { ...tipState.value, visible: false };
  }, 110);
}

function onCardEnter(item: PlanTaskItem, index: number, event: MouseEvent) {
  if (isExpanded(item.id)) return;
  const cardEl = event.currentTarget as HTMLElement | null;
  const textEl = cardEl?.querySelector('.p-text') as HTMLElement | null;
  if (!cardEl || !textEl || textEl.scrollWidth <= textEl.clientWidth + 1) return;

  const rect = cardEl.getBoundingClientRect();
  const viewportW = window.innerWidth || 1024;
  const viewportH = window.innerHeight || 768;
  const width = Math.max(240, Math.min(rect.width || 380, 440, viewportW - 24));
  const left = Math.max(12, Math.min(rect.left, viewportW - width - 12));
  const style: Record<string, string> = {
    left: `${left}px`,
    width: `${width}px`,
    maxHeight: `${Math.max(140, Math.round(viewportH * 0.45))}px`,
  };
  if (rect.bottom + 140 > viewportH && rect.top > 150) {
    style.bottom = `${Math.max(12, viewportH - rect.top + 6)}px`;
  } else {
    style.top = `${rect.bottom + 6}px`;
  }

  cancelHideTip();
  tipState.value = {
    visible: true,
    idLabel: displayItemId(item.id, index),
    status: item.status,
    text: item.text,
    style,
  };
}

function toggleExpandAll() {
  const nextOpen = !allExpanded.value;
  const next: Record<string, boolean> = { ...expandedIds.value };
  for (const item of filteredItems.value) {
    if (hasDetails(item)) {
      next[item.id] = nextOpen;
    }
  }
  expandedIds.value = next;
}

function statusIcon(status: PlanTaskStatus): IconName {
  if (status === 'done') return 'check';
  if (status === 'in_progress') return 'loader';
  if (status === 'failed') return 'x';
  if (status === 'blocked') return 'triangle-alert';
  if (status === 'cancelled') return 'ban';
  return 'circle';
}

function statusLabel(status: PlanTaskStatus): string {
  switch (status) {
    case 'done': return '已完成';
    case 'in_progress': return '进行中';
    case 'failed': return '失败';
    case 'blocked': return '已阻塞';
    case 'cancelled': return '已取消';
    default: return '待执行';
  }
}

function stageLabel(stage?: string): string {
  if (stage === 'plan') return '规划';
  if (stage === 'build') return '构建';
  if (stage === 'review') return '验收';
  return '';
}

function executionModeMeta(mode?: string, taskId?: string): { label: string; tone: string } | null {
  if (mode === 'main_takeover') return { label: '主 Agent 接管', tone: 'tag-warn' };
  if (mode === 'takeover_queued') return { label: '排队接管', tone: 'tag-warn' };
  if (mode === 'delegated' && !taskId) return { label: '子代理', tone: 'tag-accent' };
  return null;
}

function displayItemId(id: string, index: number): string {
  const clean = String(id || '').trim();
  if (!clean || /^0\.\d+$/.test(clean)) return `#${index + 1}`;
  if (/^\d+$/.test(clean)) return `#${clean}`;
  return clean;
}

function formatDepRef(depId: string): string {
  const clean = String(depId || '').trim();
  return /^\d+$/.test(clean) ? `#${clean}` : clean;
}

function depSubject(depId: string): string {
  const target = itemById.value.get(depId);
  if (!target?.text) return '';
  return target.text.length > 26 ? `${target.text.slice(0, 26)}…` : target.text;
}

function depStatus(depId: string): string {
  return itemById.value.get(depId)?.status || 'todo';
}

function openSubagent(taskId?: string) {
  if (!taskId) return;
  store.openSubagentDetail(taskId);
}

async function copyPlanMarkdown() {
  if (planItems.value.length === 0) return;
  const lines = [
    `## ${planTitle.value}（${progressPct.value}%，${doneCount.value}/${planItems.value.length}）`,
    '',
    ...planItems.value.map((item, index) => {
      const box = item.status === 'done' ? 'x' : (item.status === 'in_progress' ? '-' : ' ');
      const suffix = item.status === 'in_progress'
        ? '（进行中）'
        : item.status === 'failed'
          ? '（失败）'
          : item.status === 'blocked'
            ? '（已阻塞）'
            : item.status === 'cancelled'
              ? '（已取消）'
              : '';
      return `- [${box}] ${displayItemId(item.id, index)} ${item.text}${suffix}`;
    }),
  ];
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(lines.join('\n'));
      copyState.value = 'copied';
      if (copyResetTimer !== undefined) clearTimeout(copyResetTimer);
      copyResetTimer = setTimeout(() => {
        copyState.value = 'idle';
      }, 1500);
    }
  } catch {
    // 静默兜底：剪贴板不可用时不抛未捕获异常
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && !e.isComposing) {
    hideTipImmediate();
    emit('close');
  }
}

watch(
  () => store.currentSessionName,
  () => {
    hideTipImmediate();
    statusFilter.value = 'all';
    expandedIds.value = {};
  },
);

watch(
  () => planItems.value.length,
  (len) => {
    if (len === 0) {
      hideTipImmediate();
      statusFilter.value = 'all';
      expandedIds.value = {};
    }
  },
);

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
});

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown);
  cancelHideTip();
  if (copyResetTimer !== undefined) {
    clearTimeout(copyResetTimer);
    copyResetTimer = undefined;
  }
});
</script>

<style scoped>
.plan-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 480px;
  max-width: min(480px, calc(100vw - 32px));
  background: var(--xy-card, var(--float-bg));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 14px;
  box-shadow: var(--xy-shadow-pop, 0 12px 32px rgba(0, 0, 0, 0.35));
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(580px, 72vh);
  overflow: hidden;
  text-align: left;
  color: var(--xy-text, var(--text));
}

.pop-head {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 13px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  flex-shrink: 0;
}
.ph-ico {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  flex: none;
}
.ph-title {
  flex: 1;
  min-width: 0;
}
.ph-title h3 {
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.25;
  color: var(--xy-text, var(--text));
}
.ph-sub {
  margin: 1px 0 0;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
  line-height: 1.3;
}

.pop-close-btn {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
  display: grid;
  place-items: center;
  flex: none;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.pop-close-btn:hover {
  background: var(--xy-hover, var(--bg3));
  border-color: var(--xy-border-strong, var(--border));
  color: var(--xy-text, var(--text));
}

.pop-overview {
  padding: 9px 13px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  display: flex;
  flex-direction: column;
  gap: 7px;
  flex-shrink: 0;
}
.plan-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.plan-title-left {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  min-width: 0;
  flex: 1;
}
.dot {
  width: 7px;
  height: 7px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--xy-text-3, var(--text-faint));
  flex: none;
}
.dot.active {
  background: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 3px var(--xy-accent-soft, var(--accent-soft));
}
.dot.good {
  background: var(--xy-green, var(--green));
  box-shadow: 0 0 0 3px var(--xy-green-soft, color-mix(in srgb, var(--green) 16%, transparent));
}
.dot.bad {
  background: var(--xy-rose, var(--red));
  box-shadow: 0 0 0 3px var(--xy-rose-soft, color-mix(in srgb, var(--red) 16%, transparent));
}
.plan-title-text {
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.38;
  color: var(--xy-text, var(--text));
  overflow-wrap: anywhere;
  word-break: break-word;
}
.plan-progress-nums {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  flex-shrink: 0;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
.plan-ratio {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}
.progress-pct {
  font-size: 12px;
  font-weight: 700;
  color: var(--xy-accent, var(--accent));
}
.progress-pct.good {
  color: var(--xy-green, var(--green));
}
.progress-pct.bad {
  color: var(--xy-rose, var(--red));
}

.progress-bar {
  height: 5px;
  background: var(--xy-active, var(--bg3));
  border-radius: 99px;
  overflow: hidden;
}
.progress-bar i {
  display: block;
  height: 100%;
  border-radius: 99px;
  background: var(--xy-accent, var(--accent));
  transition: width 0.3s ease, background-color 0.25s ease;
}
.progress-bar i.good {
  background: var(--xy-green, var(--green));
}
.progress-bar i.bad {
  background: var(--xy-rose, var(--red));
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 5px;
}
.stats-grid.has-issues {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
@media (max-width: 460px) {
  .stats-grid,
  .stats-grid.has-issues {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.stat-cell {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border, var(--border));
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.stat-cell:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  border-color: var(--xy-border-strong, var(--border));
}
.stat-cell:disabled {
  cursor: default;
  opacity: 0.72;
}
.stat-cell.active {
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 55%, transparent);
  background: var(--xy-accent-soft, var(--accent-soft));
}
.stat-cell .k {
  color: var(--xy-text-3, var(--text-faint));
}
.stat-cell.active .k {
  color: var(--xy-text-2, var(--text-dim));
  font-weight: 500;
}
.stat-cell .v {
  font-family: var(--mono);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--xy-text, var(--text));
}
.stat-cell.accent .v {
  color: var(--xy-accent, var(--accent));
}
.stat-cell.green .v {
  color: var(--xy-green, var(--green));
}
.stat-cell.rose .v {
  color: var(--xy-rose, var(--red));
}

.pop-body {
  padding: 8px 13px;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.plan-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.plan-card {
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  min-width: 0;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}
.plan-card.expandable {
  cursor: pointer;
}
.plan-card.expandable:hover {
  background: var(--xy-hover, var(--bg3));
  border-color: var(--xy-border-strong, var(--border));
}
.plan-card.open {
  background: var(--xy-card, var(--bg3));
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 45%, transparent);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
}
.plan-card.in_progress {
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 48%, transparent);
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 6%, var(--xy-surface, var(--bg2)));
}
.plan-card.done {
  border-color: color-mix(in srgb, var(--xy-green, var(--green)) 26%, var(--xy-border, var(--border)));
  background: color-mix(in srgb, var(--xy-green, var(--green)) 4%, var(--xy-surface, var(--bg2)));
}
.plan-card.failed {
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 42%, transparent);
  background: color-mix(in srgb, var(--xy-rose, var(--red)) 5%, var(--xy-surface, var(--bg2)));
}
.plan-card.blocked {
  border-color: color-mix(in srgb, var(--xy-amber, var(--orange)) 42%, transparent);
  background: color-mix(in srgb, var(--xy-amber, var(--orange)) 5%, var(--xy-surface, var(--bg2)));
}
.plan-card.cancelled {
  opacity: 0.78;
}

.p-row {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.plan-card.open .p-row {
  align-items: flex-start;
}

.p-status-ico {
  width: 17px;
  height: 17px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  flex: none;
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
}
.plan-card.open .p-status-ico {
  margin-top: 1px;
}
.p-status-ico.done {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 14%, transparent));
  color: var(--xy-green, var(--green));
}
.p-status-ico.in_progress {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.p-status-ico.failed {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 14%, transparent));
  color: var(--xy-rose, var(--red));
}
.p-status-ico.blocked {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 14%, transparent));
  color: var(--xy-amber, var(--orange));
}

.p-id {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--xy-text-3, var(--text-faint));
  padding: 0 5px;
  border-radius: 4px;
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border, var(--border));
  flex: none;
}
.plan-card.open .p-id {
  margin-top: 1px;
}

.p-content {
  flex: 1;
  min-width: 0;
}
.p-text-line {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.5;
}
.plan-card.open .p-text-line {
  display: block;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.p-tag {
  display: inline-block;
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 500;
  padding: 0 5px;
  border-radius: 4px;
  line-height: 1.45;
}
.plan-card.open .p-tag {
  vertical-align: 1px;
  margin-right: 5px;
}
.p-tag.status-failed {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 13%, transparent));
  color: var(--xy-rose, var(--red));
}
.p-tag.status-blocked {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.p-tag.status-cancelled,
.p-tag.tag-muted {
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-3, var(--text-faint));
}
.p-tag.tag-warn {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.p-tag.tag-accent {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}

.p-text {
  flex: 1;
  min-width: 0;
  color: var(--xy-text, var(--text));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plan-card.open .p-text {
  white-space: pre-line;
  overflow: visible;
  text-overflow: clip;
}
.plan-card.in_progress .p-text {
  font-weight: 600;
}
.plan-card.done .p-text {
  color: var(--xy-text-2, var(--text-dim));
}
.plan-card.cancelled .p-text {
  color: var(--xy-text-3, var(--text-faint));
}

.p-row-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
}
.plan-card.open .p-row-actions {
  margin-top: 1px;
}
.p-subagent-btn {
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid color-mix(in srgb, var(--xy-accent, var(--accent)) 32%, transparent);
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-family: inherit;
  font-size: 10px;
  font-weight: 500;
  line-height: 1.45;
  cursor: pointer;
  transition: background 0.15s;
}
.p-subagent-btn:hover {
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 18%, transparent);
}
.chevron-ico {
  color: var(--xy-text-3, var(--text-faint));
  display: inline-flex;
  transition: transform 0.16s ease;
}
.chevron-ico.rotated {
  transform: rotate(90deg);
}

.p-callout {
  margin-top: 5px;
  padding: 4px 7px;
  border-radius: 6px;
  display: flex;
  align-items: flex-start;
  gap: 5px;
  font-size: 11px;
  line-height: 1.4;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.p-callout svg {
  flex: none;
  margin-top: 2px;
}
.p-callout.warn {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.p-callout.bad {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 13%, transparent));
  color: var(--xy-rose, var(--red));
}

.p-detail {
  margin-top: 7px;
  padding-top: 7px;
  border-top: 1px dashed var(--xy-border-strong, var(--border));
  display: flex;
  flex-direction: column;
  gap: 7px;
  cursor: default;
}
.detail-title {
  color: var(--xy-text-2, var(--text-dim));
  font-size: 10.5px;
  font-weight: 600;
  margin-bottom: 3px;
}
.dep-list,
.file-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.dep-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  font-size: 10.5px;
  color: var(--xy-text-2, var(--text-dim));
  max-width: 100%;
}
.dep-pill code {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
}
.dep-subject {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dep-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex: none;
  background: var(--xy-text-3, var(--text-faint));
}
.dep-pill.done .dep-dot {
  background: var(--xy-green, var(--green));
}
.dep-pill.in_progress .dep-dot {
  background: var(--xy-accent, var(--accent));
}
.dep-pill.failed .dep-dot {
  background: var(--xy-rose, var(--red));
}
.dep-pill.blocked .dep-dot {
  background: var(--xy-amber, var(--orange));
}
.file-chip {
  font-family: var(--mono);
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--code-bg, var(--bg0));
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-2, var(--text-dim));
  word-break: break-all;
}
.detail-pre {
  margin: 0;
  padding: 6px 8px;
  border-radius: 7px;
  background: var(--code-bg, var(--bg0));
  border: 1px solid var(--xy-border, var(--border));
  font-family: var(--mono);
  font-size: 10.5px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-height: 140px;
  overflow-y: auto;
  overflow-x: hidden;
  color: var(--xy-text-2, var(--text-dim));
}
.detail-pre.good {
  border-color: color-mix(in srgb, var(--xy-green, var(--green)) 28%, transparent);
}
.detail-pre.bad {
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 35%, transparent);
  color: var(--xy-rose, var(--red));
}
.detail-meta-strip {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--xy-text-3, var(--text-faint));
}

.pop-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 12px;
  line-height: 1.6;
  padding: 20px 12px;
}
.pop-empty p {
  margin: 0;
}
.empty-ico {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-3, var(--text-faint));
}

.pop-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 13px;
  border-top: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  flex-shrink: 0;
  flex-wrap: wrap;
}
.foot-hint {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}
.foot-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.pop-btn {
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.pop-btn-secondary {
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-card, var(--bg3));
  color: var(--xy-text-2, var(--text-dim));
}
.pop-btn-secondary:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.pop-btn-accent {
  border: 1px solid color-mix(in srgb, var(--xy-accent, var(--accent)) 30%, transparent);
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.pop-btn-accent:hover {
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 16%, transparent);
}

.plan-item-tip {
  position: fixed;
  z-index: 1500;
  padding: 8px 11px;
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 10px;
  background: var(--xy-card, var(--float-bg));
  color: var(--xy-text, var(--text));
  box-shadow: var(--xy-shadow-pop, 0 12px 28px rgba(0, 0, 0, 0.28));
  backdrop-filter: blur(12px);
  overflow-y: auto;
  overflow-x: hidden;
  text-align: left;
}
.tip-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.tip-id {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--xy-text-3, var(--text-faint));
  padding: 0 5px;
  border-radius: 4px;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
}
.tip-status {
  font-size: 10px;
  font-weight: 500;
  padding: 0 5px;
  border-radius: 4px;
  line-height: 1.45;
}
.tip-status.status-done {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 13%, transparent));
  color: var(--xy-green, var(--green));
}
.tip-status.status-in_progress {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.tip-status.status-todo,
.tip-status.status-cancelled {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
}
.tip-status.status-failed {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 13%, transparent));
  color: var(--xy-rose, var(--red));
}
.tip-status.status-blocked {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.tip-body {
  font-size: 12px;
  line-height: 1.5;
  color: var(--xy-text, var(--text));
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.tp-icon-spin {
  animation: tp-spin 0.9s linear infinite;
}
@keyframes tp-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
