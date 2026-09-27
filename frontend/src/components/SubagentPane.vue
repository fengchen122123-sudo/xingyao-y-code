<template>
  <div class="suba-wrap" data-test="subagent-detail">
    <!-- 任务切换条：会话内有多个子代理时快速切换查看对象 -->
    <div v-if="sessionTasks.length > 1" class="suba-switch" data-test="subagent-switcher">
      <button
        v-for="t in sessionTasks"
        :key="t.id"
        class="suba-chip"
        :class="{ active: t.id === store.subagentDetailId }"
        :title="t.description"
        @click="store.openSubagentDetail(t.id)"
      >
        <span class="suba-dot" :class="statusClass(t.status)"></span>
        <span class="suba-chip-text">{{ t.description }}</span>
      </button>
    </div>

    <template v-if="detail">
      <div class="suba-head">
        <div class="suba-title" :title="detail.description">{{ detail.description }}</div>
        <div class="suba-meta">
          <span class="suba-stage" :class="statusClass(detail.status)">{{ taskStatusLabel(detail) }}</span>
          <span v-if="detail.type" class="suba-badge">{{ detail.type }}</span>
          <span v-if="detail.background" class="suba-badge">后台</span>
          <span class="suba-elapsed">{{ detail.elapsed }}s</span>
        </div>
      </div>

      <!-- 执行流程时间线：阶段名 + 时刻 + 详情（工具名/结果摘要） -->
      <div class="suba-flow" data-test="subagent-flow">
        <div v-if="stages.length === 0" class="suba-empty">暂无阶段记录</div>
        <div
          v-for="(st, i) in stages"
          :key="i"
          class="suba-step"
          :class="{ current: isCurrentStep(i) }"
        >
          <span class="suba-step-time">{{ stageClock(st.at) }}</span>
          <span class="suba-step-label">{{ stageNameLabel(st.name) }}</span>
          <span
            v-if="st.detail"
            class="suba-step-detail"
            :class="{ err: st.detail.includes('✗'), ok: st.detail.includes('✓') }"
          >{{ st.detail }}</span>
        </div>
      </div>

      <!-- 终态补充：失败原因 / 执行结论 / 超时现场产出 -->
      <div v-if="detail.error" class="suba-sec">
        <div class="suba-sec-title">失败原因</div>
        <pre class="suba-sec-body err">{{ detail.error }}</pre>
      </div>
      <div v-else-if="detail.result" class="suba-sec" data-test="subagent-result">
        <div class="suba-sec-title">执行结论</div>
        <pre class="suba-sec-body">{{ detail.result }}</pre>
      </div>
      <div v-else-if="detail.partial" class="suba-sec">
        <div class="suba-sec-title">已产出的过程内容</div>
        <pre class="suba-sec-body">{{ detail.partial }}</pre>
      </div>
    </template>
    <div v-else class="suba-empty" data-test="subagent-missing">{{ missingText }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { SubagentStage } from '../utils/toolTrace';
import type { SubagentTaskDetail, SubagentTaskListItem } from '../types/pywebview.d';

// 激活态由 RightPanel 传入（页签可见、面板展开且停在本工作台），控制轮询开关
const props = defineProps<{ active: boolean }>();

const store = useAppStore();

const detail = ref<SubagentTaskDetail | null>(null);
const sessionTasks = ref<SubagentTaskListItem[]>([]);
const missingText = ref('正在读取子代理执行过程…');

const stages = computed<SubagentStage[]>(() => detail.value?.stages ?? []);

const STAGE_LABELS: Record<string, string> = {
  queued: '排队中',
  model: '调用模型',
  thinking: '思考中',
  tool: '调用工具',
  running: '执行中',
  waiting_approval: '等待授权',
  composing: '生成回复',
  summarizing: '总结中',
  finalizing: '收尾中',
  completed: '已完成',
  failed: '失败',
  timed_out: '超时',
  cancelled: '已取消',
};
const TERMINAL = new Set(['completed', 'failed', 'timed_out', 'cancelled']);

function taskStatusLabel(t: { status: string; stage: string }) {
  const key = TERMINAL.has(t.status) ? t.status : t.stage;
  return STAGE_LABELS[key] || key || '运行中';
}
function stageNameLabel(name: string) {
  return STAGE_LABELS[name] || name || '—';
}
function statusClass(status: string) {
  if (status === 'completed') return 'ok';
  if (TERMINAL.has(status)) return 'err';
  return 'run';
}
function isCurrentStep(index: number) {
  return detail.value != null && !TERMINAL.has(detail.value.status) && index === stages.value.length - 1;
}
function stageClock(at: number) {
  if (!at) return '--:--:--';
  const d = new Date(at * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// ── 详情轮询：1 秒拉取目标任务 + 会话任务清单（后台任务/终态回看都走这里） ──
let timer: ReturnType<typeof setInterval> | undefined;
let inFlight = false;

async function fetchDetail() {
  const id = store.subagentDetailId;
  if (!id || !(window.pywebview && window.pywebview.api) || inFlight) return;
  inFlight = true;
  try {
    const res = await window.pywebview.api.get_subagent_task_detail(id, store.currentSessionName);
    if (store.subagentDetailId !== id) return;
    if (res?.ok && res.task) {
      detail.value = res.task;
      missingText.value = '';
    } else {
      // 任务可能已被容量清理：无缓存时给出提示；有缓存继续展示最后已知状态
      missingText.value = String(res?.msg || '未找到该子代理任务');
    }
    if (Array.isArray(res?.tasks)) sessionTasks.value = res.tasks;
  } catch { /* 桥未就绪/单次失败：保留下次轮询 */ }
  finally { inFlight = false; }
}

function stopPolling() {
  if (timer !== undefined) {
    clearInterval(timer);
    timer = undefined;
  }
}

watch(
  () => [props.active, store.subagentDetailId] as const,
  ([active, id]) => {
    stopPolling();
    if (id && detail.value && detail.value.id !== id) {
      detail.value = null;
      missingText.value = '正在读取子代理执行过程…';
    }
    if (active && id) {
      void fetchDetail();
      timer = setInterval(() => { void fetchDetail(); }, 1000);
    }
  },
  { immediate: true },
);

onUnmounted(stopPolling);
</script>

<style scoped>
.suba-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 任务切换条 */
.suba-switch {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.suba-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 10px;
  border-radius: 99px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-chip:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.suba-chip.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}
.suba-chip-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-dot {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--orange);
}
.suba-dot.ok {
  background: var(--green);
}
.suba-dot.err {
  background: var(--red);
}

/* 任务头部 */
.suba-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.suba-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  word-break: break-word;
}
.suba-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
}
.suba-stage {
  flex-shrink: 0;
  border-radius: 99px;
  padding: 1px 8px;
  font-size: 10.5px;
}
.suba-stage.run {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.suba-stage.ok {
  background: rgba(63, 185, 80, 0.15);
  color: var(--green);
}
.suba-stage.err {
  background: rgba(248, 81, 73, 0.15);
  color: var(--red);
}
.suba-badge {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 99px;
  border: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 10.5px;
}
.suba-elapsed {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--text-faint);
  font-family: var(--mono);
}

/* 执行流程时间线 */
.suba-flow {
  display: flex;
  flex-direction: column;
}
.suba-step {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: 11.5px;
  font-family: var(--mono);
  line-height: 1.5;
}
.suba-step.current .suba-step-label {
  color: var(--accent);
}
.suba-step-time {
  flex-shrink: 0;
  color: var(--text-faint);
  font-size: 10.5px;
}
.suba-step-label {
  flex-shrink: 0;
  color: var(--text-dim);
  font-weight: 600;
  font-family: inherit;
}
.suba-step-detail {
  min-width: 0;
  color: var(--text-dim);
  word-break: break-word;
}
.suba-step-detail.ok {
  color: var(--text-faint);
}
.suba-step-detail.err {
  color: var(--red);
}

/* 终态补充信息 */
.suba-sec {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.suba-sec-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim);
}
.suba-sec-body {
  margin: 0;
  padding: 8px 10px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
}
.suba-sec-body.err {
  color: var(--red);
}

.suba-empty {
  padding: 20px 10px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}
</style>
