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
          >＋</button>
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="chip-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >{{ formatPermMode(store.permissionMode) }} <span class="permission-chevron">▼</span></button>

          <button
            ref="modelBtnRef"
            class="chip-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >{{ store.activeModelName || '选择模型' }} <span style="font-size:9px;color:var(--text-faint)">▼</span></button>
          <button
            id="send-btn"
            :class="{ stop: store.currentSessionRunning, active: !store.currentSessionRunning && (!!inputVal.trim() || attachments.length > 0) }"
            :title="store.currentSessionRunning ? '停止生成 (Esc)' : '发送消息 (Enter)'"
            @click="handleSend"
          >{{ store.currentSessionRunning ? '■' : '↑' }}</button>
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
        <span :class="tokenClass">Token: {{ tokenUsedDisplay }} / {{ tokenWindowDisplay }}</span>
        <span class="cache-usage" :title="store.tokenUsage.cache_invalidation_reason || '当前会话主代理缓存命中率'">
          缓存: {{ cacheRateDisplay }}
        </span>
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
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest from './InputSuggest.vue';
import { useAttachments } from '../composables/useAttachments';
import { useSuggest } from '../composables/useSuggest';

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

const tokenUsedDisplay = computed(() => (store.tokenUsage.used || 0).toLocaleString('zh-CN'));
const tokenWindowDisplay = computed(() => (store.tokenUsage.window || 0).toLocaleString('zh-CN'));
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
const formatCacheRate = (value: number | null) => (
  value === null ? '--' : `${value.toFixed(1)}%`
);
const cacheRateDisplay = computed(() => (
  `本轮 ${formatCacheRate(store.tokenUsage.cache_latest_pct)} · `
  + `热路径 ${formatCacheRate(store.tokenUsage.cache_hot_pct)} · `
  + `全量 ${formatCacheRate(store.tokenUsage.cache_session_pct)}`
));

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
    store.showToast('附件路径加载中，请稍候…', 'warn');
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
  margin-left: auto;
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
}

#send-btn {
  /* 与首页一致的圆形箭头按钮 */
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: var(--bg3);
  color: var(--text-faint);
  font-size: 15px;
  font-weight: 600;
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
.cache-usage {
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
