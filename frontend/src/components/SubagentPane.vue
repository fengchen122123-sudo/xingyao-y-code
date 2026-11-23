<template>
  <div ref="wrapRef" class="suba-wrap" data-test="subagent-detail">
    <!-- 顶部切换栏（仅在详情模式下显示；无任务选中或点击「总览」进入总览页） -->
    <div
      v-if="!showHub && sessionTasks.length > 0"
      class="suba-switch-bar"
      data-test="subagent-switcher"
    >
      <button
        v-if="sessionTasks.length > 1"
        type="button"
        class="suba-switch-trigger"
        data-test="subagent-switcher-toggle"
        :aria-expanded="switcherOpen"
        @click="switcherOpen = !switcherOpen"
      >
        <span class="suba-dot" :class="statusClass(detail?.status || 'running')"></span>
        <span class="suba-switch-idx">#{{ activeTaskIndex }}</span>
        <span class="suba-switch-name">{{ cleanedActiveTitle }}</span>
        <span class="suba-switch-count">{{ sessionTasks.length }}任务</span>
        <AppIcon
          name="chevron-down"
          :size="12"
          :stroke-width="2"
          class="suba-chevron"
          :class="{ open: switcherOpen }"
        />
      </button>
      <div v-else class="suba-switch-single">
        <span class="suba-dot" :class="statusClass(detail?.status || 'running')"></span>
        <span class="suba-switch-name">当前会话 1 个星舰任务</span>
      </div>

      <button
        type="button"
        class="suba-hub-btn"
        data-test="subagent-hub-btn"
        title="查看本会话全部子代理任务总览"
        @click="openHub"
      >
        总览
      </button>
    </div>

    <!-- 展开的任务切换下拉列表 -->
    <div v-if="!showHub && switcherOpen && sessionTasks.length > 1" class="suba-switch-menu">
      <button
        v-for="(t, idx) in sessionTasks"
        :key="t.id"
        type="button"
        class="suba-chip"
        :class="{ active: t.id === store.subagentDetailId }"
        :data-test="'subagent-chip-' + t.id"
        :title="t.description"
        @click="selectTask(t.id)"
      >
        <span class="suba-dot" :class="statusClass(t.status)"></span>
        <span class="suba-chip-idx">#{{ idx + 1 }}</span>
        <span class="suba-chip-text">{{ cleanSubagentTitle(t.description) || t.description }}</span>
        <span class="suba-chip-meta">
          <span>{{ t.type }}</span>
          <span class="suba-mono">{{ formatElapsed(t.elapsed) }}</span>
        </span>
      </button>
    </div>

    <!-- 总览模式（未指定任务或手动点开总览） -->
    <template v-if="showHub">
      <div class="suba-hub-head">
        <div>
          <div class="suba-hub-title">本会话子代理总览</div>
          <div class="suba-hub-sub">{{ hubSummaryText }}</div>
        </div>
        <span v-if="runningCount > 0" class="suba-stage run">活跃 {{ runningCount }}</span>
      </div>

      <div v-if="sessionTasks.length === 0" class="suba-empty-card" data-test="subagent-empty">
        <AppIcon name="blocks" :size="20" :stroke-width="1.6" />
        <div class="suba-empty-title">当前会话尚未派发子代理</div>
        <div class="suba-empty-desc">
          主代理调度探索、实现、测试或审查子任务时，可点击对话中的「查看」在此按主代理同款时间线追踪步骤与结论。
        </div>
      </div>

      <div v-else class="suba-hub-list" data-test="subagent-hub-list">
        <button
          v-for="(t, idx) in orderedHubTasks"
          :key="t.id"
          type="button"
          class="suba-hub-card"
          :class="{ active: t.id === store.subagentDetailId }"
          :data-test="'subagent-hub-item-' + t.id"
          @click="selectTask(t.id)"
        >
          <div class="suba-hub-row">
            <span class="suba-dot" :class="statusClass(t.status)"></span>
            <span class="suba-mono suba-dim">#{{ idx + 1 }}</span>
            <span class="suba-stage" :class="statusClass(t.status)">{{ taskStatusLabel(t) }}</span>
            <span v-if="t.type" class="suba-badge">{{ t.type }}</span>
            <span v-if="t.background" class="suba-badge">后台</span>
            <span class="suba-hub-time suba-mono">{{ formatElapsed(t.elapsed) }}</span>
          </div>
          <div class="suba-hub-desc">{{ cleanSubagentTitle(t.description) || t.description }}</div>
          <div v-if="t.stage_detail" class="suba-hub-live">{{ t.stage_detail }}</div>
          <div class="suba-hub-foot">
            <span v-if="(t.rounds ?? 0) > 0">轮次 {{ t.rounds }}</span>
            <span v-if="(t.tool_ok ?? 0) > 0 || (t.tool_err ?? 0) > 0">
              工具 {{ t.tool_ok ?? 0 }} 成功
              <template v-if="(t.tool_err ?? 0) > 0">
                · <b class="suba-bad">{{ t.tool_err }} 失败</b>
              </template>
            </span>
            <span v-if="(t.changed_count ?? 0) > 0">改动 {{ t.changed_count }} 文件</span>
          </div>
        </button>
      </div>
    </template>

    <!-- 任务详情模式（对齐主代理 TurnGroup 视图） -->
    <template v-else-if="detail">
      <!-- 轻量任务上下文抬头 -->
      <header class="suba-ctx-head">
        <div class="suba-ctx-top">
          <div class="assistant-name">
            <span class="suba-dot" :class="statusClass(detail.status)"></span>
            <b class="an-agent">星舰</b>
            <span v-if="detail.type" class="suba-badge">{{ detail.type }}</span>
            <span v-if="detail.background" class="suba-badge">后台</span>
            <span v-if="detail.model_name" class="an-model" :title="detail.model_name">{{ detail.model_name }}</span>
            <span class="suba-stage" :class="statusClass(detail.status)">{{ taskStatusLabel(detail) }}</span>
          </div>
          <div class="suba-head-actions">
            <button
              v-if="processModel.toolErr > 0"
              type="button"
              class="suba-mini-btn err"
              :class="{ active: filterMode === 'failed' }"
              data-test="subagent-filter-failed"
              @click="toggleFailedFilter"
            >
              ✕ {{ processModel.toolErr }} 失败{{ filterMode === 'failed' ? ' · 恢复' : '' }}
            </button>
            <button
              type="button"
              class="suba-mini-btn"
              data-test="subagent-copy-prompt"
              title="复制任务指令"
              @click="copyText(detail.description, '已复制任务指令')"
            >复制</button>
            <button
              v-if="!isTerminal"
              type="button"
              class="suba-mini-btn danger"
              data-test="subagent-stop"
              :disabled="stopping"
              title="协作式停止该子代理并归还并发槽位"
              @click="stopSubagent"
            >{{ stopping ? '停止中…' : '■ 停止' }}</button>
          </div>
        </div>

        <div v-if="actionNotice" class="suba-toast" data-test="subagent-toast">{{ actionNotice }}</div>

        <!-- 轻量可展开任务指令 -->
        <div class="suba-prompt-line">
          <div
            class="suba-title"
            :class="{ collapsed: !promptExpanded }"
            data-test="subagent-title"
            @mouseenter="onTitleEnter"
            @mouseleave="onTitleLeave"
          >{{ detail.description }}</div>
          <div class="suba-prompt-foot">
            <span class="suba-mono suba-prompt-id">
              {{ detail.id }}<template v-if="detail.plan_item_id"> · {{ detail.plan_item_id }}</template>
            </span>
            <span class="suba-prompt-right">
              <span class="suba-mono">
                {{ formatElapsed(displayExecElapsed) }}<template v-if="budgetSeconds > 0"> / {{ formatElapsed(budgetSeconds) }}</template>
                <template v-if="waitElapsed > 0"> · 等审批 {{ formatElapsed(waitElapsed) }}</template>
              </span>
              <button
                type="button"
                class="suba-link-btn"
                data-test="subagent-toggle-prompt"
                @click="promptExpanded = !promptExpanded"
              >{{ promptExpanded ? '收起' : '全文' }}</button>
            </span>
          </div>
        </div>

        <Teleport to="body">
          <div
            v-if="tipVisible"
            class="suba-title-tip"
            data-test="subagent-title-tip"
            :style="tipStyle"
            @mouseenter="cancelHideTip"
            @mouseleave="hideTip"
          >{{ detail.description }}</div>
        </Teleport>

        <!-- 2px 细预算进度条 -->
        <div v-if="budgetSeconds > 0" class="suba-progress-track">
          <div
            class="suba-progress-fill"
            :class="budgetToneClass"
            :style="{ width: budgetPercent + '%' }"
          ></div>
        </div>
        <div v-if="handoffText" class="suba-handoff-note">{{ handoffText }}</div>
      </header>

      <!-- 等待权限审批提示横幅 -->
      <div
        v-if="!isTerminal && detail.stage === 'waiting_approval'"
        class="suba-wait-banner"
        data-test="subagent-wait-banner"
      >
        <AppIcon name="triangle-alert" :size="14" :stroke-width="2" class="suba-wait-ico" />
        <div class="suba-wait-body">
          <div class="suba-wait-title">该子代理正在等待权限授权</div>
          <div v-if="detail.approval_summary" class="suba-wait-cmd">{{ detail.approval_summary }}</div>
          <div class="suba-wait-hint">请在主对话区的审批卡片中处理，等待期间超时计时已暂停。</div>
        </div>
      </div>

      <!-- 主代理同款：执行过程折叠 / 展开容器 -->
      <section v-if="hasProcessSection" class="suba-turn-sec" data-test="subagent-flow">
        <!-- 运行中：展开式 .xy-process-card，支持收起为单行微光 -->
        <div v-if="!isTerminal" class="xy-run-process">
          <div v-if="runningExpanded" class="xy-process-card">
            <button
              type="button"
              class="xy-process-head"
              @click="runningExpanded = false"
            >
              <span class="xy-process-head-l">
                <AppIcon name="loader" :size="12.5" :stroke-width="2.2" class="xy-spin" />
                <span class="xy-process-title">{{ runningProcessTitle }}</span>
              </span>
              <span class="xy-process-meta">{{ formatElapsed(displayExecElapsed) }} · 收起 ▴</span>
            </button>
            <div class="xy-process-body">
              <template v-if="processModel.hasStructuredProcess">
                <div v-if="processModel.steps.length === 0" class="suba-empty-inline">
                  {{ filterMode === 'failed' ? '当前没有失败的工具调用' : '暂无执行记录' }}
                </div>
                <div v-for="step in processModel.steps" :key="step.id" class="suba-step">
                  <div v-if="step.thinking" class="think-block">
                    <FoldCard
                      :open="isThinkingOpen(step.id)"
                      caret="end"
                      @update:open="(v) => setThinkingOpen(step.id, v)"
                    >
                      <template #title>
                        <span class="tk-head">
                          <AppIcon class="tk-brain" name="lightbulb" :size="13" :stroke-width="1.8" />
                          <span class="tk-label" :class="{ 'is-thinking': step.thinkingLive }">
                            {{ step.thinkingLive ? '正在思考' : '思考' }}
                          </span>
                          <template v-if="step.thinkingSummary">
                            <span class="tk-sep">·</span>
                            <span class="tk-summary">{{ step.thinkingSummary }}</span>
                          </template>
                        </span>
                      </template>
                      <div class="tk-cnt">{{ step.thinking }}</div>
                    </FoldCard>
                  </div>

                  <div
                    v-if="step.text"
                    class="suba-step-prose md-body"
                    v-html="renderStepMarkdown(step.text)"
                    @click="onResultMdClick"
                  ></div>

                  <div
                    v-for="(ms, mi) in step.milestones"
                    :key="`ms-${step.id}-${mi}`"
                    class="suba-milestone"
                    :class="{ wait: ms.name === 'waiting_approval' }"
                  >
                    <span class="suba-mono">{{ stageClock(ms.at) }}</span>
                    <span>{{ stageNameLabel(ms.name) }}</span>
                    <span v-if="ms.detail" class="suba-milestone-detail">{{ ms.detail }}</span>
                  </div>

                  <ToolExecutionTrace
                    v-if="step.calls.length > 0"
                    :calls="step.calls"
                    :legacy-diffs="[]"
                    :state="executionState"
                    :group-commands="true"
                    :show-inline-error="true"
                    tool-test-prefix="subagent-tool-"
                  />
                </div>
              </template>

              <!-- 兼容仅含 stages 的旧版扁平回退 -->
              <template v-else>
                <div v-if="legacyFlatView.rows.length === 0" class="suba-empty-inline">暂无执行记录</div>
                <div
                  v-for="(row, i) in legacyFlatView.rows"
                  :key="i"
                  class="suba-row"
                  :class="legacyRowClass(row, i)"
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
              </template>
            </div>
          </div>
          <button
            v-else
            type="button"
            class="xy-process-collapsed"
            @click="runningExpanded = true"
          >
            <AppIcon name="loader" :size="12.5" :stroke-width="2.2" class="xy-spin" />
            <span class="xy-shimmer-text">{{ liveBannerText }}</span>
            <span class="xy-process-expand-hint">展开 ▾</span>
          </button>
        </div>

        <!-- 已完成：对齐 TurnGroup 的 FoldCard（有结论时默认折叠，优先展示结论与改动文件） -->
        <div v-else class="turn-fold" :class="{ 'is-open': completedExpanded }">
          <FoldCard :open="completedExpanded" @update:open="onCompletedFoldToggle">
            <template #title>
              <span>{{ completedProcessTitle }}<template v-if="!completedExpanded && displayExecElapsed > 0"> · {{ formatElapsed(displayExecElapsed) }}</template></span>
            </template>
            <template v-if="completedExpanded" #head-right>
              <span class="xy-process-meta">{{ displayExecElapsed > 0 ? `${formatElapsed(displayExecElapsed)} · ` : '' }}收起 ▴</span>
            </template>
            <div class="xy-process-body">
              <template v-if="processModel.hasStructuredProcess">
                <div v-if="processModel.steps.length === 0" class="suba-empty-inline">
                  {{ filterMode === 'failed' ? '当前没有失败的工具调用' : '暂无执行记录' }}
                </div>
                <div v-for="step in processModel.steps" :key="step.id" class="suba-step">
                  <div v-if="step.thinking" class="think-block">
                    <FoldCard
                      :open="isThinkingOpen(step.id)"
                      caret="end"
                      @update:open="(v) => setThinkingOpen(step.id, v)"
                    >
                      <template #title>
                        <span class="tk-head">
                          <AppIcon class="tk-brain" name="lightbulb" :size="13" :stroke-width="1.8" />
                          <span class="tk-label">思考</span>
                          <template v-if="step.thinkingSummary">
                            <span class="tk-sep">·</span>
                            <span class="tk-summary">{{ step.thinkingSummary }}</span>
                          </template>
                        </span>
                      </template>
                      <div class="tk-cnt">{{ step.thinking }}</div>
                    </FoldCard>
                  </div>

                  <div
                    v-if="step.text"
                    class="suba-step-prose md-body"
                    v-html="renderStepMarkdown(step.text)"
                    @click="onResultMdClick"
                  ></div>

                  <div
                    v-for="(ms, mi) in step.milestones"
                    :key="`ms-${step.id}-${mi}`"
                    class="suba-milestone"
                    :class="{ wait: ms.name === 'waiting_approval' }"
                  >
                    <span class="suba-mono">{{ stageClock(ms.at) }}</span>
                    <span>{{ stageNameLabel(ms.name) }}</span>
                    <span v-if="ms.detail" class="suba-milestone-detail">{{ ms.detail }}</span>
                  </div>

                  <ToolExecutionTrace
                    v-if="step.calls.length > 0"
                    :calls="step.calls"
                    :legacy-diffs="[]"
                    :state="executionState"
                    :group-commands="true"
                    :show-inline-error="true"
                    tool-test-prefix="subagent-tool-"
                  />
                </div>
              </template>

              <template v-else>
                <div v-if="legacyFlatView.rows.length === 0" class="suba-empty-inline">暂无执行记录</div>
                <div
                  v-for="(row, i) in legacyFlatView.rows"
                  :key="i"
                  class="suba-row"
                  :class="legacyRowClass(row, i)"
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
              </template>
            </div>
          </FoldCard>
        </div>
      </section>

      <!-- 实时过程状态条（仅运行中显示） -->
      <div v-if="!isTerminal" class="suba-live-banner" data-test="subagent-live-banner">
        <span class="suba-dot run"></span>
        <span class="suba-live-text">{{ liveBannerText }}</span>
        <button
          v-if="userScrolledUp"
          type="button"
          class="suba-scroll-btn"
          @click="scrollToBottom(true)"
        >↓ 回到底部</button>
      </div>

      <!-- 失败原因 / Markdown 执行结论 / 实时过程草稿（置为主位呈现） -->
      <section v-if="detail.error" class="suba-card-sec err">
        <div class="suba-sec-title">失败原因</div>
        <pre class="suba-sec-body err">{{ detail.error }}</pre>
      </section>

      <section v-else-if="detail.result" class="suba-answer-sec" data-test="subagent-result">
        <div class="suba-answer-head">
          <span>执行结论</span>
          <div class="suba-sec-actions">
            <button
              type="button"
              class="suba-copy"
              data-test="subagent-full-result"
              @click="fullResultOpen = true"
            >全览</button>
            <button
              type="button"
              class="suba-copy"
              data-test="subagent-copy"
              @click="copyResult"
            >{{ copyHint || '复制' }}</button>
          </div>
        </div>
        <div
          class="suba-answer-md md-body"
          v-html="renderedResultHtml"
          @click="onResultMdClick"
        ></div>
        <div v-if="detail.result_truncated" class="suba-trunc-note">
          内容较长，此处仅展示前 12,000 字符。
        </div>
      </section>

      <section v-else-if="detail.partial" class="suba-partial-sec" data-test="subagent-partial">
        <div class="suba-answer-head">
          <span>实时过程草稿</span>
          <button
            type="button"
            class="suba-link-btn"
            @click="partialExpanded = !partialExpanded"
          >{{ partialExpanded ? '收起' : '展开' }}</button>
        </div>
        <div
          v-if="partialExpanded"
          class="suba-partial-md md-body"
          v-html="renderedPartialHtml"
          @click="onResultMdClick"
        ></div>
      </section>

      <!-- 主代理同款改动文件面板 -->
      <ChangedFilesPanel
        v-if="fileChanges.length > 0"
        :changes="fileChanges"
        class="suba-changes"
        data-test="subagent-files"
      />

      <!-- 主代理同款收尾时长条 -->
      <div v-if="isTerminal" class="xy-turn-footer">
        <span class="working-indicator done">
          <AppIcon
            :name="detail.status === 'completed' ? 'check' : 'triangle-alert'"
            :size="13"
            :stroke-width="2.2"
            :class="detail.status === 'completed' ? 'wi-check' : 'wi-warn'"
          />
          <span class="wi-text">星舰本轮工作 {{ formatElapsed(displayExecElapsed) }}</span>
        </span>
        <span class="msg-metrics-bar">
          <template v-if="processModel.rounds > 0">{{ processModel.rounds }} 轮 · </template>
          {{ totalToolCount }} 次工具调用
        </span>
      </div>

      <!-- 全览结论弹窗 -->
      <Teleport to="body">
        <div
          v-if="fullResultOpen && detail.result"
          class="modal-mask show"
          data-test="subagent-full-modal"
          @click.self="fullResultOpen = false"
        >
          <div class="modal suba-full-modal">
            <div class="modal-head">
              <div class="mh-ico"><AppIcon name="blocks" :size="16" /></div>
              <div class="suba-full-head-text">
                <h3 class="suba-full-title">执行结论全览</h3>
                <p class="suba-full-sub">{{ detail.id }} · {{ cleanedActiveTitle }}</p>
              </div>
              <button
                type="button"
                class="chip-btn"
                @click="copyResult"
              >{{ copyHint || '复制全文' }}</button>
              <button
                type="button"
                class="chip-btn"
                @click="fullResultOpen = false"
              >关闭</button>
            </div>
            <div
              class="suba-full-body md-body"
              v-html="renderedResultHtml"
              @click="onResultMdClick"
            ></div>
          </div>
        </div>
      </Teleport>
    </template>

    <div v-else class="suba-empty" data-test="subagent-missing">{{ missingText }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import ChangedFilesPanel from './ChangedFilesPanel.vue';
import FoldCard from './FoldCard.vue';
import ToolExecutionTrace from './ToolExecutionTrace.vue';
import { useAppStore } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';
import { toAbsoluteLocalPath } from '../utils/localPath';
import {
  buildSubagentFileChanges,
  buildSubagentFlatView,
  buildSubagentProcessSteps,
  cleanSubagentTitle,
  type ExecutionState,
  type SubagentFlatRow,
} from '../utils/toolTrace';
import type { SubagentTaskDetail, SubagentTaskListItem } from '../types/pywebview.d';

const props = defineProps<{ active: boolean }>();

const store = useAppStore();

const wrapRef = ref<HTMLElement | null>(null);
const detail = ref<SubagentTaskDetail | null>(null);
const sessionTasks = ref<SubagentTaskListItem[]>([]);
const missingText = ref('正在读取子代理执行过程…');
const copyHint = ref('');
const actionNotice = ref('');
const switcherOpen = ref(false);
const showHub = ref(false);
const promptExpanded = ref(false);
const partialExpanded = ref(true);
const fullResultOpen = ref(false);
const filterMode = ref<'all' | 'failed'>('all');
const runningExpanded = ref(true);
const completedExpanded = ref(false);
const userFoldOverriddenTask = ref('');
const openThinkingSteps = ref<Set<string>>(new Set());
const stopping = ref(false);
const userScrolledUp = ref(false);

const STAGE_LABELS: Record<string, string> = {
  queued: '排队中',
  model: '调用模型',
  thinking: '思考中',
  tool: '调用工具',
  running: '执行中',
  waiting_approval: '等待授权',
  budget_notice: '预算提醒',
  composing: '生成回复',
  summarizing: '总结中',
  finalizing: '收尾中',
  completed: '已完成',
  failed: '失败',
  timed_out: '超时',
  cancelled: '已取消',
};
const TERMINAL = new Set(['completed', 'failed', 'timed_out', 'cancelled']);

const isTerminal = computed(() => {
  const s = detail.value?.status;
  return !!s && TERMINAL.has(s);
});

const executionState = computed<ExecutionState>(() => {
  const s = detail.value?.status || '';
  if (s === 'completed') return 'completed';
  if (s === 'cancelled') return 'cancelled';
  if (s === 'failed' || s === 'timed_out') return 'error';
  return 'running';
});

const processModel = computed(() =>
  buildSubagentProcessSteps({
    toolRecords: detail.value?.tool_records,
    roundNotes: detail.value?.round_notes,
    stages: detail.value?.stages,
    status: detail.value?.status,
    stage: detail.value?.stage,
    rounds: detail.value?.rounds,
    toolOk: detail.value?.tool_ok,
    toolErr: detail.value?.tool_err,
    filterFailed: filterMode.value === 'failed',
  }),
);

const legacyFlatView = computed(() => buildSubagentFlatView(detail.value?.stages ?? []));

const hasProcessSection = computed(() => {
  if (processModel.value.hasStructuredProcess) return true;
  if (legacyFlatView.value.rows.length > 0) return true;
  return !isTerminal.value;
});

const fileChanges = computed(() =>
  buildSubagentFileChanges({
    fileDiffs: detail.value?.file_diffs,
    toolRecords: detail.value?.tool_records,
    changedFiles: detail.value?.changed_files,
  }),
);

const totalToolCount = computed(() => {
  const recLen = detail.value?.tool_records?.length ?? 0;
  const agg = processModel.value.toolOk + processModel.value.toolErr;
  return Math.max(recLen, agg);
});

const runningProcessTitle = computed(() => {
  const count = totalToolCount.value;
  if (count > 0) return `星舰执行过程 · ${count} 个动作`;
  return '星舰执行过程 · 正在处理';
});

const completedProcessTitle = computed(() => {
  const count = totalToolCount.value;
  const label = taskStatusLabel(detail.value || { status: 'completed', stage: 'completed' });
  if (count > 0) return `星舰执行过程 · ${count} 个工具调用 · ${label}`;
  return `星舰执行过程 · ${Math.max(1, processModel.value.rounds)} 轮 · ${label}`;
});

function syncDefaultCompletedFold(d: SubagentTaskDetail | null) {
  if (!d || !TERMINAL.has(d.status)) return;
  if (userFoldOverriddenTask.value === d.id) return;
  // 有结果时默认收起过程（与主代理 TurnGroup 一致），报错或无结论时保持展开以便排查
  completedExpanded.value = Boolean(d.error || !d.result?.trim());
}

function onCompletedFoldToggle(nextOpen: boolean) {
  completedExpanded.value = nextOpen;
  if (detail.value?.id) userFoldOverriddenTask.value = detail.value.id;
}

function toggleFailedFilter() {
  filterMode.value = filterMode.value === 'failed' ? 'all' : 'failed';
  if (filterMode.value === 'failed') {
    runningExpanded.value = true;
    completedExpanded.value = true;
    if (detail.value?.id) userFoldOverriddenTask.value = detail.value.id;
  }
}

function isThinkingOpen(stepId: string): boolean {
  return openThinkingSteps.value.has(stepId);
}

function setThinkingOpen(stepId: string, open: boolean) {
  const next = new Set(openThinkingSteps.value);
  if (open) next.add(stepId);
  else next.delete(stepId);
  openThinkingSteps.value = next;
}

function renderStepMarkdown(text: string): string {
  return renderMarkdown(String(text || '').replace(/\r\n/g, '\n'));
}

const orderedHubTasks = computed(() => {
  const active: SubagentTaskListItem[] = [];
  const done: SubagentTaskListItem[] = [];
  for (const t of sessionTasks.value) {
    if (TERMINAL.has(t.status)) done.push(t);
    else active.push(t);
  }
  return [...active, ...done];
});

const activeTaskIndex = computed(() => {
  const idx = sessionTasks.value.findIndex((t) => t.id === (detail.value?.id || store.subagentDetailId));
  return idx >= 0 ? idx + 1 : 1;
});

const cleanedActiveTitle = computed(() => {
  const raw = detail.value?.description || '';
  return cleanSubagentTitle(raw) || raw;
});

const runningCount = computed(
  () => sessionTasks.value.filter((t) => !TERMINAL.has(t.status)).length,
);

const hubSummaryText = computed(() => {
  const total = sessionTasks.value.length;
  if (total === 0) return '暂无任务';
  const waiting = sessionTasks.value.filter((t) => !TERMINAL.has(t.status) && t.stage === 'waiting_approval').length;
  const failed = sessionTasks.value.filter((t) => t.status === 'failed' || t.status === 'timed_out').length;
  const parts = [`共 ${total} 个任务`];
  if (runningCount.value > 0) parts.push(`${runningCount.value} 运行中`);
  if (waiting > 0) parts.push(`${waiting} 待授权`);
  if (failed > 0) parts.push(`${failed} 异常`);
  return parts.join(' · ');
});

const budgetSeconds = computed(() => Number(detail.value?.timeout_seconds || 0));
const displayExecElapsed = computed(() => {
  const d = detail.value;
  if (!d) return 0;
  if (typeof d.execution_elapsed === 'number' && d.execution_elapsed > 0) {
    return d.execution_elapsed;
  }
  return d.elapsed || 0;
});
const waitElapsed = computed(() => Number(detail.value?.approval_wait_elapsed || 0));
const budgetPercent = computed(() => {
  if (!budgetSeconds.value) return 0;
  return Math.min(100, Math.round((displayExecElapsed.value / budgetSeconds.value) * 100));
});
const budgetToneClass = computed(() => {
  const s = detail.value?.status || '';
  if (s === 'completed') return 'ok';
  if (TERMINAL.has(s)) return 'err';
  if (budgetPercent.value >= 75) return 'warn';
  return 'run';
});
const handoffText = computed(() => {
  const h = detail.value?.handoff;
  if (h === 'main_takeover' || h === 'takeover_queued') return '已超时移交主代理接管';
  if (h === 'failed') return '移交主代理失败';
  return '';
});

const liveBannerText = computed(() => {
  const d = detail.value;
  if (!d) return '';
  if (d.stage === 'waiting_approval') return '挂起等待权限审批中…';
  if (d.stage === 'composing') {
    const len = (d.partial || '').length;
    return len > 0 ? `正在生成结论（已输出 ${len} 字）` : '正在生成结论…';
  }
  if (d.stage === 'thinking') {
    return d.stage_detail ? `正在思考 · ${d.stage_detail}` : '正在深度思考…';
  }
  if (d.stage === 'tool' && d.stage_detail) return `正在执行 · ${d.stage_detail}`;
  return `${taskStatusLabel(d)}…`;
});

const renderedResultHtml = computed(() =>
  renderMarkdown((detail.value?.result || '').replace(/\r\n/g, '\n')),
);
const renderedPartialHtml = computed(() =>
  renderMarkdown((detail.value?.partial || '').replace(/\r\n/g, '\n')),
);

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
function legacyRowClass(row: SubagentFlatRow, index: number) {
  const classes: string[] = [row.kind];
  if (row.kind === 'stage') {
    if (row.name === 'waiting_approval') classes.push('wait');
    else classes.push('dim');
  }
  if (
    detail.value != null &&
    !TERMINAL.has(detail.value.status) &&
    index === legacyFlatView.value.rows.length - 1
  ) {
    classes.push('current');
  }
  return classes;
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
function runningSeconds(at: number) {
  if (!at) return 0;
  return Math.max(0, Math.floor(Date.now() / 1000 - at));
}

function selectTask(taskId: string) {
  switcherOpen.value = false;
  showHub.value = false;
  filterMode.value = 'all';
  store.openSubagentDetail(taskId);
}

function openHub() {
  switcherOpen.value = false;
  showHub.value = true;
  void fetchDetail();
}

function openChangedFile(path: string) {
  const abs = toAbsoluteLocalPath(path, store.workdir);
  if (!abs) {
    showNotice('当前会话未绑定工作目录，无法解析相对路径');
    return;
  }
  store.openFilePreview(abs);
}

function onResultMdClick(e: MouseEvent) {
  const target = e.target as HTMLElement | null;
  if (!target) return;
  const fileLink = target.closest('a[data-file-link]');
  if (fileLink) {
    e.preventDefault();
    const raw = fileLink.getAttribute('data-file-link') || '';
    if (raw) openChangedFile(raw);
    return;
  }
  const copyBtn = target.closest('.code-copy');
  if (copyBtn) {
    const code = copyBtn.closest('.code-block')?.querySelector('code');
    const text = code?.textContent || '';
    if (text) {
      void copyText(text, '').then(() => {
        const prev = copyBtn.textContent;
        copyBtn.textContent = '已复制';
        setTimeout(() => { copyBtn.textContent = prev || '复制'; }, 1500);
      });
    }
  }
}

let noticeTimer: ReturnType<typeof setTimeout> | undefined;
function showNotice(msg: string) {
  if (!msg) return;
  actionNotice.value = msg;
  if (noticeTimer !== undefined) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { actionNotice.value = ''; }, 2400);
}

async function copyText(text: string, noticeMsg: string) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (noticeMsg) showNotice(noticeMsg);
  } catch {
    if (noticeMsg) showNotice('复制失败');
  }
}

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

