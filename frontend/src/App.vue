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
      <!-- 左侧边栏：IDE 模式整块换成 IDE 布局，会话侧栏必须退场——否则它按固定宽度
           占住左边，把编辑器区挤成一条缝（会话列表的入口由对话头部的新会话/历史按钮承担） -->
      <Sidebar v-if="!store.ideMode()" />

      <!-- 左栏宽度拖拽手柄：压分界线的隐形热区，双击恢复默认宽度 -->
      <div
        v-if="!isMobile && !store.sidebarCollapsed && !store.ideMode()"
        class="panel-resizer"
        :class="{ resizing: sidebarResizing }"
        title="拖拽调整侧栏宽度，双击恢复默认"
        data-test="sidebar-resizer"
        @pointerdown="onSidebarResizerDown"
        @dblclick="resetSidebarWidth"
      ></div>

      <!-- IDE 模式：中部整块换成 IDE 布局（活动栏 / 文件树 / 编辑器 / 底部面板 / 对话区宿主） -->
      <IdeLayout v-if="store.ideMode()" />

      <!-- 中间主对话与聊天区域：对话模式就地渲染；IDE 模式用 CSS 停靠到 #main 右侧
           （见下方 #center.ide-docked）。
           这里原本用 Teleport 把这块搬进 IdeLayout 的宿主，但 Teleport 只在挂载时解析
           一次目标，而宿主在 v-if 的 IdeLayout 里——对话模式下首次挂载时目标不存在，
           target 解析成 null，之后切到 IDE 模式也不会重新解析，对话区就一直留在原地
           按 flex:1 跟编辑器抢宽度（实测 #center 705px，而 368px 的宿主一直空置）。
           CSS 停靠同样只有一份 DOM、不重建组件、模式切换状态不丢，且没有时序问题。 -->
      <div
        id="center"
        :class="{
          'ide-docked': store.ideMode(),
          'ide-docked-hidden': store.ideMode() && store.ideChatCollapsed,
        }"
      >
        <div id="chat-header" data-theme-region="conversation_toolbar">
          <!-- IDE 模式：对话头部按设计稿补上星瑶头像，会话名沿用同一行的 crumb——
               不另起一行渲染第二个头部（IdeLayout 侧已不再重复渲染头部）。
               对话模式下不渲染头像，头部与加入本段之前完全一致 -->
          <div v-if="store.ideMode()" class="ai-avatar" data-test="ai-head-avatar">瑶</div>
          <span class="crumb">
            <template v-if="!store.ideMode()">会话 / </template><b>{{ store.currentSessionDisplayName }}</b>
          </span>
          <span class="top-spacer"></span>

          <!-- 计划入口：星瑶创建了任务计划时显示；悬浮面板锚定在按钮下方，不遮全屏 -->
          <div v-if="taskEntry.visible" class="plan-entry-wrap">
            <button
              class="chip-btn task-entry"
              data-test="task-entry"
              title="查看任务计划执行进度"
              @click.stop="taskDialogOpen = !taskDialogOpen"
            >
              <AppIcon name="loader" :size="12" :stroke-width="2" :class="{ 'task-entry-spin': taskEntry.activeCount > 0 }" /> 计划
              <span v-if="taskEntry.activeCount > 0" class="task-entry-badge" data-test="task-entry-badge">{{ taskEntry.activeCount }}</span>
            </button>
            <TaskProgressDialog v-if="taskDialogOpen" @close="taskDialogOpen = false" />
          </div>

          <div class="export-wrap">
            <button class="chip-btn" title="导出会话" @click.stop="showExportMenu = !showExportMenu"><AppIcon name="download" :size="13" /> 导出</button>
            <div v-if="showExportMenu" class="export-menu" @click.stop>
              <div class="export-item" @click="doExport('md')">Markdown (.md)</div>
              <div class="export-item" @click="doExport('html')">HTML (.html)</div>
              <div class="export-item" @click="doExport('json')">JSON (.json)</div>
            </div>
          </div>

          <!-- IDE 模式：新会话 / 历史 / 收起。这三个入口在对话模式下由侧栏与面板承担，
               IDE 模式侧栏已退场，必须跟到对话头部来——尤其「收起」必须配对一条恢复
               路径，否则收起后就再也打不开对话区（活动栏「星瑶」动作是那条路径） -->
          <template v-if="store.ideMode()">
            <button
              class="chip-btn icon-only"
              title="新会话"
              data-test="ide-chat-new-session"
              @click="store.createSessionSmart()"
            ><AppIcon name="plus" :size="13" /></button>
            <button
              class="chip-btn icon-only"
              title="历史会话"
              data-test="ide-chat-history"
              @click="store.openSessionSearchModal()"
            ><AppIcon name="search" :size="13" /></button>
            <button
              class="chip-btn icon-only"
              title="收起对话区（点活动栏「星瑶」恢复）"
              data-test="ide-chat-collapse"
              @click="store.ideChatCollapsed = true"
            ><AppIcon name="chevron-right" :size="13" /></button>
          </template>
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
            <!-- 历史分页加载指示：滚动到顶翻页时显示 -->
            <div v-if="store.historyLoading" class="history-loading" data-test="history-loading">正在加载更早的消息…</div>
            <!-- Kimi 空状态卡片（仅真正的新对话/空会话；切换加载中由下面的分支接管） -->
            <!-- 会话切换加载指示：目标会话历史仍在后端返回路上时替代空状态卡片，
                 避免与“新对话”首屏混淆；数据到达后由消息流或空状态卡片接管 -->
            <KimiEmptyState v-if="(!store.messages || store.messages.length === 0) && !store.sessionSwitching" />
            <div v-else-if="store.sessionSwitching" class="session-switching" data-test="session-switching">
              <span class="ss-ring"></span>
              <span>正在加载会话…</span>
            </div>

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

            <!-- C11：项目级 Hook 信任卡片（跟随权限卡之后，信任前命令不执行） -->
            <HookTrustCard />

            <!-- 回合活动指示：内容开始输出后才显示（等待首内容阶段由气泡内
                 等待线负责，两条不重叠）；结束后保留一行静态工作时间 -->
            <div v-if="showFlowWorking" class="working-indicator">
              <span class="wi-ring"></span>
              <span class="wi-text">{{ store.agentName }}正在干活</span>
              <span class="wi-dots"><i>.</i><i>.</i><i>.</i></span>
              <span v-if="workElapsed >= 1" class="wi-elapsed">已工作 {{ fmtWorkTime(workElapsed) }}</span>
            </div>
            <div v-else-if="finalWorkTime !== null" class="working-indicator done">
              <span class="wi-check"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
              <span class="wi-text">{{ store.agentName }}本轮工作 {{ fmtWorkTime(finalWorkTime) }}</span>
            </div>
          </div>
          </div>

          <!-- 红点导航轨道：相对外壳定位，钉在右侧不随内容滚动 -->
          <MessageNavRail
            ref="railRef"
            :get-scroll-el="() => chatScrollRef"
            :get-user-msg-elements="getUserMsgElements"
            :nav-items="store.navItems"
            :loaded-first-id="store.firstLoadedMsgId"
            @jump="onNavJump"
            @jump-start="onRailJumpStart"
          />

          <!-- 回到底部悬浮按钮：用户上滑浏览历史时出现 -->
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

        <!-- 二级抽屉页面 -->
        <Subpage
          :visible="store.subpage.show"
          :sub-type="store.subpage.type"
          @close="store.closeSubpage()"
        />

        <!-- 后台任务执行条：list_active_tasks 快照非空时出现，详情展开任务卡片 -->
        <BackgroundTasksBar />

        <!-- 底部固定输入框：跟随中间对话栏宽度（存在消息时呈现） -->
        <InputArea v-if="store.messages && store.messages.length > 0" />

        <!-- 底部内嵌交互式终端（ZCode 式弹出）：v-show 保持挂载，
             收起期间输出持续累积到 xterm 缓冲不丢失。
             IDE 模式下终端由 IdeLayout 的 BottomPanel 独占——这里必须 v-if 退场，
             否则会同时存在两个 xterm 实例、两个 PTY 会话 -->
        <TerminalPane v-if="!store.ideMode()" v-show="store.terminalPanelOpen" />
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

      <!-- 右侧控制面板：IDE 模式下默认让位给对话区（控制面板默认就是收起态，所以
           默认不显示 = 布局与设计稿一致），但保留按需展开——顶栏「切换面板」按钮与
           状态栏 Git 分支项在 IDE 模式下仍然可用，不能因为它们唯一的宿主被摘掉而失效。
           必须用 v-show 而不是 v-if：卸载会重建组件，它内部的原生 WebView2 表面
           来不及 hide 就会以白块形式留在屏幕上（模式来回切一次就现形） -->
      <RightPanel v-show="!store.ideMode() || !store.rightPanelCollapsed" />
      </template>
    </div>

    <!-- 状态栏 -->
    <div id="statusbar" data-theme-region="statusbar">
      <!-- IDE 模式徽标：最左的小胶囊，仅 IDE 模式渲染；对话模式下状态栏与加入本段之前完全一致 -->
      <span
        v-if="store.ideMode()"
        class="si status-ide-mode"
        data-test="ide-status-mode"
        title="当前处于 IDE 模式"
      >IDE</span>
      <span class="si status-connection" data-test="status-connection" data-mobile-status="primary">
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
      <span class="si status-skill" data-test="status-skill" data-mobile-status="hidden" :title="statusSkillTitle">
        <span class="skill-text">技能: {{ statusSkillText }}</span>
      </span>
      <span
        class="si status-sandbox"
        data-test="status-sandbox"
        data-mobile-status="primary"
        :class="{ 'si-warn': store.sandboxEnabled }"
        :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅允许访问工作目录内文件'"
      >沙盒: {{ store.sandboxEnabled ? '解除' : '限制' }}</span>
      <!-- AI 生成内容声明：居中落在状态栏中段剩余区域，空间不足时收缩省略 -->
      <span
        class="si status-ai-notice"
        data-test="status-ai-notice"
        data-mobile-status="hidden"
        title="本界面展示的内容由 AI 生成，仅供参考，请仔细甄别"
      ><span class="notice-text">内容由 AI 生成，仅供参考，请仔细甄别</span></span>
      <div class="right status-runtime" data-test="status-runtime">
        <span class="si" data-test="status-running" data-mobile-status="primary">{{ store.running ? '生成中…' : '空闲' }}</span>
        <span class="si" data-test="status-charset" data-mobile-status="hidden">UTF-8</span>
      </div>
      <!-- IDE 模式「编辑器段」：光标行列 / 缩进 / 编码 / 换行符 / 语言，追加在右侧运行状态之后。
           整段以「有活动标签」为前提：这些字段的数据源就是活动标签与它的编辑器实例，
           没有活动标签时宁可整段不渲染，也不拿上一个文件的残留值充数。 -->
      <div
        v-if="store.ideMode() && ideStore.activeTab"
        class="right status-editor"
        data-test="status-editor"
      >
        <span class="si" data-test="ide-status-cursor" data-mobile-status="hidden">行 {{ ideStore.cursorLine }}，列 {{ ideStore.cursorColumn }}</span>
        <span class="si" data-test="ide-status-doc-lines" data-mobile-status="hidden">共 {{ ideStore.docLines }} 行</span>
        <!-- 缩进是项目约定值（编辑器未单独配置 indentUnit，本段只做展示，不去读编辑器配置） -->
        <span class="si" data-test="ide-status-indent" data-mobile-status="hidden">空格：4</span>
        <span class="si" data-test="ide-status-encoding" data-mobile-status="hidden">{{ ideEncodingText }}</span>
        <span class="si" data-test="ide-status-eol" data-mobile-status="hidden">{{ ideEolText }}</span>
        <span
          class="si"
          data-test="ide-status-language"
          data-mobile-status="hidden"
          :title="ideStore.activeTab.path"
        >{{ ideLanguageText }}</span>
      </div>
    </div>

    <!-- 确认弹窗 -->
    <ConfirmModal />

    <!-- 软件更新弹窗 -->
    <UpdateModal />

    <!-- 会话全局搜索弹窗 (Ctrl+F) -->
    <SessionSearchModal />

    <!-- 免责条款首次使用确认：正常启动校验完成后，未同意或条款版本升级后才弹出；已同意过则不弹出 -->
    <DisclaimerGate v-if="store.initialized && store.disclaimerNeedsAccept" />

    <!-- 图片大图查看 -->
    <ImageLightbox />

    <!-- 手机端内置目录浏览器（引入新项目工作区） -->
    <RemoteDirBrowser />

    <!-- 全局通知已迁移：对话类提示走对话流 notice 消息，非对话类提示走 ConfirmModal notice 模式 -->

    <!-- 启动画面：初始化期间全屏展示，加载完成后淡出进入对话 -->
    <Transition name="splash">
      <div v-if="!store.initialized" id="splash">
        <img class="sp-logo" :src="'/icon.png'" alt="星瑶Code">
        <div class="sp-title">星瑶Code</div>
        <div class="sp-sub">{{ store.agentName }}·编码助手</div>
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
import { useGitStore } from './stores/gitStore';
import { useIdeStore } from './stores/ideStore';
import { languageIdForPath } from './editor/setup';
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
import TerminalPane from './components/TerminalPane.vue';
import IdeLayout from './components/IdeLayout.vue';
import ComputerControlBar from './components/ComputerControlBar.vue';
import ConfirmModal from './components/ConfirmModal.vue';
import UpdateModal from './components/UpdateModal.vue';
import SessionSearchModal from './components/SessionSearchModal.vue';
import TaskProgressDialog from './components/TaskProgressDialog.vue';
import BackgroundTasksBar from './components/BackgroundTasksBar.vue';
import { taskEntryState } from './utils/taskEntry';
// 免责条款首次使用确认（合规链路）：未同意或条款升级后强制弹出，不同意则退出
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
  // 右面板按视口比例记忆：窗口放大/缩小等比跟随，拖到 50% 就一直保持 50%
  viewportRatio: true,
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

