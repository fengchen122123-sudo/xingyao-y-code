<template>
  <div
    id="input-area"
    data-theme-region="composer"
    :class="{ 'is-resizing': isResizing, 'right-panel-open': !store.rightPanelCollapsed }"
  >
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
      <!-- QQ2009 皮肤专属：经典工具栏行（仅内置 QQ2009 主题激活时渲染；
           只接入真实存在的功能，样式在 themes/qq2009.css，本组件不加样式） -->
      <div v-if="store.guiTheme?.id === 'builtin.qq2009'" class="qq09-toolbar" data-test="qq09-toolbar">
        <!-- 图片：星瑶的图片发送与文件共用同一个附件入口（pick_files 系统对话框，
             图片扩展名会被识别出缩略图），无独立图片菜单，故与「文件」同调 onAttachClick -->
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
        <!-- 文件：同一附件入口，任意格式文件都可选 -->
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
          :placeholder="`向${store.agentName}提问… / 调用技能与 MCP，@ 引用文件`"
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
          <!-- 屏幕截图：全屏框选屏幕任意区域；截图「发到输入框」后后端经
               snip_attachment_ready 桥事件回传路径，由 store 加入输入框附件 -->
          <button
            data-test="screen-snip-button"
            class="chip-btn snip-btn"
            title="截图：框选屏幕任意区域"
            @click.stop="onScreenSnipClick"
          ><AppIcon name="scissors" :size="14" /></button>
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
          <!-- Git 分支入口：非 Git 工作区不显示；点击弹出右侧 Git 管理面板 -->
          <button
            v-if="gitBranchLabel"
            data-test="composer-git-branch"
            class="chip-btn git-branch-btn"
            title="Git 工作台：点击打开 Git 管理面板"
            @click.stop="openGitPanel"
          ><AppIcon name="git-branch" :size="12" :stroke-width="2" /><span class="git-branch-label">{{ gitBranchLabel }}</span></button>

          <!-- 上下文统计圆环：弧线占比 = 已用上下文/窗口；悬停弹出用量详情面板，
               触摸设备点按切换（面板见 ContextUsagePanel） -->
          <span
            ref="ctxRingRef"
            class="ctx-ring"
            :class="tokenClass"
            data-test="context-ring"
            @mouseenter="openContextUsage"
            @mouseleave="scheduleContextUsageClose"
            @click.stop="toggleContextUsageOnTouch"
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
    <!-- 上下文用量悬停面板：挂在圆环上方，鼠标在两元素间移动不关闭 -->
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
    <!-- 会话 token 弹层：点击状态行摘要向上弹出（本会话/全部/会话列表） -->
    <TokenUsagePopover
      :visible="showTokenPopover"
      :anchor-el="tokenSummaryRef"
      @close="showTokenPopover = false"
    />

    <!-- 触摸设备不再使用应用内附件菜单：点回形针直接唤起系统文件选择器 -->

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
        <span class="hint-full"><span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import { useGitStore } from '../stores/gitStore';
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
import { useAttachments } from '../composables/useAttachments';
import { useIsTouch } from '../composables/useIsTouch';
import { useSuggest } from '../composables/useSuggest';
import { useComposerResize } from '../composables/useComposerResize';
import { buildSessionTokenSummary, formatTokenCount } from '../utils/tokenFormat';

const store = useAppStore();
const gitStore = useGitStore();
// Git 分支 chip 文案：复用状态栏格式（去掉 ⎇ 前缀），带脏标记与同步状态；
// snapshot 未加载或非 Git 工作区时为空，chip 由 v-if 隐藏
const gitBranchLabel = computed(() => {
  const text = gitStore.statusbarText;
  return text ? text.slice(2) : '';
});
function openGitPanel() {
  gitStore.requestOpenPanel();
  store.openRightPanelTab('git');
}
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
const showTokenPopover = ref(false);
const tokenSummaryRef = ref<HTMLElement | null>(null);

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

