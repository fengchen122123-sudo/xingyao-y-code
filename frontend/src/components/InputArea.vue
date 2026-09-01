<template>
  <div id="input-area" data-theme-region="composer" :class="{ 'is-resizing': isResizing }">
    <!-- 顶边拖拽手柄：向上拖抬高输入框（多显示几行），向下拖以默认高度为下限；
         双击恢复默认高度 -->
    <div
      class="resize-handle"
      data-test="composer-resize-handle"
      title="拖拽调节输入框高度 · 双击恢复默认"
      :class="{ active: isResizing }"
      @pointerdown="onHandlePointerdown"
      @dblclick="resetHeight"
    ></div>
    <!-- 排队消息条：回合运行期间发送的消息在此排队，不打断当前任务；
         删除按钮取消排队，闪电按钮停止当前任务并优先发送该消息 -->
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
      <div id="input-box" ref="inputBoxRef" :class="{ 'drop-hover': dragging }">
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
          rows="2"
          :style="textareaHeight != null ? { height: textareaHeight + 'px' } : undefined"
          placeholder="向星瑶提问… / 调用技能与 MCP，@ 引用文件"
          @keydown="onKeydown"
          @input="detectToken"
          @click="detectToken"
          @keyup="detectToken"
          @paste="handlePaste"
        ></textarea>

        <div id="input-bar">
          <button
            ref="attachBtnRef"
            data-test="attachment-button"
            class="chip-btn plus-btn"
            title="附加文件"
            @click.stop="onAttachClick"
          ><AppIcon name="paperclip" :size="14" /></button>
          <!-- 触摸设备隐藏文件入口：点击后由系统弹出拍照/相册/文件选择 -->
          <input ref="touchFileInputRef" type="file" multiple style="display:none" @change="onPickInputChange">
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="chip-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          ><AppIcon class="perm-icon" name="shield" :size="13" :stroke-width="2" /><span class="perm-label">{{ formatPermMode(store.permissionMode) }}</span> <AppIcon class="permission-chevron" name="chevron-down" :size="11" :stroke-width="2" /></button>
          <button
            data-test="sandbox-button"
            class="chip-btn sandbox-btn"
            :class="{ 'sandbox-open': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件（点击恢复限制）' : '沙盒已开启：仅允许访问工作目录内文件（点击解除限制）'"
            @click.stop="store.setSandbox(!store.sandboxEnabled)"
          ><span class="sandbox-label-full">{{ store.sandboxEnabled ? '沙盒: 开' : '沙盒: 限' }}</span><span class="sandbox-label-short">{{ store.sandboxEnabled ? '开' : '限' }}</span></button>

          <!-- 上下文统计圆环：弧线占比 = 已用上下文/窗口，悬停查看用量详情 -->
          <span
            class="ctx-ring"
            :class="tokenClass"
            :title="ctxTooltip"
            data-test="context-ring"
          >
            <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
              <circle class="ctx-ring-bg" cx="10" cy="10" r="8" />
              <circle
                class="ctx-ring-fg"
                cx="10"
                cy="10"
                r="8"
                :stroke-dasharray="`${ctxArc} ${CTX_CIRCUMFERENCE}`"
              />
            </svg>
          </span>
          <button
            ref="modelBtnRef"
            data-test="model-button"
            class="chip-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          ><AppIcon class="model-icon" name="box" :size="13" :stroke-width="2" /><span class="model-label">{{ store.activeModelName || '选择模型' }}</span> <AppIcon name="chevron-down" :size="10" :stroke-width="2" class="model-chevron" /></button>
          <ReasoningSelector
            ref="reasoningSelectorRef"
            variant="compact"
            @open="closeComposerMenus"
          />
          <button
            id="send-btn"
            data-test="send-button"
            :class="{ stop: sendStopMode, active: !sendStopMode && hasInputContent }"
            :title="sendStopMode ? '停止生成 (Esc)' : (store.currentSessionRunning ? '排队发送 (Enter)' : '发送消息 (Enter)')"
            @click="handleSend"
          >
            <AppIcon v-if="sendStopMode" name="square" :size="14" :stroke-width="2" />
            <AppIcon v-else name="arrow-up" :size="16" :stroke-width="2.25" />
          </button>
        </div>
      </div>
    </div>

    <!-- 模型 / 权限下拉（与空状态页共用组件） -->
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

    <div id="input-meta">
      <div class="l">
        <span class="token-summary" :title="summaryTitle">{{ tokenSummary }}</span>
      </div>
      <div class="r keyboard-hints" data-test="keyboard-hints">
        <span class="ctx-percent" :class="tokenClass" :title="ctxTooltip">{{ ctxPctLabel }}</span>
        <span class="hint-full"><span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { QueuedMessage } from '../types/pywebview';
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
import { useComposerResize } from '../composables/useComposerResize';
import { buildTokenSummary, formatTokenCount } from '../utils/tokenFormat';