async function stopSubagent() {
  const id = detail.value?.id;
  const api = window.pywebview?.api;
  if (!id || !api?.cancel_subagent_task || stopping.value) return;
  stopping.value = true;
  try {
    const res = await api.cancel_subagent_task(id, store.currentSessionName);
    showNotice(res?.msg || (res?.ok ? '已请求停止该子代理' : '停止失败'));
    await fetchDetail();
  } catch {
    showNotice('停止子代理失败');
  } finally {
    stopping.value = false;
  }
}

// ── 标题悬浮全文气泡 ─────────────────────────────────────────────
const tipVisible = ref(false);
const tipStyle = ref<Record<string, string>>({});
let tipTimer: ReturnType<typeof setTimeout> | undefined;

function cancelHideTip() {
  if (tipTimer !== undefined) {
    clearTimeout(tipTimer);
    tipTimer = undefined;
  }
}
function hideTip() {
  cancelHideTip();
  tipTimer = setTimeout(() => { tipVisible.value = false; }, 120);
}
function showTip(el: HTMLElement) {
  const rect = el.getBoundingClientRect();
  const viewportW = window.innerWidth || 1024;
  const viewportH = window.innerHeight || 768;
  const width = Math.max(200, Math.min(rect.width || 260, 420, viewportW - 24));
  const left = Math.max(12, Math.min(rect.left, viewportW - width - 12));
  const style: Record<string, string> = {
    left: `${left}px`,
    width: `${width}px`,
    maxHeight: `${Math.max(160, Math.round(viewportH * 0.6))}px`,
  };
  if (rect.bottom > viewportH * 0.55) {
    style.bottom = `${Math.max(12, viewportH - rect.top + 6)}px`;
  } else {
    style.top = `${rect.bottom + 6}px`;
  }
  tipStyle.value = style;
  cancelHideTip();
  tipVisible.value = true;
}
function onTitleEnter(e: MouseEvent) {
  const el = e.currentTarget as HTMLElement | null;
  if (!el || el.scrollHeight <= el.clientHeight + 1) return;
  showTip(el);
}
function onTitleLeave() {
  hideTip();
}

