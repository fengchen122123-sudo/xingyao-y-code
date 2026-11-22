<template>
  <div
    id="input-area"
    data-theme-region="composer"
    :class="{ 'is-resizing': isResizing, 'right-panel-open': !store.rightPanelCollapsed }"
  >
    <div
      class="resize-handle"
      data-test="composer-resize-handle"
      title="拖拽调节输入框高度 · 双击恢复默认"
      :class="{ active: isResizing }"
      @pointerdown="onHandlePointerdown"
      @dblclick="resetHeight"
    ></div>
    <div v-if="queueItems.length" id="queue-strip">
      <div
        v-for="(item, idx) in queueItems"
        :key="item.id"
        class="queue-item"
        data-test="queue-item"
      >
        <span class="queue-badge">{{ idx + 1 }}</span>
        <span class="queue-text" :title="item.text">{{ item.text }}</span>
        <button
          class="queue-btn queue-jump-btn"
          data-test="queue-jump-btn"
          title="停止当前任务并优先发送这条消息"
          @click="store.jumpQueue(store.currentSessionName, item.id)"
        ><AppIcon name="zap" :size="13" :stroke-width="2" /></button>
        <button
          class="queue-btn queue-undo-btn"
          data-test="queue-undo-btn"
          title="撤销到输入框"
          @click="undoQueue(item)"
        ><AppIcon name="undo" :size="13" :stroke-width="2" /></button>
        <button
          class="queue-btn queue-cancel-btn"
          data-test="queue-cancel-btn"
          title="取消排队"
          @click="store.cancelQueuedMessage(store.currentSessionName, item.id)"
        ><AppIcon name="trash" :size="13" :stroke-width="2" /></button>
      </div>
    </div>

    <div id="input-wrap">
      <div v-if="store.guiTheme?.id === 'builtin.qq2009'" class="qq09-toolbar" data-test="qq09-toolbar">
        <button
          type="button"
          class="qq09-tb-btn"
          data-test="qq09-tb-image"
          title="发送图片：打开附件选择（png / jpg / gif / webp 等，选中后显示缩略图）"
          @click="onAttachClick"
        >
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="4.5" width="18" height="15" rx="2.5" />
            <circle cx="8.5" cy="9.5" r="1.6" />
            <path d="M4 17.8l4.8-4.8 3.4 3.4 3-2.8 4.8 4.8" />
          </svg>
        </button>
        <button
          type="button"
          class="qq09-tb-btn"
          data-test="qq09-tb-file"
          title="发送文件：打开附件选择（任意格式，可多选）"
          @click="onAttachClick"
        >
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M3.5 7.2a2 2 0 0 1 2-2h3.4l1.8 2h8.2a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z" />
            <path d="M7.5 11.5h9" />
          </svg>
        </button>
      </div>
      <div id="input-box" ref="inputBoxRef" :class="{ 'drop-hover': dragging }">
        <AttachmentChips :items="attachments" @remove="removeAttachment" @open="openAttachmentImage" />
        <InputSuggest
          :items="suggestItems"
          :active-index="suggestIndex"
          :empty-hint="suggestEmpty"
          @select="applySuggest"
          @hover="i => suggestIndex = i"
        />
        <div class="composer-field">
          <ComposerEditor
            ref="editorRef"
            v-model="inputVal"
            :height="textareaHeight"
            :skill-ids="skillIds"
            :command-ids="commandIds"
            :placeholder="`向${store.agentName}提问… / 调用技能与 MCP，@ 引用文件`"
            @keydown="onKeydown"
            @input="detectToken"
            @click="detectToken"
            @keyup="detectToken"
            @paste="handlePaste"
          />
        </div>

        <div id="input-bar">
          <button
            ref="attachBtnRef"
            data-test="attachment-button"
            class="chip-btn plus-btn"
            title="附加文件"
            @click.stop="onAttachClick"
          ><AppIcon name="paperclip" :size="14.5" :stroke-width="1.8" /></button>
          <button
            data-test="screen-snip-button"
            class="chip-btn snip-btn"
            title="截图：框选屏幕任意区域"
            @click.stop="onScreenSnipClick"
          ><AppIcon name="scissors" :size="14.5" :stroke-width="1.8" /></button>
          <input ref="touchFileInputRef" type="file" multiple style="display:none" @change="onPickInputChange">
          <span class="composer-sep" aria-hidden="true"></span>
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="chip-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          ><span class="perm-dot" aria-hidden="true"></span><AppIcon class="perm-icon" name="shield" :size="13" :stroke-width="2" /><span class="perm-label">{{ formatPermMode(store.permissionMode) }}</span></button>
          <button
            data-test="sandbox-button"
            class="chip-btn sandbox-btn"
            :class="{ 'sandbox-open': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件（点击恢复限制）' : '沙盒已开启：仅允许访问工作目录内文件（点击解除限制）'"
            @click.stop="store.setSandbox(!store.sandboxEnabled)"
          ><span class="sb-dot" aria-hidden="true"></span><span class="sandbox-label-full">{{ store.sandboxEnabled ? '沙盒已解除' : '沙盒限制中' }}</span><span class="sandbox-label-short">{{ store.sandboxEnabled ? '开' : '限' }}</span></button>
          <span
            ref="ctxRingRef"
            class="ctx-ring"
            :class="tokenClass"
            data-test="context-ring"
            @mouseenter="openContextUsage"
            @mouseleave="scheduleContextUsageClose"
            @click.stop="onCtxRingClick"
          >
            <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true">
              <circle class="ctx-ring-bg" cx="10" cy="10" r="7.5" />
              <circle
                class="ctx-ring-fg"
                cx="10"
                cy="10"
                r="7.5"
                :stroke-dasharray="`${ctxArc} ${CTX_CIRCUMFERENCE}`"
              />
            </svg>
            <span class="ctx-ring-pct">{{ Math.round(tokenPct) }}%</span>
          </span>
          <button
            ref="modelBtnRef"
            data-test="model-button"
            class="chip-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          ><AppIcon class="model-icon" name="box" :size="13" :stroke-width="2" /><span class="model-label">{{ store.activeModelName || '选择模型' }}</span><span class="model-chevron" aria-hidden="true">▾</span></button>
          <ReasoningSelector
            ref="reasoningSelectorRef"
            variant="compact"
            @open="closeComposerMenus"
          />
          <button
            id="send-btn"
            data-test="send-button"
            :class="{ stop: sendStopMode, active: !sendStopMode && hasInputContent }"
            :title="sendStopMode ? '停止生成' : (store.currentSessionRunning ? '排队发送 (Enter)' : '发送消息 (Enter)')"
            @click="handleSend"
          >
            <AppIcon v-if="sendStopMode" name="square" :size="11" :stroke-width="2" />
            <AppIcon v-else name="arrow-up" :size="14" :stroke-width="2.2" />
          </button>
        </div>
      </div>
    </div>

    <ModelCascader
      :visible="showModelCascader"
      :anchor-el="modelBtnRef"
      @close="showModelCascader = false"
    />
    <ContextUsagePanel
      :visible="showContextUsage"
      :anchor-el="ctxRingRef"
      @hover-enter="cancelContextUsageClose"
      @hover-leave="scheduleContextUsageClose"
    />
    <PermDropdown
      :visible="showPermDropdown"
      :anchor-el="permBtnRef"
      @close="showPermDropdown = false"
    />
    <TokenUsagePopover
      :visible="showTokenPopover"
      :anchor-el="ctxRingRef"
      @close="showTokenPopover = false"
    />

    <div id="input-meta">
      <div class="l">
        <span
          ref="tokenSummaryRef"
          class="token-summary token-summary-clickable"
          data-test="token-summary"
          :title="summaryTitle"
          @click.stop="toggleTokenPopover"
        >{{ tokenSummary }}</span>
      </div>
      <div class="r keyboard-hints" data-test="keyboard-hints">
        <span class="ctx-percent" :class="tokenClass" :title="ctxTooltip">{{ ctxPctLabel }}</span>
        <span class="hint-full"><span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { QueuedMessage } from '../types/pywebview';