// 会话头部“计划”入口 + 任务计划进度对话框：计划数据由 plan_updated 事件流
// 实时驱动，入口只在存在计划时显示，角标统计进行中的计划条目
const taskDialogOpen = ref(false);
const taskEntry = computed(() => taskEntryState({ plan: store.planData }));

async function doExport(fmt: 'md' | 'html' | 'json') {
  showExportMenu.value = false;
  await store.exportSession(fmt);
}

// 文档级点击统一收起头部悬浮层（导出菜单/计划面板）；按钮与面板内部
// 用 @click.stop 挡泡，避免点开即关
function closeHeaderPopovers() {
  showExportMenu.value = false;
  taskDialogOpen.value = false;
}

const statusTokenPct = computed(() => {
  const w = store.tokenUsage.window || 0;
  if (!w) return 0;
  return Math.round((store.tokenUsage.used / w) * 100);
});

// ── Git 状态栏分支项（规格 §11.1：底栏常驻分支 + 同步摘要；非 Git 项目显示占位）──
const gitStore = useGitStore();
const gitBranchText = computed(() => {
  if (!gitStore.snapshot && !gitStore.error) return '';
  if (gitStore.error) return '';
  const base = gitStore.statusbarText;
  return base || '';
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

// ── IDE 模式状态栏的「编辑器段」（光标 / 缩进 / 编码 / 换行符 / 语言）──
// 语言推断复用 editor/setup 的 languageIdForPath（与 ideStore.languageId 同一口径，
// 状态栏不自己按后缀猜一遍，避免两处推断结果不一致）。
const ideStore = useIdeStore();

/** 语言 id → 状态栏友好名；未命中的语言 id 原样显示（推断不到就不装懂） */
const IDE_LANGUAGE_LABELS: Record<string, string> = {
  python: 'Python',
  javascript: 'JavaScript',
  typescript: 'TypeScript',
  json: 'JSON',
  markdown: 'Markdown',
  html: 'HTML',
  css: 'CSS',
  vue: 'Vue',
};

/**
 * 仅用于显示的后缀兜底。
 *
 * `.vue` 在用户眼里有公认名字，但 languageIdForPath 推断不出来（后端没有 vue 语言
 * 服务器、CodeMirror 也没装 vue 语言包，编辑器按纯文本处理），只会返回空串。
 * 这里补一层显示映射，只影响状态栏文字，不动语言推断与 LSP 行为。
 */
const IDE_DISPLAY_ONLY_EXT: Record<string, string> = { vue: 'Vue' };

const ideLanguageText = computed(() => {
  const path = ideStore.activeTab?.path ?? '';
  const languageId = languageIdForPath(path);
  if (languageId) return IDE_LANGUAGE_LABELS[languageId] ?? languageId;
  // 推断不出语言 id：有公认名字的后缀走显示兜底，其余按纯文本显示
  //（与 editor/setup.ts「未命中的语言一律走纯文本」同一口径）
  const ext = (path.split(/[\\/]/).pop() ?? '').split('.').pop()?.toLowerCase() ?? '';
  return IDE_DISPLAY_ONLY_EXT[ext] ?? '纯文本';
});

// 换行符：crlf → CRLF，其余（lf / cr / mixed）一律按 LF 显示
const ideEolText = computed(() => (ideStore.activeTab?.eol === 'crlf' ? 'CRLF' : 'LF'));

// 文件编码：读活动标签的真实编码，不用固定串掩盖（gbk 文件显示 UTF-8 会误导用户）
const ideEncodingText = computed(() => (ideStore.activeTab?.encoding === 'gbk' ? 'GBK' : 'UTF-8'));

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

// 红点跳转的「退出跟随」处理（滚动权状态机）。
// 点击红点=用户显式离开底部：跳转发起时退出跟随（following=false），
// 跳转期间与结束后内容变化都不再吸底；直到用户滚回底部（或点回底按钮）才恢复。
// 不再需要「抑制窗口/恢复延迟」——following=false 本身就是全部保证。
function onRailJumpStart() {
  commitFollowing(false);
}

/** 显式回底（按钮/发送/切会话/设置页返回）：强制吸底恢复跟随，并取消进行中的跳转校正 */
function forceScrollToBottom() {
  scrollToBottom(true);
  railRef.value?.cancelJump?.();
}

// 智能滚动：跟随模式（贴底自动吸底 / 上滑冻结）+ force 场景强制回底
const {
  scrollToBottom,
  handleScroll,
  syncStick,
  showJumpButton,
  noteUserScrollIntent,
  commitFollowing,
} = useChatScroll(() => chatScrollRef.value);

// 历史滚动翻页：滚动近顶部时向前翻一页，加载后按内容增量补偿 scrollTop（视口不跳动）。
// 自动翻页只由「用户滚动」触发：程序化滚动（跳转定位/翻页补偿）不抢翻页控制权。
function onChatScroll() {
  if (handleScroll()) void maybeLoadOlder();
}

// 用户滚动输入登记（滚轮/触摸/滚动条拖动）：useChatScroll 据此区分
// 「用户滚动」与「程序化滚动」——跳转/补偿的滚动不再被误读为用户意图
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
// 翻页补偿的最长持续帧数（≈0.5s）：既覆盖图片解码、代码高亮等异步静默撑高，
// 又不会长时间霸占滚动控制权
const LOAD_OLDER_MAX_FRAMES = 30;

async function maybeLoadOlder() {
  const el = chatScrollRef.value;
  if (!el || el.scrollTop >= 120) return;
  // 调用方已保证本次是「用户滚动」来源；跳转/补偿等程序化滚动不会走到这里
  if (!store.historyHasMore || store.historyLoading) return;
  const oldHeight = el.scrollHeight;
  const oldTop = el.scrollTop;
  // 用户接管检测：校正期间用户若主动滚动就立即让位。触发翻页那一瞬的惯性
  // 残留（前 80ms 的输入事件）不算接管，否则刚滚到顶就会立刻放弃校正。
  const startedAt = performance.now();
  let userTookOver = false;
  const markUserTookOver = () => {
    if (performance.now() - startedAt > 80) userTookOver = true;
  };
  // 滚动条拖动不产生 wheel/touchmove：命中容器自身的 pointerdown 同样视为用户接管
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
        if (++stableFrames >= 2) return; // 高度连续两帧不变 = 撑高结束
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

// 红点导航点击未加载区域：连续向前翻页直到目标消息进入列表，再交回 rail 定位。
// 本地 SQLite 翻页极快，体感为直接跳转；60 页护栏防异常死循环。
// 每页 prepend 后按高度增量补偿 scrollTop（幂等）：anchoring 生效时与浏览器结果
// 一致，失效时兜底保持视口——连续翻页 + 异步内容撑高时 anchoring 会波动，
// 不补偿就会「一直跳动」；补偿后再让出一帧渲染，避免批量变化把锚点冲垮。
// 跳转已由 rail 的 jumpStart 退出跟随（following=false），翻页期间内容变化
// 不再吸底，无需任何抑制窗口；seq 令牌保证连续点击时旧循环立即让位。
let navJumpSeq = 0;
async function onNavJump(payload: { index: number; id: number }) {
  const seq = ++navJumpSeq;
  commitFollowing(false); // 双保险：跨页跳转同样显式退出跟随
  const el = chatScrollRef.value;
  const sessionAtJump = store.currentSessionName;
  let guard = 0;
  while (guard++ < 60) {
    if (seq !== navJumpSeq) return; // 新跳转接管：旧循环退出
    if (store.currentSessionName !== sessionAtJump) return; // 切会话：放弃跳转
    const first = store.firstLoadedMsgId;
    if (first !== null && payload.id >= first) break;
    if (!store.historyHasMore) break;
    const oldHeight = el?.scrollHeight ?? 0;
    const oldTop = el?.scrollTop ?? 0;
    await store.loadOlderMessages();
    await nextTick();
    if (!el || chatScrollRef.value !== el || store.currentSessionName !== sessionAtJump) return;
    if (seq !== navJumpSeq) return;
    // 绝对定位补偿（幂等）：anchoring 生效时此赋值与浏览器结果一致，不会叠加
    el.scrollTop = el.scrollHeight - oldHeight + oldTop;
    // 让出一帧：给布局/anchoring 落地时间，下一轮 prepend 不在同一帧内叠加
    await new Promise<void>((resolve) => {
      window.requestAnimationFrame(() => resolve());
    });
  }
  if (seq !== navJumpSeq) return;
  await nextTick();
  railRef.value?.jumpToLoaded?.(payload.index);
}

// 红点导航轨道：每条「我发的消息」对应一个红点，DOM 变化后重算红点数量
const railRef = ref<InstanceType<typeof MessageNavRail> | null>(null);
function refreshRail() {
  railRef.value?.refresh();
}
function getUserMsgElements(): HTMLElement[] {
  return Array.from(chatScrollRef.value?.querySelectorAll('.msg-user') ?? []);
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
// setup 时先记下当前尾部（若已有消息）：它属于「已知的旧尾部」，不算刚发送
let lastTailMsg: unknown = store.messages.length ? store.messages[store.messages.length - 1] : null;
watch(userMsgCount, (n, o) => {
  const tail = store.messages[store.messages.length - 1] as { role?: string } | undefined;
  const tailIsNew = tail !== undefined && tail !== lastTailMsg;
  lastTailMsg = tail;
  if (n <= o || !tailIsNew) return;
  if (tail?.role !== 'user') return;
  forceScrollToBottom();
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
      // 内容变化（思考流/工具时间线/Diff 展开/工时计时等）：仅在仍持有跟随权
      // （following=true 且无新鲜向上意图）时吸底；跳转/上滚浏览期间不打扰。
      scrollToBottom();
      refreshRail();
    });
    bodyObserver.observe(body, { childList: true, subtree: true, characterData: true, attributes: true });
  });
}

watch(() => store.messages.length, () => {
  // 跳转/上滚浏览期间由 following=false 天然拦截吸底，无需额外判断
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
    forceScrollToBottom();
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
  // 切会话回到底部并恢复跟随（force 顺带取消进行中的跳转校正），
  // 红点轨道按新会话消息重算
  forceScrollToBottom();
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

onUnmounted(() => {
  window.clearInterval(workTimer);
});
// 权限卡片出现/收起时同步滚动，保证用户能立即看到询问（强制回底）
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
}
function onPywebviewReady() {
  startInit();
}

function onGlobalKeydown(e: KeyboardEvent) {
  // 对话 / IDE 双模式切换：Ctrl+Alt+I。
  // 快捷键盘点（frontend/src 下 *.vue / *.ts）：应用内全局只绑了 Ctrl+F（会话检索）、
  // 编辑器内绑了 Ctrl+S；Ctrl+Shift+I 应用内未占用，但它是 Chromium / WebView2 的
  // DevTools 加速键、会被宿主抢先消费，故改用 Ctrl+Alt+I。
  // 绑定放在这里而不是 IdeLayout：IdeLayout 只在 IDE 模式下存在，挂在它上面只能退出、
  // 无法从对话模式进入 IDE；窗口级监听本就在 App 统一注册。
  if (!e.repeat && (e.ctrlKey || e.metaKey) && e.altKey && !e.shiftKey && e.key.toLowerCase() === 'i') {
    e.preventDefault();
    store.toggleIdeMode();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    // 拦截全局 Ctrl+F / Cmd+F 呼出会话多维检索面板
    e.preventDefault();
    if (store.sessionSearchModal.show) {
      store.closeSessionSearchModal();
    } else {
      store.openSessionSearchModal();
    }
    return;
  }
  // 键盘滚动键：登记用户滚动意图（useChatScroll 据此区分用户/程序化滚动）。
  // 输入控件内的光标按键不属于对话区滚动，忽略。
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
  document.addEventListener('click', closeHeaderPopovers);
  window.addEventListener('keydown', onGlobalKeydown);
  void syncInitialWindowMaximized();
  // 启动拉一次后台任务快照，并每 5 秒轮询：底部执行条、关闭确认的
  // hasActiveTasks 都依赖它保持新鲜（list_active_tasks 为进程内查询，开销极小）
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
  document.removeEventListener('click', closeHeaderPopovers);
  clearInterval(bgTaskPollTimer);
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
  /* IDE 模式下 #center 要绝对停靠到右侧（见下），这里建立定位上下文 */
  position: relative;
}

/* IDE 模式：对话区停靠到 #main 右侧，宽度按设计稿 368px。
   绝对定位让它脱离 flex 宽度分配——编辑器区因此拿到全部剩余宽度（设计稿就是这个
   关系），而不是和对话区按 flex:1 平分。收起对话区时整块 display:none，编辑器区
   自然全屏；恢复入口在对话头部、活动栏「星瑶」和编辑器右上角的浮动按钮三处。 */
#center.ide-docked {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 368px;
  z-index: 6;
  border-left: 1px solid var(--border);
  background: var(--bg1);
}

#center.ide-docked.ide-docked-hidden {
  display: none;
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

/* IDE 模式：对话区经 Teleport 迁进 368px 窄栏（#ide-chat-host）后，内部容器必须跟着
   收缩——外层 .ide-chat 的 min-width:0 只解开宿主自身，里面的表格 / 代码块仍会按
   min-content 撑破容器。这两个选择器只在 IDE 模式下命中（对话模式 #center 不在
   #ide-chat-host 内），因此对话模式的布局与加入本段之前完全一致 */
#ide-chat-host #center,
#ide-chat-host #center > * {
  min-width: 0;
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
  /* IDE 模式对话栏只有 368px 宽，会话名必须可压缩：空间不足时省略号截断，
     而不是把右侧操作按钮挤出行外 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#chat-header .crumb b {
  color: var(--text);
}
/* IDE 模式对话头部：星瑶头像块（与活动栏「星瑶」同一品牌色，取色走主题变量） */
#chat-header .ai-avatar {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}
/* IDE 模式对话头部：图标型 chip（只有图标、无文字），把宽度省给会话名 */
#chat-header .chip-btn.icon-only {
  padding: 4px 7px;
  flex-shrink: 0;
}
.top-spacer {
  flex: 1;
}