// ── 智能滚底跟随 ─────────────────────────────────────────────────
function scrollHost(): HTMLElement | null {
  return wrapRef.value?.closest('#right-content') as HTMLElement | null;
}
function onHostScroll() {
  const host = scrollHost();
  if (!host) return;
  const dist = host.scrollHeight - host.scrollTop - host.clientHeight;
  userScrolledUp.value = dist > 56;
}
function scrollToBottom(force = false) {
  const host = scrollHost();
  if (!host) return;
  if (!force && userScrolledUp.value) return;
  host.scrollTop = host.scrollHeight;
  userScrolledUp.value = false;
}
let boundScrollHost: HTMLElement | null = null;
function attachScrollListener() {
  const host = scrollHost();
  if (host === boundScrollHost) return;
  if (boundScrollHost) boundScrollHost.removeEventListener('scroll', onHostScroll);
  boundScrollHost = host;
  if (boundScrollHost) boundScrollHost.addEventListener('scroll', onHostScroll, { passive: true });
}

// ── 轮询与生命周期（终态自动停止轮询） ─────────────────────────────
let timer: ReturnType<typeof setInterval> | undefined;
let inFlight = false;

function stopPolling() {
  if (timer !== undefined) {
    clearInterval(timer);
    timer = undefined;
  }
}

