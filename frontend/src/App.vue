<template>
  <div id="app-root">
    <div v-if="store.guiThemeError" class="gui-theme-error" data-test="gui-theme-error">
      <div class="gui-theme-error-card">
        <div class="gui-theme-error-kicker">GUI 主题无法加载</div>
        <h1>需要选择一个可用主题</h1>
        <p>{{ store.guiThemeError.message }}</p>
        <div class="gui-theme-error-code">
          {{ store.guiThemeError.code }} · {{ store.guiThemeError.path }}
        </div>
        <div class="gui-theme-error-actions">
          <button data-test="gui-theme-reset" @click="store.resetInvalidGuiTheme('builtin.dark')">
            重置为内置深色
          </button>
          <button data-test="gui-theme-reset" @click="store.resetInvalidGuiTheme('builtin.light')">
            重置为内置浅色
          </button>
        </div>
      </div>
    </div>
    <template v-else>
    <div v-if="store.themePreviewMode && !store.settingsOpen" class="theme-preview-bar" data-test="theme-preview-bar">
      <span class="tpb-text">正在预览主题效果（未保存）</span>
      <button class="tpb-btn primary" data-test="preview-back" @click="backToThemeEditor">返回编辑</button>
    </div>

    <Topbar />

    <div
      v-for="edge in WINDOW_RESIZE_EDGES"
      v-if="!windowMaximized"
      :key="edge"
      class="window-resize-handle"
      :class="`resize-${edge.replace('_', '-')}`"
      data-test="window-resize-handle"
      :data-resize-edge="edge"
      @mousedown.left.prevent="beginWindowResize(edge)"
    ></div>

    <div id="main" :class="{ 'panel-resizing': anyPanelResizing }">
      <SettingsPage v-if="store.settingsOpen" />
      <template v-else>
      <Sidebar />

      <div
        v-if="!isMobile && !store.sidebarCollapsed"
        class="panel-resizer"
        :class="{ resizing: sidebarResizing }"
        title="拖拽调整侧栏宽度，双击恢复默认"
        data-test="sidebar-resizer"
        @pointerdown="onSidebarResizerDown"
        @dblclick="resetSidebarWidth"
      ></div>

      <div class="xy-main-card">
      <div id="center">
        <div id="chat-header" data-theme-region="conversation_toolbar">
          <span
            class="crumb"
            :class="{ 'is-mobile-clickable': isMobile }"
            :role="isMobile ? 'button' : undefined"
            :tabindex="isMobile ? 0 : undefined"
            :title="isMobile ? '切换或新建会话' : undefined"
            @click="isMobile && store.toggleSidebar()"
            @keydown.enter.prevent="isMobile && store.toggleSidebar()"
          >
            <span class="crumb-prefix">会话</span>
            <span class="crumb-sep">/</span>
            <b class="crumb-name">{{ store.currentSessionDisplayName }}</b>
            <AppIcon
              v-if="isMobile"
              name="chevron-down"
              :size="13"
              :stroke-width="2"
              class="crumb-mobile-caret"
            />
            <span v-if="showHeaderWorkdirPill" class="crumb-workdir">{{ store.currentWorkdirLabel }}</span>
          </span>
          <span class="top-spacer"></span>

          <BackgroundTasksBar
            :open="bgTasksOpen"
            @toggle="toggleBgTasks"
            @close="bgTasksOpen = false"
          />

          <div v-if="taskEntry.visible" class="plan-entry-wrap">
            <button
              class="chip-btn task-entry"
              :class="{ active: taskDialogOpen }"
              :aria-expanded="taskDialogOpen"
              data-test="task-entry"
              title="查看星链执行进度"
              @click.stop="toggleTaskDialog"
            >
              <AppIcon
                :name="taskEntry.activeCount > 0 ? 'loader' : 'layers'"
                :size="12"
                :stroke-width="2"
                :class="{ 'task-entry-spin': taskEntry.activeCount > 0 }"
              /><span class="chip-label">星链</span>
              <span v-if="taskEntry.activeCount > 0" class="task-entry-badge" data-test="task-entry-badge">{{ taskEntry.activeCount }}</span>
            </button>
            <TaskProgressDialog v-if="taskDialogOpen" @close="taskDialogOpen = false" />
          </div>

          <div v-if="store.decisionHasRecords" class="decision-entry-wrap">
            <button
              class="chip-btn"
              :class="{ active: decisionPanelOpen }"
              :aria-expanded="decisionPanelOpen"
              data-test="decision-entry"
              title="查看 JEV 决策记录"
              @click.stop="toggleDecisionPanel"
            ><AppIcon name="brain" :size="12" :stroke-width="2" /><span class="chip-label">决策</span><span v-if="store.decisionUnread > 0" class="decision-entry-badge" data-test="decision-entry-badge">{{ store.decisionUnread > 99 ? '99+' : store.decisionUnread }}</span></button>
            <DecisionRecordPopup v-if="decisionPanelOpen" @close="decisionPanelOpen = false" />
          </div>

          <div v-if="showExportEntry" class="export-wrap">
            <button
              class="chip-btn"
              :class="{ active: showExportMenu }"
              :aria-expanded="showExportMenu"
              title="导出会话"
              @click.stop="toggleExportMenu"
            ><AppIcon name="download" :size="13" :stroke-width="1.8" /><span class="chip-label">导出</span></button>
            <div v-if="showExportMenu" class="export-menu" @click.stop>
              <div class="export-item" @click="doExport('md')">Markdown (.md)</div>
              <div class="export-item" @click="doExport('html')">HTML (.html)</div>
              <div class="export-item" @click="doExport('json')">JSON (.json)</div>
            </div>
          </div>

          <button
            v-if="gitBranchLabel"
            data-test="chat-header-git-branch"
            class="chip-btn git-branch-btn"
            title="Git 工作台：点击打开 Git 管理面板"
            @click.stop="openGitPanel"
          ><AppIcon name="git-branch" :size="12" :stroke-width="2" /><span class="git-branch-label">{{ gitBranchLabel }}</span></button>
        </div>

        <ComputerControlBar />

        <GoalBar />

        <div class="chat-viewport">
          <div
            ref="chatScrollRef"
            id="chat-scroll"
            data-theme-region="workspace"
            @scroll="onChatScroll"
            @wheel="onChatWheel"
            @touchstart="onChatTouchStart"
            @touchmove="onChatTouchMove"
            @touchend="onChatTouchEnd"
            @pointerdown="onChatPointerDown"
          >
          <div id="chat-body">
            <div v-if="store.historyLoading" class="history-loading" data-test="history-loading">正在加载更早的消息…</div>
            <KimiEmptyState v-if="(!store.messages || store.messages.length === 0) && !store.sessionSwitching" />
            <div v-else-if="store.sessionSwitching" class="session-switching" data-test="session-switching">
              <span class="ss-ring"></span>
              <span>正在加载会话…</span>
            </div>

            <template v-else>
              <TurnGroup
                v-for="(group, gIdx) in messageGroups"
                :key="groupKey(group)"
                :group="group"
                :running-elapsed-text="group.running && workElapsed >= 1 ? fmtWorkTime(workElapsed) : ''"
                :final-work-time-text="gIdx === messageGroups.length - 1 && !group.running && finalWorkTime !== null ? fmtWorkTime(finalWorkTime) : ''"
              />
            </template>

            <PermCard />

            <HookTrustCard />
          </div>
          </div>

          <MessageNavRail
            ref="railRef"
            :get-scroll-el="() => chatScrollRef"
            :get-user-msg-elements="getUserMsgElements"
            :get-user-msg-element-by-id="getUserMsgElementById"
            :nav-items="store.navItems"
            :loaded-first-id="store.firstLoadedMsgId"
            @jump="onNavJump"
            @jump-start="onRailJumpStart"
          />

          <button
            v-if="showJumpButton"
            class="jump-bottom"
            data-test="jump-bottom"
            title="回到底部"
            @click="forceScrollToBottom"
          >
            <AppIcon name="chevron-down" :size="16" :stroke-width="2.25" />
          </button>
        </div>

        <Subpage
          :visible="store.subpage.show"
          :sub-type="store.subpage.type"
          @close="store.closeSubpage()"
        />

        <InputArea v-if="store.messages && store.messages.length > 0" />

        <TerminalPane v-show="store.terminalPanelOpen" />

        <div id="statusbar" data-theme-region="statusbar">
          <span
            class="si status-connection"
            :class="{ 'is-connected': store.connected }"
            data-test="status-connection"
            data-mobile-status="primary"
          >
            <span class="s-dot" :class="{ off: !store.connected }"></span>
            <span class="conn-full">{{ store.connected ? '后端已连接' : '连接已断开' }}</span>
            <span class="conn-short">{{ store.connected ? '已连接' : '已断开' }}</span>
          </span>
          <span class="si status-model" data-test="status-model" data-mobile-status="primary" :title="'当前模型: ' + (store.activeModelName || '未选择')">{{ store.activeModelName || '未选择模型' }}</span>
          <span class="si status-workspace" data-test="status-workspace" data-mobile-status="primary" :title="'当前工作区: ' + (store.workdir || store.currentWorkdirLabel)">{{ store.currentWorkdirLabel }}</span>
          <span
            v-if="gitBranchText"
            class="si status-git-branch"
            data-test="status-git-branch"
            data-mobile-status="hidden"
            :title="'Git 工作台: ' + gitBranchText + '（点击打开 Git 面板）'"
            style="cursor: pointer"
            @click="openGitPanel"
          >{{ gitBranchText }}</span>
          <span class="si status-token" data-test="status-token" data-mobile-status="hidden" :class="statusTokenClass" title="上下文 Token 占用百分比">Token {{ statusTokenPct }}%</span>
          <span class="si status-permission" data-test="status-permission" data-mobile-status="hidden">权限: <span>{{ formatPermMode(store.permissionMode) }}</span></span>
          <span
            class="si status-skill"
            :class="{ 'is-empty': !store.usedSkills.length }"
            data-test="status-skill"
            data-mobile-status="hidden"
            :title="statusSkillTitle"
          >
            <span class="skill-text">技能: {{ statusSkillText }}</span>
          </span>
          <span
            class="si status-sandbox"
            data-test="status-sandbox"
            data-mobile-status="primary"
            :class="{ 'si-warn': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅允许访问工作目录内文件'"
          >沙盒: {{ store.sandboxEnabled ? '解除' : '限制' }}</span>
          <span
            class="si status-ai-notice"
            data-test="status-ai-notice"
            data-mobile-status="hidden"
            title="本界面展示的内容由 AI 生成，仅供参考，请仔细甄别"
          ><span class="notice-text">内容由 AI 生成，仅供参考，请仔细甄别</span></span>
          <div class="right status-runtime" data-test="status-runtime">
            <span class="si" data-test="status-running" data-mobile-status="primary">{{ statusRunningText }}</span>
            <span class="si" data-test="status-charset" data-mobile-status="hidden">UTF-8</span>
          </div>
        </div>
      </div>

      <div
        v-if="!isMobile && !store.rightPanelCollapsed"
        class="panel-resizer panel-resizer-inner"
        :class="{ resizing: rightPanelResizing }"
        title="拖拽调整面板宽度，双击恢复默认"
        data-test="right-panel-resizer"
        @pointerdown="onRightPanelResizerDown"
        @dblclick="onResetRightPanelWidth"
      ></div>

      <!-- 右侧控制面板：必须用 v-show 而不是 v-if——卸载会重建组件，它内部的原生
           WebView2 表面来不及 hide 就会以白块形式留在屏幕上。
           外层 Transition 负责拖住 display:none 直到滑入/滑出动画跑完：v-show 单独
           使用时 display 和 margin 在同一帧切换，负 margin 的过渡根本来不及生效 -->
      <Transition name="right-panel-slide">
        <RightPanel v-show="!store.rightPanelCollapsed" />
      </Transition>
      </div>
      </template>
    </div>

    <ConfirmModal />

    <UpdateModal />

    <SessionSearchModal />

    <DisclaimerGate v-if="store.initialized && store.disclaimerNeedsAccept" />

    <ImageLightbox />

    <RemoteDirBrowser />

    <Transition name="splash">
      <div v-if="!store.initialized" id="splash">
        <img class="sp-logo" :src="'/icon.png'" alt="星瑶Code">
        <div class="sp-title">星瑶Code</div>
        <div class="sp-sub">{{ store.agentName }}·编码助手</div>
        <div class="sp-bar"><i></i></div>
      </div>
    </Transition>

    <div v-if="store.closeConfirm.show" class="close-confirm-mask" data-test="close-confirm">
      <div class="close-confirm-card">
        <h3>有对话回合仍在执行</h3>
        <p>关闭窗口前，请选择如何处理正在运行的任务。</p>
        <div class="cc-actions">
          <button v-if="store.closeConfirm.trayAvailable" class="cc-btn" data-test="cc-background" @click="store.resolveCloseConfirm('background')">
            最小化到托盘继续
          </button>
          <button class="cc-btn danger" data-test="cc-exit" @click="store.resolveCloseConfirm('exit')">
            停止任务并退出
          </button>
          <button class="cc-btn ghost" data-test="cc-cancel" @click="store.resolveCloseConfirm('cancel')">取消</button>
        </div>
      </div>
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed, watch } from 'vue';
import { useAppStore } from './stores/appStore';
import { useGitStore } from './stores/gitStore';
import { formatPermMode } from './utils/markdown';
import { buildMessageGroups, msgKey, type TurnGroupInfo } from './utils/turnGroups';
import { useChatScroll } from './composables/useChatScroll';
import { useIsMobile } from './composables/useViewport';
import { chatLayoutWidth } from './composables/chatLayoutWidth';
import {
  pickWideDefaultTab,
  WIDE_SPLIT_MIN_VIEWPORT_WIDTH,
  wideRightPanelDefaultWidth,
} from './composables/wideSplitPanel';
import {
  RIGHT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_MAX_WIDTH,
  RIGHT_PANEL_MIN_WIDTH,
  RIGHT_PANEL_WIDTH_KEY,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_WIDTH_KEY,
  usePanelResize,
} from './composables/usePanelResize';