import { formatPermMode } from '../utils/markdown';
import AppIcon from './AppIcon.vue';
import ModelCascader from './ModelCascader.vue';
import ContextUsagePanel from './ContextUsagePanel.vue';
import PermDropdown from './PermDropdown.vue';
import TokenUsagePopover from './TokenUsagePopover.vue';
import ReasoningSelector from './ReasoningSelector.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest from './InputSuggest.vue';
import ComposerEditor from './ComposerEditor.vue';
import { useAttachments } from '../composables/useAttachments';
import { useIsTouch } from '../composables/useIsTouch';
import { useSuggest } from '../composables/useSuggest';
import { useComposerResize } from '../composables/useComposerResize';
import { buildSessionTokenSummary, formatTokenCount } from '../utils/tokenFormat';

const store = useAppStore();
const inputVal = computed<string>({
  get: () => store._getSessionRuntime().composerText || '',
  set: value => { store._getSessionRuntime().composerText = value; },
});
const editorRef = ref<InstanceType<typeof ComposerEditor> | null>(null);
const composerControl = computed(() => editorRef.value?.control ?? null);

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);
const reasoningSelectorRef = ref<{ close: () => void } | null>(null);
const showTokenPopover = ref(false);

function closeComposerMenus() {
  showModelCascader.value = false;
  showPermDropdown.value = false;
}