const store = useAppStore();
const inputVal = computed<string>({
  get: () => store._getSessionRuntime().composerText || '',
  set: value => { store._getSessionRuntime().composerText = value; },
});
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);
const reasoningSelectorRef = ref<{ close: () => void } | null>(null);

function closeComposerMenus() {
  showModelCascader.value = false;
  showPermDropdown.value = false;
}

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

// 状态栏摘要：与 CLI（main.py get_status）同口径——输入/输出/命中，命中率分母为输入
const tokenSummary = computed(() => buildTokenSummary(store.tokenUsage));
const summaryTitle = computed(() => (
  store.tokenUsage.cache_invalidation_reason
  || '会话累计 Token（输入 / 输出 / 缓存命中），口径与 CLI 状态栏一致'
));

// 上下文圆环：周长固定，弧长按占用百分比推进，悬停显示用量详情
const CTX_CIRCUMFERENCE = 2 * Math.PI * 8;
const ctxArc = computed(() => (Math.min(tokenPct.value, 100) / 100) * CTX_CIRCUMFERENCE);
const ctxPctLabel = computed(() => `上下文 ${Math.round(tokenPct.value)}%`);
const ctxTooltip = computed(() => {
  const used = store.tokenUsage.used || 0;
  const win = store.tokenUsage.window || 0;
  const remain = Math.max(win - used, 0);
  return `${ctxPctLabel.value}（${formatTokenCount(used)} / ${formatTokenCount(win)}）· 剩余约 ${formatTokenCount(remain)}`;
});

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste, dragging, bindDrop, addPaths, uploadFiles, openAttachmentImage } = useAttachments();
const { suggestItems, suggestIndex, detectToken, applySuggest, onKeydown: suggestKeydown, clearSuggest } = useSuggest(inputVal, textareaRef);
// 输入框高度拖拽：最小=默认渲染高度，向上拖抬高、松手记忆，双击恢复
const { textareaHeight, isResizing, onHandlePointerdown, resetHeight } = useComposerResize(() => textareaRef.value);

// 排队消息：回合运行期间发送的消息（后端 message_queue 快照驱动）
const queueItems = computed(() => store.currentMessageQueue);
// 输入框是否有可发送内容（文本或附件）
const hasInputContent = computed(() => !!inputVal.value.trim() || attachments.value.length > 0);
// 运行中且没有新内容时发送按钮为"停止"态；有内容则保持"发送"态
// （运行中发送 = 加入排队，不打断正在执行的任务）
const sendStopMode = computed(() => store.currentSessionRunning && !hasInputContent.value);

// 输入框容器：拖拽文件进入时高亮提示，drop 由原生事件桥（pywebview）提供完整路径
const inputBoxRef = ref<HTMLElement | null>(null);
onMounted(() => { if (inputBoxRef.value) bindDrop(inputBoxRef.value); });

function onKeydown(e: KeyboardEvent) {
  // 先让 composable 处理补全导航键（方向键/Tab/Enter/Esc）
  suggestKeydown(e);
  if (e.defaultPrevented) return;
  // 补全未拦截：Enter 发送，Escape 停止
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  } else if (e.key === 'Escape') {
    store.stopCurrentTurn();
  }
}

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

async function onPickInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';  // 允许重复选同一文件
  if (files.length) await uploadFiles(files);
}

