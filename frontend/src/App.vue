<template>
  <div id="app-root">
    <!-- 顶栏 -->
    <Topbar />

    <!-- 主体区域 -->
    <div id="main">
      <SettingsPage v-if="store.settingsOpen" />
      <template v-else>
      <!-- 左侧边栏 -->
      <Sidebar />

      <!-- 中间主对话与聊天区域 -->
      <div id="center">
        <div id="chat-header">
          <span class="crumb">会话 / <b>{{ store.currentSessionDisplayName }}</b></span>
          <span class="top-spacer"></span>

          <div class="export-wrap">
            <button class="chip-btn" title="导出会话" @click.stop="showExportMenu = !showExportMenu">⤓ 导出</button>
            <div v-if="showExportMenu" class="export-menu" @click.stop>
              <div class="export-item" @click="doExport('md')">Markdown (.md)</div>
              <div class="export-item" @click="doExport('html')">HTML (.html)</div>
              <div class="export-item" @click="doExport('json')">JSON (.json)</div>
            </div>
          </div>
          <button class="chip-btn" title="清空当前会话历史" @click="store.clearSessionHistory()">清空</button>
        </div>

        <!-- 对话主体滚动区 -->
        <div ref="chatScrollRef" id="chat-scroll">
          <div id="chat-body">
            <!-- Kimi 空状态卡片 -->
            <KimiEmptyState v-if="!store.messages || store.messages.length === 0" />

            <!-- 消息流列表 -->
            <template v-else>
              <MessageBubble
                v-for="msg in store.messages"
                :key="msgKey(msg)"
                :msg="msg"
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
              <span class="wi-check">✓</span>
              <span class="wi-text">星瑶本轮工作 {{ fmtWorkTime(finalWorkTime) }}</span>
            </div>
          </div>
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

      <!-- 右侧控制面板 -->
      <RightPanel />
      </template>
    </div>

    <!-- 状态栏 -->
    <div id="statusbar">
      <span class="si">
        <span class="s-dot" :class="{ off: !store.connected }"></span>
        <span>{{ store.connected ? '后端已连接' : '连接已断开' }}</span>
      </span>
      <span class="si" :title="'当前模型: ' + (store.activeModelName || '未选择')">{{ store.activeModelName || '未选择模型' }}</span>
      <span class="si" :title="'当前工作区: ' + (store.workdir || store.currentWorkdirLabel)">{{ store.currentWorkdirLabel }}</span>
      <span class="si" :class="statusTokenClass" title="上下文 Token 占用百分比">Token {{ statusTokenPct }}%</span>
      <span class="si">权限: <span>{{ formatPermMode(store.permissionMode) }}</span></span>
      <span
        class="si"
        :class="{ 'si-warn': store.sandboxEnabled }"
        :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅允许访问工作目录内文件'"
      >沙盒: {{ store.sandboxEnabled ? '解除' : '限制' }}</span>
      <div class="right">
        <span class="si">{{ store.running ? '生成中…' : '空闲' }}</span>
        <span class="si">UTF-8</span>
      </div>
    </div>

    <!-- 确认弹窗 -->
    <ConfirmModal />

    <!-- 图片大图查看 -->
    <ImageLightbox />

    <!-- 全局通知已迁移：对话类提示走对话流 notice 消息，非对话类提示走 ConfirmModal notice 模式 -->

    <!-- 启动画面：初始化期间全屏展示，加载完成后淡出进入对话 -->
    <Transition name="splash">
      <div v-if="!store.initialized" id="splash">
        <img class="sp-logo" src="/icon.png" alt="Y-CODE">
        <div class="sp-title">Y-CODE</div>
        <div class="sp-sub">星瑶·编码助手</div>
        <div class="sp-bar"><i></i></div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed, watch } from 'vue';
import { useAppStore } from './stores/appStore';
import { formatPermMode } from './utils/markdown';

import Topbar from './components/Topbar.vue';
import Sidebar from './components/Sidebar.vue';
import KimiEmptyState from './components/KimiEmptyState.vue';
import MessageBubble from './components/MessageBubble.vue';
import InputArea from './components/InputArea.vue';
import RightPanel from './components/RightPanel.vue';
import ConfirmModal from './components/ConfirmModal.vue';
import PermCard from './components/PermCard.vue';
import ImageLightbox from './components/ImageLightbox.vue';
import Subpage from './components/Subpage.vue';
import SettingsPage from './components/SettingsPage.vue';