import Topbar from './components/Topbar.vue';
import AppIcon from './components/AppIcon.vue';
import MessageNavRail from './components/MessageNavRail.vue';
import Sidebar from './components/Sidebar.vue';
import KimiEmptyState from './components/KimiEmptyState.vue';
import TurnGroup from './components/TurnGroup.vue';
import InputArea from './components/InputArea.vue';
import RightPanel from './components/RightPanel.vue';
import TerminalPane from './components/TerminalPane.vue';
import ComputerControlBar from './components/ComputerControlBar.vue';
import GoalBar from './components/GoalBar.vue';
import ConfirmModal from './components/ConfirmModal.vue';
import UpdateModal from './components/UpdateModal.vue';
import SessionSearchModal from './components/SessionSearchModal.vue';
import TaskProgressDialog from './components/TaskProgressDialog.vue';
import DecisionRecordPopup from './components/DecisionRecordPopup.vue';
import BackgroundTasksBar from './components/BackgroundTasksBar.vue';
import { taskEntryState } from './utils/taskEntry';
import DisclaimerGate from './components/DisclaimerGate.vue';
import PermCard from './components/PermCard.vue';
import HookTrustCard from './components/HookTrustCard.vue';
import ImageLightbox from './components/ImageLightbox.vue';
import RemoteDirBrowser from './components/RemoteDirBrowser.vue';
import Subpage from './components/Subpage.vue';
import SettingsPage from './components/SettingsPage.vue';