function shouldKeepPolling(): boolean {
  if (!props.active) return false;
  if (showHub.value) {
    return sessionTasks.value.some((t) => !TERMINAL.has(t.status));
  }
  if (!store.subagentDetailId) return false;
  if (detail.value && TERMINAL.has(detail.value.status)) return false;
  return true;
}

function syncPolling() {
  if (shouldKeepPolling()) {
    if (timer === undefined) {
      timer = setInterval(() => { void fetchDetail(); }, 1000);
    }
  } else {
    stopPolling();
  }
}

async function fetchDetail() {
  const api = window.pywebview?.api;
  if (!api?.get_subagent_task_detail || inFlight) return;
  const id = showHub.value ? '' : store.subagentDetailId;
  inFlight = true;
  try {
    const res = await api.get_subagent_task_detail(id, store.currentSessionName);
    if (!showHub.value && store.subagentDetailId !== id) return;
    if (Array.isArray(res?.tasks)) sessionTasks.value = res.tasks;

    if (!id) {
      showHub.value = true;
      missingText.value = '';
      syncPolling();
      return;
    }

    if (res?.ok && res.task) {
      const prevCount = detail.value?.tool_records?.length ?? detail.value?.stages?.length ?? 0;
      detail.value = res.task;
      missingText.value = '';
      syncDefaultCompletedFold(res.task);
      const nextCount = res.task.tool_records?.length ?? res.task.stages?.length ?? 0;
      if (nextCount > prevCount && !TERMINAL.has(res.task.status)) {
        await nextTick();
        attachScrollListener();
        scrollToBottom(false);
      }
    } else {
      missingText.value = String(res?.msg || '未找到该子代理任务');
    }
    syncPolling();
  } catch { }
  finally { inFlight = false; }
}