const store = useAppStore();
const chatScrollRef = ref<HTMLElement | null>(null);

const showExportMenu = ref(false);
// 滚动合并标记：同一帧内多次 scrollToBottom 只执行一次
let scrollRaf: number | undefined;

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

function scrollToBottom() {
  // 合并到下一帧：流式输出期间会被高频调用，逐帧滚动足够顺滑且避免布局抖动
  if (scrollRaf !== undefined) return;
  scrollRaf = window.requestAnimationFrame(() => {
    scrollRaf = undefined;
    if (chatScrollRef.value) {
      chatScrollRef.value.scrollTop = chatScrollRef.value.scrollHeight;
    }
  });
}

// M32 修复：消息列表 key 不用数组索引（插入/删除时组件局部状态会错挂）。
// 消息对象没有后端 id，这里按对象身份分配稳定的渲染 key：
// 同一消息对象在列表内移动/前后增删时 key 不变，组件状态不错位。
const msgKeyMap = new WeakMap<object, number>();
let msgKeySeq = 0;
function msgKey(msg: object): number {
  let k = msgKeyMap.get(msg);
  if (k === undefined) {
    k = ++msgKeySeq;
    msgKeyMap.set(msg, k);
  }
  return k;
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
      const el = chatScrollRef.value;
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
      if (nearBottom) scrollToBottom();
    });
    bodyObserver.observe(body, { childList: true, subtree: true, characterData: true, attributes: true });
  });
}

watch(() => store.messages.length, () => {
  scrollToBottom();
  ensureBodyObserver();
});
watch(() => store.currentStreamingText, () => {
  scrollToBottom();
});
// 从设置页返回对话：v-if 切换会重建对话区 DOM，滚动位置被重置到顶部，
// 这里在 DOM 重建完成后滚回底部，保持"始终停留在最新消息"的体验
watch(() => store.settingsOpen, (open) => {
  if (!open) {
    scrollToBottom();
    ensureBodyObserver();
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
// 权限卡片出现/收起时同步滚动，保证用户能立即看到询问
watch(() => store.permRequest.show, () => {
  scrollToBottom();
});
// M25 修复：pywebviewready 事件与 100ms 兜底轮询是双通道，原先无互斥，
// initFromBackend 可能执行两次互相覆盖；且事件监听器注册后从不移除。
// initStarted 互斥标志保证只初始化一次，触发后移除监听并停轮询。
let initStarted = false;
let initPollTimer: number | undefined;
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

  if (window.pywebview && window.pywebview.api) {
    startInit();
  } else {
    window.addEventListener('pywebviewready', onPywebviewReady);
    // 兜底轮询监听 PyWebView ready 注入
    let attempts = 0;
    initPollTimer = window.setInterval(() => {
      attempts++;
      if (window.pywebview && window.pywebview.api) {
        startInit();
      } else if (attempts > 60) {
        clearInterval(initPollTimer);
        initPollTimer = undefined;
        window.removeEventListener('pywebviewready', onPywebviewReady);
      }
    }, 100);
  }

  // 首次挂载后注册 DOM 观察器（chat-body 可能尚未渲染，由 nextTick 兜底）
  ensureBodyObserver();
});
onUnmounted(() => {
  document.removeEventListener('click', closeExportMenu);
  window.removeEventListener('pywebviewready', onPywebviewReady);
  if (initPollTimer !== undefined) {
    clearInterval(initPollTimer);
    initPollTimer = undefined;
  }
  bodyObserver?.disconnect();
  bodyObserver = undefined;
  observedBody = null;
  if (scrollRaf !== undefined) window.cancelAnimationFrame(scrollRaf);
  scrollRaf = undefined;
});
</script>

<style scoped>
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
  background: var(--bg0);
  color: var(--text);
  font-family: var(--sans);
  overflow: hidden;
}

#main {
  flex: 1;
  display: flex;
  min-height: 0;
}

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
  background: var(--bg1);
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

#chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 10px;
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
  background: var(--bg1);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 12px;
  gap: 16px;
  font-size: 11px;
  color: var(--text-faint);
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
.si.tok-orange {
  color: var(--orange);
}
.si.tok-red {
  color: var(--red);
}
</style>
