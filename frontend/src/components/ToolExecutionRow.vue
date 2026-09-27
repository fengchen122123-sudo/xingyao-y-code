<template>
  <div class="trace-row" :class="`status-${row.status}`">
    <span class="trace-icon" aria-hidden="true">
      <AppIcon
        :name="statusIcon"
        :size="13"
        :stroke-width="2"
        :class="{ 'icon-spin': row.status === 'running' }"
      />
    </span>
    <div class="trace-main">
      <div class="trace-title">
        <span>{{ title }}</span>
        <span v-if="takeoverBadge" class="trace-takeover-badge">{{ takeoverBadge }}</span>
        <span v-if="durationText" class="trace-duration">{{ durationText }}</span>
      </div>
      <div v-if="row.command || row.detail || row.errorSummary" class="trace-detail">
        {{ row.errorSummary || row.command || row.detail }}
      </div>
      <div v-if="row.command && row.command !== detailText" class="trace-command">{{ row.command }}</div>

      <!-- 分组读取 / 搜索：列出每个文件目标 -->
      <ul v-if="groupFiles.length > 0" class="trace-files">
        <li v-for="(file, fi) in groupFiles" :key="fi">{{ file }}</li>
      </ul>

      <!-- 文件变更统计与 Diff 展开 -->
      <div v-if="diffStats" class="trace-diffstat">
        <span class="diff-add">+{{ diffStats.added }}</span>
        <span class="diff-sep"> / </span>
        <span class="diff-del">-{{ diffStats.removed }}</span>
        <span> 行</span>
        <button data-diff-toggle :aria-expanded="diffOpen" @click="diffOpen = !diffOpen">
          {{ diffOpen ? '收起 Diff' : '展开 Diff' }}
        </button>
      </div>
      <div v-if="diffOpen && diffs.length > 0" class="diff-body">
        <template v-for="(d, di) in diffs" :key="di">
          <div v-if="diffs.length > 1" class="diff-file">{{ d.file_path }}</div>
          <div
            v-for="(line, li) in splitDiffLines(d.diff_text)"
            :key="li"
            class="diff-line"
            :class="diffLineClass(line)"
          >{{ line }}</div>
        </template>
      </div>

      <!-- run_subagent 子代理任务快照 -->
      <div v-if="subagentTasks.length > 0" class="subagent-tasks">
        <div v-for="task in subagentTasks" :key="task.id" class="subagent-task">
          <span class="subagent-stage" :class="subagentStageClass(task)">{{ subagentStageLabel(task) }}</span>
          <span class="subagent-desc" :title="task.description">{{ task.description }}</span>
          <span v-if="task.stage_detail" class="subagent-detail">{{ task.stage_detail }}</span>
          <span class="subagent-time">{{ task.elapsed }}s</span>
          <button
            class="subagent-view"
            :data-test="'subagent-view-' + task.id"
            title="在右侧面板查看该子代理的执行流程"
            @click="store.openSubagentDetail(task.id)"
          >查看</button>
        </div>
      </div>

      <button v-if="hasResult" data-detail-toggle :aria-expanded="resultOpen" @click="resultOpen = !resultOpen">
        {{ resultOpen ? '收起详情' : '展开详情' }}
      </button>
      <pre v-if="resultOpen" class="trace-result">{{ resultText }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import {
  diffCounts,
  type LegacyDiff,
  type SubagentTaskSnapshot,
  type ToolTraceRow,
} from '../utils/toolTrace';

const props = defineProps<{
  row: ToolTraceRow;
  elapsedMs?: number;
}>();

const store = useAppStore();

// 本地展开状态：只影响本行 UI，不写入任何 store
const resultOpen = ref(false);
const diffOpen = ref(false);

const STATUS_ICONS: Record<ToolTraceRow['status'], string> = {
  running: 'loader',
  completed: 'check',
  failed: 'x',
  cancelled: 'ban',
};
const statusIcon = computed(() => STATUS_ICONS[props.row.status] ?? 'circle');

const title = computed(() => {
  const base = props.row.action || '工具调用';
  if (props.row.status === 'failed') return `${base}失败`;
  if (props.row.status === 'cancelled') return `${base}已取消`;
  return base;
});

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// durationMs 缺失时不渲染时长徽标；运行中的行可回退到实时 elapsedMs
const durationText = computed(() => {
  if (props.row.durationMs !== undefined) return formatDuration(props.row.durationMs);
  if (props.row.status === 'running' && props.elapsedMs !== undefined) {
    return formatDuration(props.elapsedMs);
  }
  return '';
});

const detailText = computed(() => props.row.errorSummary || props.row.command || props.row.detail);

// 分组行（read-group / search-group）列出每个调用的目标文件
const isGroup = computed(() => props.row.kind === 'read-group' || props.row.kind === 'search-group');
const groupFiles = computed(() => {
  if (!isGroup.value) return [];
  return props.row.calls.map(
    (call) => call.result?.display?.target || call.display?.target || call.label,
  );
});

// 汇总本行所有调用携带的 Diff 以及未绑定的历史 Diff
const diffs = computed<LegacyDiff[]>(() => {
  const list: LegacyDiff[] = [];
  for (const call of props.row.calls) {
    if (call.diff) list.push(call.diff);
  }
  if (props.row.legacyDiffs) list.push(...props.row.legacyDiffs);
  return list;
});

const diffStats = computed(() => {
  if (diffs.value.length > 0) {
    let added = 0;
    let removed = 0;
    for (const diff of diffs.value) {
      const changes = diffCounts(diff);
      added += changes.added;
      removed += changes.removed;
    }
    return { added, removed };
  }
  const added = props.row.metrics.added ?? 0;
  const removed = props.row.metrics.removed ?? 0;
  if (added > 0 || removed > 0) return { added, removed };
  return null;
});

// run_subagent 调用携带的子代理任务快照
const subagentTasks = computed<SubagentTaskSnapshot[]>(() =>
  props.row.calls.flatMap((call) => call.subagent_tasks ?? []),
);

const SUB_STAGE_LABELS: Record<string, string> = {
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
const SUB_TERMINAL = new Set(['completed', 'failed', 'timed_out', 'cancelled']);

function subagentStageLabel(task: SubagentTaskSnapshot) {
  const key = SUB_TERMINAL.has(task.status) ? task.status : task.stage;
  return SUB_STAGE_LABELS[key] || key || '运行中';
}

function subagentStageClass(task: SubagentTaskSnapshot) {
  if (task.status === 'completed') return 'ok';
  if (task.status === 'failed' || task.status === 'timed_out' || task.status === 'cancelled') return 'err';
  if (task.stage === 'waiting_approval') return 'warn';
  return 'run';
}

// 子 Agent 超时接管标记：结果 JSON 带 handoff 字段时展示固定徽标，
// 卡片状态保持 timed_out，不与 completed 合并
const takeoverBadge = computed(() => {
  for (const call of props.row.calls) {
    const text = call.result?.result ?? '';
    if (!text) continue;
    try {
      const payload = JSON.parse(text);
      if (payload?.status === 'timed_out' && payload?.handoff === 'main_takeover') {
        return '主 Agent 已接管';
      }
      if (payload?.status === 'timed_out' && payload?.handoff === 'takeover_queued') {
        return '等待主 Agent 接管';
      }
    } catch {
      // 非 JSON 结果（普通失败文本等）不展示徽标，继续检查其他调用
    }
  }
  return '';
});

// 结果文本：只拼接有实际内容的调用结果，全部为空则不显示展开按钮
const resultText = computed(() =>
  props.row.calls
    .map((call) => call.result?.result ?? '')
    .filter((text) => text.trim().length > 0)
    .join('\n\n'),
);
const hasResult = computed(() => resultText.value.trim().length > 0);

function splitDiffLines(text: string): string[] {
  if (!text) return [];
  return text.split('\n');
}

function diffLineClass(line: string) {
  if (
    line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')
    || line.startsWith('diff ') || line.startsWith('index ')
  ) return 'hdr';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return '';
}
</script>

<style scoped>
.trace-row {
  display: flex;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  margin: 7px 0;
  background: var(--bg1);
  font-size: 12.5px;
}
.trace-row.status-failed {
  border-left: 3px solid var(--red);
}
.trace-row.status-cancelled {
  border-left: 3px solid var(--orange);
}
.trace-row.status-running {
  border-left: 3px solid var(--accent);
}
.trace-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  line-height: 1.6;
}
.icon-spin {
  animation: trace-icon-spin 0.9s linear infinite;
}
@keyframes trace-icon-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.status-completed > .trace-icon {
  color: var(--green);
}
.status-failed > .trace-icon {
  color: var(--red);
}
.status-cancelled > .trace-icon {
  color: var(--orange);
}
.status-running > .trace-icon {
  color: var(--accent);
}
.trace-main {
  flex: 1;
  min-width: 0;
}
.trace-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text);
}
.trace-takeover-badge {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--warning, #d97706);
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 0 8px;
  line-height: 18px;
}
.trace-duration {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-faint);
}
.trace-detail {
  margin-top: 2px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.trace-command {
  margin-top: 2px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.trace-files {
  margin: 4px 0 0;
  padding: 0 0 0 18px;
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
}
.trace-diffstat {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 2px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
}
.trace-diffstat .diff-add {
  color: var(--green);
}
.trace-diffstat .diff-del {
  color: var(--red);
}
.trace-diffstat button {
  margin-left: 8px;
}
.trace-row button {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  color: var(--text-faint);
  font-size: 11px;
  font-family: inherit;
}
.trace-row button:hover {
  color: var(--text);
}
.diff-body {
  margin-top: 4px;
  font-family: var(--mono);
  font-size: 11.5px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  max-height: 280px;
  overflow-y: auto;
  line-height: 1.55;
  padding: 4px 0;
}
.diff-file {
  padding: 2px 12px;
  color: var(--text-faint);
  font-weight: 600;
}
.diff-line {
  padding: 1px 12px;
  white-space: pre;
  color: var(--text-dim);
}
.diff-line.add {
  background: rgba(63, 185, 80, 0.12);
  color: var(--green);
}
.diff-line.del {
  background: rgba(248, 81, 73, 0.12);
  color: var(--red);
}
.diff-line.hdr {
  color: var(--text-faint);
}
.subagent-tasks {
  margin-top: 4px;
  border-top: 1px solid var(--border);
  padding-top: 4px;
}
.subagent-task {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 11.5px;
  font-family: var(--mono);
}
.subagent-stage {
  flex-shrink: 0;
  border-radius: 99px;
  padding: 1px 8px;
  font-size: 10.5px;
}
.subagent-stage.run {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.subagent-stage.warn {
  background: rgba(210, 153, 34, 0.18);
  color: var(--orange);
}
.subagent-stage.ok {
  background: rgba(63, 185, 80, 0.15);
  color: var(--green);
}
.subagent-stage.err {
  background: rgba(248, 81, 73, 0.15);
  color: var(--red);
}
.subagent-desc {
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.subagent-detail {
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}
.subagent-time {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--text-faint);
}
.subagent-view {
  flex-shrink: 0;
  color: var(--accent);
  opacity: 0.85;
}
.subagent-view:hover {
  opacity: 1;
  text-decoration: underline;
}
.trace-result {
  display: block;
  margin: 6px 0 0;
  padding: 8px 12px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
}
</style>
