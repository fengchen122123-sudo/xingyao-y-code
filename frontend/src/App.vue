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
    <!-- 主题预览浮条：设置页点"预览效果"后收起设置、直接看真实首页，提供返回编辑 -->
    <div v-if="store.themePreviewMode && !store.settingsOpen" class="theme-preview-bar" data-test="theme-preview-bar">
      <span class="tpb-text">正在预览主题效果（未保存）</span>
      <button class="tpb-btn primary" data-test="preview-back" @click="backToThemeEditor">返回编辑</button>
    </div>

    <!-- 顶栏 -->
    <Topbar />

    <!-- frameless 窗口透明 resize 热区：WebView 保持全铺，不露出宿主边带 -->
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

    <!-- 主体区域 -->
    <div id="main" :class="{ 'panel-resizing': anyPanelResizing }">
      <SettingsPage v-if="store.settingsOpen" />
      <template v-else>
      <!-- 左侧边栏 -->
      <Sidebar />

      <!-- 左栏宽度拖拽手柄：压分界线的隐形热区，双击恢复默认宽度 -->
      <div
        v-if="!isMobile && !store.sidebarCollapsed"
        class="panel-resizer"
        :class="{ resizing: sidebarResizing }"
        title="拖拽调整侧栏宽度，双击恢复默认"
        data-test="sidebar-resizer"
        @pointerdown="onSidebarResizerDown"
        @dblclick="resetSidebarWidth"
      ></div>

      <!-- 中间主对话与聊天区域 -->
      <div id="center">
        <div id="chat-header" data-theme-region="conversation_toolbar">
          <span class="crumb">会话 / <b>{{ store.currentSessionDisplayName }}</b></span>
          <span class="top-spacer"></span>

          <div class="export-wrap">
            <button class="chip-btn" title="导出会话" @click.stop="showExportMenu = !showExportMenu"><AppIcon name="download" :size="13" /> 导出</button>
            <div v-if="showExportMenu" class="export-menu" @click.stop>
              <div class="export-item" @click="doExport('md')">Markdown (.md)</div>
              <div class="export-item" @click="doExport('html')">HTML (.html)</div>
              <div class="export-item" @click="doExport('json')">JSON (.json)</div>
            </div>
          </div>
          <button class="chip-btn" title="清空当前会话历史" @click="store.clearSessionHistory()">清空</button>
        </div>

        <!-- 电脑操控安全条（设计 16.1）：聊天标题栏下方、滚动区外——对话滚动时保持可见；
             仅 run 活动期（starting/running/paused/waiting_for_user/
             provider_safety_required/stopping/failed）渲染 -->
        <ComputerControlBar />

        <!-- 对话主体滚动区：.chat-viewport 是不滚动的定位外壳，
             红点轨道与"回到底部"按钮放在它里面、#chat-scroll 外面——
             若放进 overflow 滚动的 #chat-scroll 内，position:absolute 会相对
             整个内容区定位，会话一长红点就被推到内容中部、随内容滚出视口 -->
        <div class="chat-viewport">
          <div ref="chatScrollRef" id="chat-scroll" data-theme-region="workspace" @scroll="handleScroll">
          <div id="chat-body">
            <!-- Kimi 空状态卡片 -->
            <KimiEmptyState v-if="!store.messages || store.messages.length === 0" />

            <!-- 消息流列表：按回合分组渲染——回合结束后中间过程折叠成一块，
                 只突出组尾的最终总结；运行中的回合保持逐条实时显示 -->
            <template v-else>
              <TurnGroup
                v-for="group in messageGroups"
                :key="groupKey(group)"
                :group="group"
              />
            </template>

            <!-- 权限询问内联卡片（codex 风格，跟随对话流末尾） -->
            <PermCard />

            <!-- 回合活动指示：内容开始输出后才显示（等待首内容阶段由气泡内
                 等待线负责，两条不重叠）；结束后保留一行静态工作时间 -->
            <div v-if="showFlowWorking" class="working-indicator">
              <span class="wi-ring"></span>
              <span class="wi-text">星瑶正在干活</span>
              <span class="wi-dots"><i>.</i><i>.</i><i>.</i></span>
              <span v-if="workElapsed >= 1" class="wi-elapsed">已工作 {{ fmtWorkTime(workElapsed) }}</span>
            </div>
            <div v-else-if="finalWorkTime !== null" class="working-indicator done">
              <span class="wi-check"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
              <span class="wi-text">星瑶本轮工作 {{ fmtWorkTime(finalWorkTime) }}</span>
            </div>
          </div>
          </div>

          <!-- 红点导航轨道：相对外壳定位，钉在右侧不随内容滚动 -->
          <MessageNavRail
            ref="railRef"
            :get-scroll-el="() => chatScrollRef"
            :get-user-msg-elements="getUserMsgElements"
          />

          <!-- 回到底部悬浮按钮：用户上滑浏览历史时出现 -->
          <button
            v-if="showJumpButton"
            class="jump-bottom"
            data-test="jump-bottom"
            title="回到底部"
            @click="scrollToBottom(true)"
          >
            <AppIcon name="chevron-down" :size="16" :stroke-width="2.25" />
          </button>
        </div>

        <!-- 二级抽屉页面 -->
        <Subpage
          :visible="store.subpage.show"
          :sub-type="store.subpage.type"
          @close="store.closeSubpage()"
        />

        <!-- 底部固定输入框：跟随中间对话栏宽度（存在消息时呈现） -->
        <InputArea v-if="store.messages && store.messages.length > 0" />
      </div>

      <!-- 右面板宽度拖拽手柄：压分界线的隐形热区，双击恢复默认宽度 -->
      <div
        v-if="!isMobile && !store.rightPanelCollapsed"
        class="panel-resizer"
        :class="{ resizing: rightPanelResizing }"
        title="拖拽调整面板宽度，双击恢复默认"
        data-test="right-panel-resizer"
        @pointerdown="onRightPanelResizerDown"
        @dblclick="resetRightPanelWidth"
      ></div>

      <!-- 右侧控制面板 -->
      <RightPanel />
      </template>
    </div>

    <!-- 状态栏 -->
    <div id="statusbar" data-theme-region="statusbar">
      <span class="si status-connection" data-test="status-connection" data-mobile-status="primary">
        <span class="s-dot" :class="{ off: !store.connected }"></span>
        <span class="conn-full">{{ store.connected ? '后端已连接' : '连接已断开' }}</span>
        <span class="conn-short">{{ store.connected ? '已连接' : '已断开' }}</span>
      </span>
      <span class="si status-model" data-test="status-model" data-mobile-status="primary" :title="'当前模型: ' + (store.activeModelName || '未选择')">{{ store.activeModelName || '未选择模型' }}</span>
      <span class="si status-workspace" data-test="status-workspace" data-mobile-status="primary" :title="'当前工作区: ' + (store.workdir || store.currentWorkdirLabel)">{{ store.currentWorkdirLabel }}</span>
      <span class="si status-token" data-test="status-token" data-mobile-status="hidden" :class="statusTokenClass" title="上下文 Token 占用百分比">Token {{ statusTokenPct }}%</span>
      <span class="si status-permission" data-test="status-permission" data-mobile-status="hidden">权限: <span>{{ formatPermMode(store.permissionMode) }}</span></span>
      <span class="si status-skill" data-test="status-skill" data-mobile-status="hidden" :title="statusSkillTitle">技能: {{ statusSkillText }}</span>
      <span
        class="si status-sandbox"
        data-test="status-sandbox"
        data-mobile-status="primary"
        :class="{ 'si-warn': store.sandboxEnabled }"
        :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅允许访问工作目录内文件'"
      >沙盒: {{ store.sandboxEnabled ? '解除' : '限制' }}</span>
      <div class="right status-runtime" data-test="status-runtime">
        <span class="si" data-test="status-running" data-mobile-status="primary">{{ store.running ? '生成中…' : '空闲' }}</span>
        <span class="si" data-test="status-charset" data-mobile-status="hidden">UTF-8</span>
      </div>
    </div>

    <!-- 确认弹窗 -->
    <ConfirmModal />

    <!-- 软件更新弹窗 -->
    <UpdateModal />

    <!-- 图片大图查看 -->
    <ImageLightbox />

    <!-- 手机端内置目录浏览器（引入新项目工作区） -->
    <RemoteDirBrowser />

    <!-- 全局通知已迁移：对话类提示走对话流 notice 消息，非对话类提示走 ConfirmModal notice 模式 -->

    <!-- 启动画面：初始化期间全屏展示，加载完成后淡出进入对话 -->
    <Transition name="splash">
      <div v-if="!store.initialized" id="splash">
        <img class="sp-logo" :src="'/icon.png'" alt="Y-CODE">
        <div class="sp-title">Y-CODE</div>
        <div class="sp-sub">星瑶·编码助手</div>
        <div class="sp-bar"><i></i></div>
      </div>
    </Transition>

    <!-- frameless 关闭确认（现代样式，替代 win32 原生 MessageBox） -->
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
import { formatPermMode } from './utils/markdown';
import { buildMessageGroups, msgKey, type TurnGroupInfo } from './utils/turnGroups';
import { useChatScroll } from './composables/useChatScroll';
import { useIsMobile } from './composables/useViewport';
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
import ComputerControlBar from './components/ComputerControlBar.vue';
import ConfirmModal from './components/ConfirmModal.vue';
import UpdateModal from './components/UpdateModal.vue';
import PermCard from './components/PermCard.vue';
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
    // 浏览器开发态或桥尚未注入时保持默认非最大化，后续宿主事件会同步。
  }
}