/* 会话头部“计划”入口：进行中徽标 + 运行时旋转图标；悬浮面板锚定其下 */
.plan-entry-wrap {
  position: relative;
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
  /* 会话区做成与下方输入框一致的浮层卡片：四周 6px 衬边。
     用 padding 而非 margin：margin 区不绘制本元素背景。
     衬边色跟随工作区分区的背景透明度混合（--theme-workspace-* 由主题运行时
     写在 documentElement 上）：实色主题 opacity=100% 衬边为纯白面板色；
     全局背景图皮肤下调低透明度时，衬边与会话区一起透出背景图，
     否则不透明白垫在半透明会话区下面会把背景图整块挡死。 */
  padding: 6px;
  background: color-mix(in srgb, var(--bg1) var(--theme-workspace-bg-opacity-pct, 100%), transparent);
}
#chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 10px;
  /* 圆角对齐输入框 #input-box（10px），内容随圆角裁剪；
     描边与输入框同款：主题里会话区底色常与四周留白的 app 底色几乎同色，
     没有这条 1px 描边时圆角卡片边界不可见（"看不到留白"的根因） */
  border: 1px solid var(--border);
  border-radius: 10px;
  /* 保留浏览器 scroll anchoring（默认 auto）：翻页 prepend 与图片/异步内容
     分批撑高时由渲染引擎持续保持视口锚点，这是"不回退"的主力机制。
     JS 侧 maybeLoadOlder 的补偿是绝对定位赋值（oldTop + 内容增量），
     幂等且与 anchoring 同向，仅作兜底，不会双重叠加。 */
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
/* 技能项：空间不足时优先收缩让位，不挤压其它状态项；强制单行，
   超宽用省略号截断（完整技能列表在 title 中查看） */
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
/* IDE 模式徽标：小胶囊样式（--accent-soft 底 + --accent 文字 + 字重 600），
   颜色一律走主题变量，不硬编码；不参与收缩，空间不足时先挤其它项 */
#statusbar .status-ide-mode {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
  height: 16px;
  padding: 0 8px;
  border-radius: 999px;
  flex-shrink: 0;
}
/* AI 生成内容声明：占据状态栏中段剩余空间并居中，窄窗口时优先收缩省略 */
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
/* 历史分页加载指示：细条、居中、弱化色，与现有轻提示风格一致 */
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
