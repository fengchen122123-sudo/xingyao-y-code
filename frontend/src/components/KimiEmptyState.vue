<template>
  <div class="kimi-empty-container">
    <div class="kimi-hero">
      <div class="kimi-title-row">
        <span class="kimi-title">让 {{ store.agentName }} 来帮你完成任务</span>
        <span class="kimi-beta">Beta</span>
      </div>
    </div>

    <div class="kimi-card" ref="kimiCardRef" :class="{ 'drop-hover': dragging }">
      <AttachmentChips :items="attachments" @remove="removeAttachment" @open="openAttachmentImage" />
      <!-- / 斜杠命令 与 @ 文件引用的自动补全面板 -->
      <InputSuggest
        :items="suggestItems"
        :active-index="suggestIndex"
        @select="applySuggest"
        @hover="i => suggestIndex = i"
      />
      <textarea
        ref="textareaRef"
        v-model="inputVal"
        placeholder="尽管问，或做个任务… / 调用技能与 MCP，@ 引用文件"
        @keydown="handleKeydown"
        @input="detectToken"
        @click="detectToken"
        @keyup="detectToken"
        @paste="handlePaste"
      ></textarea>

      <div class="kimi-card-bar">
        <div style="display:flex;align-items:center;gap:8px">
          <button
            ref="attachBtnRef"
            data-test="attachment-button"
            class="kimi-pill-btn"
            title="添加附件"
            @click.stop="onAttachClick"
          ><AppIcon name="paperclip" :size="14" /></button>
          <!-- 屏幕截图：全屏框选屏幕任意区域；截图「发到输入框」后后端经
               snip_attachment_ready 桥事件回传路径，由 store 加入输入框附件 -->
          <button
            data-test="screen-snip-button"
            class="kimi-pill-btn"
            title="截图：框选屏幕任意区域"
            @click.stop="onScreenSnipClick"
          ><AppIcon name="scissors" :size="14" /></button>
          <!-- 触摸设备隐藏文件入口：点击后由系统弹出拍照/相册/文件选择 -->
          <input ref="touchFileInputRef" type="file" multiple style="display:none" @change="onPickInputChange">
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="kimi-pill-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >
            <AppIcon class="perm-icon" name="shield" :size="13" :stroke-width="2" /><span class="perm-label">{{ formatPermMode(store.permissionMode) }}</span>
            <AppIcon class="permission-chevron" name="chevron-down" :size="11" :stroke-width="2" />
          </button>
          <!-- 沙盒限制开关（绿框位）：与输入框沙盒 chip 同一后端配置 -->
          <button
            data-test="hero-sandbox-button"
            class="kimi-pill-btn sandbox-pill"
            :class="{ 'sandbox-open': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件（点击恢复限制）' : '沙盒已开启：仅允许访问工作目录内文件（点击解除限制）'"
            @click.stop="store.setSandbox(!store.sandboxEnabled)"
          ><span class="sandbox-label-full">{{ store.sandboxEnabled ? '沙盒: 开' : '沙盒: 限' }}</span><span class="sandbox-label-short">{{ store.sandboxEnabled ? '开' : '限' }}</span></button>
        </div>

        <div style="display:flex;align-items:center;gap:10px">
          <button
            ref="modelBtnRef"
            data-test="model-button"
            class="kimi-pill-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >
            <AppIcon class="model-icon" name="box" :size="13" :stroke-width="2" /><span class="model-label">{{ store.activeModelName || '选择模型' }}</span>
            <AppIcon name="chevron-down" :size="10" :stroke-width="2" class="model-chevron" />
          </button>

          <ReasoningSelector
            ref="reasoningSelectorRef"
            variant="hero"
            @open="closeComposerMenus"
          />

          <button
            data-test="send-button"
            class="kimi-send-btn"
            :class="{ active: (inputVal.trim() || attachments.length) && !store.currentSessionRunning }"
            :disabled="store.currentSessionRunning"
            title="发送消息 (Enter)"
            @click="handleSend"
          ><AppIcon name="arrow-up" :size="16" :stroke-width="2.25" /></button>
        </div>
      </div>
    </div>

    <div
      ref="workdirBtnRef"
      class="kimi-footer-workdir"
      @click.stop="toggleWorkdirDropdown"
    >
      <span>{{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '进入项目工作区' : '当前工作区: ' + store.currentWorkdirLabel }}</span>
      <AppIcon name="chevron-down" :size="11" :stroke-width="2" class="model-chevron" />
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
        <span class="wd-label"><AppIcon name="folder" :size="13" /> {{ g.folder_name }}</span>
        <span v-if="g.folder_path === store.workdir" class="wd-check"><AppIcon name="check" :size="13" :stroke-width="2" /></span>
      </div>
      <div class="workdir-sep"></div>
      <div class="workdir-item" @click="chooseOpenFolder">
        <span class="wd-label"><AppIcon name="plus" :size="13" /> 进入项目工作区…</span>
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

    <!-- 触摸设备不再使用应用内附件菜单：点回形针直接唤起系统文件选择器 -->
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import AppIcon from './AppIcon.vue';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import ReasoningSelector from './ReasoningSelector.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest from './InputSuggest.vue';
import { useAttachments } from '../composables/useAttachments';
import { useIsTouch } from '../composables/useIsTouch';
import { useSuggest } from '../composables/useSuggest';

const store = useAppStore();
const inputVal = computed<string>({
  get: () => store._getSessionRuntime().composerText || '',
  set: value => { store._getSessionRuntime().composerText = value; },
});
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const showWorkdirDropdown = ref(false);
const workdirDropdownStyle = ref<Record<string, string>>({});

const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);
const workdirBtnRef = ref<HTMLElement | null>(null);
const reasoningSelectorRef = ref<{ close: () => void } | null>(null);