// 状态栏摘要：本会话口径（token_usage 落库聚合，重启不清零）；
// 点击弹出会话明细（全部会话合计 + 会话列表）。命中率分母只含已报告
// 缓存明细的轮次（未报告 ≠ 0），比旧全局口径的 cached/prompt 更真实
const tokenSummary = computed(() => buildSessionTokenSummary(store.tokenUsage.session_totals));
const summaryTitle = computed(() => (
  store.tokenUsage.cache_invalidation_reason
  || '本会话 Token 统计（输入 / 输出 / 缓存命中，重启不清零）· 点击查看全部会话'
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

// —— 上下文用量面板：悬停圆环打开、移出延迟关闭（给鼠标移入面板的过渡时间）；
//    面板自身悬停取消关闭；触摸设备点按切换；打开期间 Esc 关闭 ——
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

function toggleContextUsageOnTouch() {
  if (!isTouch.value) return;
  if (showContextUsage.value) {
    clearContextUsageClose();
    showContextUsage.value = false;
  } else {
    openContextUsage();
  }
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
  /* 图标化收纳档位的容器：挤压来自两侧面板展开而非窗口变窄，
     媒体查询感知不到，按此容器宽度分档（见文件末尾 @container） */
  container-type: inline-size;
  container-name: composer;
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
  /* 侧栏展开挤压输入栏时按钮被压缩，CJK 文本会逐字换行把按钮撑成
     多行“竖条”、与其他按钮高低不一——文字一律单行，收缩交给可截断的
     模型/分支按钮（见下），固定文案按钮禁止压缩 */
  white-space: nowrap;
}
.plus-btn {
  font-size: 14px;
  font-weight: 600;
  padding: 3px 10px;
  line-height: 1.2;
  flex-shrink: 0;
}
.permission-mode-btn,
.sandbox-btn {
  flex-shrink: 0;
}
/* 输入栏内按钮统一高度：回形针等图标按钮与文字胶囊不出现高低差 */
#input-bar .chip-btn {
  min-height: 28px;
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
  /* 唯一允许被挤压的按钮：min-width:0 解除 flex 最小内容宽度限制，
     空间不足时以省略号收缩而不是换行撑高 */
  min-width: 0;
}

/* 上下文统计圆环：margin-left:auto 把它与模型/发送按钮一起顶到右侧（红圈位置）；
   悬停弹出用量面板，指针样式提示可交互 */
.ctx-ring {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  color: var(--accent);
  cursor: pointer;
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

/* Git 分支入口：长分支名截断显示，完整名在 title 中；空间不足时与模型按钮
   一起收缩（label 需 min-width:0 才能在 flex 内真正截断出省略号） */
.git-branch-btn {
  max-width: 200px;
  min-width: 0;
}
.git-branch-btn .git-branch-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  /* 元信息条收纳档位的容器：右侧栏展开挤窄中列时媒体查询感知不到，
     按自身实际宽度分档隐藏（见文件末尾 @container input-meta） */
  container-type: inline-size;
  container-name: input-meta;
}
#input-meta .l {
  display: flex;
  gap: 12px;
  /* 允许收缩配合省略号：token 摘要不换行，缺 min-width 会溢出压到右侧提示上 */
  min-width: 0;
}
.token-summary {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 状态行摘要可点击（弹出会话 token 面板）：加悬停反馈但不抢视觉 */
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
/* 上下文百分比：桌面默认隐藏，手机端或空间不足档位时顶替快捷键提示的位置 */
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

/* ── 宽度不足时的图标化收纳（容器查询，对齐手机版形态）──
   两侧面板展开挤窄输入栏时禁止按钮换行/压成竖条，按 #input-wrap 容器
   宽度分档收纳，任何档位都保持单行：
   ≤640 只隐 Git 分支文字（模型名继续省略号收缩）；
   ≤540 权限/模型转手机版纯图标；
   ≤430 沙盒收成 开/限 并隐藏分支入口 */
@container composer (max-width: 640px) {
  .git-branch-btn .git-branch-label {
    display: none;
  }
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
  .git-branch-btn {
    display: none;
  }
  .sandbox-label-full {
    display: none;
  }
  .sandbox-label-short {
    display: inline;
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
  /* 手机端分支信息看状态栏即可，输入框不挤占空间 */
  .git-branch-btn {
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