function handleSend() {
  // 运行中且没有新内容：按钮为停止态，点击中断当前回合；
  // 有内容则照常发送——后端把消息加入该会话排队，回合结束后自动补发
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

// 撤销排队消息：把该条消息的文本与附件（图片/文件）恢复到输入框，供重新
// 编辑；随后从排队队列移除（复用取消排队）。文本覆盖当前输入，附件按路径
// 合并去重（addPaths 已去重）。
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
  border-top: 1px solid var(--border);
  /* 顶部分隔线保持全宽，输入框本体限宽居中与上方消息列对齐 */
  padding: 12px 10px 10px;
  flex-shrink: 0;
  position: relative;
}
/* 拖拽手柄：钉在输入区顶边（红线位置），全宽命中便于拖拽 */
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
  background: var(--border);
  transition: background 0.15s, width 0.15s;
}
.resize-handle:hover::before,
.resize-handle.active::before {
  background: var(--accent);
  width: 56px;
}
/* 拖拽过程中禁止选中文本，避免指针划过消息区时高亮一片 */
#input-area.is-resizing {
  user-select: none;
}
#input-wrap {
  max-width: var(--chat-col-width);
  margin: 0 auto;
}

/* 排队消息条：输入框上方逐条展示回合运行期间发送的排队消息 */
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
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
#input-box:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
#input-box.drop-hover {
  border-color: var(--accent);
  border-style: dashed;
  box-shadow: 0 0 0 3px var(--accent-soft);
}
textarea {
  width: 100%;
  background: none;
  border: none;
  outline: none;
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  resize: none;
  line-height: 1.65;
}
#input-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}
.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 4px 10px;
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
  transition: 0.15s;
}
.plus-btn {
  font-size: 14px;
  font-weight: 600;
  padding: 3px 10px;
  line-height: 1.2;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.chip-btn.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}

.model-chip {
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
}

/* 上下文统计圆环：margin-left:auto 把它与模型/发送按钮一起顶到右侧（红圈位置） */
.ctx-ring {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  color: var(--accent);
  cursor: default;
}
.ctx-ring svg {
  display: block;
  /* 弧线从 12 点方向开始推进 */
  transform: rotate(-90deg);
}
.ctx-ring-bg {
  fill: none;
  stroke: var(--bg3);
  stroke-width: 2.5;
}
.ctx-ring-fg {
  fill: none;
  stroke: currentColor;
  stroke-width: 2.5;
  stroke-linecap: round;
  transition: stroke-dasharray 0.3s;
}

.sandbox-btn.sandbox-open {
  border-color: var(--orange);
  color: var(--orange);
}

/* 模型按钮上的小箭头（对齐旧 9px 文字箭头的视觉重量） */
.model-chevron {
  color: var(--text-faint);
}

#send-btn {
  /* 与首页一致的圆形箭头按钮 */
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: var(--bg3);
  color: var(--text-faint);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: 0.15s;
}
#input-box:focus-within #send-btn,
#send-btn.active {
  background: var(--accent);
  color: #fff;
}
#send-btn.stop {
  background: var(--red);
  color: #fff;
}

#input-meta {
  margin: 6px auto 0;
  max-width: var(--chat-col-width);
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  color: var(--text-faint);
}
#input-meta .l {
  display: flex;
  gap: 12px;
}
.token-summary {
  white-space: nowrap;
}
.kbd {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 10px;
}
/* 上下文百分比：仅手机端元信息条右侧显示（桌面该位置是快捷键提示） */
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

/* 紧凑形态元素（图标/短文案）仅手机端显示，桌面端隐藏 */
.perm-icon,
.model-icon,
.sandbox-label-short {
  display: none;
}

@media (max-width: 768px) {
  /* 手机端元信息条右侧改显上下文百分比，快捷键提示隐藏 */
  .keyboard-hints .hint-full {
    display: none;
  }
  .ctx-percent {
    display: inline;
  }
  /* 手机端收窄按钮：权限/模型只显示图标（颜色仍跟随状态），沙盒只留 开/限 */
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron,
  .model-chip .model-label,
  .model-chip .model-chevron,
  .sandbox-label-full {
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