function toggleModelCascader() {
  reasoningSelectorRef.value?.close();
  showPermDropdown.value = false;
  showTokenPopover.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
  reasoningSelectorRef.value?.close();
  showModelCascader.value = false;
  showTokenPopover.value = false;
  showPermDropdown.value = !showPermDropdown.value;
}

function toggleTokenPopover() {
  closeComposerMenus();
  showTokenPopover.value = !showTokenPopover.value;
}

const tokenPct = computed(() => {
  const w = store.tokenUsage.window || 0;
  if (!w) return 0;
  return (store.tokenUsage.used / w) * 100;
});
const tokenClass = computed(() => {
  if (tokenPct.value > 90) return 'tok-red';
  if (tokenPct.value > 70) return 'tok-orange';
  return '';
});

const tokenSummary = computed(() => buildSessionTokenSummary(store.tokenUsage.session_totals));
const summaryTitle = computed(() => (
  store.tokenUsage.cache_invalidation_reason
  || '本会话 Token 统计（输入 / 输出 / 缓存命中，重启不清零）· 点击查看全部会话'
));

const CTX_CIRCUMFERENCE = 2 * Math.PI * 7.5;
const ctxArc = computed(() => (Math.min(tokenPct.value, 100) / 100) * CTX_CIRCUMFERENCE);
const ctxPctLabel = computed(() => `上下文 ${Math.round(tokenPct.value)}%`);
const ctxTooltip = computed(() => {
  const used = store.tokenUsage.used || 0;
  const win = store.tokenUsage.window || 0;
  const remain = Math.max(win - used, 0);
  return `${ctxPctLabel.value}（${formatTokenCount(used)} / ${formatTokenCount(win)}）· 剩余约 ${formatTokenCount(remain)}`;
});

const CTX_PANEL_CLOSE_DELAY = 160;
const showContextUsage = ref(false);
const ctxRingRef = ref<HTMLElement | null>(null);
let ctxCloseTimer: ReturnType<typeof setTimeout> | null = null;

function clearContextUsageClose() {
  if (ctxCloseTimer !== null) {
    clearTimeout(ctxCloseTimer);
    ctxCloseTimer = null;
  }
}

function openContextUsage() {
  clearContextUsageClose();
  showContextUsage.value = true;
}

