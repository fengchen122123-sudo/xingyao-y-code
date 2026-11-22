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
          主代理调度探索、实现、测试或审查子任务时，可点击对话中的「查看」在此实时追踪步骤、参数与结论。
        </div>
      </div>

      <div v-else class="suba-hub-list" data-test="subagent-hub-list">
        <button
          v-for="(t, idx) in sessionTasks"
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

    <!-- 任务详情模式 -->
    <template v-else-if="detail">
      <!-- 任务头卡 -->
      <section class="suba-head-card">
        <div class="suba-head-top">
          <div class="suba-meta">
            <span class="suba-dot" :class="statusClass(detail.status)"></span>
            <span class="suba-stage" :class="statusClass(detail.status)">{{ taskStatusLabel(detail) }}</span>
            <span v-if="detail.type" class="suba-badge">{{ detail.type }}</span>
            <span v-if="detail.background" class="suba-badge">后台</span>
            <span v-if="detail.model_name" class="suba-badge accent" :title="detail.model_name">{{ detail.model_name }}</span>
          </div>
          <div class="suba-head-actions">
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

        <!-- 可折叠任务指令框 -->
        <div class="suba-prompt-box">
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
            <button
              type="button"
              class="suba-link-btn"
              data-test="subagent-toggle-prompt"
              @click="promptExpanded = !promptExpanded"
            >{{ promptExpanded ? '收起指令' : '展开全文' }}</button>
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

        <!-- 预算进度条 -->
        <div class="suba-budget">
          <div class="suba-budget-row">
            <span>
              已用 <b class="suba-num">{{ formatElapsed(displayExecElapsed) }}</b>
              <template v-if="budgetSeconds > 0"> / 预算 {{ formatElapsed(budgetSeconds) }}</template>
              <template v-if="waitElapsed > 0"> · 等审批 {{ formatElapsed(waitElapsed) }}</template>
            </span>
            <span v-if="timeRangeText" class="suba-mono">{{ timeRangeText }}</span>
          </div>
          <div v-if="budgetSeconds > 0" class="suba-progress-track">
            <div
              class="suba-progress-fill"
              :class="budgetToneClass"
              :style="{ width: budgetPercent + '%' }"
            ></div>
          </div>
          <div v-if="handoffText" class="suba-handoff-note">{{ handoffText }}</div>
        </div>

        <!-- 可点击统计筛选胶囊 -->
        <div class="suba-filter-pills">
          <button
            type="button"
            class="suba-filter-pill"
            :class="{ active: filterMode === 'all' }"
            data-test="subagent-filter-all"
            @click="filterMode = 'all'"
          >
            全部 · 轮次 <b class="suba-num">{{ timeline.rounds }}</b> · 工具 <b class="suba-num">{{ timeline.toolOk }}</b> 成功
          </button>
          <button
            v-if="timeline.toolErr > 0"
            type="button"
            class="suba-filter-pill err"
            :class="{ active: filterMode === 'failed' }"
            data-test="subagent-filter-failed"
            @click="filterMode = filterMode === 'failed' ? 'all' : 'failed'"
          >
            ✗ <b class="suba-num bad">{{ timeline.toolErr }}</b> 失败（{{ filterMode === 'failed' ? '点此恢复' : '点击聚焦' }}）
          </button>
          <button
            v-if="changedFiles.length > 0"
            type="button"
            class="suba-filter-pill"
            :class="{ active: filterMode === 'files' }"
            data-test="subagent-filter-files"
            @click="filterMode = filterMode === 'files' ? 'all' : 'files'"
          >
            改动 <b class="suba-num">{{ changedFiles.length }}</b> 文件
          </button>
        </div>
      </section>

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

      <!-- 改动文件列表模式 -->
      <section v-if="filterMode === 'files'" class="suba-card-sec" data-test="subagent-files">
        <div class="suba-sec-title">本子代理改动的文件（{{ changedFiles.length }}）</div>
        <div class="suba-file-list">
          <button
            v-for="filePath in changedFiles"
            :key="filePath"
            type="button"
            class="suba-file-item"
            @click="openChangedFile(filePath)"
          >
            <AppIcon name="file" :size="13" :stroke-width="1.9" />
            <span class="suba-file-path">{{ filePath }}</span>
            <span class="suba-file-open">预览</span>
          </button>
        </div>
      </section>

      <!-- 执行过程时间线 -->
      <section v-else class="suba-timeline-sec">
        <div class="suba-flow" data-test="subagent-flow">
          <!-- 结构化轮次分组视图 -->
          <template v-if="timeline.hasStructuredTools">
            <div v-if="filteredGroups.length === 0" class="suba-empty">
              {{ filterMode === 'failed' ? '当前没有失败的工具调用' : '暂无执行记录' }}
            </div>
            <div v-for="group in filteredGroups" :key="group.round" class="suba-round-group">
              <div class="suba-round-head">
                <span class="suba-round-title">Round {{ group.round }}</span>
                <span v-if="group.startedAt" class="suba-mono suba-round-clock">{{ stageClock(group.startedAt) }}</span>
              </div>

              <template v-if="filterMode === 'all'">
                <div
                  v-for="(ms, mi) in group.milestones"
                  :key="'ms-' + group.round + '-' + mi"
                  class="suba-milestone"
                  :class="{ wait: ms.name === 'waiting_approval' }"
                >
                  <span class="suba-mono">{{ stageClock(ms.at) }}</span>
                  <span>{{ stageNameLabel(ms.name) }}</span>
                  <span v-if="ms.detail" class="suba-milestone-detail">{{ ms.detail }}</span>
                </div>
              </template>

              <div class="suba-tool-list">
                <div
                  v-for="tool in group.tools"
                  :key="tool.id"
                  class="suba-tool-card"
                  :class="{ err: tool.status === 'err', run: tool.status === 'running' }"
                  :data-test="'subagent-tool-' + tool.id"
                >
                  <button
                    type="button"
                    class="suba-tool-btn"
                    @click="toggleToolExpand(tool.id)"
                  >
                    <span class="suba-tool-left">
                      <AppIcon
                        :name="toolCategoryIcon(tool.category)"
                        :size="13"
                        :stroke-width="2"
                        class="suba-tool-ico"
                        :class="'cat-' + (tool.category || 'other')"
                      />
                      <span class="suba-tool-action">{{ tool.action || tool.name }}</span>
                      <span v-if="tool.target" class="suba-tool-target" :title="tool.target">{{ tool.target }}</span>
                    </span>
                    <span class="suba-tool-right">
                      <span v-if="tool.status === 'ok'" class="suba-row-res ok">✓ {{ tool.duration_text || '0ms' }}</span>
                      <span v-else-if="tool.status === 'err'" class="suba-row-res err">✗ 失败</span>
                      <span v-else-if="tool.status === 'cancelled'" class="suba-row-res warn">已取消</span>
                      <span v-else class="suba-row-res run">运行 {{ runningSeconds(tool.started_at) }}s</span>
                      <AppIcon
                        name="chevron-right"
                        :size="12"
                        :stroke-width="2"
                        class="suba-chevron"
                        :class="{ open: isToolExpanded(tool.id) }"
                      />
                    </span>
                  </button>

                  <div
                    v-if="tool.status === 'err' && !isToolExpanded(tool.id) && tool.error_summary"
                    class="suba-tool-err-inline"
                  >{{ tool.error_summary }}</div>

                  <div v-if="isToolExpanded(tool.id)" class="suba-tool-detail">
                    <div class="suba-detail-kv">
                      <span class="suba-mono">{{ tool.name }}</span>
                      <span class="suba-mono">{{ stageClock(tool.started_at) }}</span>
                    </div>
                    <template v-if="tool.args_preview">
                      <div class="suba-detail-lbl">调用参数</div>
                      <pre class="suba-detail-pre">{{ tool.args_preview }}</pre>
                    </template>
                    <template v-if="tool.result_preview || tool.error_summary">
                      <div class="suba-detail-lbl" :class="{ err: tool.status === 'err' }">
                        {{ tool.status === 'err' ? '错误详情' : '结果摘要' }}
                      </div>
                      <pre class="suba-detail-pre" :class="{ err: tool.status === 'err' }">{{ tool.result_preview || tool.error_summary }}</pre>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- 兼容旧版 stages 的扁平回退渲染 -->
          <template v-else>
            <div v-if="legacyFlatView.rows.length === 0" class="suba-empty">暂无执行记录</div>
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

        <!-- 方案 A：底部单行实时状态条 -->
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
      </section>

      <!-- 失败原因 / Markdown 执行结论 / 实时过程草稿 -->
      <section v-if="detail.error" class="suba-card-sec err">
        <div class="suba-sec-title">失败原因</div>
        <pre class="suba-sec-body err">{{ detail.error }}</pre>
      </section>

      <section v-else-if="detail.result" class="suba-card-sec" data-test="subagent-result">
        <div class="suba-sec-title">
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
          class="suba-md md-body"
          v-html="renderedResultHtml"
          @click="onResultMdClick"
        ></div>
        <div v-if="detail.result_truncated" class="suba-trunc-note">
          内容较长，此处仅展示前 12,000 字符。
        </div>
      </section>

      <section v-else-if="detail.partial" class="suba-card-sec" data-test="subagent-partial">
        <div class="suba-sec-title">
          <span>实时过程草稿</span>
          <button
            type="button"
            class="suba-link-btn"
            @click="partialExpanded = !partialExpanded"
          >{{ partialExpanded ? '收起' : '展开' }}</button>
        </div>
        <div
          v-if="partialExpanded"
          class="suba-md md-body"
          v-html="renderedPartialHtml"
          @click="onResultMdClick"
        ></div>
      </section>

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
import { useAppStore } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';
import { toAbsoluteLocalPath } from '../utils/localPath';
import {
  buildSubagentFlatView,
  buildSubagentRoundGroups,
  cleanSubagentTitle,
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
const filterMode = ref<'all' | 'failed' | 'files'>('all');
const expandedTools = ref<Set<string>>(new Set());
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

const timeline = computed(() =>
  buildSubagentRoundGroups({
    toolRecords: detail.value?.tool_records,
    stages: detail.value?.stages,
    rounds: detail.value?.rounds,
    toolOk: detail.value?.tool_ok,
    toolErr: detail.value?.tool_err,
  }),
);

const legacyFlatView = computed(() => buildSubagentFlatView(detail.value?.stages ?? []));

const filteredGroups = computed(() => {
  if (filterMode.value !== 'failed') return timeline.value.groups;
  return timeline.value.groups
    .map((g) => ({
      ...g,
      tools: g.tools.filter((t) => t.status === 'err'),
    }))
    .filter((g) => g.tools.length > 0);
});

const changedFiles = computed(() => detail.value?.changed_files ?? []);

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
    return len > 0 ? `正在生成回复（已实时产出 ${len} 字）` : '正在生成回复…';
  }
  if (d.stage === 'thinking') return '正在深度思考…';
  if (d.stage === 'tool' && d.stage_detail) return `正在执行工具：${d.stage_detail}`;
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
function toolCategoryIcon(category: string) {
  if (category === 'read') return 'file';
  if (category === 'search') return 'search';
  if (category === 'command') return 'square-terminal';
  if (category === 'mutation') return 'pencil';
  if (category === 'skill') return 'sparkles';
  if (category === 'computer') return 'scan';
  return 'box';
}
function isToolExpanded(id: string) {
  return expandedTools.value.has(id);
}
function toggleToolExpand(id: string) {
  const next = new Set(expandedTools.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expandedTools.value = next;
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
const timeRangeText = computed(() => {
  const d = detail.value;
  if (!d?.started_at) return '';
  const start = stageClock(d.started_at);
  if (d.finished_at) return `${start} → ${stageClock(d.finished_at)}`;
  return `${start} 开始`;
});

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
      expandedTools.value = new Set();
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
  color: var(--text);
}

.suba-mono {
  font-family: var(--mono);
}
.suba-dim {
  color: var(--text-faint);
}
.suba-bad {
  color: var(--red);
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
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s;
}
.suba-switch-trigger:hover {
  border-color: var(--accent);
}
.suba-switch-single {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-dim);
  font-size: 12px;
}
.suba-switch-idx {
  flex-shrink: 0;
  font-family: var(--mono);
  color: var(--text-faint);
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
  background: var(--bg3);
  color: var(--text-faint);
  font-size: 11px;
}
.suba-hub-btn {
  flex-shrink: 0;
  padding: 5px 9px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-hub-btn:hover {
  color: var(--text);
  border-color: var(--text-faint);
}

.suba-switch-menu {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
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
  color: var(--text-dim);
  font-size: 12px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.suba-chip:hover {
  background: var(--bg1);
  color: var(--text);
}
.suba-chip.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
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
  color: var(--text-faint);
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
  color: var(--text-bright, var(--text));
}
.suba-hub-sub {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.suba-hub-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.suba-hub-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 10px 11px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s;
}
.suba-hub-card:hover,
.suba-hub-card.active {
  border-color: var(--accent);
}
.suba-hub-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.suba-hub-time {
  margin-left: auto;
  color: var(--text-faint);
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
.suba-hub-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 11.5px;
  color: var(--text-faint);
}

.suba-empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 28px 14px;
  border-radius: 10px;
  border: 1px dashed var(--border);
  background: var(--bg2);
  color: var(--text-faint);
  text-align: center;
}
.suba-empty-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
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
  background: var(--orange);
}
.suba-dot.ok {
  background: var(--green);
}
.suba-dot.err {
  background: var(--red);
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
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 500;
}
.suba-stage.run {
  background: color-mix(in srgb, var(--orange) 15%, transparent);
  color: var(--orange);
}
.suba-stage.ok {
  background: color-mix(in srgb, var(--green) 15%, transparent);
  color: var(--green);
}
.suba-stage.err {
  background: color-mix(in srgb, var(--red) 15%, transparent);
  color: var(--red);
}
.suba-badge {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 99px;
  border: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 11px;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-badge.accent {
  border-color: transparent;
  background: var(--accent-soft);
  color: var(--accent);
}

/* ── 任务头卡 ── */
.suba-head-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 11px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
}
.suba-head-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.suba-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  min-width: 0;
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
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-mini-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--text-faint);
}
.suba-mini-btn.danger {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 36%, transparent);
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.suba-mini-btn.danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--red) 18%, transparent);
}
.suba-mini-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.suba-toast {
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11.5px;
}