watch(
  () => [props.active, store.subagentDetailId, store.subagentOpenNonce] as const,
  ([active, id]) => {
    stopPolling();
    switcherOpen.value = false;
    fullResultOpen.value = false;
    if (!id) {
      detail.value = null;
      showHub.value = true;
    } else if (detail.value && detail.value.id !== id) {
      detail.value = null;
      showHub.value = false;
      filterMode.value = 'all';
      runningExpanded.value = true;
      userFoldOverriddenTask.value = '';
      openThinkingSteps.value = new Set();
      missingText.value = '正在读取子代理执行过程…';
    } else {
      showHub.value = false;
    }
    if (active) {
      void nextTick().then(attachScrollListener);
      void fetchDetail();
      syncPolling();
    }
  },
  { immediate: true },
);

onUnmounted(() => {
  stopPolling();
  cancelHideTip();
  if (noticeTimer !== undefined) clearTimeout(noticeTimer);
  if (boundScrollHost) boundScrollHost.removeEventListener('scroll', onHostScroll);
});
</script>

<style scoped>
.suba-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 12px;
  color: var(--xy-text, var(--text));
}

.suba-mono {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
.suba-dim {
  color: var(--xy-text-3, var(--text-faint));
}
.suba-bad {
  color: var(--xy-rose, var(--red));
}

/* ── 顶部切换器 ── */
.suba-switch-bar {
  display: flex;
  align-items: center;
  gap: 6px;
}
.suba-switch-trigger {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text, var(--text));
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s;
}
.suba-switch-trigger:hover {
  border-color: var(--xy-accent, var(--accent));
}
.suba-switch-single {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--xy-text-2, var(--text-dim));
  font-size: 12px;
}
.suba-switch-idx {
  flex-shrink: 0;
  font-family: var(--mono);
  color: var(--xy-text-3, var(--text-faint));
}
.suba-switch-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
  font-weight: 500;
}
.suba-switch-count {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--xy-card, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
}
.suba-hub-btn {
  flex-shrink: 0;
  padding: 5px 9px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-hub-btn:hover {
  color: var(--xy-text, var(--text));
  border-color: var(--xy-text-3, var(--text-faint));
}

.suba-switch-menu {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border-radius: 10px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
}
.suba-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 8px;
  border-radius: 7px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--xy-text-2, var(--text-dim));
  font-size: 12px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.suba-chip:hover {
  background: var(--xy-canvas, var(--bg1));
  color: var(--xy-text, var(--text));
}
.suba-chip.active {
  border-color: var(--xy-accent, var(--accent));
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-weight: 500;
}
.suba-chip-idx {
  flex-shrink: 0;
  font-family: var(--mono);
  opacity: 0.7;
}
.suba-chip-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-chip-meta {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
}