function scheduleContextUsageClose() {
  clearContextUsageClose();
  ctxCloseTimer = setTimeout(() => {
    ctxCloseTimer = null;
    showContextUsage.value = false;
  }, CTX_PANEL_CLOSE_DELAY);
}

function cancelContextUsageClose() {
  clearContextUsageClose();
}

function onCtxRingClick() {
  if (isTouch.value) {
    if (showContextUsage.value) {
      clearContextUsageClose();
      showContextUsage.value = false;
    } else {
      openContextUsage();
    }
    return;
  }
  toggleTokenPopover();
}

function onContextUsageKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    clearContextUsageClose();
    showContextUsage.value = false;
  }
}

watch(showContextUsage, (visible) => {
  if (visible) document.addEventListener('keydown', onContextUsageKeydown);
  else document.removeEventListener('keydown', onContextUsageKeydown);
});

onUnmounted(() => {
  clearContextUsageClose();
  document.removeEventListener('keydown', onContextUsageKeydown);
});

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste, dragging, bindDrop, addPaths, uploadFiles, openAttachmentImage } = useAttachments();
const { suggestItems, suggestIndex, suggestEmpty, skillIds, commandIds, detectToken, applySuggest, onKeydown: suggestKeydown, clearSuggest } = useSuggest(inputVal, composerControl);
const { textareaHeight, isResizing, onHandlePointerdown, resetHeight } = useComposerResize(() => editorRef.value?.el ?? null);

const queueItems = computed(() => store.currentMessageQueue);
const hasInputContent = computed(() => !!inputVal.value.trim() || attachments.value.length > 0);
const sendStopMode = computed(() => store.currentSessionRunning && !hasInputContent.value);

// 输入框容器：拖拽文件进入时高亮提示，drop 由原生事件桥（pywebview）提供完整路径
const inputBoxRef = ref<HTMLElement | null>(null);
onMounted(() => { if (inputBoxRef.value) bindDrop(inputBoxRef.value); });

function onKeydown(e: KeyboardEvent) {
  suggestKeydown(e);
  if (e.defaultPrevented) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
}

onUnmounted(() => clearSuggest());

async function handlePickFiles() {
  await pickAttachments();
}

const isTouch = useIsTouch();
const touchFileInputRef = ref<HTMLInputElement | null>(null);

function onAttachClick() {
  if (!isTouch.value) { handlePickFiles(); return; }
  touchFileInputRef.value?.click();
}

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
  input.value = '';
  if (files.length) await uploadFiles(files);
}

function handleSend() {
  if (sendStopMode.value) {
    store.stopCurrentTurn();
    return;
  }

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

async function undoQueue(item: QueuedMessage) {
  inputVal.value = item.text;
  if (item.attachments && item.attachments.length) {
    await addPaths(item.attachments);
  }
  store.cancelQueuedMessage(store.currentSessionName, item.id);
}
</script>

<style scoped>
#input-area {
  border-top: none;
  padding: 4px 36px 14px;
  flex-shrink: 0;
  position: relative;
}
.resize-handle {
  position: absolute;
  top: -3px;
  left: 0;
  right: 0;
  height: 10px;
  cursor: row-resize;
  z-index: 5;
  touch-action: none;
  display: flex;
  align-items: center;
  justify-content: center;
}
.resize-handle::before {
  content: '';
  width: 36px;
  height: 3px;
  border-radius: 999px;
  background: transparent;
  transition: background 0.15s, width 0.15s;
}
.resize-handle:hover::before,
.resize-handle.active::before {
  background: var(--xy-accent, var(--accent));
  width: 56px;
}
/* 拖拽过程中禁止选中文本，避免指针划过消息区时高亮一片 */
#input-area.is-resizing {
  user-select: none;
}
#input-wrap {
  max-width: var(--chat-col-width);
  margin: 0 auto;
  container-type: inline-size;
  container-name: composer;
}

