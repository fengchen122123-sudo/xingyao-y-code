<template>
  <div id="input-area">
    <div id="input-wrap">
      <div id="input-box" ref="inputBoxRef" :class="{ 'drop-hover': dragging }">
        <AttachmentChips :items="attachments" @remove="removeAttachment" />
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
          placeholder="向星瑶提问… / 调用技能与 MCP，@ 引用文件"
          @keydown="onKeydown"
          @input="detectToken"
          @click="detectToken"
          @keyup="detectToken"
          @paste="handlePaste"
        ></textarea>

        <div id="input-bar">
          <button
            data-test="attachment-button"
            class="chip-btn plus-btn"
            title="附加文件"
            @click="handlePickFiles"
          ><AppIcon name="paperclip" :size="14" /></button>
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="chip-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >{{ formatPermMode(store.permissionMode) }} <AppIcon class="permission-chevron" name="chevron-down" :size="11" :stroke-width="2" /></button>
          <button
            data-test="sandbox-button"
            class="chip-btn sandbox-btn"
            :class="{ 'sandbox-open': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件（点击恢复限制）' : '沙盒已开启：仅允许访问工作目录内文件（点击解除限制）'"
            @click.stop="store.setSandbox(!store.sandboxEnabled)"
          >{{ store.sandboxEnabled ? '沙盒: 开' : '沙盒: 限' }}</button>

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
            class="chip-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >{{ store.activeModelName || '选择模型' }} <AppIcon name="chevron-down" :size="10" :stroke-width="2" class="model-chevron" /></button>
          <button
            id="send-btn"
            :class="{ stop: store.currentSessionRunning, active: !store.currentSessionRunning && (!!inputVal.trim() || attachments.length > 0) }"
            :title="store.currentSessionRunning ? '停止生成 (Esc)' : '发送消息 (Enter)'"
            @click="handleSend"
          >
            <AppIcon v-if="store.currentSessionRunning" name="square" :size="14" :stroke-width="2" />
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

    <div id="input-meta">
      <div class="l">
        <span class="token-summary" :title="summaryTitle">{{ tokenSummary }}</span>
      </div>
      <div class="r">
        <span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import AppIcon from './AppIcon.vue';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest from './InputSuggest.vue';
import { useAttachments } from '../composables/useAttachments';
import { useSuggest } from '../composables/useSuggest';
import { buildTokenSummary, formatTokenCount } from '../utils/tokenFormat';

const store = useAppStore();
const inputVal = ref('');
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);

function toggleModelCascader() {
  showPermDropdown.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
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
const ctxTooltip = computed(() => {
  const used = store.tokenUsage.used || 0;
  const win = store.tokenUsage.window || 0;
  const pct = win ? Math.round((used / win) * 100) : 0;
  const remain = Math.max(win - used, 0);
  return `上下文 ${pct}%（${formatTokenCount(used)} / ${formatTokenCount(win)}）· 剩余约 ${formatTokenCount(remain)}`;
});

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste, dragging, bindDrop } = useAttachments();
const { suggestItems, suggestIndex, detectToken, applySuggest, onKeydown: suggestKeydown, clearSuggest } = useSuggest(inputVal, textareaRef);

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

function handleSend() {
  if (store.currentSessionRunning) {
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
</script>

<style scoped>
#input-area {
  border-top: 1px solid var(--border);
  background: var(--bg1);
  /* 两侧只留 10px 呼吸位，输入框拉伸占满中间栏，不再留 28px 大白边 */
  padding: 12px 10px 10px;
  flex-shrink: 0;
}
#input-wrap {
  /* 自适应拉伸占满输入区，不再限宽居中（两侧不留白） */
  margin: 0 auto;
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
.tok-orange {
  color: var(--orange);
}
.tok-red {
  color: var(--red);
}
</style>