// 窗口重新聚焦：存在已知的电脑操控 run 时拉取完整快照校准 Store（设计 16.4）
function onWindowFocusRecalibrateComputer() {
  if (store.computerRun.run_id) void store.refreshComputerRun();
}

const store = useAppStore();

// 左右面板宽度拖拽（桌面端）：宽度存 store 实时生效，松手持久化到
// localStorage，双击手柄恢复默认；移动端布局由 CSS 媒体查询接管，手柄不渲染
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
});
// 拖拽期间给 #main 加 class：全局禁文本选择并强制 col-resize 光标
const anyPanelResizing = computed(() => sidebarResizing.value || rightPanelResizing.value);

const chatScrollRef = ref<HTMLElement | null>(null);

// 主题预览：从真实首页返回设置页继续编辑（退出预览模式、重开设置并落到外观与主题）
function backToThemeEditor() {
  store.themePreviewMode = false;
  store.settingsSection = 'appearance';
  store.settingsOpen = true;
}

const showExportMenu = ref(false);

async function doExport(fmt: 'md' | 'html' | 'json') {
  showExportMenu.value = false;
  await store.exportSession(fmt);
}

function closeExportMenu() {
  showExportMenu.value = false;
}

const statusTokenPct = computed(() => {
  const w = store.tokenUsage.window || 0;
  if (!w) return 0;
  return Math.round((store.tokenUsage.used / w) * 100);
});
const statusTokenClass = computed(() => {
  if (statusTokenPct.value > 90) return 'tok-red';
  if (statusTokenPct.value > 70) return 'tok-orange';
  return '';
});

