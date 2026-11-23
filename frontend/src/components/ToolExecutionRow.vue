<template>
  <div
    class="trace-row"
    :class="[`status-${row.status}`, { 'is-open': rowOpen }]"
    :data-test="toolTestPrefix && row.calls.length === 1 ? toolTestPrefix + row.calls[0]!.id : undefined"
  >
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
        name="chevron-down"
        :size="12"
        :stroke-width="2"
      />
    </div>

    <div
      v-if="showInlineError && row.status === 'failed' && !rowOpen && row.errorSummary"
      class="trace-err-inline"
    >{{ row.errorSummary }}</div>

    <div v-if="rowOpen" class="trace-detail">
      <div v-if="isComputerRow" class="computer-detail">
        <div v-if="computerShotUrl" class="computer-shot-stage">
          <img
            class="computer-shot"
            :src="computerShotUrl"
            alt="该动作当帧截图"
          />
          <svg
            v-if="matchedComputerOverlay"
            class="computer-overlay"
            data-test="computer-svg-overlay"
            :viewBox="`0 0 ${matchedComputerOverlay.width} ${matchedComputerOverlay.height}`"
            preserveAspectRatio="none"
          >
            <circle
              v-if="matchedComputerOverlay.kind === 'point'"
              :cx="matchedComputerOverlay.x"
              :cy="matchedComputerOverlay.y"
              r="14"
            />
            <line
              v-else-if="matchedComputerOverlay.kind === 'drag'"
              :x1="matchedComputerOverlay.x1"
              :y1="matchedComputerOverlay.y1"
              :x2="matchedComputerOverlay.x2"
              :y2="matchedComputerOverlay.y2"
            />
          </svg>
        </div>
        <div class="computer-params">
          <span v-if="row.detail">目标：{{ row.detail }}</span>
          <span v-if="computerShotUrl">画面：该动作当帧（按画面哈希匹配）</span>
          <span v-else>画面：该帧已过期，右侧「电脑操控」面板可看本 run 最新一张</span>
          <span v-if="!matchedComputerOverlay" class="computer-trace-fallback" data-test="computer-trace-fallback">
            轨迹：无匹配帧几何，仅展示文本摘要（不猜测坐标）
          </span>
        </div>
      </div>

      <ul v-if="groupFiles.length > 0" class="trace-files">
        <li v-for="(file, fi) in groupFiles" :key="fi">{{ file }}</li>
      </ul>

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

      <div v-if="subagentTasks.length > 0" class="subagent-tasks">
        <div v-for="task in subagentTasks" :key="task.id" class="subagent-task">
          <span class="subagent-stage" :class="subagentStageClass(task)">{{ subagentStageLabel(task) }}</span>
          <span class="subagent-desc" :title="task.description">{{ cleanSubagentTitle(task.description) || task.description }}</span>
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

      <pre v-if="argsPreviewText" class="trace-args">{{ argsPreviewText }}</pre>
      <pre v-if="hasResult" class="trace-result">{{ resultText }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import {
  cleanSubagentTitle,
  diffCounts,
  type LegacyDiff,
  type SubagentTaskSnapshot,
  type ToolTraceRow,
} from '../utils/toolTrace';

const props = defineProps<{
  row: ToolTraceRow;
  elapsedMs?: number;
  showInlineError?: boolean;
  toolTestPrefix?: string;
}>();

const store = useAppStore();

const rowOpen = ref(false);

function onLineClick() {
  if (!canExpand.value) return;
  rowOpen.value = !rowOpen.value;
}

const NODE_TONES: Record<ToolTraceRow['kind'], string> = {
  'read-group': 'read',
  'search-group': 'search',
  'command-group': 'command',
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
  'command-group': '运行',
  command: '运行',
  mutation: '编辑',
  skill: '技能',
  computer: '电脑',
  subagent: '星舰',
  tool: '工具',
  'legacy-diffs': '变更',
};

const kindLabel = computed(() =>
  props.row.kind === 'computer'
    ? props.row.action || '电脑'
    : (KIND_LABELS[props.row.kind] ?? '工具'),
);
const nodeToneClass = computed(() => `node-${NODE_TONES[props.row.kind] ?? 'tool'}`);

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

const metaTitle = computed(() =>
  props.row.status === 'failed' ? props.row.errorSummary || props.row.action : '',
);

const lineTitle = computed(() => {
  const parts = [props.row.action, props.row.detail || props.row.command];
  if (props.row.errorSummary) parts.push(props.row.errorSummary);
  return parts.filter(Boolean).join(' · ');
});

const isGroup = computed(
  () => props.row.kind === 'read-group'
    || props.row.kind === 'search-group'
    || props.row.kind === 'command-group',
);
const groupFiles = computed(() => {
  if (!isGroup.value) return [];
  return props.row.calls.map((call) => {
    if (props.row.kind === 'command-group') {
      const cmd = call.result?.display?.command || call.display?.command
        || call.result?.display?.target || call.display?.target || call.label;
      const dur = call.result?.duration_ms !== undefined ? formatDuration(call.result.duration_ms) : '';
      return dur ? `${cmd} (${dur})` : cmd;
    }
    return call.result?.display?.target || call.display?.target || call.label;
  });
});

function compactTargetDisplay(raw: string, isCommand: boolean): string {
  let text = raw.trim();
  if (!text) return '';
  if (isCommand) {
    const stripped = text
      .replace(/^(?:cd(?:\s+\/d)?|Set-Location)\s+(?:"[^"]+"|'[^']+'|[^\s;&]+)\s*(?:;|&&)\s*/i, '')
      .trim();
    if (stripped) text = stripped;
  }
  if (text.length > 32 && /^(?:[a-zA-Z]:[/\\]|\/)/.test(text) && !text.includes(' ')) {
    const parts = text.replace(/\\/g, '/').split('/').filter(Boolean);
    if (parts.length >= 3) {
      return `…/${parts.slice(-2).join('/')}`;
    }
  }
  return text;
}

function shortPathBase(filePath: string): string {
  const parts = filePath.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts[parts.length - 1] || filePath;
}

const targetText = computed(() => {
  if (props.row.kind === 'command-group') {
    return `${props.row.detail || '命令'} · ${props.row.count} 次`;
  }
  if (isGroup.value) return `${props.row.count} 个文件`;
  if (props.row.kind === 'subagent') {
    if (subagentTasks.value.length > 1) {
      const first = cleanSubagentTitle(subagentTasks.value[0]?.description || '');
      return first ? `${subagentTasks.value.length} 个星舰并行 · ${first}` : `${subagentTasks.value.length} 个星舰并行`;
    }
    if (subagentTasks.value.length === 1) {
      return cleanSubagentTitle(subagentTasks.value[0]?.description || '') || compactTargetDisplay(props.row.detail || '', false);
    }
    return cleanSubagentTitle(props.row.detail || '') || compactTargetDisplay(props.row.detail || '', false);
  }
  if (props.row.kind === 'legacy-diffs' && diffs.value.length > 1) {
    const first = shortPathBase(diffs.value[0]?.file_path || '');
    return first ? `${first} 等 ${diffs.value.length} 个文件` : `${diffs.value.length} 个文件`;
  }
  if (props.row.command) return compactTargetDisplay(props.row.command, true);
  return compactTargetDisplay(props.row.detail || '', false);
});

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
    }
  }
  return '';
});

