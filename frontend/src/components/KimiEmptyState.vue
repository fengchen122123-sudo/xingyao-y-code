<template>
  <div class="kimi-empty-container">
    <div class="kimi-hero">
      <div class="kimi-title-row">
        <span class="kimi-title">让 星瑶 来帮你完成任务</span>
        <span class="kimi-beta">Beta</span>
      </div>
    </div>

    <div class="kimi-card">
      <AttachmentChips :items="attachments" @remove="removeAttachment" />
      <textarea 
        v-model="inputVal" 
        placeholder="尽管问，或做个任务..." 
        @keydown.enter.exact.prevent="handleSend"
        @paste="handlePaste"
      ></textarea>

      <div class="kimi-card-bar">
        <div style="display:flex;align-items:center;gap:8px">
          <button
            data-test="attachment-button"
            class="kimi-pill-btn"
            title="添加附件"
            @click="handlePickFiles"
          >＋</button>
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="kimi-pill-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >
            <span>{{ formatPermMode(store.permissionMode) }}</span>
            <span class="permission-chevron">▼</span>
          </button>
        </div>

        <div style="display:flex;align-items:center;gap:10px">
          <button
            ref="modelBtnRef"
            class="kimi-pill-btn"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >
            <span>{{ store.activeModelName || '选择模型' }}</span>
            <span style="font-size:10px;color:var(--text-faint)">▼</span>
          </button>

          <button
            class="kimi-send-btn"
            :class="{ active: (inputVal.trim() || attachments.length) && !store.running }"
            :disabled="store.running"
            title="发送消息 (Enter)"
            @click="handleSend"
          >↑</button>
        </div>
      </div>
    </div>

    <div
      ref="workdirBtnRef"
      class="kimi-footer-workdir"
      @click.stop="toggleWorkdirDropdown"
    >
      <span>{{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '进入项目工作区' : '当前工作区: ' + store.currentWorkdirLabel }}</span>
      <span style="font-size:10px;color:var(--text-faint)">▼</span>
    </div>

    <!-- 工作区切换下拉：可退出工作区回自由对话 / 快捷切换项目 / 引入新项目 -->
    <div v-if="showWorkdirDropdown" class="workdir-dropdown" :style="workdirDropdownStyle" @click.stop>
      <div
        v-if="!isGlobalWorkdir"
        class="workdir-item"
        @click="chooseGlobal"
      >
        <span class="wd-label">💬 自由对话（退出工作区）</span>
      </div>
      <div
        v-for="g in projectGroups"
        :key="g.folder_path"
        class="workdir-item"
        :class="{ active: g.folder_path === store.workdir }"
        @click="chooseProject(g.folder_path)"
      >
        <span class="wd-label">📁 {{ g.folder_name }}</span>
        <span v-if="g.folder_path === store.workdir" class="wd-check">✓</span>
      </div>
      <div class="workdir-sep"></div>
      <div class="workdir-item" @click="chooseOpenFolder">
        <span class="wd-label">➕ 进入项目工作区…</span>
      </div>
    </div>

    <!-- 下拉弹出节点 -->
    <ModelCascader 
      :visible="showModelCascader" 
      :anchor-el="modelBtnRef" 
      @close="showModelCascader = false" 
    />
    <PermDropdown 
      :visible="showPermDropdown" 
      :anchor-el="permBtnRef" 
      @close="showPermDropdown = false" 
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import AttachmentChips from './AttachmentChips.vue';
import { useAttachments } from '../composables/useAttachments';

const store = useAppStore();
const inputVal = ref('');

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const showWorkdirDropdown = ref(false);
const workdirDropdownStyle = ref<Record<string, string>>({});

const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);
const workdirBtnRef = ref<HTMLElement | null>(null);

// 工作区下拉数据：非 GLOBAL 的项目分组
const projectGroups = computed(() =>
  store.sessionGroups.filter(g => !g.is_global && g.folder_path && g.folder_path !== 'GLOBAL')
);
const isGlobalWorkdir = computed(() =>
  !store.workdir || store.currentWorkdirLabel === '对话' || store.currentWorkdirLabel === 'GLOBAL'
);

function toggleWorkdirDropdown() {
  showWorkdirDropdown.value = !showWorkdirDropdown.value;
  if (showWorkdirDropdown.value && workdirBtnRef.value) {
    const r = workdirBtnRef.value.getBoundingClientRect();
    workdirDropdownStyle.value = {
      position: 'fixed',
      bottom: `${window.innerHeight - r.top + 6}px`,
      left: `${r.left}px`,
    };
  }
}

function chooseGlobal() {
  showWorkdirDropdown.value = false;
  store.exitWorkspaceToGlobal();
}

function chooseProject(folderPath: string) {
  showWorkdirDropdown.value = false;
  if (folderPath !== store.workdir) {
    store.switchToWorkspaceGroup(folderPath);
  }
}

function chooseOpenFolder() {
  showWorkdirDropdown.value = false;
  store.openWorkspaceFolder();
}

function onGlobalClickForWorkdir(e: MouseEvent) {
  if (showWorkdirDropdown.value && workdirBtnRef.value && !workdirBtnRef.value.contains(e.target as Node)) {
    showWorkdirDropdown.value = false;
  }
}
onMounted(() => document.addEventListener('click', onGlobalClickForWorkdir));
onUnmounted(() => document.removeEventListener('click', onGlobalClickForWorkdir));

function toggleModelCascader() {
  showPermDropdown.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
  showModelCascader.value = false;
  showPermDropdown.value = !showPermDropdown.value;
}

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste } = useAttachments();

async function handlePickFiles() {
  await pickAttachments();
}

function handleSend() {
  if (store.running) return;
  const text = inputVal.value;
  const paths = attachments.value.map(a => a.path);
  if (!text.trim() && !paths.length) return;
  inputVal.value = '';
  clearAttachments();
  store.sendMessage(text, paths);
}
</script>

<style scoped>
.workdir-dropdown {
  z-index: 999;
  min-width: 220px;
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  padding: 6px;
  backdrop-filter: blur(14px);
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}
.workdir-item {
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-dim);
  transition: 0.15s;
  user-select: none;
}
.workdir-item:hover,
.workdir-item.active {
  background: var(--bg3);
  color: var(--text);
}
.workdir-item .wd-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.workdir-item .wd-check {
  color: var(--accent);
  font-size: 12px;
  margin-left: 8px;
}
.workdir-sep {
  height: 1px;
  background: var(--border);
  margin: 5px 6px;
}
</style>