/* ── 总览视图 ── */
.suba-hub-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.suba-hub-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--xy-text, var(--text-bright));
}
.suba-hub-sub {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
}
.suba-hub-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.suba-hub-card {
  display: flex;
  flex-direction: column;
  gap: 5px;
  width: 100%;
  padding: 10px 11px;
  border-radius: 10px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg2));
  color: var(--xy-text, var(--text));
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s;
}
.suba-hub-card:hover,
.suba-hub-card.active {
  border-color: var(--xy-accent, var(--accent));
}
.suba-hub-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.suba-hub-time {
  margin-left: auto;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11.5px;
}
.suba-hub-desc {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
  font-weight: 500;
  line-height: 1.45;
  word-break: break-word;
}
.suba-hub-live {
  font-size: 11.5px;
  color: var(--xy-text-2, var(--text-dim));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.suba-hub-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
}

.suba-empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 28px 14px;
  border-radius: 10px;
  border: 1px dashed var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg2));
  color: var(--xy-text-3, var(--text-faint));
  text-align: center;
}
.suba-empty-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--xy-text-2, var(--text-dim));
}
.suba-empty-desc {
  font-size: 12px;
  line-height: 1.55;
}

/* ── 状态圆点与徽章 ── */
.suba-dot {
  flex-shrink: 0;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--xy-accent, var(--accent));
}
.suba-dot.ok {
  background: var(--xy-green, var(--green));
}
.suba-dot.err {
  background: var(--xy-rose, var(--red));
}
.suba-dot.run {
  animation: suba-pulse 1.6s ease-in-out infinite;
}
@keyframes suba-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.suba-stage {
  flex-shrink: 0;
  border-radius: 99px;
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 500;
}
.suba-stage.run {
  background: var(--xy-accent-soft, rgba(79, 134, 247, 0.14));
  color: var(--xy-accent, var(--accent));
}
.suba-stage.ok {
  background: var(--xy-green-soft, rgba(74, 222, 128, 0.13));
  color: var(--xy-green, var(--green));
}
.suba-stage.err {
  background: var(--xy-rose-soft, rgba(248, 113, 113, 0.13));
  color: var(--xy-rose, var(--red));
}
.suba-badge {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 99px;
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 轻量上下文抬头（对齐 TurnGroup） ── */
.suba-ctx-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 2px 6px;
  border-bottom: 1px solid var(--xy-border, var(--border));
}
.suba-ctx-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.assistant-name {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
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
.suba-head-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.suba-mini-btn {
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg1));
  color: var(--xy-text-2, var(--text-dim));
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-mini-btn:hover:not(:disabled) {
  color: var(--xy-text, var(--text));
  border-color: var(--xy-text-3, var(--text-faint));
}
.suba-mini-btn.err {
  color: var(--xy-rose, var(--red));
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 35%, transparent);
}
.suba-mini-btn.err.active {
  background: var(--xy-rose-soft, rgba(248, 113, 113, 0.13));
  border-color: var(--xy-rose, var(--red));
}
.suba-mini-btn.danger {
  color: var(--xy-rose, var(--red));
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 36%, transparent);
  background: var(--xy-rose-soft, rgba(248, 113, 113, 0.1));
}
.suba-mini-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.suba-toast {
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-size: 11.5px;
}