const argsPreviewText = computed(() => {
  if (isGroup.value) return '';
  return props.row.calls
    .map((call) => call.args_preview ?? '')
    .filter((text) => text.trim().length > 0)
    .join('\n');
});

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
const computerCallPayloads = computed(() => {
  if (!isComputerRow.value) return [] as Array<Record<string, any>>;
  const out: Array<Record<string, any>> = [];
  for (const call of props.row.calls) {
    const text = call.result?.result ?? '';
    if (!text) continue;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') out.push(parsed as Record<string, any>);
    } catch {
    }
  }
  return out;
});
const computerShotUrl = computed(() => {
  for (const payload of computerCallPayloads.value) {
    const sha = String(payload?.screen_sha256 || '');
    if (sha) {
      const url = store.computerShots[sha];
      if (url) return url;
    }
  }
  return '';
});
const matchedComputerOverlay = computed(() => {
  for (const payload of computerCallPayloads.value) {
    const sha = String(payload?.screen_sha256 || '');
    const frameId = String(payload?.frame_id || '');
    const geom = payload?.action_geometry;
    if (!sha || !frameId || !geom || typeof geom !== 'object') continue;
    const frameShot = store.computerFrameShots?.[sha];
    if (!frameShot || !frameShot.frame_id || frameShot.frame_id !== frameId) continue;
    const width = Number(frameShot.image_width || payload?.image_width || 0);
    const height = Number(frameShot.image_height || payload?.image_height || 0);
    if (!(width > 0 && height > 0)) continue;
    if (geom.kind === 'point' && Number.isFinite(Number(geom.x)) && Number.isFinite(Number(geom.y))) {
      return { kind: 'point' as const, width, height, x: Number(geom.x), y: Number(geom.y) };
    }
    if (
      geom.kind === 'drag'
      && Number.isFinite(Number(geom.x1))
      && Number.isFinite(Number(geom.y1))
      && Number.isFinite(Number(geom.x2))
      && Number.isFinite(Number(geom.y2))
    ) {
      return {
        kind: 'drag' as const,
        width,
        height,
        x1: Number(geom.x1),
        y1: Number(geom.y1),
        x2: Number(geom.x2),
        y2: Number(geom.y2),
      };
    }
  }
  return null;
});