type WindowResizeEdge =
  | 'left' | 'right' | 'top' | 'bottom'
  | 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right';

const WINDOW_RESIZE_EDGES: WindowResizeEdge[] = [
  'left', 'right', 'top', 'bottom',
  'top_left', 'top_right', 'bottom_left', 'bottom_right',
];

function beginWindowResize(edge: WindowResizeEdge) {
  void window.pywebview?.api?.window_begin_resize?.(edge);
}

// 最大化状态从 store 读取（后端通过 window_maximize_state 事件推送），
// 本地不再维护独立 ref 和事件监听。启动时仍需向宿主查询一次初始状态：
// window_maximize_state 只覆盖启动后的切换，不能表示窗口以最大化状态启动。
const windowMaximized = computed(() => store.windowMaximized);

async function syncInitialWindowMaximized() {
  try {
    const result = await window.pywebview?.api?.window_is_maximized?.();
    if (result?.ok) store.windowMaximized = !!result.maximized;
  } catch {
  }
}

function onWindowFocusRecalibrateComputer() {
  if (store.computerRun.run_id) void store.refreshComputerRun();
}

const COMPUTER_CALIBRATION_INTERVAL_MS = 2000;
let computerCalibTimer: number | undefined;
function stopComputerCalibrationTimer() {
  if (computerCalibTimer !== undefined) {
    clearInterval(computerCalibTimer);
    computerCalibTimer = undefined;
  }
}

const store = useAppStore();

watch(
  () => store.computerControlBarVisible && store.computerRun.status !== 'failed',
  (active) => {
    stopComputerCalibrationTimer();
    if (!active) return;
    computerCalibTimer = window.setInterval(() => {
      if ((window as any).pywebview?.api?.get_computer_control_status) {
        void store.refreshComputerRun();
      }
    }, COMPUTER_CALIBRATION_INTERVAL_MS);
  },
  { immediate: true },
);

const isMobile = useIsMobile();
const {
  isResizing: sidebarResizing,
  onHandlePointerdown: onSidebarResizerDown,
  resetWidth: resetSidebarWidth,
} = usePanelResize({
  direction: 'left',
  storageKey: SIDEBAR_WIDTH_KEY,
  minWidth: SIDEBAR_MIN_WIDTH,
  maxWidth: SIDEBAR_MAX_WIDTH,
  defaultWidth: SIDEBAR_DEFAULT_WIDTH,
  getWidth: () => store.sidebarWidth,
  setWidth: (w) => { store.sidebarWidth = w; },
});
const {
  isResizing: rightPanelResizing,
  onHandlePointerdown: onRightPanelResizerDown,
  resetWidth: resetRightPanelWidth,
} = usePanelResize({
  direction: 'right',
  storageKey: RIGHT_PANEL_WIDTH_KEY,
  minWidth: RIGHT_PANEL_MIN_WIDTH,
  maxWidth: RIGHT_PANEL_MAX_WIDTH,
  defaultWidth: RIGHT_PANEL_DEFAULT_WIDTH,
  getWidth: () => store.rightPanelWidth,
  setWidth: (w) => { store.rightPanelWidth = w; },
  viewportRatio: true,
});
const anyPanelResizing = computed(() => sidebarResizing.value || rightPanelResizing.value);

const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1024);
function onViewportResize() {
  viewportWidth.value = window.innerWidth;
}
function hasCustomRightPanelWidth(): boolean {
  try {
    return localStorage.getItem(RIGHT_PANEL_WIDTH_KEY) !== null;
  } catch {
    return false;
  }
}
const wideSplitActive = computed(
  () => !isMobile.value
    && store.initialized
    && !store.settingsOpen
    && chatLayoutWidth.value === 'wide'
    && viewportWidth.value >= WIDE_SPLIT_MIN_VIEWPORT_WIDTH,
);
function syncWideSplitLayout() {
  if (!hasCustomRightPanelWidth()) {
    store.rightPanelWidth = wideSplitActive.value
      ? wideRightPanelDefaultWidth(viewportWidth.value)
      : RIGHT_PANEL_DEFAULT_WIDTH;
  }
  if (!wideSplitActive.value) {
    store.leaveWideSplit();
    return;
  }
  const tab = pickWideDefaultTab({
    workdir: store.workdir,
    bgTaskCount: store.currentSessionBackgroundTasks.length,
    subagentTaskCount: store.subagentTasks.length,
  });
  if (store.rightPanelCollapsed) {
    store.applyWideSplitExpand(tab);
  } else if (tab && store.rightPanelTabs.length === 0 && store.rightPanelPicker) {
    const wasAuto = store.wideSplitAutoExpanded;
    store.openRightPanelTab(tab);
    store.wideSplitAutoExpanded = wasAuto;
  }
}
function onResetRightPanelWidth() {
  resetRightPanelWidth();
  if (wideSplitActive.value) {
    store.rightPanelWidth = wideRightPanelDefaultWidth(viewportWidth.value);
  }
}
watch(
  [
    wideSplitActive,
    viewportWidth,
    () => store.currentSessionName,
    () => store.workdir,
    () => store.currentSessionBackgroundTasks.length,
    () => store.subagentTasks.length,
  ],
  () => { syncWideSplitLayout(); },
  { immediate: true },
);

const chatScrollRef = ref<HTMLElement | null>(null);

