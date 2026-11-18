<template>
  <!-- 触发本轮的用户消息。data-msg-id 经 attrs 透传到 MessageBubble 根元素
       （.msg-user）：红点导航按消息 id 精确定位用；本地新消息无 _msg_id 时不渲染 -->
  <MessageBubble v-if="group.userMsg" :msg="group.userMsg" :data-msg-id="group.userMsg?._msg_id ?? undefined" />

  <div v-if="hasAssistant || group.tail.length" class="xy-turn-wrap">
    <!-- 身份抬头：整个助手回合顶部显示一次，位于执行过程与正文之上 -->
    <div v-if="hasAssistant" class="assistant-name">
      <b class="an-agent">{{ store.agentName }}</b>
      <span class="an-model">{{ turnModelName }}</span>
      <span v-if="turnCancelled" class="cancel-badge">已取消</span>
    </div>

    <!-- 方案A · 轻量原生流：运行态默认展开（可收起成流光胶囊），完成态默认折叠（可点开） -->
    <template v-if="hasProcess">
      <div v-if="group.running" class="xy-run-process">
        <div v-if="runningExpanded" class="xy-process-card">
          <button
            type="button"
            class="xy-process-head"
            @click="runningExpanded = false"
          >
            <span class="xy-process-head-l">
              <AppIcon name="loader" :size="12.5" :stroke-width="2.2" class="xy-spin" />
              <span class="xy-process-title">{{ runningTitle }}</span>
            </span>
            <span class="xy-process-meta">{{ runningElapsedText ? `${runningElapsedText} · ` : '' }}收起 ▴</span>
          </button>
          <div class="xy-process-body">
            <MessageBubble
              v-for="m in group.middle"
              :key="msgKey(m)"
              :msg="m"
              :show-name="false"
              :show-metrics="false"
              :show-streaming-speed="false"
            />
            <MessageBubble
              v-for="m in tailProcessMessages"
              :key="`tp-${msgKey(m)}`"
              :msg="m"
              :show-name="false"
              :show-metrics="false"
              :show-streaming-speed="false"
              :show-answer="false"
            />
          </div>
        </div>
        <button
          v-else
          type="button"
          class="xy-process-collapsed"
          @click="runningExpanded = true"
        >
          <AppIcon name="loader" :size="12.5" :stroke-width="2.2" class="xy-spin" />
          <span class="xy-shimmer-text">{{ runningCollapsedText }}</span>
          <span class="xy-process-expand-hint">展开 ▾</span>
        </button>
      </div>

      <div v-else class="turn-fold" :class="{ 'is-open': expanded }">
        <FoldCard v-model:open="expanded">
          <template #title>
            <span>{{ title }}<template v-if="!expanded && processDurationText"> · {{ processDurationText }}</template></span>
          </template>
          <template v-if="expanded" #head-right>
            <span class="xy-process-meta">{{ processDurationText ? `${processDurationText} · ` : '' }}收起 ▴</span>
          </template>
          <div class="xy-process-body">
            <MessageBubble
              v-for="m in group.middle"
              :key="msgKey(m)"
              :msg="m"
              :show-name="false"
              :show-metrics="false"
              :show-streaming-speed="false"
            />
            <MessageBubble
              v-for="m in tailProcessMessages"
              :key="`tp-${msgKey(m)}`"
              :msg="m"
              :show-name="false"
              :show-metrics="false"
              :show-streaming-speed="false"
              :show-answer="false"
            />
          </div>
        </FoldCard>
      </div>
    </template>

    <!-- 组尾：总结轮 assistant 正文 + 其后的 error 消息，始终可见 -->
    <MessageBubble
      v-for="m in group.tail"
      :key="msgKey(m)"
      :msg="m"
      :show-name="false"
      :show-metrics="false"
      :show-streaming-speed="false"
      :show-process="false"
    />

    <!-- 回合结束且有文件改动时：底部展示可折叠的"文件更改"面板
         （含按回合撤销按钮：按权威 turnId 绑定状态） -->
    <ChangedFilesPanel
      v-if="!group.running && fileChanges.length"
      class="turn-changes"
      :changes="fileChanges"
      :undo-state="undoState"
      :turn-id="group.turnId"
    />

    <!-- 回合结束底部总结行：左工作时长 + 右速率/耗时/tokens -->
    <div v-if="(!group.running && finalWorkTimeText) || tailMetricsText" class="xy-turn-footer">
      <span v-if="!group.running && finalWorkTimeText" class="working-indicator done">
        <AppIcon name="check" :size="13" :stroke-width="2.2" class="wi-check" />
        <span class="wi-text">{{ store.agentName }}本轮工作 {{ finalWorkTimeText }}</span>
      </span>
      <span v-else></span>
      <span
        v-if="tailMetricsText"
        class="msg-metrics-bar"
        :title="tailMetricsTooltip"
        data-test="msg-metrics"
      >{{ tailMetricsText }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import ChangedFilesPanel from './ChangedFilesPanel.vue';
import FoldCard from './FoldCard.vue';
import MessageBubble from './MessageBubble.vue';
import { useAppStore } from '../stores/appStore';
import { formatDuration } from '../utils/tokenSpeed';
import {
  collectFileChanges,
  groupStats,
  msgKey,
  type TurnGroupInfo,
} from '../utils/turnGroups';

const props = withDefaults(defineProps<{
  group: TurnGroupInfo;
  runningElapsedText?: string;
  finalWorkTimeText?: string;
}>(), {
  runningElapsedText: '',
  finalWorkTimeText: '',
});

const store = useAppStore();

// 回合结束时默认收起，只留一行摘要；点击可展开回看完整过程。
const expanded = ref(false);
// 回合运行中默认展开，点击可收起为单行流光胶囊。
const runningExpanded = ref(true);

const STATE_LABELS: Record<TurnGroupInfo['state'], string> = {
  completed: '已完成',
  cancelled: '已取消',
  error: '异常结束',
};

const stats = computed(() => groupStats(props.group));

const allAssistants = computed(() =>
  [...props.group.middle, ...props.group.tail].filter((m) => m.role === 'assistant'),
);
const hasAssistant = computed(() => allAssistants.value.length > 0);
const turnModelName = computed(() => {
  const firstWithModel = allAssistants.value.find((m) => m.model_name);
  return firstWithModel?.model_name || store.activeModelName || '模型';
});
const turnCancelled = computed(() => allAssistants.value.some((m) => m.cancelled));

function msgHasProcess(m: TurnGroupInfo['tail'][number]): boolean {
  if (m.role !== 'assistant') return false;
  if (m.reasoning_content && m.reasoning_content.trim()) return true;
  if (m.tool_calls && m.tool_calls.length > 0) return true;
  if (m.legacy_diffs && m.legacy_diffs.length > 0) return true;
  return false;
}

const tailProcessMessages = computed(() =>
  props.group.tail.filter((m) => msgHasProcess(m)),
);

const hasProcess = computed(() =>
  props.group.middle.length > 0 || tailProcessMessages.value.length > 0,
);

const activeSubagentCount = computed(() => {
  let count = 0;
  for (const m of allAssistants.value) {
    for (const c of m.tool_calls || []) {
      for (const t of c.subagent_tasks || []) {
        if (t.status === 'running' || t.status === 'queued') count += 1;
      }
    }
  }
  return count;
});

const runningTitle = computed(() => {
  if (activeSubagentCount.value > 0) {
    return `${store.agentName}执行过程 · ${activeSubagentCount.value} 个星舰运行中`;
  }
  if (stats.value.toolCount > 0) {
    return `${store.agentName}执行过程 · ${stats.value.toolCount} 个动作`;
  }
  return `${store.agentName}执行过程 · 正在处理`;
});

const runningCollapsedText = computed(() => {
  const base = activeSubagentCount.value > 0
    ? `正在执行（${activeSubagentCount.value} 个星舰）`
    : `${store.agentName}正在执行`;
  return props.runningElapsedText ? `${base} · ${props.runningElapsedText}` : base;
});

// 回合文件更改摘要（无改动则空数组，面板不渲染）
const fileChanges = computed(() => collectFileChanges(props.group));

// 按回合撤销：本回合的权威检查点状态（按 turnId 绑定；无关联回合为 null）
const undoState = computed(() => {
  const turnId = props.group.turnId;
  return turnId ? (store.turnUndoStates[turnId] ?? null) : null;
});

const title = computed(() => {
  const { rounds, toolCount } = stats.value;
  const stateLabel = STATE_LABELS[props.group.state];
  const agentName = store.agentName;
  if (toolCount > 0) {
    return `${agentName}执行过程 · ${toolCount} 个工具调用 · ${stateLabel}`;
  }
  return `${agentName}执行过程 · ${rounds} 轮 · ${stateLabel}`;
});

const lastAssistant = computed(() => {
  for (let i = props.group.tail.length - 1; i >= 0; i -= 1) {
    if (props.group.tail[i].role === 'assistant') return props.group.tail[i];
  }
  return null;
});

const processDurationText = computed(() => {
  if (props.finalWorkTimeText) return props.finalWorkTimeText;
  const d = lastAssistant.value?.metrics?.duration_s;
  return d ? formatDuration(d) : '';
});

const tailMetricsText = computed(() => {
  const m = lastAssistant.value?.metrics;
  if (!m || (!m.token_speed && !m.duration_s)) return '';
  const parts: string[] = [];
  if (m.token_speed) parts.push(`${m.token_speed} t/s`);
  if (m.duration_s) parts.push(formatDuration(m.duration_s));
  if (m.completion_tokens) parts.push(`${m.completion_tokens.toLocaleString()} tokens`);
  return parts.join(' · ');
});

const tailMetricsTooltip = computed(() => {
  const m = lastAssistant.value?.metrics;
  if (!m) return '';
  const parts: string[] = [];
  if (m.ttft_ms != null && m.ttft_ms > 0) parts.push(`首字延迟: ${m.ttft_ms}ms`);
  if (m.output_duration_s != null && m.output_duration_s > 0) parts.push(`生成耗时: ${m.output_duration_s}s`);
  else if (m.duration_s != null && m.duration_s > 0) parts.push(`总耗时: ${m.duration_s}s`);
  if (m.completion_tokens) parts.push(`输出: ${m.completion_tokens} tokens`);
  if (m.token_speed) parts.push(`平均速度: ${m.token_speed} t/s`);
  return parts.join(' · ');
});
</script>

<style scoped>
.xy-turn-wrap {
  max-width: var(--chat-col-width);
  margin: 0 auto 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.xy-turn-wrap > :deep(.msg) {
  width: 100%;
  margin: 0;
}

.assistant-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.assistant-name .an-agent {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
}
.assistant-name .an-model {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.cancel-badge {
  font-size: 10.5px;
  color: var(--xy-amber, var(--orange));
  background: var(--xy-amber-soft, rgba(240, 136, 62, 0.12));
  border-radius: 6px;
  padding: 1px 7px;
}

/* 方案A 过程容器（运行中展开态 & 完成后展开态共用同一通透卡片外观） */
.xy-process-card,
.turn-fold.is-open {
  border-radius: 12px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg1));
  padding: 10px 14px;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.xy-process-head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: none;
  border: none;
  padding: 0;
  font-family: inherit;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  text-align: left;
  cursor: pointer;
}
.xy-process-head-l {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.xy-process-title {
  font-weight: 500;
  color: var(--xy-text-2, var(--text-dim));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.xy-process-meta {
  flex-shrink: 0;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.xy-process-body {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.xy-process-body :deep(.msg) {
  margin: 0;
}

/* 运行中折叠态：流光文字轻一行 */
.xy-process-collapsed {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border: none;
  background: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  cursor: pointer;
}
.xy-process-collapsed:hover {
  color: var(--xy-text-2, var(--text-dim));
}
.xy-shimmer-text {
  font-weight: 500;
  background: linear-gradient(90deg, var(--xy-text-2, var(--text-dim)) 25%, var(--xy-text, var(--text)) 50%, var(--xy-text-2, var(--text-dim)) 75%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: xy-shimmer 2.6s linear infinite;
}
@keyframes xy-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.xy-spin {
  color: var(--xy-accent, var(--accent));
  animation: xy-spin 1.1s linear infinite;
}
@keyframes xy-spin {
  to { transform: rotate(360deg); }
}

.turn-fold {
  max-width: var(--chat-col-width);
  margin: 0;
}
.turn-fold :deep(.fold-head) {
  padding: 2px 0;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.turn-fold :deep(.fold-head:hover) {
  color: var(--xy-text-2, var(--text-dim));
}
.turn-fold :deep(.fold-title) {
  font-weight: 400;
}
.turn-fold.is-open :deep(.fold-head) {
  width: 100%;
}
.turn-fold.is-open :deep(.xy-process-meta) {
  margin-left: auto;
}
.turn-fold :deep(.fold-body) {
  margin-top: 0;
}

.turn-changes {
  max-width: var(--chat-col-width);
  margin: 2px 0 0;
}

.xy-turn-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 2px;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  user-select: none;
}
.working-indicator.done {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.wi-check {
  color: var(--xy-accent, var(--accent));
  flex-shrink: 0;
}
.msg-metrics-bar {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
</style>
