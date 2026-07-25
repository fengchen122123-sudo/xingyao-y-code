<template>
  <div id="app-root">
    <!-- 顶栏 -->
    <Topbar />

    <!-- 主体区域 -->
    <div id="main">
      <!-- 左侧边栏 -->
      <Sidebar @open-sub="openSubpage" />

      <!-- 中间主对话与聊天区域 -->
      <div id="center">
        <div id="chat-header">
          <span class="crumb">会话 / <b>{{ store.currentSessionDisplayName }}</b></span>
          <span class="top-spacer"></span>
          <button class="chip-btn" title="导出会话" @click="store.showToast('导出会话 (Markdown)', 'ok')">⤓ 导出</button>
          <button class="chip-btn" title="清空会话" @click="clearCurrentSession">🗑 清空</button>
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
          :visible="showSubpage" 
          :sub-type="currentSubpageType" 
          @close="showSubpage = false" 
        />

        <!-- 底部固定输入框 (当存在消息时固定呈现) -->
        <InputArea v-if="store.messages && store.messages.length > 0" />
      </div>

      <!-- 右侧控制面板 -->
      <RightPanel />
    </div>

    <!-- 状态栏 -->
    <div id="statusbar">
      <span class="si"><span class="s-dot"></span><span>后端已连接</span></span>
      <span class="si">⚙ 权限: <span>{{ store.permissionMode }}</span></span>
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
      {{ store.toast.msg }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick, watch } from 'vue';
import { useAppStore } from './stores/appStore';

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

const showSubpage = ref(false);
const currentSubpageType = ref<'skills' | 'mcp'>('skills');

function openSubpage(type: 'skills' | 'mcp') {
  currentSubpageType.value = type;
  showSubpage.value = true;
}

function clearCurrentSession() {
  store.messages = [];
  store.showToast('已重置为空白新会话');
}

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
</style>