// 状态栏"技能"项：显示本回合实际用到的技能名（来自 is_skill 工具调用收集），
// 回合结束后固化；未使用任何技能时显示"无"，让"很少触发"从感觉变成可见数字
const statusSkillText = computed(() =>
  store.usedSkills.length ? store.usedSkills.join('、') : '无',
);
const statusSkillTitle = computed(() =>
  store.usedSkills.length
    ? '本回合使用的技能：' + store.usedSkills.join('、')
    : '本回合未使用任何技能',
);

// 智能滚动：跟随模式（贴底自动吸底 / 上滑冻结）+ force 场景强制回底
const {
  scrollToBottom,
  handleScroll,
  syncStick,
  showJumpButton,
} = useChatScroll(() => chatScrollRef.value);

// 红点导航轨道：每条「我发的消息」对应一个红点，DOM 变化后重算红点数量
const railRef = ref<InstanceType<typeof MessageNavRail> | null>(null);
function refreshRail() {
  railRef.value?.refresh();
}
function getUserMsgElements(): HTMLElement[] {
  return Array.from(chatScrollRef.value?.querySelectorAll('.msg-user') ?? []);
}

// 新用户消息（发送）→ 强制回底并恢复跟随；其余消息变化仅在跟随模式下跟随
const userMsgCount = computed(() => store.messages.filter((m) => m.role === 'user').length);
watch(userMsgCount, (n, o) => {
  if (n > o) scrollToBottom(true);
});

