<template>
  <div class="trace-row" :class="[`status-${row.status}`, { 'is-open': rowOpen }]">
    <!-- 摘要行（ZCode 形态）：类别图标 + 类别 + 目标 + 统计/耗时 + 悬停才出现的箭头。
         整行可点开合并后的详情区（文件清单 / Diff / 结果 / 子代理任务），
         不再有"展开详情""展开 Diff"两级文字开关和卡片边框 -->
    <div
      class="trace-line"
      :class="{ 'can-toggle': canExpand }"
      :role="canExpand ? 'button' : undefined"
      :tabindex="canExpand ? 0 : undefined"
      :aria-expanded="canExpand ? rowOpen : undefined"
      :title="lineTitle"
      :data-row-toggle="canExpand ? '' : undefined"
      @click="onLineClick"
      @keydown.enter.prevent="onLineClick"
      @keydown.space.prevent="onLineClick"
    >
      <span class="trace-icon" aria-hidden="true">
        <AppIcon :name="rowIcon" :size="13" :stroke-width="2" />
      </span>
      <span class="trace-kind" :class="{ 'is-live': row.status === 'running' }">{{ kindLabel }}</span>
      <span v-if="targetText" class="trace-target" :title="targetText">{{ targetText }}</span>
      <span v-if="takeoverBadge" class="trace-takeover-badge">{{ takeoverBadge }}</span>
      <span v-if="diffStats" class="trace-diff-badge">
        <span class="add">+{{ diffStats.added }}</span>
        <span class="del">-{{ diffStats.removed }}</span>
      </span>
      <span v-if="metaText" class="trace-meta" :class="metaClass" :title="metaTitle">{{ metaText }}</span>
      <AppIcon
        v-if="canExpand"
        class="trace-caret"
        :class="{ 'is-open': rowOpen }"
        name="chevron-right"
        :size="11"
        :stroke-width="2"
      />
    </div>

    <div v-if="rowOpen" class="trace-detail">
      <!-- 分组读取 / 搜索：列出每个文件目标 -->
      <ul v-if="groupFiles.length > 0" class="trace-files">
        <li v-for="(file, fi) in groupFiles" :key="fi">{{ file }}</li>
      </ul>

      <!-- 文件变更 Diff：展开本行即直接可见，不再做二级开关 -->
      <div v-if="diffs.length > 0" class="diff-body">
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
            @click.stop="store.openSubagentDetail(task.id)"
          >查看</button>
        </div>
      </div>

      <pre v-if="hasResult" class="trace-result">{{ resultText }}</pre>
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

// 本地展开状态：整行一个开关，只影响本行 UI，不写入任何 store
const rowOpen = ref(false);

function onLineClick() {
  if (!canExpand.value) return;
  rowOpen.value = !rowOpen.value;
}

// ZCode 形态：图标表达工具类别，只有失败/取消才切成状态图标
const KIND_ICONS: Record<ToolTraceRow['kind'], string> = {
  'read-group': 'file',
  'search-group': 'search',
  command: 'square-terminal',
  mutation: 'pencil',
  skill: 'gem',
  tool: 'box',
  'legacy-diffs': 'layers',
};
const KIND_LABELS: Record<ToolTraceRow['kind'], string> = {
  'read-group': '读取',
  'search-group': '搜索',
  command: '运行',
  mutation: '编辑',
  skill: '技能',
  tool: '工具',
  'legacy-diffs': '变更',
};

const kindLabel = computed(() => KIND_LABELS[props.row.kind] ?? '工具');
const rowIcon = computed(() => {
  if (props.row.status === 'failed') return 'x';
  if (props.row.status === 'cancelled') return 'ban';
  return KIND_ICONS[props.row.kind] ?? 'circle';
});

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// durationMs 缺失时不渲染耗时；运行中的行可回退到实时 elapsedMs
const durationText = computed(() => {
  if (props.row.durationMs !== undefined) return formatDuration(props.row.durationMs);
  if (props.row.status === 'running' && props.elapsedMs !== undefined) {
    return formatDuration(props.elapsedMs);
  }
  return '';
});

// 行尾：失败/取消优先给状态词（悬停看完整报错），其余给耗时
const metaText = computed(() => {
  if (props.row.status === 'failed') return '失败';
  if (props.row.status === 'cancelled') return '已取消';
  if (props.row.status === 'running') return durationText.value || '运行中';
  return durationText.value;
});

const metaClass = computed(() => {
  if (props.row.status === 'failed') return 'is-error';
  if (props.row.status === 'cancelled') return 'is-warn';
  if (props.row.status === 'running') return 'is-live';
  return '';
});

// 失败行的状态词悬停展示完整报错（对齐 ZCode 的虚线下划线 + tooltip）
const metaTitle = computed(() =>
  props.row.status === 'failed' ? props.row.errorSummary || props.row.action : '',
);