#queue-strip {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0 auto 8px;
  max-width: var(--chat-col-width);
}
.queue-item {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 5px 6px 5px 6px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.queue-badge {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 2px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.queue-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.queue-btn {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: 0.15s;
}
.queue-btn:hover {
  border-color: var(--border);
  background: var(--bg3);
  color: var(--text);
}
.queue-cancel-btn:hover {
  color: var(--red);
}
.queue-jump-btn:hover {
  color: var(--accent);
}
.queue-undo-btn:hover {
  color: var(--green);
}
#input-box {
  position: relative;
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  padding: 12px;
  box-shadow: var(--xy-shadow-composer, none);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
#input-box:focus-within {
  border-color: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 3px var(--xy-accent-soft, var(--accent-soft)), var(--xy-shadow-composer, none);
}
#input-box.drop-hover {
  border-color: var(--xy-accent, var(--accent));
  border-style: dashed;
  box-shadow: 0 0 0 3px var(--xy-accent-soft, var(--accent-soft));
}
.composer-field {
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.625;
}
#input-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  padding-top: 2px;
  border-top: none;
}
.composer-sep {
  width: 1px;
  height: 14px;
  margin: 0 2px;
  background: var(--xy-border, var(--border));
  flex-shrink: 0;
}
.chip-btn {
  background: transparent;
  border: none;
  color: var(--xy-text-2, var(--text-dim));
  border-radius: 8px;
  padding: 0 8px;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  cursor: pointer;
  transition: background 0.14s, color 0.14s;
  white-space: nowrap;
}
.plus-btn,
.snip-btn {
  width: 28px;
  height: 28px;
  padding: 0;
  flex-shrink: 0;
}
.permission-mode-btn,
.sandbox-btn {
  flex-shrink: 0;
  padding: 0 10px;
  font-weight: 500;
}
.perm-dot,
.sb-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
#input-bar .chip-btn {
  height: 28px;
}
.chip-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.chip-btn.active {
  color: var(--xy-accent, var(--accent));
  background: var(--xy-accent-soft, var(--accent-soft));
}

.model-chip {
  max-width: 170px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.model-chip .model-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ctx-ring {
  margin-left: auto;
  height: 28px;
  padding: 0 8px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--xy-text-2, var(--text-dim));
  cursor: pointer;
  transition: background 0.14s, color 0.14s;
}
.ctx-ring:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.ctx-ring svg {
  display: block;
  transform: rotate(-90deg);
}
.ctx-ring-bg {
  fill: none;
  stroke: var(--xy-border-strong, var(--bg3));
  stroke-width: 2.2;
}
.ctx-ring-fg {
  fill: none;
  stroke: var(--xy-accent, var(--accent));
  stroke-width: 2.2;
  stroke-linecap: round;
  transition: stroke-dasharray 0.3s;
}
.ctx-ring.tok-orange .ctx-ring-fg {
  stroke: var(--xy-amber, var(--orange));
}
.ctx-ring.tok-red .ctx-ring-fg {
  stroke: var(--xy-rose, var(--red));
}
.ctx-ring-pct {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

.sandbox-btn {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 12%, transparent));
  color: var(--xy-green, var(--green));
}
.sandbox-btn:hover {
  background: color-mix(in srgb, var(--xy-green, var(--green)) 18%, transparent);
  color: var(--xy-green, var(--green));
}
.sandbox-btn.sandbox-open {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 12%, transparent));
  color: var(--xy-amber, var(--orange));
}
.sandbox-btn.sandbox-open:hover {
  background: color-mix(in srgb, var(--xy-amber, var(--orange)) 18%, transparent);
  color: var(--xy-amber, var(--orange));
}

.model-chevron {
  color: var(--xy-text-3, var(--text-faint));
}

#send-btn {
  width: 28px;
  height: 28px;
  border-radius: 9999px;
  border: none;
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}
#input-box:focus-within #send-btn,
#send-btn.active {
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #fff);
}
#send-btn.stop {
  background: var(--xy-text, var(--text));
  color: var(--xy-canvas, #fff);
}