// M32 修复：消息列表 key 不用数组索引（插入/删除时组件局部状态会错挂）。
// 消息对象没有后端 id，按对象身份分配稳定渲染 key（实现移至 utils/turnGroups，
// 与 TurnGroup 折叠体共用，保证分组重排时组件实例不错位重挂载）。

// 按回合分组消息流：user 消息开组，组内最后一条 assistant（最终总结）留在
// 组外展示，其余中间过程在回合结束后折叠（见 TurnGroup.vue）
const messageGroups = computed(() => buildMessageGroups(store.messages));

function groupKey(group: TurnGroupInfo): number {
  return msgKey(group.userMsg ?? group.middle[0] ?? group.tail[0]);
}

// 对话区 DOM 变化观察器：覆盖所有不经过 store 顶层状态的更新——
// 思考流、工具执行时间线、Diff 展开、working 指示每秒计时等子组件内部变化。
// 只有用户停留在底部附近（阈值 200px）时才自动吸底，向上翻历史不打扰。
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
      // 内容变化（思考流/工具时间线/Diff 展开/工时计时等）：跟随模式下自动吸底
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
// 从设置页返回对话：v-if 切换会重建对话区 DOM，滚动位置被重置到顶部，
// 这里在 DOM 重建完成后强制滚回底部并重算轨道位置，恢复"停留在最新消息"体验
watch(() => store.settingsOpen, (open) => {
  if (!open) {
    scrollToBottom(true);
    ensureBodyObserver();
    nextTick(() => {
      syncStick();
      refreshRail();
    });
  }
});

// ---------------------------------------------------------------------------
// 回合活动指示与工时计时
// 等待首内容阶段（空助手气泡）只显示气泡内等待线，流程指示不重复出现；
// 有内容（正文/思考/工具调用）输出后才显示流程指示。回合结束后保留一行
// 静态工作时间（不再增长），下轮开始 / 切会话 / 清空会话时重置。
// ---------------------------------------------------------------------------
const workElapsed = ref(0);
const finalWorkTime = ref<number | null>(null);
let workTimer: number | undefined;
let turnStartTs = 0;

