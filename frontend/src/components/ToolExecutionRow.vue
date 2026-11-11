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
      <!-- 星轨节点：类别色圆点串在一条轨道线上，运行中自带扩散光环；
           失败/取消时圆点换成状态图标，整行仍是同一颗节点 -->
      <span class="trace-icon" :class="nodeToneClass" aria-hidden="true">
        <AppIcon
          v-if="nodeMark"
          :name="nodeMark"
          :size="11"
          :stroke-width="2.4"
          class="trace-node-mark"
        />
        <span v-else class="trace-node-dot" />
        <span v-if="row.status === 'running'" class="trace-node-pulse" />
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
      <!-- 电脑操控：该动作当帧的截图（按画面哈希反查）+ 目标说明 -->
      <div v-if="isComputerRow" class="computer-detail">
        <img
          v-if="computerShotUrl"
          class="computer-shot"
          :src="computerShotUrl"
          alt="该动作当帧截图"
        />
        <div class="computer-params">
          <span v-if="row.detail">目标：{{ row.detail }}</span>
          <span v-if="computerShotUrl">画面：该动作当帧（按画面哈希匹配）</span>
          <span v-else>画面：该帧已过期，右侧「电脑操控」面板可看本 run 最新一张</span>
        </div>
      </div>

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

// 轨道节点按工具类别取色（颜色在 scoped 样式里映射到语义 token）
const NODE_TONES: Record<ToolTraceRow['kind'], string> = {
  'read-group': 'read',
  'search-group': 'search',
  command: 'command',
  mutation: 'mutation',
  skill: 'skill',
  computer: 'computer',
  subagent: 'subagent',
  tool: 'tool',
  'legacy-diffs': 'legacy',
};
const KIND_LABELS: Record<ToolTraceRow['kind'], string> = {
  'read-group': '读取',
  'search-group': '搜索',
  command: '运行',
  mutation: '编辑',
  skill: '技能',
  computer: '电脑',
  // run_subagent 派生的是子代理任务，与右侧「星舰 · 子代理」面板同名
  subagent: '星舰',
  tool: '工具',
  'legacy-diffs': '变更',
};

// 电脑操控行直接把动作动词当类别（观察 / 点击 / 输入…），其余行用固定类别名
const kindLabel = computed(() =>
  props.row.kind === 'computer'
    ? props.row.action || '电脑'
    : (KIND_LABELS[props.row.kind] ?? '工具'),
);
const nodeToneClass = computed(() => `node-${NODE_TONES[props.row.kind] ?? 'tool'}`);

// 只有失败/取消把圆点换成状态图标，其余状态一律是类别色圆点
const nodeMark = computed(() => {
  if (props.row.status === 'failed') return 'x';
  if (props.row.status === 'cancelled') return 'ban';
  return '';
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

// 电脑操控行：展开后显示该动作**当帧**的截图。做法是用结果 JSON 里的
// screen_sha256 去 store.computerShots 反查——不按"第几个动作"猜，也不把
// store 里最新一张硬套到历史动作上，避免图与动作错配。
const isComputerRow = computed(() => props.row.kind === 'computer');
const computerShotUrl = computed(() => {
  if (!isComputerRow.value) return '';
  for (const call of props.row.calls) {
    const text = call.result?.result ?? '';
    if (!text) continue;
    try {
      const payload = JSON.parse(text);
      const sha = String(payload?.screen_sha256 || '');
      if (sha) {
        const url = store.computerShots[sha];
        if (url) return url;
      }
    } catch {
      // 非 JSON 结果（错误文本等）不参与匹配
    }
  }
  return '';
});

// 有展开内容时整行才可点：否则不挂 role=button、不显示箭头（对齐 ZCode 的 canToggle=false）
const canExpand = computed(
  () => groupFiles.value.length > 0
    || diffs.value.length > 0
    || subagentTasks.value.length > 0
    || hasResult.value
    || isComputerRow.value,
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
  margin: 0;
}
.trace-line {
  display: flex;
  align-items: center;
  gap: 8px;
  width: fit-content;
  max-width: 100%;
  padding: 3px 8px 3px 0;
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
/* 节点列：固定 18px 宽，圆点居中落在轨道线上 */
.trace-icon {
  position: relative;
  flex: none;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
}
/* 轨道线：贯穿摘要行高度，相邻行首尾相接成为一条连续竖线；
   首行从节点中心起、末行到节点中心止，线不会伸出整组两端 */
.trace-icon::before {
  content: '';
  position: absolute;
  left: 50%;
  top: -3px;
  bottom: -3px;
  width: 1px;
  margin-left: -0.5px;
  background: var(--border);
}
.trace-row.trace-first .trace-icon::before {
  top: 50%;
}
.trace-row.trace-last .trace-icon::before {
  bottom: 50%;
}
/* 类别色：读取蓝 / 搜索青 / 运行绿 / 编辑橙 / 技能紫 / 通用灰 / 历史变更黄 */
.trace-icon.node-read { color: var(--accent); }
.trace-icon.node-search { color: var(--cyan); }
.trace-icon.node-command { color: var(--green); }
.trace-icon.node-mutation { color: var(--orange); }
.trace-icon.node-skill { color: var(--purple); }
.trace-icon.node-computer { color: var(--cyan); }
.trace-icon.node-subagent { color: var(--purple); }
.trace-icon.node-tool { color: var(--text-dim); }
.trace-icon.node-legacy { color: var(--yellow); }
.status-failed .trace-icon { color: var(--red); }
.status-cancelled .trace-icon { color: var(--orange); }
/* 节点圆点：当前色实心点，完成/取消后收一点透明度，运行中保持满亮 */
.trace-node-dot {
  position: relative;
  z-index: 1;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.status-completed .trace-node-dot {
  opacity: 0.55;
}
/* 运行中：圆点外圈持续扩散的脉冲光环（替代文字扫光） */
.trace-node-pulse {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 6px;
  height: 6px;
  margin: -3px 0 0 -3px;
  border-radius: 50%;
  background: currentColor;
  animation: trace-node-pulse 2s ease-out infinite;
  pointer-events: none;
}
@keyframes trace-node-pulse {
  0% {
    transform: scale(1);
    opacity: 0.4;
  }
  70%,
  100% {
    transform: scale(2.8);
    opacity: 0;
  }
}
.trace-node-mark {
  position: relative;
  z-index: 1;
} 
/* 运行中不转圈：图标保持静态，状态交给扫光标签与行尾耗时 */
.trace-kind {
  flex: none;
  font-weight: 500;
  white-space: nowrap;
  color: var(--text-dim);
}
.trace-kind.is-live {
  color: var(--text-bright);
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

/* 展开区：左侧细线与轨道线同一条竖线（9px 处），视觉上属于同一个节点 */
.trace-detail {
  margin: 0 0 4px 9px;
  padding: 2px 0 6px 13px;
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
/* 电脑操控展开区：截图在前、参数在后，图限宽避免撑破对话列 */
.computer-detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.computer-shot {
  display: block;
  max-width: 100%;
  max-height: 260px;
  width: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.computer-params {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-faint);
  word-break: break-word;
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
  .trace-node-pulse {
    animation: none;
    opacity: 0.25;
  }
}
</style>