function backToThemeEditor() {
  store.themePreviewMode = false;
  store.settingsSection = 'appearance';
  store.settingsOpen = true;
}

const showExportMenu = ref(false);
const showExportEntry = computed(() => store.messages.length > 0 || !!store.currentSessionName);
const showHeaderWorkdirPill = computed(() =>
  !!store.workdir && !!store.currentWorkdirLabel
  && store.currentWorkdirLabel !== '对话'
  && store.currentWorkdirLabel !== 'GLOBAL',
);

const taskDialogOpen = ref(false);
const decisionPanelOpen = ref(false);
const bgTasksOpen = ref(false);
const taskEntry = computed(() => taskEntryState({ plan: store.planData }));

async function doExport(fmt: 'md' | 'html' | 'json') {
  showExportMenu.value = false;
  await store.exportSession(fmt);
}

function toggleBgTasks() {
  const next = !bgTasksOpen.value;
  closeHeaderPopovers();
  bgTasksOpen.value = next;
}

function toggleTaskDialog() {
  const next = !taskDialogOpen.value;
  closeHeaderPopovers();
  taskDialogOpen.value = next;
}

function toggleDecisionPanel() {
  const next = !decisionPanelOpen.value;
  closeHeaderPopovers();
  decisionPanelOpen.value = next;
}

function toggleExportMenu() {
  const next = !showExportMenu.value;
  closeHeaderPopovers();
  showExportMenu.value = next;
}

// 文档级点击统一收起头部悬浮层（导出菜单/计划/决策/后台任务面板）；
// 按钮与面板内部用 @click.stop 挡泡，避免点开即关
function closeHeaderPopovers() {
  showExportMenu.value = false;
  taskDialogOpen.value = false;
  decisionPanelOpen.value = false;
  bgTasksOpen.value = false;
}

const statusTokenPct = computed(() => {
  const w = store.tokenUsage.window || 0;
  if (!w) return 0;
  return Math.round((store.tokenUsage.used / w) * 100);
});

const gitStore = useGitStore();
const gitBranchText = computed(() => {
  if (!gitStore.snapshot && !gitStore.error) return '';
  if (gitStore.error) return '';
  const base = gitStore.statusbarText;
  return base || '';
});
const gitBranchLabel = computed(() => {
  const text = gitStore.statusbarText;
  return text ? text.slice(2) : '';
});
function openGitPanel() {
  // 非 Git 项目点击不展开（面板内已有"非仓库"提示态），避免空操作困惑
  gitStore.requestOpenPanel();
  store.openRightPanelTab('git');
}
watch(
  () => store.workdir,
  (wd) => {
    if (wd) void gitStore.ensureLoaded(wd);
    else void gitStore.$reset();
  },
  { immediate: false }
);
const statusTokenClass = computed(() => {
  if (statusTokenPct.value > 90) return 'tok-red';
  if (statusTokenPct.value > 70) return 'tok-orange';
  return '';
});

// 状态栏"技能"项：显示本回合实际用到的技能名（来自 is_skill 工具调用收集），
// 回合结束后固化；未使用任何技能时显示"无"，让"很少触发"从感觉变成可见数字
// 技能较多时只显示装得下的部分并以省略号结尾（完整列表见 title），
// 避免长串技能名把状态栏挤压换行；CSS 侧另有单行省略号兜底
const STATUS_SKILL_CHAR_BUDGET = 48;
const statusSkillText = computed(() => {
  const list = store.usedSkills;
  if (!list.length) return '无';
  let text = '';
  for (const name of list) {
    const next = text ? `${text}、${name}` : name;
    if (text && next.length > STATUS_SKILL_CHAR_BUDGET) return text + '…';
    text = next;
  }
  return text;
});
const statusSkillTitle = computed(() =>
  store.usedSkills.length
    ? '本回合使用的技能：' + store.usedSkills.join('、')
    : '本回合未使用任何技能',
);

function onRailJumpStart() {
  commitFollowing(false);
}

function forceScrollToBottom() {
  scrollToBottom(true);
  railRef.value?.cancelJump?.();
}

const {
  scrollToBottom,
  handleScroll,
  syncStick,
  showJumpButton,
  noteUserScrollIntent,
  commitFollowing,
} = useChatScroll(() => chatScrollRef.value);

function onChatScroll() {
  if (handleScroll()) void maybeLoadOlder();
}

function onChatWheel(e: WheelEvent) {
  noteUserScrollIntent(e.deltaY < 0 ? 'awayFromBottom' : e.deltaY > 0 ? 'towardBottom' : 'none');
}
let lastTouchY: number | null = null;
function onChatTouchStart(e: TouchEvent) {
  lastTouchY = e.touches[0]?.clientY ?? null;
}
function onChatTouchMove(e: TouchEvent) {
  const y = e.touches[0]?.clientY;
  if (y == null || lastTouchY == null) {
    noteUserScrollIntent('unknown');
    return;
  }
  noteUserScrollIntent(y > lastTouchY ? 'awayFromBottom' : y < lastTouchY ? 'towardBottom' : 'none');
  lastTouchY = y;
}
function onChatTouchEnd() {
  lastTouchY = null;
}
function onChatPointerDown(e: PointerEvent) {
  // 命中滚动容器自身（滚动条/空白）：方向未知，随后由真实 scroll 落点裁决。
  // 滚动条拖动/中键自动滚动不产生 wheel/touchmove——这里直接按「用户接管」
  // 处理：取消进行中的跳转校正，防止校正把视口从用户拖到的位置拉回目标
  if (e.target === chatScrollRef.value) {
    noteUserScrollIntent('unknown');
    railRef.value?.cancelJump?.();
  }
}
const LOAD_OLDER_MAX_FRAMES = 30;

