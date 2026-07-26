<template>
  <div id="app-root">
    <!-- 顶栏 -->
    <Topbar />

    <!-- 主体区域 -->
    <div id="main">
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
          <button class="chip-btn" title="清空当前会话历史" @click="store.clearSessionHistory()">🗑 清空</button>
        </div>

        <!-- 对话主体滚动区 -->
        <div ref="chatScrollRef" id="chat-scroll">
          <div id="chat-body">
            <!-- Kimi 空状态卡片 -->
            <KimiEmptyState v-if="!store.messages || store.messages.length === 0" />

            <!-- 消息流列表 -->
            <template v-else>
              <MessageBubble
                v-for="(msg, i) in store.messages"
                :key="i"
                :msg="msg"
              />
            </template>
          </div>
        </div>

        <!-- 二级抽屉页面 -->
        <Subpage
          :visible="store.subpage.show"
          :sub-type="store.subpage.type"
          @close="store.closeSubpage()"
        />

        <!-- 底部固定输入框 (当存在消息时固定呈现) -->
        <InputArea v-if="store.messages && store.messages.length > 0" />
      </div>

      <!-- 右侧控制面板 -->
      <RightPanel />
    </div>

    <!-- 状态栏 -->
    <div id="statusbar">
      <span class="si">
        <span class="s-dot" :class="{ off: !store.connected }"></span>
        <span>{{ store.connected ? '后端已连接' : '连接已断开' }}</span>
      </span>
      <span class="si" :title="'当前模型: ' + (store.activeModelName || '未选择')">🤖 {{ store.activeModelName || '未选择模型' }}</span>
      <span class="si" :title="'当前工作区: ' + (store.workdir || store.currentWorkdirLabel)">📁 {{ store.currentWorkdirLabel }}</span>
      <span class="si" :class="statusTokenClass" title="上下文 Token 占用百分比">🔢 {{ statusTokenPct }}%</span>
      <span class="si">⚙ 权限: <span>{{ formatPermMode(store.permissionMode) }}</span></span>
      <div class="right">
        <span class="si">{{ store.running ? '生成中…' : '空闲' }}</span>
        <span class="si">UTF-8</span>
      </div>
    </div>

    <!-- 确认与权限弹窗 -->
    <ConfirmModal />
    <PermModal />

    <!-- 全局 Toast -->
    <div id="toast" :class="[store.toast.show ? 'show' : '', store.toast.type]">
      <span>{{ store.toast.msg }}</span>
      <button v-if="store.toast.action" class="toast-action" @click="store.toast.action.fn()">{{ store.toast.action.label }}</button>
    </div>
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
import PermModal from './components/PermModal.vue';
import Subpage from './components/Subpage.vue';

const store = useAppStore();
const chatScrollRef = ref<HTMLElement | null>(null);

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

function scrollToBottom() {
  nextTick(() => {
    if (chatScrollRef.value) {
      chatScrollRef.value.scrollTop = chatScrollRef.value.scrollHeight;
    }
  });
}

watch(() => store.messages.length, () => {
  scrollToBottom();
});
watch(() => store.currentStreamingText, () => {
  scrollToBottom();
});

onMounted(() => {
  document.addEventListener('click', closeExportMenu);

  if (window.pywebview && window.pywebview.api) {
    store.initFromBackend();
  } else {
    window.addEventListener('pywebviewready', () => {
      store.initFromBackend();
    });
    // 兜底轮询监听 PyWebView ready 注入
    let attempts = 0;
    const timer = setInterval(() => {
      attempts++;
      if (window.pywebview && window.pywebview.api) {
        clearInterval(timer);
        store.initFromBackend();
      } else if (attempts > 60) {
        clearInterval(timer);
      }
    }, 100);
  }
});

onUnmounted(() => {
  document.removeEventListener('click', closeExportMenu);
});
</script>

<style scoped>
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
  border-color: var(--accent);
}

.export-wrap {
  position: relative;
}
.export-menu {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  background: rgba(22, 27, 34, 0.97);
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

#toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(80px);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 10px 18px;
  font-size: 13px;
  z-index: 10000;
  transition: transform 0.3s;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  gap: 12px;
}
#toast.show {
  transform: translateX(-50%);
}
#toast.ok {
  border-color: var(--green);
}
#toast.warn {
  border-color: var(--orange);
}
.toast-action {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  flex-shrink: 0;
  transition: 0.15s;
}
.toast-action:hover {
  opacity: 0.85;
}
</style>