.suba-prompt-box {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 8px 9px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg1);
}
.suba-title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
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
  color: var(--text-faint);
}
.suba-prompt-id {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-link-btn {
  flex-shrink: 0;
  border: none;
  background: none;
  padding: 0;
  color: var(--accent);
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
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg1);
  color: var(--text);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-y: auto;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
}

/* ── 预算进度条 ── */
.suba-budget {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.suba-budget-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-faint);
}
.suba-num {
  font-weight: 600;
  color: var(--text-dim);
  font-family: var(--mono);
  font-style: normal;
}
.suba-num.bad {
  color: var(--red);
}
.suba-progress-track {
  height: 5px;
  width: 100%;
  border-radius: 99px;
  background: var(--bg3);
  overflow: hidden;
}
.suba-progress-fill {
  height: 100%;
  border-radius: 99px;
  background: var(--accent);
  transition: width 0.25s ease;
}
.suba-progress-fill.ok {
  background: var(--green);
}
.suba-progress-fill.warn {
  background: var(--orange);
}
.suba-progress-fill.err {
  background: var(--red);
}
.suba-handoff-note {
  font-size: 11.5px;
  color: var(--orange);
}

/* ── 可点击统计筛选胶囊 ── */
.suba-filter-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.suba-filter-pill {
  padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.suba-filter-pill:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.suba-filter-pill.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}