async function maybeLoadOlder() {
  const el = chatScrollRef.value;
  if (!el || el.scrollTop >= 120) return;
  // 调用方已保证本次是「用户滚动」来源；跳转/补偿等程序化滚动不会走到这里
  // 守卫与 store 的翻页守卫同源：has_more=false 但游标为空 = 分页状态未初始化/
  // 被失败响应清空，此时仍要进 store 走一次自愈 + 真实翻页，否则滚到顶永远不加载
  if (!(store.historyHasMore || store.historyOldestId == null) || store.historyLoading) return;
  const oldHeight = el.scrollHeight;
  const oldTop = el.scrollTop;
  // 用户接管检测：校正期间用户若主动滚动就立即让位。触发翻页那一瞬的惯性
  // 残留（前 80ms 的输入事件）不算接管，否则刚滚到顶就会立刻放弃校正。
  const startedAt = performance.now();
  let userTookOver = false;
  const markUserTookOver = () => {
    if (performance.now() - startedAt > 80) userTookOver = true;
  };
  const markPointerTookOver = (ev: PointerEvent) => {
    if (ev.target === el) markUserTookOver();
  };
  el.addEventListener('wheel', markUserTookOver, { passive: true });
  el.addEventListener('touchmove', markUserTookOver, { passive: true });
  el.addEventListener('keydown', markUserTookOver);
  el.addEventListener('pointerdown', markPointerTookOver);
  try {
    await store.loadOlderMessages();
    // 绝对定位补偿（幂等）：anchoring 生效时此赋值与浏览器结果一致，不会叠加；
    // anchoring 失效（锚点元素被移除、变化幅度超阈值）时由它兜底保持视口。
    // 关键：长对话的一页旧消息常含图片/代码高亮等异步内容，高度分多批撑开，
    // 单帧补偿只会停在「第一批」的中间值上，表现为视口相对内容下移（用户反馈的
    // "懒加载时回退到下面"）。因此按帧反复补偿，直到高度连续两帧不再变化。
    let lastHeight = -1;
    let stableFrames = 0;
    for (let frame = 0; frame < LOAD_OLDER_MAX_FRAMES; frame++) {
      await nextTick();
      if (userTookOver || chatScrollRef.value !== el) return;
      el.scrollTop = el.scrollHeight - oldHeight + oldTop;
      if (el.scrollHeight === lastHeight) {
        if (++stableFrames >= 2) return;
      } else {
        stableFrames = 0;
        lastHeight = el.scrollHeight;
      }
      await new Promise<void>((resolve) => {
        window.requestAnimationFrame(() => resolve());
      });
    }
  } finally {
    el.removeEventListener('wheel', markUserTookOver);
    el.removeEventListener('touchmove', markUserTookOver);
    el.removeEventListener('keydown', markUserTookOver);
    el.removeEventListener('pointerdown', markPointerTookOver);
  }
}

let navJumpSeq = 0;
async function onNavJump(payload: { index: number; id: number }) {
  const seq = ++navJumpSeq;
  commitFollowing(false);
  const el = chatScrollRef.value;
  const sessionAtJump = store.currentSessionName;
  let guard = 0;
  while (guard++ < 60) {
    if (seq !== navJumpSeq) return;
    if (store.currentSessionName !== sessionAtJump) return;
    const first = store.firstLoadedMsgId;
    if (first !== null && payload.id >= first) break;
    const cursorBefore = store.historyOldestId;
    const oldHeight = el?.scrollHeight ?? 0;
    const oldTop = el?.scrollTop ?? 0;
    // force=true：入口不信任 historyHasMore（该标记随会话 runtime 保存/恢复，
    // 可能是坏值）——否则跨页跳转在这里就被拦死，表现为点未加载红点没反应
    await store.loadOlderMessages(true);
    await nextTick();
    if (!el || chatScrollRef.value !== el || store.currentSessionName !== sessionAtJump) return;
    if (seq !== navJumpSeq) return;
    el.scrollTop = el.scrollHeight - oldHeight + oldTop;
    if (store.historyOldestId === cursorBefore && store.firstLoadedMsgId === first) break;
    await new Promise<void>((resolve) => {
      window.requestAnimationFrame(() => resolve());
    });
  }
  if (seq !== navJumpSeq) return;
  await nextTick();
  // 交回定位时带目标 id：rail 按 id 精确查找目标气泡（索引换算兜底），
  // 翻页边界对齐异常时也不会把落点换到邻近消息上
  railRef.value?.jumpToLoaded?.(payload.index, payload.id);
}

const railRef = ref<InstanceType<typeof MessageNavRail> | null>(null);
function refreshRail() {
  railRef.value?.refresh?.();
}
function getUserMsgElements(): HTMLElement[] {
  return Array.from(chatScrollRef.value?.querySelectorAll('.msg-user') ?? []);
}
function getUserMsgElementById(id: number): HTMLElement | null {
  return chatScrollRef.value?.querySelector(`.msg-user[data-msg-id="${id}"]`) ?? null;
}

// 新用户消息（发送）→ 强制回底并恢复跟随；其余消息变化仅在跟随模式下跟随。
// 仅当消息流「尾部」出现新 user 消息（= 用户刚发送）才强制吸底：历史翻页
// prepend 只动头部，尾部消息对象不变（即使尾部恰好就是 user 消息、user 消息
// 总数增加），因此这里按「尾部对象身份」判定，绝不误触发——也就不再需要依赖
// historyLoading 的时序（不再需要 flush:'sync' 的同步守卫）。
// 约束：该判定依赖 appStore.loadOlderMessages 的 prepend 保持既有消息对象身份
//（数组前插、不重建对象）。若未来 store 改为重建消息对象，此判定与
// App.historyPaged.test.ts 的「prepend 不得吸底」用例需同步调整。
const userMsgCount = computed(() => store.messages.filter((m) => m.role === 'user').length);
let lastTailMsg: unknown = store.messages.length ? store.messages[store.messages.length - 1] : null;
watch(userMsgCount, (n, o) => {
  const tail = store.messages[store.messages.length - 1] as { role?: string } | undefined;
  const tailIsNew = tail !== undefined && tail !== lastTailMsg;
  lastTailMsg = tail;
  if (n <= o || !tailIsNew) return;
  if (tail?.role !== 'user') return;
  forceScrollToBottom();
});

const messageGroups = computed(() => buildMessageGroups(store.messages));

function groupKey(group: TurnGroupInfo): number {
  return msgKey(group.userMsg ?? group.middle[0] ?? group.tail[0]);
}

let bodyObserver: MutationObserver | undefined;
let observedBody: HTMLElement | null = null;

function ensureBodyObserver() {
  nextTick(() => {
    const body = document.getElementById('chat-body');
    if (!body) return;
    if (observedBody === body) return;
    bodyObserver?.disconnect();
    observedBody = body;
    bodyObserver = new MutationObserver(() => {
      scrollToBottom();
      refreshRail();
    });
    bodyObserver.observe(body, { childList: true, subtree: true, characterData: true, attributes: true });
  });
}

watch(() => store.messages.length, () => {
  scrollToBottom();
  ensureBodyObserver();
  refreshRail();
});
watch(() => store.currentStreamingText, () => {
  scrollToBottom();
});
watch(() => store.settingsOpen, (open) => {
  if (!open) {
    forceScrollToBottom();
    ensureBodyObserver();
    nextTick(() => {
      syncStick();
      refreshRail();
    });
  }
});

const workElapsed = ref(0);
const finalWorkTime = ref<number | null>(null);
let workTimer: number | undefined;
let turnStartTs = 0;