function fmtWorkTime(s: number): string {
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`;
}

// 与 MessageBubble 的等待线互斥：最后一条是空助手气泡 = 等待首内容阶段
const showFlowWorking = computed(() => {
  if (!store.running) return false;
  const msgs = store.messages;
  if (!msgs.length) return false;
  const last = msgs[msgs.length - 1];
  if (last.role !== 'assistant') return true;
  if (last.content && last.content.trim()) return true;
  if (last.reasoning_content && last.reasoning_content.trim()) return true;
  if (last.tool_calls && last.tool_calls.length) return true;
  return false;
});

watch(() => store.running, (running) => {
  if (running) {
    // 用会话级回合开始时间戳（store.turnStartedAt），切回运行中的会话时
    // 计时从该会话的真实开始时刻继续，而非重新从 0 计
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
    // 回合结束（含用户终止）：保留本轮工时静态显示
    if (turnStartTs) {
      finalWorkTime.value = Math.max(0, Math.floor((Date.now() - turnStartTs) / 1000));
      turnStartTs = 0;
    }
  }
  scrollToBottom();
});

// 切会话 / 清空会话后，上一轮的工时残留无意义，一并重置
watch(() => store.currentSessionName, () => {
  finalWorkTime.value = null;
  // 切会话回到底部并恢复跟随，红点轨道按新会话消息重算
  scrollToBottom(true);
  nextTick(refreshRail);
  // 关键：A/B 都在运行时切换会话，store.running 值不变（true→true），
  // watch(store.running) 不会触发，计时器会继续用旧会话的时间戳串号。
  // 这里按目标会话的回合开始时间戳刷新计时。
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

onUnmounted(() => window.clearInterval(workTimer));
// 权限卡片出现/收起时同步滚动，保证用户能立即看到询问（强制回底）
watch(() => store.permRequest.show, () => {
  scrollToBottom(true);
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
}
function onPywebviewReady() {
  startInit();
}

onMounted(() => {
  document.addEventListener('click', closeExportMenu);
  void syncInitialWindowMaximized();

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
        // 轮询放弃也走初始化：initFromBackend 里 _api() 为空时会弹
        // "后端连接失败" 提示并撤下启动页，而不是永远卡在启动画面
        startInit();
      }
    }, 100);
  }

  // 首次挂载后注册 DOM 观察器（chat-body 可能尚未渲染，由 nextTick 兜底）
  ensureBodyObserver();
});
onUnmounted(() => {
  document.removeEventListener('click', closeExportMenu);
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
/* 主题预览浮条：设置页预览效果时置顶显示 */
/* frameless 关闭确认（现代三选框） */
.close-confirm-mask {
  position: fixed;
  inset: 0;
  z-index: 5000;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(6px);
}
.close-confirm-card {
  width: min(420px, calc(100vw - 48px));
  padding: 24px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--bg1);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
}
.close-confirm-card h3 { margin: 0 0 8px; font-size: 16px; }
.close-confirm-card p { margin: 0 0 18px; color: var(--text-dim); font-size: 13px; line-height: 1.6; }
.cc-actions { display: flex; flex-direction: column; gap: 8px; }
.cc-btn {
  min-height: 38px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.cc-btn:hover { border-color: var(--accent); }
.cc-btn.danger { color: var(--red); border-color: color-mix(in srgb, var(--red) 45%, var(--border)); }
.cc-btn.danger:hover { background: color-mix(in srgb, var(--red) 10%, var(--bg2)); }
.cc-btn.ghost { background: transparent; color: var(--text-dim); }
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

/* ---------- 启动画面 ---------- */
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
/* 加载完成后淡出 */
.splash-leave-active {
  transition: opacity 0.35s ease, transform 0.35s ease;
}
.splash-leave-to {
  opacity: 0;
  transform: scale(1.015);
}

/* ---------- 回合活动指示器 ---------- */
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
  /* 不设背景色：全局背景图（body::before，最底层）可透到各分区间隙 */
  color: var(--text);
  font-family: var(--sans);
  overflow: hidden;
}

#main {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* 面板宽度拖拽手柄：5px 隐形热区压在分界线上（负 margin 向两侧各溢出
   2px 便于抓取），悬停/拖拽时显示 accent 高亮中线；移动端由 v-if 不渲染 */
.panel-resizer {
  width: 5px;
  margin: 0 -2px;
  flex-shrink: 0;
  cursor: col-resize;
  position: relative;
  z-index: 30;
}
.panel-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 2px;
  background: transparent;
  transition: background 0.15s;
}
.panel-resizer:hover::after,
.panel-resizer.resizing::after {
  background: var(--accent);
}
/* 拖拽期间禁止选中界面文本、强制列宽光标（指针移出手柄也保持） */
#main.panel-resizing {
  user-select: none;
  cursor: col-resize;
}

.window-resize-handle {
  position: fixed;
  /* 高于常规内容，低于所有 1000+ 模态层，弹窗不会被边缘热区穿透。 */
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
}

#chat-header {
  height: 38px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 10px;
  flex-shrink: 0;
}
#chat-header .crumb {
  font-size: 13px;
  color: var(--text-dim);
}
#chat-header .crumb b {
  color: var(--text);
}
.top-spacer {
  flex: 1;
}

.chat-viewport {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
}
#chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 10px;
}
/* 对话内容容器：消息列表的公共父级 */
#chat-body {
  position: relative;
}
/* 回到底部悬浮按钮：用户上滑浏览历史时出现（相对 .chat-viewport 定位） */
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
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 4px 10px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 5px;
  transition: 0.15s;
}
.chip-btn:hover {
  color: var(--text);
  border-color: var(--text-faint);
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
  height: 24px;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 12px;
  gap: 16px;
  font-size: 11px;
  flex-shrink: 0;
}
#statusbar .si {
  display: flex;
  align-items: center;
  gap: 5px;
}
#statusbar .si-warn {
  color: var(--orange);
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
</style>