#input-meta {
  display: none;
  margin: 6px auto 0;
  max-width: var(--chat-col-width);
  justify-content: space-between;
  font-size: 11.5px;
  color: var(--text-faint);
  container-type: inline-size;
  container-name: input-meta;
}
#input-meta .l {
  display: flex;
  gap: 12px;
  min-width: 0;
}
.token-summary {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.token-summary-clickable {
  cursor: pointer;
  border-radius: 4px;
  transition: color 0.15s;
}
.token-summary-clickable:hover {
  color: var(--text);
}
.kbd {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 10px;
}
/* 快捷键提示不参与收缩：窄时按档位整块隐藏，绝不折行叠到摘要上 */
.keyboard-hints {
  flex-shrink: 0;
  white-space: nowrap;
}
.ctx-percent {
  display: none;
  white-space: nowrap;
}
.tok-orange {
  color: var(--orange);
}
.tok-red {
  color: var(--red);
}

.perm-icon,
.model-icon,
.sandbox-label-short {
  display: none;
}

@container composer (min-width: 860px) {
  .model-chip {
    max-width: 240px;
  }
}

@container composer (max-width: 660px) {
  .permission-mode-btn .perm-dot,
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron {
    display: none;
  }
  .perm-icon {
    display: inline-flex;
  }
}
@container composer (max-width: 520px) {
  .model-chip .model-label,
  .model-chip .model-chevron {
    display: none;
  }
  .model-icon {
    display: inline-flex;
  }
}
@container composer (max-width: 560px) {
  .sandbox-label-full {
    display: none;
  }
  .sandbox-label-short {
    display: inline;
  }
}
@container composer (max-width: 470px) {
  .snip-btn {
    display: none;
  }
  #input-bar :deep(.reasoning-label),
  #input-bar :deep(.reasoning-chevron) {
    display: none;
  }
}
@container composer (max-width: 325px) {
  .sandbox-btn {
    display: none;
  }
}
@container composer (max-width: 320px) {
  .ctx-ring {
    display: none;
  }
}
@container composer (max-width: 300px) {
  #input-bar :deep(.reasoning-button) {
    display: none;
  }
}

/* ── 元信息条收纳（右侧栏展开 / 窄窗口 / 手机端）──
   token 摘要与快捷键提示加起来约 600px，中间列被右侧栏挤窄后互相重叠。
   右侧栏展开时按用户要求整行不显示快捷键提示；再按 #input-meta 实际宽度
   兜底分档：≤640 提示换位为上下文百分比，≤540（手机竖屏宽度）连 token
   摘要一起隐藏，完整数据仍在底栏与点击弹层中可查 */
#input-area.right-panel-open .keyboard-hints .hint-full {
  display: none;
}
#input-area.right-panel-open .ctx-percent {
  display: inline;
}
@container input-meta (max-width: 640px) {
  .keyboard-hints .hint-full {
    display: none;
  }
  .ctx-percent {
    display: inline;
  }
}
@container input-meta (max-width: 540px) {
  .token-summary {
    display: none;
  }
}

@media (max-width: 768px) {
  #input-area {
    padding: 4px 12px 6px;
  }
  #input-bar {
    padding: 5px 10px 7px;
  }
  .composer-tools,
  .composer-right {
    gap: 4px;
  }
  .keyboard-hints .hint-full {
    display: none;
  }
  .ctx-percent {
    display: inline;
  }
  .permission-mode-btn .perm-dot,
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron,
  .model-chip .model-label,
  .model-chip .model-chevron,
  .sandbox-label-full,
  .sb-dot,
  .ctx-ring-pct {
    display: none;
  }
  .perm-icon,
  .model-icon {
    display: inline-flex;
  }
  .sandbox-label-short {
    display: inline;
  }
}
</style>