// 工作区下拉数据：非 GLOBAL 的项目分组（秘书分组不是工作区，选它会切到
// folder_path='SECRETARY' 这个并不存在的"目录"，故一并过滤）
const projectGroups = computed(() =>
  store.sessionGroups.filter(g => !g.is_global && !g.is_secretary && g.folder_path && g.folder_path !== 'GLOBAL')
);
const isGlobalWorkdir = computed(() =>
  !store.workdir || store.currentWorkdirLabel === '对话' || store.currentWorkdirLabel === 'GLOBAL'
);

function toggleWorkdirDropdown() {
  reasoningSelectorRef.value?.close();
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
  reasoningSelectorRef.value?.close();
  showPermDropdown.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
  reasoningSelectorRef.value?.close();
  showModelCascader.value = false;
  showPermDropdown.value = !showPermDropdown.value;
}

function closeComposerMenus() {
  showModelCascader.value = false;
  showPermDropdown.value = false;
  showWorkdirDropdown.value = false;
}

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste, dragging, bindDrop, uploadFiles, openAttachmentImage } = useAttachments();
const { suggestItems, suggestIndex, detectToken, applySuggest, onKeydown, clearSuggest } = useSuggest(inputVal, textareaRef);

// 输入卡片容器：拖拽文件进入时高亮提示，drop 由原生事件桥（pywebview）提供完整路径
const kimiCardRef = ref<HTMLElement | null>(null);
onMounted(() => { if (kimiCardRef.value) bindDrop(kimiCardRef.value); });

onUnmounted(() => clearSuggest());

async function handlePickFiles() {
  await pickAttachments();
}

// 触摸设备（手机/平板）：点回形针直接触发隐藏文件入口，由系统弹出拍照/相册/文件选择；
// 桌面仍走系统文件对话框
const isTouch = useIsTouch();
const touchFileInputRef = ref<HTMLInputElement | null>(null);

function onAttachClick() {
  if (!isTouch.value) { handlePickFiles(); return; }
  touchFileInputRef.value?.click();
}

// 屏幕截图：调后端打开全屏框选遮罩窗口；截图结果经桥事件
// snip_attachment_ready 回传（见 appStore._attachSnipToComposer）
async function onScreenSnipClick() {
  const api = window.pywebview?.api;
  if (!api?.screen_snip_start) {
    store.notifyChat('桌面桥未就绪，无法使用截图');
    return;
  }
  try {
    const res = await api.screen_snip_start();
    if (res && !res.ok) store.notifyChat(res.msg || '截图启动失败');
  } catch {
    store.notifyChat('截图启动失败：桥接调用异常');
  }
}

async function onPickInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';  // 允许重复选同一文件
  if (files.length) await uploadFiles(files);
}

function handleSend() {
  if (store.currentSessionRunning) return;
  // 补全面板打开时由 onKeydown 拦截，不会走到这里
  const text = inputVal.value;
  // 拖拽的附件还在等路径时不允许发送，避免把占位的假路径发给后端
  if (attachments.value.some(a => a.loading)) {
    store.notifyChat('附件路径加载中，请稍候…');
    return;
  }
  const paths = attachments.value.map(a => a.path);
  if (!text.trim() && !paths.length) return;
  inputVal.value = '';
  clearAttachments();
  store.sendMessage(text, paths);
}

// 独立的 Enter 发送处理：onKeydown 没拦截时（补全未打开）才发送
function handleKeydown(e: KeyboardEvent) {
  onKeydown(e);
  // 补全已处理（导航键/选中/关闭），不需要进一步处理
  if (e.defaultPrevented) return;
  // Enter 发送（补全关闭时）
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
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
/* 沙盒开关解除态：橙色提示（与输入框沙盒 chip 一致） */
.sandbox-pill.sandbox-open {
  border-color: var(--orange);
  color: var(--orange);
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
  display: flex;
  align-items: center;
  gap: 6px;
}
.workdir-item .wd-check {
  color: var(--accent);
  margin-left: 8px;
}
/* 模型/工作区按钮上的小箭头（对齐旧 10px 文字箭头的视觉重量） */
.model-chevron {
  color: var(--text-faint);
}
.workdir-sep {
  height: 1px;
  background: var(--border);
  margin: 5px 6px;
}
/* 紧凑形态元素（图标）仅手机端显示，桌面端隐藏 */
.perm-icon,
.model-icon,
.sandbox-label-short {
  display: none;
}

/* ── 宽度不足时的图标化收纳（容器查询，对齐手机版形态）──
   侧栏展开挤窄首页卡片时禁止按钮换行，按 .kimi-card 容器宽度分档，
   档位与对话中输入栏一致：≤540 权限·模型转图标 / ≤430 沙盒收成 开/限 */
.kimi-card {
  container-type: inline-size;
  container-name: composer;
}
@container composer (max-width: 540px) {
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron,
  .model-chip .model-label,
  .model-chip .model-chevron {
    display: none;
  }
  .perm-icon,
  .model-icon {
    display: inline-flex;
  }
}
@container composer (max-width: 430px) {
  .sandbox-label-full {
    display: none;
  }
  .sandbox-label-short {
    display: inline;
  }
}

@media (max-width: 768px) {
  /* 手机端收窄按钮：权限/模型只显示图标（颜色仍跟随状态），对齐对话中输入栏样式 */
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron,
  .model-chip .model-label,
  .model-chip .model-chevron {
    display: none;
  }
  .perm-icon,
  .model-icon {
    display: inline-flex;
  }
  /* 手机端沙盒只留 开/限 */
  .sandbox-pill {
    font-size: 12px;
    padding: 6px 9px;
  }
}
</style>