// 有展开内容时整行才可点：否则不挂 role=button、不显示箭头（对齐 ZCode 的 canToggle=false）
const canExpand = computed(
  () => groupFiles.value.length > 0
    || diffs.value.length > 0
    || subagentTasks.value.length > 0
    || Boolean(argsPreviewText.value)
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
.trace-row {
  margin: 0;
}
.trace-line {
  display: flex;
  align-items: center;
  gap: 8px;
  width: fit-content;
  max-width: 100%;
  padding: 2px 6px 2px 2px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.375;
  color: var(--xy-text-2, var(--text-faint));
  transition: opacity 0.12s ease, background 0.12s ease;
}
.trace-line.can-toggle {
  cursor: pointer;
}
.trace-line.can-toggle:hover {
  opacity: 0.85;
}
.trace-line.can-toggle:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 1px;
}
.trace-line.can-toggle:hover .trace-caret,
.trace-line.can-toggle:focus-visible .trace-caret,
.trace-caret.is-open {
  opacity: 1;
}
.trace-icon {
  position: relative;
  flex: none;
  width: 10px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--xy-green, var(--green));
}
.trace-icon.node-subagent,
.trace-icon.node-skill {
  color: #8b5cf6;
}
.trace-icon.node-read,
.trace-icon.node-search,
.trace-icon.node-command,
.trace-icon.node-mutation,
.trace-icon.node-computer,
.trace-icon.node-tool,
.trace-icon.node-legacy {
  color: var(--xy-green, var(--green));
}
.status-running .trace-icon {
  color: var(--xy-accent, var(--accent));
}
.status-failed .trace-icon {
  color: var(--xy-rose, var(--red));
}
.status-cancelled .trace-icon {
  color: var(--xy-amber, var(--orange));
}
.trace-node-dot {
  position: relative;
  z-index: 1;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.status-running .trace-node-dot {
  width: 8px;
  height: 8px;
  animation: xy-dot-pulse 1.8s cubic-bezier(0.4, 0, 0.6, 1) infinite;
}
.trace-node-pulse {
  display: none;
}
@keyframes xy-dot-pulse {
  0%, 100% { transform: scale(1); opacity: 0.95; }
  50% { transform: scale(1.35); opacity: 0.35; }
}
.trace-node-mark {
  position: relative;
  z-index: 1;
}
.trace-kind {
  flex: none;
  font-weight: 500;
  white-space: nowrap;
  color: var(--xy-text-2, var(--text-dim));
}
.trace-kind.is-live {
  color: var(--xy-accent, var(--accent));
}
.trace-target {
  min-width: 0;
  font-family: inherit;
  font-size: 13px;
  color: var(--xy-text, var(--text));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.trace-takeover-badge {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--xy-amber, var(--orange));
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 0 7px;
  line-height: 16px;
}
.trace-diff-badge {
  flex: none;
  display: inline-flex;
  gap: 5px;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}
.trace-diff-badge .add {
  color: var(--xy-green, var(--green));
}
.trace-diff-badge .del {
  color: var(--xy-green, var(--green));
}
.trace-meta {
  flex: none;
  font-size: 12px;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--xy-text-3, var(--text-faint));
}
.trace-meta.is-error {
  color: var(--xy-rose, var(--red));
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  cursor: help;
}
.trace-meta.is-warn {
  color: var(--xy-amber, var(--orange));
}
.trace-meta.is-live {
  color: var(--xy-accent, var(--accent));
}
.trace-caret {
  flex: none;
  opacity: 0.75;
  color: var(--xy-text-3, var(--text-faint));
  transition: opacity 0.12s ease, transform 0.15s ease;
}
.trace-caret.is-open {
  transform: rotate(180deg);
}

.trace-detail {
  margin: 2px 0 4px 18px;
  padding: 0;
  border-left: none;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
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
.computer-shot-stage {
  position: relative;
  display: inline-block;
  max-width: 100%;
  width: fit-content;
}
.computer-shot {
  display: block;
  max-width: 100%;
  max-height: 260px;
  width: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.computer-overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.computer-overlay circle {
  fill: rgba(255, 86, 48, 0.18);
  stroke: #ff5630;
  stroke-width: 3;
}
.computer-overlay line {
  stroke: #ff5630;
  stroke-width: 3;
  stroke-linecap: round;
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
.trace-err-inline {
  margin: 1px 0 3px 18px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--xy-rose, var(--red));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.trace-args,
.trace-result {
  display: block;
  margin: 4px 0 0;
  padding: 6px 10px;
  background: var(--code-bg);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
}
.trace-args {
  color: var(--text-faint);
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