.suba-prompt-line {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.suba-title {
  font-size: 12.5px;
  color: var(--xy-text-2, var(--text-dim));
  line-height: 1.5;
  word-break: break-word;
}
.suba-title.collapsed {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
}
.suba-prompt-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}
.suba-prompt-id {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-prompt-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.suba-link-btn {
  flex-shrink: 0;
  border: none;
  background: none;
  padding: 0;
  color: var(--xy-accent, var(--accent));
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
}
.suba-link-btn:hover {
  text-decoration: underline;
}

.suba-title-tip {
  position: fixed;
  z-index: 1500;
  padding: 8px 11px;
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 9px;
  background: var(--xy-canvas, var(--bg1));
  color: var(--xy-text, var(--text));
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-y: auto;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
}

.suba-progress-track {
  height: 2px;
  width: 100%;
  border-radius: 99px;
  background: var(--xy-border, var(--border));
  overflow: hidden;
}
.suba-progress-fill {
  height: 100%;
  border-radius: 99px;
  background: var(--xy-accent, var(--accent));
  transition: width 0.25s ease;
}
.suba-progress-fill.ok { background: var(--xy-green, var(--green)); }
.suba-progress-fill.warn { background: var(--xy-amber, var(--orange)); }
.suba-progress-fill.err { background: var(--xy-rose, var(--red)); }
.suba-handoff-note {
  font-size: 11.5px;
  color: var(--xy-amber, var(--orange));
}

/* ── 待审批提醒横幅 ── */
.suba-wait-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 9px;
  border: 1px solid color-mix(in srgb, var(--xy-amber, #c9a24a) 42%, transparent);
  background: var(--xy-amber-soft, rgba(201, 162, 74, 0.12));
  color: var(--xy-amber, #c9a24a);
}
.suba-wait-ico {
  flex-shrink: 0;
  margin-top: 1px;
}
.suba-wait-body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.suba-wait-title {
  font-weight: 600;
}
.suba-wait-cmd {
  font-family: var(--mono);
  word-break: break-all;
  color: var(--xy-text, var(--text));
}
.suba-wait-hint {
  font-size: 11.5px;
  opacity: 0.9;
}

/* ── 主代理同款 TurnGroup 过程卡 ── */
.suba-turn-sec {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.xy-process-card,
.turn-fold.is-open {
  border-radius: 12px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg1));
  padding: 10px 12px;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.xy-process-head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
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
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.xy-process-collapsed {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
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

.suba-step {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.suba-step-prose {
  font-size: 13px;
  line-height: 1.62;
  color: var(--xy-text, var(--text));
}

/* ── 主代理同款思考折叠块 ── */
.think-block {
  margin: 2px 0;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.think-block :deep(.fold-head) {
  width: fit-content;
  max-width: 100%;
  gap: 6px;
  padding: 1px 0;
  color: inherit;
}
.think-block :deep(.fold-head:hover) {
  color: var(--xy-text-2, var(--text-dim));
}
.tk-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.tk-brain {
  flex: none;
  opacity: 0.85;
}
.tk-label {
  flex: none;
  font-weight: 400;
  white-space: nowrap;
}
.tk-label.is-thinking {
  background: linear-gradient(
    90deg,
    var(--xy-text-3, var(--text-faint)) 0%,
    var(--xy-text, var(--text-bright)) 45%,
    var(--xy-text-3, var(--text-faint)) 90%
  );
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: tk-shimmer 2.4s linear infinite;
}
@keyframes tk-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
.tk-sep {
  flex: none;
  opacity: 0.65;
}
.tk-summary {
  display: inline-flex;
  align-items: center;
  flex: 1 1 auto;
  min-width: 0;
  font-weight: 400;
  color: inherit;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tk-cnt {
  margin: 4px 0 2px;
  padding-left: 20px;
  max-height: calc(1.6em * 10 + 2px);
  overflow-y: auto;
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: var(--xy-text-3, var(--text-faint));
}

.suba-milestone {
  display: flex;
  align-items: baseline;
  gap: 7px;
  padding: 4px 8px;
  border-radius: 7px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
  font-size: 11.5px;
}
.suba-milestone.wait {
  border-color: color-mix(in srgb, var(--xy-amber, #c9a24a) 40%, transparent);
  background: var(--xy-amber-soft, rgba(201, 162, 74, 0.12));
  color: var(--xy-amber, #c9a24a);
}
.suba-milestone-detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 实时状态条 ── */
.suba-live-banner {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--xy-accent, var(--accent)) 35%, transparent);
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.suba-live-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.suba-scroll-btn {
  flex-shrink: 0;
  padding: 2px 7px;
  border-radius: 5px;
  border: 1px solid var(--xy-accent, var(--accent));
  background: var(--xy-canvas, var(--bg1));
  color: var(--xy-accent, var(--accent));
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;
}

/* ── 兼容旧版扁平时间流行 ── */
.suba-row {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 0 2px 10px;
  font-size: 12.5px;
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
.suba-row.current::before { background: var(--xy-accent, var(--accent)); }
.suba-row-time {
  flex-shrink: 0;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
  font-family: var(--mono);
}
.suba-row-label {
  flex-shrink: 0;
  color: var(--xy-text-2, var(--text-dim));
  font-weight: 600;
  font-family: var(--mono);
  font-size: 12px;
}
.suba-row-label.dim {
  color: var(--xy-text-3, var(--text-faint));
  font-weight: 400;
  font-family: inherit;
}
.suba-row-res {
  min-width: 0;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  word-break: break-word;
}
.suba-row-res.ok { color: var(--xy-green, var(--green)); }
.suba-row-res.err { color: var(--xy-rose, var(--red)); white-space: normal; }
.suba-row-res.run { color: var(--xy-amber, var(--orange)); }
.suba-row.wait {
  border-radius: 6px;
  background: var(--xy-amber-soft, rgba(201, 162, 74, 0.12));
}
.suba-row.wait .suba-row-label {
  color: var(--xy-amber, #c9a24a);
}

/* ── 主位结论与草稿 ── */
.suba-answer-sec {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.suba-answer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  font-weight: 500;
  color: var(--xy-text-3, var(--text-faint));
}
.suba-answer-md {
  font-size: 13.5px;
  line-height: 1.68;
  color: var(--xy-text, var(--text));
}
.suba-partial-sec {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.suba-partial-md {
  border-left: 2px solid var(--xy-accent, var(--accent));
  padding-left: 10px;
  font-size: 13px;
  line-height: 1.62;
  color: var(--xy-text-2, var(--text-dim));
}
.suba-changes {
  margin-top: 2px;
}

.suba-card-sec {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 10px 11px;
  border-radius: 10px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
}
.suba-card-sec.err {
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 40%, transparent);
}
.suba-sec-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
}
.suba-sec-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.suba-copy {
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg1));
  color: var(--xy-text-2, var(--text-dim));
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-copy:hover {
  color: var(--xy-text, var(--text));
  border-color: var(--xy-text-3, var(--text-faint));
}
.suba-sec-body {
  margin: 0;
  padding: 8px 10px;
  background: var(--code-bg);
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--xy-text-2, var(--text-dim));
  max-height: 280px;
  overflow-y: auto;
}
.suba-sec-body.err {
  color: var(--xy-rose, var(--red));
}
.suba-trunc-note {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}

/* ── 主代理同款收尾栏 ── */
.xy-turn-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 2px 0;
  border-top: 1px solid var(--xy-border, var(--border));
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
.wi-warn {
  color: var(--xy-amber, var(--orange));
  flex-shrink: 0;
}
.msg-metrics-bar {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}

/* ── 全览模态框 ── */
.suba-full-modal {
  width: 780px;
  max-width: 92vw;
}
.suba-full-head-text {
  flex: 1;
  min-width: 0;
}
.suba-full-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.suba-full-sub {
  margin: 2px 0 0;
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-full-body {
  padding: 16px 20px;
  max-height: 68vh;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.7;
  background: var(--xy-canvas, var(--bg1));
}

.suba-empty,
.suba-empty-inline {
  padding: 16px 10px;
  text-align: center;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 12px;
}
.suba-chevron {
  color: var(--xy-text-3, var(--text-faint));
  transition: transform 0.15s;
}
.suba-switch-trigger .suba-chevron.open {
  transform: rotate(180deg);
}
</style>