// 悬停整行的完整说明
const lineTitle = computed(() => {
  const parts = [props.row.action, props.row.detail || props.row.command];
  if (props.row.errorSummary) parts.push(props.row.errorSummary);
  return parts.filter(Boolean).join(' · ');
});

// 分组行（read-group / search-group）列出每个调用的目标文件
const isGroup = computed(() => props.row.kind === 'read-group' || props.row.kind === 'search-group');
const groupFiles = computed(() => {
  if (!isGroup.value) return [];
  return props.row.calls.map(
    (call) => call.result?.display?.target || call.display?.target || call.label,
  );
});

// 行内目标：分组行给文件数（清单在展开区），命令行给命令原文，其余给路径
const targetText = computed(() => {
  if (isGroup.value) return `${props.row.count} 个文件`;
  return props.row.command || props.row.detail || '';
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

// 结果文本：只拼接有实际内容的调用结果，全部为空则不展示结果区
const resultText = computed(() =>
  props.row.calls
    .map((call) => call.result?.result ?? '')
    .filter((text) => text.trim().length > 0)
    .join('\n\n'),
);
const hasResult = computed(() => resultText.value.trim().length > 0);

// 有展开内容时整行才可点：否则不挂 role=button、不显示箭头（对齐 ZCode 的 canToggle=false）
const canExpand = computed(
  () => groupFiles.value.length > 0
    || diffs.value.length > 0
    || subagentTasks.value.length > 0
    || hasResult.value,
);

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
/* 每条工具 = 一行摘要（无边框无底色，宽度贴合内容）+ 按需展开的缩进详情 */
.trace-row {
  margin: 1px 0;
}
.trace-line {
  display: flex;
  align-items: center;
  gap: 8px;
  width: fit-content;
  max-width: 100%;
  padding: 3px 6px 3px 4px;
  margin-left: -4px;
  border-radius: 6px;
  font-size: 12.5px;
  color: var(--text-faint);
  transition: background 0.12s ease;
}
.trace-line.can-toggle {
  cursor: pointer;
}
.trace-line.can-toggle:hover {
  background: color-mix(in srgb, var(--text) 5%, transparent);
}
.trace-line.can-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
/* 箭头平时不出现，悬停 / 键盘聚焦 / 已展开时才浮现（与思考块一致） */
.trace-line.can-toggle:hover .trace-caret,
.trace-line.can-toggle:focus-visible .trace-caret,
.trace-caret.is-open {
  opacity: 1;
}
.trace-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  color: var(--text-faint);
}
.status-failed .trace-icon {
  color: var(--red);
}
.status-cancelled .trace-icon {
  color: var(--orange);
}
/* 运行中不转圈：图标保持静态，状态交给扫光标签与行尾耗时 */
.trace-kind {
  flex: none;
  font-weight: 500;
  white-space: nowrap;
  color: var(--text-dim);
}
.trace-kind.is-live {
  background: linear-gradient(90deg, var(--text-faint) 0%, var(--text) 45%, var(--text-faint) 90%);
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: trace-shimmer 2.4s linear infinite;
}
@keyframes trace-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
.trace-target {
  min-width: 0;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.trace-takeover-badge {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--orange);
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 0 7px;
  line-height: 16px;
}
.trace-diff-badge {
  flex: none;
  display: inline-flex;
  gap: 4px;
  font-family: var(--mono);
  font-size: 11px;
}
.trace-diff-badge .add {
  color: var(--green);
}
.trace-diff-badge .del {
  color: var(--red);
}
.trace-meta {
  flex: none;
  font-size: 11px;
  color: var(--text-faint);
}
/* 失败：红色状态词 + 虚线下划线，悬停出完整报错 */
.trace-meta.is-error {
  color: var(--red);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  cursor: help;
}
.trace-meta.is-warn {
  color: var(--orange);
}
.trace-meta.is-live {
  color: var(--accent);
}
.trace-caret {
  flex: none;
  opacity: 0;
  color: var(--text-faint);
  transition: opacity 0.12s ease, transform 0.15s ease;
}
.trace-caret.is-open {
  transform: rotate(90deg);
}

/* 展开区：左 1px 细线 + 缩进 + 弱灰，与思考块同一套语言 */
.trace-detail {
  margin: 2px 0 6px 8px;
  padding: 4px 0 4px 12px;
  border-left: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-faint);
}
.trace-files {
  margin: 0;
  padding: 0 0 0 14px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-dim);
}
.diff-body {
  margin-top: 2px;
  padding: 4px 0;
  background: var(--code-bg);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.55;
  max-height: 280px;
  overflow-y: auto;
}
.diff-file {
  padding: 2px 10px;
  color: var(--text-faint);
  font-weight: 600;
}
.diff-line {
  padding: 1px 10px;
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
  margin-top: 2px;
}
.subagent-task {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
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
  margin: 4px 0 0;
  padding: 6px 10px;
  background: var(--code-bg);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
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
@media (prefers-reduced-motion: reduce) {
  .trace-kind.is-live {
    animation: none;
    background: none;
    color: var(--text-dim);
  }
}
</style>