.suba-filter-pill.err {
  border-color: color-mix(in srgb, var(--red) 35%, transparent);
  color: var(--red);
}
.suba-filter-pill.err.active {
  background: color-mix(in srgb, var(--red) 14%, transparent);
  border-color: var(--red);
}

/* ── 待审批提醒横幅 ── */
.suba-wait-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 9px;
  border: 1px solid color-mix(in srgb, var(--yellow, #c9a24a) 42%, transparent);
  background: color-mix(in srgb, var(--yellow, #c9a24a) 12%, transparent);
  color: var(--yellow, #c9a24a);
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
  color: var(--text);
}
.suba-wait-hint {
  font-size: 11.5px;
  opacity: 0.9;
}

/* ── 改动文件列表 ── */
.suba-file-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.suba-file-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text);
  font-size: 12px;
  font-family: var(--mono);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s;
}
.suba-file-item:hover {
  border-color: var(--accent);
}
.suba-file-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-file-open {
  flex-shrink: 0;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--accent);
}

/* ── 结构化轮次时间线 ── */
.suba-timeline-sec {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.suba-flow {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.suba-round-group {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.suba-round-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 2px;
}
.suba-round-title {
  font-weight: 600;
  color: var(--text-dim);
}
.suba-round-clock {
  font-size: 11px;
  color: var(--text-faint);
}

.suba-milestone {
  display: flex;
  align-items: baseline;
  gap: 7px;
  padding: 5px 8px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 11.5px;
}
.suba-milestone.wait {
  border-color: color-mix(in srgb, var(--yellow, #c9a24a) 40%, transparent);
  background: color-mix(in srgb, var(--yellow, #c9a24a) 12%, transparent);
  color: var(--yellow, #c9a24a);
}
.suba-milestone-detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.suba-tool-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.suba-tool-card {
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg2);
  overflow: hidden;
  transition: border-color 0.15s;
}
.suba-tool-card:hover {
  border-color: var(--text-faint);
}
.suba-tool-card.err {
  border-color: color-mix(in srgb, var(--red) 40%, transparent);
}
.suba-tool-card.run {
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
}
.suba-tool-btn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 7px 9px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.suba-tool-left {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.suba-tool-ico.cat-read { color: var(--cyan, var(--accent)); }
.suba-tool-ico.cat-search { color: var(--accent); }
.suba-tool-ico.cat-command { color: var(--orange); }
.suba-tool-ico.cat-mutation { color: var(--green); }
.suba-tool-ico.cat-skill { color: var(--purple, var(--accent)); }
.suba-tool-ico.cat-other { color: var(--text-faint); }
.suba-tool-action {
  flex-shrink: 0;
  font-weight: 500;
  color: var(--text);
}
.suba-tool-target {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
}
.suba-tool-right {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.suba-chevron {
  color: var(--text-faint);
  transition: transform 0.15s;
}
.suba-chevron.open {
  transform: rotate(90deg);
}
.suba-switch-trigger .suba-chevron.open {
  transform: rotate(180deg);
}
.suba-tool-err-inline {
  padding: 0 9px 7px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--red);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suba-tool-detail {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 7px 9px 9px;
  border-top: 1px solid var(--border);
  background: var(--bg1);
}
.suba-detail-kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-faint);
}
.suba-detail-lbl {
  font-size: 11px;
  color: var(--text-faint);
}
.suba-detail-lbl.err {
  color: var(--red);
}
.suba-detail-pre {
  margin: 0;
  padding: 7px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--code-bg);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-dim);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}
.suba-detail-pre.err {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 35%, transparent);
}

/* ── 实时状态条 ── */
.suba-live-banner {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
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
  border: 1px solid var(--accent);
  background: var(--bg1);
  color: var(--accent);
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
  padding: 3px 0 3px 10px;
  font-size: 12px;
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
  font-size: 11px;
  font-family: var(--mono);
}
.suba-row-label {
  flex-shrink: 0;
  color: var(--text-dim);
  font-weight: 600;
  font-family: var(--mono);
  font-size: 11.5px;
}
.suba-row-label.dim {
  color: var(--text-faint);
  font-weight: 400;
  font-family: inherit;
}
.suba-row-res {
  min-width: 0;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
  word-break: break-word;
}
.suba-row-res.ok { color: var(--green); }
.suba-row-res.err { color: var(--red); white-space: normal; }
.suba-row-res.run { color: var(--orange); }
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

/* ── 结论与草稿卡片 ── */
.suba-card-sec {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 10px 11px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
}
.suba-card-sec.err {
  border-color: color-mix(in srgb, var(--red) 40%, transparent);
}
.suba-sec-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
}
.suba-sec-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.suba-copy {
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 11px;
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
  max-height: 280px;
  overflow-y: auto;
}
.suba-sec-body.err {
  color: var(--red);
}
.suba-md {
  padding: 9px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg1);
  font-size: 12.5px;
  line-height: 1.6;
  max-height: 360px;
  overflow-y: auto;
}
.suba-trunc-note {
  font-size: 11px;
  color: var(--text-faint);
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
  color: var(--text-faint);
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
  background: var(--bg1);
}

.suba-empty {
  padding: 20px 10px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}
</style>