function fmtWorkTime(s: number): string {
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

const statusRunningText = computed(() => {
  if (!store.running) return '空闲';
  return workElapsed.value >= 1 ? `执行中 · ${fmtWorkTime(workElapsed.value)}` : '执行中';
});

watch(() => store.running, (running) => {
  if (running) {
    turnStartTs = store.turnStartedAt || Date.now();
    workElapsed.value = store.turnStartedAt
      ? Math.floor((Date.now() - store.turnStartedAt) / 1000)
      : 0;
    finalWorkTime.value = null;
    window.clearInterval(workTimer);
    workTimer = window.setInterval(() => {
      workElapsed.value = Math.floor((Date.now() - turnStartTs) / 1000);
    }, 1000);
  } else {
    window.clearInterval(workTimer);
    workTimer = undefined;
    if (turnStartTs) {
      finalWorkTime.value = Math.max(0, Math.floor((Date.now() - turnStartTs) / 1000));
      turnStartTs = 0;
    }
  }
  scrollToBottom();
});

watch(() => store.currentSessionName, () => {
  finalWorkTime.value = null;
  forceScrollToBottom();
  nextTick(refreshRail);
  if (store.running) {
    turnStartTs = store.turnStartedAt || Date.now();
    workElapsed.value = store.turnStartedAt
      ? Math.floor((Date.now() - store.turnStartedAt) / 1000)
      : 0;
    window.clearInterval(workTimer);
    workTimer = window.setInterval(() => {
      workElapsed.value = Math.floor((Date.now() - turnStartTs) / 1000);
    }, 1000);
  } else {
    window.clearInterval(workTimer);
    workTimer = undefined;
    turnStartTs = 0;
  }
});
watch(() => store.messages.length, (len) => {
  if (len === 0) finalWorkTime.value = null;
});

onUnmounted(() => {
  window.clearInterval(workTimer);
});
watch(() => store.permRequest.show, () => {
  forceScrollToBottom();
});
// M25 修复：pywebviewready 事件与 100ms 兜底轮询是双通道，原先无互斥，
// initFromBackend 可能执行两次互相覆盖；且事件监听器注册后从不移除。
// initStarted 互斥标志保证只初始化一次，触发后移除监听并停轮询。
let initStarted = false;
let initPollTimer: number | undefined;
// 判断 pywebview JS 桥是否真正就绪。pywebview 分两步注入：先注入 api.js
// （此时 window.pywebview.api 只是空对象 {}），再注入 finish.js 才把
// get_initial_state 等方法填进去并触发 pywebviewready。若在第一步完成后
// 就抢跑调用，桥通道尚未就绪，postMessage 消息丢失、Promise 永不 settle，
// 表现为启动画面一直转圈。因此必须等到方法真正注入完成才算就绪。
function pywebviewApiReady(): boolean {
  const api = window.pywebview && window.pywebview.api;
  return !!(api && typeof api.get_initial_state === 'function');
}
function startInit() {
  if (initStarted) return;
  initStarted = true;
  window.removeEventListener('pywebviewready', onPywebviewReady);
  if (initPollTimer !== undefined) {
    clearInterval(initPollTimer);
    initPollTimer = undefined;
  }
  store.initFromBackend();
  void store.refreshDecisionAvailability();
}
function onPywebviewReady() {
  startInit();
}

function onGlobalKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault();
    if (store.sessionSearchModal.show) {
      store.closeSessionSearchModal();
    } else {
      store.openSessionSearchModal();
    }
    return;
  }
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const isScrollKey = e.key === 'ArrowUp' || e.key === 'PageUp' || e.key === 'Home'
    || e.key === 'ArrowDown' || e.key === 'PageDown' || e.key === 'End' || e.key === ' ';
  if (!isScrollKey) return;
  // 键盘滚动同样是「用户接管」：取消进行中的跳转校正（与 wheel/touchmove 口径一致），
  // 防止跳转检查点把视口从用户刚滚到的位置拉回目标
  railRef.value?.cancelJump?.();
  if (e.key === 'ArrowUp' || e.key === 'PageUp' || e.key === 'Home') {
    noteUserScrollIntent('awayFromBottom');
  } else if (e.key === 'ArrowDown' || e.key === 'PageDown' || e.key === 'End') {
    noteUserScrollIntent('towardBottom');
  } else if (e.key === ' ') {
    noteUserScrollIntent(e.shiftKey ? 'awayFromBottom' : 'towardBottom');
  }
}

let bgTaskPollTimer: number | undefined;

onMounted(() => {
  onViewportResize();
  window.addEventListener('resize', onViewportResize);
  document.addEventListener('click', closeHeaderPopovers);
  window.addEventListener('keydown', onGlobalKeydown);
  void syncInitialWindowMaximized();
  void store.refreshBackgroundTasks();
  bgTaskPollTimer = window.setInterval(() => {
    void store.refreshBackgroundTasks();
  }, 5000);

  // 窗口重新聚焦时校准电脑操控快照（设计 16.4）：_js_queue 满时允许丢新事件，
  // 终态不能只依赖事件推送，聚焦即用 get_computer_control_status() 复核
  window.addEventListener('focus', onWindowFocusRecalibrateComputer);

  if (pywebviewApiReady()) {
    startInit();
  } else {
    window.addEventListener('pywebviewready', onPywebviewReady);
    // 兜底轮询监听 PyWebView ready 注入
    let attempts = 0;
    initPollTimer = window.setInterval(() => {
      attempts++;
      if (pywebviewApiReady()) {
        startInit();
      } else if (attempts > 300) {
        clearInterval(initPollTimer);
        initPollTimer = undefined;
        window.removeEventListener('pywebviewready', onPywebviewReady);
        startInit();
      }
    }, 100);
  }

  // 首次挂载后注册 DOM 观察器（chat-body 可能尚未渲染，由 nextTick 兜底）
  ensureBodyObserver();
});
onUnmounted(() => {
  window.removeEventListener('resize', onViewportResize);
  document.removeEventListener('click', closeHeaderPopovers);
  clearInterval(bgTaskPollTimer);
  stopComputerCalibrationTimer();
  window.removeEventListener('keydown', onGlobalKeydown);
  window.removeEventListener('focus', onWindowFocusRecalibrateComputer);
  window.removeEventListener('pywebviewready', onPywebviewReady);
  if (initPollTimer !== undefined) {
    clearInterval(initPollTimer);
    initPollTimer = undefined;
  }
  bodyObserver?.disconnect();
  bodyObserver = undefined;
  observedBody = null;
});
</script>

<style scoped>
.close-confirm-mask {
  position: fixed;
  inset: 0;
  z-index: 5000;
  display: grid;
  place-items: center;
  padding: 16px;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}
