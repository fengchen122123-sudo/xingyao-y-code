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
        <div class="suba-meta">
          <span class="suba-dot" :class="statusClass(detail.status)"></span>
          <span class="suba-stage" :class="statusClass(detail.status)">{{ taskStatusLabel(detail) }}</span>
          <span v-if="detail.type" class="suba-badge">{{ detail.type }}</span>
          <span v-if="detail.background" class="suba-badge">后台</span>
        </div>
        <div class="suba-title" :title="detail.description">{{ detail.description }}</div>
        <div class="suba-stats">
          <span>已用 <b class="suba-num">{{ formatElapsed(detail.elapsed) }}</b></span>
          <span v-if="timeRangeText">{{ timeRangeText }}</span>
          <span v-if="view.rounds > 0">轮次 <b class="suba-num">{{ view.rounds }}</b></span>
          <span v-if="view.toolOk > 0 || view.toolErr > 0">
            工具 <b class="suba-num">{{ view.toolOk }}</b> 成功<template v-if="view.toolErr > 0">
              · <b class="suba-num bad">{{ view.toolErr }}</b> 失败</template>
          </span>
        </div>
      </div>

      <!-- 执行过程：保持时间顺序的扁平行，工具开始/结束已配对合并 -->
      <div class="suba-flow" data-test="subagent-flow">
        <div v-if="view.rows.length === 0" class="suba-empty">暂无执行记录</div>
        <div
          v-for="(row, i) in view.rows"
          :key="i"
          class="suba-row"
          :class="rowClass(row, i)"
        >
          <span class="suba-row-time">{{ stageClock(row.at) }}</span>
          <template v-if="row.kind === 'tool'">
            <span class="suba-row-label">{{ row.name }}</span>
            <span v-if="row.status === 'ok'" class="suba-row-res ok">✓ {{ row.durationText }}</span>
            <span v-else-if="row.status === 'err'" class="suba-row-res err">✗ {{ row.errorText || '执行失败' }}</span>
            <span v-else class="suba-row-res run">运行 {{ runningSeconds(row.at) }}s</span>
          </template>
          <template v-else>
            <span class="suba-row-label dim">{{ stageNameLabel(row.name) }}</span>
          </template>
        </div>
      </div>

      <!-- 终态补充：失败原因 / 执行结论 / 超时现场产出 -->
      <div v-if="detail.error" class="suba-sec">
        <div class="suba-sec-title">失败原因</div>
        <pre class="suba-sec-body err">{{ detail.error }}</pre>
      </div>
      <div v-else-if="detail.result" class="suba-sec" data-test="subagent-result">
        <div class="suba-sec-title">
          执行结论
          <button class="suba-copy" data-test="subagent-copy" @click="copyResult">
            {{ copyHint || '复制' }}
          </button>
        </div>
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
import { buildSubagentFlatView, type SubagentFlatRow } from '../utils/toolTrace';
import type { SubagentTaskDetail, SubagentTaskListItem } from '../types/pywebview.d';

// 激活态由 RightPanel 传入（页签可见、面板展开且停在本工作台），控制轮询开关
const props = defineProps<{ active: boolean }>();

const store = useAppStore();

const detail = ref<SubagentTaskDetail | null>(null);
const sessionTasks = ref<SubagentTaskListItem[]>([]);
const missingText = ref('正在读取子代理执行过程…');
const copyHint = ref('');

const view = computed(() => buildSubagentFlatView(detail.value?.stages ?? []));

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
function rowClass(row: SubagentFlatRow, index: number) {
  const classes: string[] = [row.kind];
  if (row.kind === 'stage') {
    // 模板阶段（调用模型/思考中/生成回复）弱化显示；等待授权单独提亮
    if (row.name === 'waiting_approval') classes.push('wait');
    else classes.push('dim');
  }
  if (isCurrentRow(index)) classes.push('current');
  return classes;
}
function isCurrentRow(index: number) {
  return (
    detail.value != null &&
    !TERMINAL.has(detail.value.status) &&
    index === view.value.rows.length - 1
  );
}
function stageClock(at: number) {
  if (!at) return '--:--:--';
  const d = new Date(at * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function formatElapsed(seconds: number) {
  const s = Math.max(0, Math.floor(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}
// 运行中工具行的实时耗时：轮询每秒刷新 detail 间接触发重算
function runningSeconds(at: number) {
  return Math.max(0, Math.floor(Date.now() / 1000 - at));
}
const timeRangeText = computed(() => {
  const d = detail.value;
  if (!d?.started_at) return '';
  const start = stageClock(d.started_at);
  if (d.finished_at) return `${start} → ${stageClock(d.finished_at)}`;
  return `${start} 开始`;
});

async function copyResult() {
  const text = detail.value?.result;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    copyHint.value = '已复制';
  } catch {
    copyHint.value = '复制失败';
  }
  setTimeout(() => { copyHint.value = ''; }, 1500);
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
    // 查看对象变化或被清空（关闭页签）时丢弃上次详情，避免重开瞬间显示旧详情
    if (detail.value && (!id || detail.value.id !== id)) {
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
  gap: 7px;
  padding-bottom: 9px;
  border-bottom: 1px solid var(--border);
}
.suba-meta {
  display: flex;
  align-items: center;
  gap: 7px;
}
.suba-head .suba-dot {
  width: 7px;
  height: 7px;
}
.suba-head .suba-dot.run {
  animation: suba-pulse 1.6s ease-in-out infinite;
}
@keyframes suba-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.suba-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.5;
  word-break: break-word;
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
.suba-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 11px;
  color: var(--text-faint);
}
.suba-stats .suba-num {
  font-weight: 600;
  color: var(--text-dim);
  font-family: var(--mono);
  font-style: normal;
}
.suba-stats .suba-num.bad {
  color: var(--red);
}

/* 执行过程：扁平行，模板行弱化、当前行左侧色条 */
.suba-flow {
  display: flex;
  flex-direction: column;
}
.suba-row {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0 3px 10px;
  font-size: 11.5px;
  line-height: 1.5;
}
.suba-row::before {
  content: '';
  position: absolute;
  left: 0;
  top: 2px;
  bottom: 2px;
  width: 2px;
  border-radius: 2px;
  background: transparent;
}
.suba-row.current::before {
  background: var(--accent);
}
.suba-row-time {
  flex-shrink: 0;
  color: var(--text-faint);
  font-size: 10px;
  font-family: var(--mono);
}
.suba-row-label {
  flex-shrink: 0;
  color: var(--text-dim);
  font-weight: 600;
  font-family: var(--mono);
  font-size: 11px;
}
.suba-row-label.dim {
  color: var(--text-faint);
  font-weight: 400;
  font-family: inherit;
  font-size: 10.5px;
}
.suba-row-res {
  min-width: 0;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-faint);
  word-break: break-word;
}
.suba-row-res.ok {
  color: var(--green);
}
.suba-row-res.err {
  color: var(--red);
  white-space: normal;
}
.suba-row-res.run {
  color: var(--orange);
}
.suba-row.wait {
  border-radius: 6px;
  background: rgba(201, 162, 74, 0.12);
  padding-left: 10px;
}
.suba-row.wait .suba-row-label {
  color: var(--yellow, #c9a24a);
  font-weight: 600;
}
.suba-row.current .suba-row-label {
  color: var(--accent);
}

/* 终态补充信息 */
.suba-sec {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.suba-sec-title {
  display: flex;
  align-items: center;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim);
}
.suba-copy {
  margin-left: auto;
  padding: 1px 9px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
  font-size: 10.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-copy:hover {
  color: var(--text);
  border-color: var(--text-faint);
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