.close-confirm-card {
  width: min(416px, calc(100vw - 32px));
  padding: 22px 22px 20px;
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  background: var(--xy-card, var(--bg1));
  box-shadow: var(--xy-shadow-pop, 0 16px 40px rgba(0, 0, 0, 0.35));
}
.close-confirm-card h3 { margin: 0 0 6px; font-size: 15.5px; font-weight: 600; color: var(--xy-text, var(--text-bright)); }
.close-confirm-card p { margin: 0 0 18px; color: var(--xy-text-2, var(--text-dim)); font-size: 13px; line-height: 1.6; }
.cc-actions { display: flex; flex-direction: column; gap: 8px; }
.cc-btn {
  min-height: 36px;
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 9px;
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text, var(--text));
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease;
}
.cc-btn:hover { background: var(--xy-hover, var(--bg3)); border-color: var(--xy-accent, var(--accent)); }
.cc-btn.danger {
  color: var(--xy-rose, var(--red));
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 35%, var(--xy-border, var(--border)));
  background: var(--xy-rose-soft, transparent);
}
.cc-btn.danger:hover {
  background: color-mix(in srgb, var(--xy-rose, var(--red)) 18%, transparent);
  border-color: var(--xy-rose, var(--red));
}
.cc-btn.ghost { background: transparent; border-color: transparent; color: var(--xy-text-2, var(--text-dim)); }
.cc-btn.ghost:hover { background: var(--xy-hover, var(--bg2)); border-color: transparent; color: var(--xy-text, var(--text)); }
.theme-preview-bar {
  position: sticky;
  top: 0;
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 7px 14px;
  background: var(--accent);
  color: #fff;
  font-size: 12.5px;
  flex-shrink: 0;
}
.tpb-btn {
  border: 1px solid rgba(255, 255, 255, 0.5);
  background: rgba(255, 255, 255, 0.16);
  color: #fff;
  border-radius: 7px;
  padding: 3px 12px;
  font-size: 12px;
}
.tpb-btn:hover { background: rgba(255, 255, 255, 0.28); }

.gui-theme-error {
  position: fixed;
  inset: 0;
  z-index: 2100;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--bg0);
  color: var(--text);
}
.gui-theme-error-card {
  width: min(520px, 100%);
  padding: 32px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--bg1);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
}
.gui-theme-error-kicker {
  color: var(--red);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.gui-theme-error-card h1 {
  margin: 10px 0 8px;
  font-size: 22px;
}
.gui-theme-error-card p {
  margin: 0;
  color: var(--text-dim);
  line-height: 1.6;
}
.gui-theme-error-code {
  margin-top: 14px;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11px;
}
.gui-theme-error-actions {
  display: flex;
  gap: 10px;
  margin-top: 24px;
}
.gui-theme-error-actions button {
  flex: 1;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text);
}
.gui-theme-error-actions button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

#splash {
  position: fixed;
  inset: 0;
  z-index: 2000;
  background: var(--bg0);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
}
.sp-logo {
  width: 84px;
  height: 84px;
  border-radius: 20px;
  object-fit: cover;
  display: block;
  box-shadow: 0 0 42px var(--accent-soft), 0 0 18px var(--accent-soft);
  animation: sp-pulse 1.6s ease-in-out infinite;
}
@keyframes sp-pulse {
  0%, 100% { transform: scale(1); box-shadow: 0 0 24px var(--accent-soft); }
  50% { transform: scale(1.06); box-shadow: 0 0 52px var(--accent); }
}
.sp-title {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 6px;
  color: var(--text);
  animation: sp-rise 0.6s cubic-bezier(0.16, 1, 0.3, 1) both;
}
.sp-sub {
  font-size: 12.5px;
  color: var(--text-faint);
  letter-spacing: 2px;
  animation: sp-rise 0.6s 0.12s cubic-bezier(0.16, 1, 0.3, 1) both;
}
@keyframes sp-rise {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}
.sp-bar {
  margin-top: 10px;
  width: 168px;
  height: 3px;
  border-radius: 2px;
  background: var(--bg2);
  overflow: hidden;
}
.sp-bar i {
  display: block;
  width: 40%;
  height: 100%;
  border-radius: 2px;
  background: var(--accent);
  animation: sp-slide 1.1s ease-in-out infinite;
}
@keyframes sp-slide {
  0% { transform: translateX(-110%); }
  100% { transform: translateX(380%); }
}
.splash-leave-active {
  transition: opacity 0.35s ease, transform 0.35s ease;
}
.splash-leave-to {
  opacity: 0;
  transform: scale(1.015);
}

.working-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 4px 4px;
  font-size: 12.5px;
  color: var(--text-faint);
  user-select: none;
  animation: wi-fadein 0.25s ease;
}
@keyframes wi-fadein {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
.wi-ring {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 2px solid var(--accent-soft);
  border-top-color: var(--accent);
  animation: wi-spin 0.9s linear infinite;
  flex-shrink: 0;
}
@keyframes wi-spin {
  to { transform: rotate(360deg); }
}
.wi-text {
  letter-spacing: 1px;
}
.wi-dots {
  display: inline-flex;
  margin-left: -4px;
  font-size: 16px;
  line-height: 1;
}
.wi-dots i {
  font-style: normal;
  animation: wi-blink 1.2s infinite;
}
.wi-dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.wi-dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes wi-blink {
  0%, 60%, 100% { opacity: 0.15; }
  30% { opacity: 1; }
}
.wi-elapsed {
  margin-left: 4px;
  font-size: 11.5px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.working-indicator.done {
  opacity: 0.75;
}
.wi-check {
  color: var(--accent);
  font-size: 12px;
  flex-shrink: 0;
}

#app-root {
  height: 100vh;
  display: flex;
  flex-direction: column;
  color: var(--xy-text, var(--text));
  font-family: var(--sans);
  overflow: hidden;
}

#main {
  flex: 1;
  display: flex;
  min-height: 0;
  position: relative;
  padding: 0 8px 8px;
}

.xy-main-card {
  flex: 1;
  min-width: 0;
  display: flex;
  border-radius: 16px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-canvas, var(--bg1));
  box-shadow: var(--xy-shadow-sm, none);
  overflow: hidden;
}

.panel-resizer {
  width: 8px;
  margin: 0;
  flex-shrink: 0;
  cursor: col-resize;
  position: relative;
  z-index: 30;
}
.panel-resizer::after {
  content: '';
  position: absolute;
  top: 8px;
  bottom: 8px;
  left: 3px;
  width: 2px;
  border-radius: 999px;
  background: transparent;
  transition: background 0.15s;
}
.panel-resizer:hover::after,
.panel-resizer.resizing::after {
  background: var(--accent);
}
.panel-resizer-inner {
  width: 5px;
  margin: 0 -2px;
}
.panel-resizer-inner::after {
  top: 0;
  bottom: 0;
  left: 2px;
}
#main.panel-resizing {
  user-select: none;
  cursor: col-resize;
}

.window-resize-handle {
  position: fixed;
  z-index: 999;
  background: transparent;
  user-select: none;
}
.resize-left,
.resize-right {
  top: 6px;
  bottom: 6px;
  width: 3px;
  cursor: ew-resize;
}
.resize-left { left: 0; }
.resize-right { right: 0; }
.resize-top,
.resize-bottom {
  left: 6px;
  right: 6px;
  height: 3px;
  cursor: ns-resize;
}
.resize-top { top: 0; }
.resize-bottom { bottom: 0; }
.resize-top-left,
.resize-top-right,
.resize-bottom-left,
.resize-bottom-right {
  width: 6px;
  height: 6px;
}
.resize-top-left { top: 0; left: 0; cursor: nwse-resize; }
.resize-top-right { top: 0; right: 0; cursor: nesw-resize; }
.resize-bottom-left { bottom: 0; left: 0; cursor: nesw-resize; }
.resize-bottom-right { right: 0; bottom: 0; cursor: nwse-resize; }

#center {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
  container-type: inline-size;
  container-name: center-pane;
}

#chat-header {
  height: 44px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 8px;
  flex-shrink: 0;
  container-type: inline-size;
  container-name: chat-header;
}
#chat-header .crumb {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--xy-text-3, var(--text-dim));
  /* 会话名必须可压缩：空间不足时省略号截断，而不是把右侧操作按钮挤出行外 */
  min-width: 0;
}
#chat-header .crumb.is-mobile-clickable {
  cursor: pointer;
  padding: 4px 6px;
  margin-left: -6px;
  border-radius: 8px;
  transition: background 0.15s;
}
#chat-header .crumb.is-mobile-clickable:active {
  background: var(--xy-hover, var(--bg2));
}
#chat-header .crumb-mobile-caret {
  flex-shrink: 0;
  color: var(--xy-text-3, var(--text-faint));
}
#chat-header .crumb-prefix,
#chat-header .crumb-sep {
  flex-shrink: 0;
  color: var(--xy-text-3, var(--text-faint));
}
#chat-header .crumb-name {
  font-weight: 600;
  color: var(--xy-text, var(--text));
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#chat-header .crumb-workdir {
  flex-shrink: 0;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 400;
  background: var(--xy-hover, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
}
/* 对话头部 Git 分支 chip：图标 + 分支名，空间不足时省略号收缩（min-width:0
   才能突破 flex 最小内容宽度，否则按钮被挤成竖条、不省略） */
#chat-header .git-branch-btn {
  max-width: 200px;
  min-width: 0;
}
#chat-header .git-branch-btn .git-branch-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.top-spacer {
  flex: 1;
}

.plan-entry-wrap {
  position: relative;
}
.decision-entry-wrap {
  position: relative;
}
.decision-entry-badge {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 99px;
  background: var(--accent);
  color: #fff;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
  display: inline-block;
}
.task-entry {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.task-entry-badge {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 99px;
  background: var(--accent);
  color: #fff;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
}
@keyframes task-entry-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.task-entry .task-entry-spin {
  animation: task-entry-spin 0.9s linear infinite;
}

.chat-viewport {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
  padding: 0;
  background: transparent;
}
#chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 16px 36px 12px;
  border: none;
  border-radius: 0;
  /* 保留浏览器 scroll anchoring（默认 auto）：翻页 prepend 与图片/异步内容
     分批撑高时由渲染引擎持续保持视口锚点，这是"不回退"的主力机制。
     JS 侧 maybeLoadOlder 的补偿是绝对定位赋值（oldTop + 内容增量），
     幂等且与 anchoring 同向，仅作兜底，不会双重叠加。 */
}
#chat-body {
  position: relative;
  max-width: var(--chat-col-width);
  margin: 0 auto;
}
.jump-bottom {
  position: absolute;
  right: 24px;
  bottom: 18px;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--float-bg);
  color: var(--text-dim);
  cursor: pointer;
  box-shadow: 0 6px 18px var(--theme-shadow-color);
  z-index: 30;
  transition: color 0.15s, border-color 0.15s, transform 0.15s;
}
.jump-bottom:hover {
  color: var(--text);
  border-color: var(--text-faint);
  transform: translateY(-1px);
}

.chip-btn {
  height: 28px;
  background: transparent;
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-2, var(--text-dim));
  border-radius: 8px;
  padding: 0 10px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  white-space: nowrap;
  flex-shrink: 0;
  position: relative;
}
.chip-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
  border-color: var(--xy-border, var(--border));
}
.chip-btn.active {
  background: var(--xy-active, var(--bg3));
  color: var(--xy-text, var(--text));
  border-color: var(--xy-border-strong, var(--border));
}

@container chat-header (max-width: 430px) {
  #chat-header .chip-label,
  #chat-header :deep(.bg-entry-btn .chip-label),
  #chat-header .git-branch-label {
    display: none;
  }
  #chat-header .chip-btn,
  #chat-header :deep(.bg-entry-btn) {
    padding: 4px 7px;
    gap: 4px;
  }
  #chat-header .git-branch-btn {
    max-width: none;
  }
  #chat-header .task-entry-badge,
  #chat-header .decision-entry-badge,
  #chat-header :deep(.bg-entry-badge) {
    position: absolute;
    top: -4px;
    right: -4px;
    margin-left: 0;
    min-width: 13px;
    height: 13px;
    padding: 0 3px;
    font-size: 9.5px;
    line-height: 13px;
  }
}

.export-wrap {
  position: relative;
}
.export-menu {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 200;
  min-width: 150px;
  padding: 5px;
}
.export-item {
  padding: 8px 12px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-dim);
}
.export-item:hover {
  background: var(--bg3);
  color: var(--text);
}

#statusbar {
  height: 26px;
  border-top: 1px solid var(--xy-border, var(--border));
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 16px;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  flex-shrink: 0;
  user-select: none;
}
#statusbar .si {
  display: flex;
  align-items: center;
  gap: 5px;
}
#statusbar .status-skill {
  min-width: 0;
  flex-shrink: 100;
}
#statusbar .status-skill .skill-text {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#statusbar .si-warn {
  color: var(--orange);
}
#statusbar .status-ai-notice {
  flex: 1 1 auto;
  min-width: 0;
  justify-content: center;
  color: var(--text-dim);
  opacity: 0.75;
}
#statusbar .status-ai-notice .notice-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#statusbar .right {
  margin-left: auto;
  display: flex;
  gap: 16px;
}
.s-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
}
.s-dot.off {
  background: var(--red);
}
.status-connection .conn-short {
  display: none;
}
.si.tok-orange {
  color: var(--orange);
}
.si.tok-red {
  color: var(--red);
}
@media (min-width: 769px) {
  #statusbar .si.status-connection.is-connected,
  #statusbar .si.status-model,
  #statusbar .si.status-workspace,
  #statusbar .si.status-git-branch,
  #statusbar .si.status-token,
  #statusbar .si.status-permission,
  #statusbar .si.status-sandbox,
  #statusbar .si.status-skill,
  #statusbar [data-test="status-charset"] {
    display: none;
  }
  #statusbar .status-runtime .si {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
  }
}
@media (max-width: 768px) {
  #main {
    padding: 0;
  }
  .xy-main-card {
    border-radius: 0;
    border: none;
    box-shadow: none;
  }
}
.history-loading {
  text-align: center;
  font-size: 12px;
  color: var(--text-faint);
  padding: 6px 0;
  user-select: none;
}
/* 会话切换加载指示：切换期间替换空状态卡片（细条 + 弱化色，与 history-loading 同风格）；
   旋转动画复用上方 .wi-ring 已定义的 @keyframes wi-spin，避免重复定义冲突的 keyframes */
.session-switching {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  min-height: 240px;
  color: var(--text-faint);
  font-size: 13px;
  user-select: none;
}
.session-switching .ss-ring {
  width: 18px;
  height: 18px;
  border: 2px solid var(--border);
  border-top-color: var(--text-faint);
  border-radius: 50%;
  animation: wi-spin 0.8s linear infinite;
}
</style>
