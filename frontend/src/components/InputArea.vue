<template>
  <div id="input-area">
    <div id="input-wrap">
      <div id="input-box">
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
            :class="{ stop: store.running, active: !store.running && (!!inputVal.trim() || attachments.length > 0) }"
            :title="store.running ? '停止生成 (Esc)' : '发送消息 (Enter)'"
            @click="handleSend"
          >{{ store.running ? '■' : '↑' }}</button>
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
import { ref, computed, nextTick, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest, { type SuggestItem } from './InputSuggest.vue';
import { useAttachments } from '../composables/useAttachments';

const store = useAppStore();
const inputVal = ref('');

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

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste } = useAttachments();

// ---------------------------------------------------------------------------
// 输入自动补全：/ 斜杠（已启用技能 + 已启用 MCP 服务）与 @ 文件引用（子串匹配）
// CLI 内置命令（/models /theme 等）在 GUI 均有可视化入口，不再列入候选。
// ---------------------------------------------------------------------------
const textareaRef = ref<HTMLTextAreaElement | null>(null);
// 当前补全状态：mode=补全类型，start=词在文本中的起始下标，query=已输入部分
const suggest = ref<{ mode: 'command' | 'file'; start: number; query: string } | null>(null);
const suggestItems = ref<SuggestItem[]>([]);
const suggestIndex = ref(0);
let fileSuggestTimer: number | undefined;

function buildCommandItems(query: string) {
  const q = query.toLowerCase();
  const items: SuggestItem[] = [];
  // 已启用技能生成 /<short_id> 候选
  for (const s of store.skills) {
    if (!s.enabled || !s.short_id) continue;
    items.push({
      key: 'skill:' + s.short_id,
      icon: '⚡',
      label: '/' + s.short_id,
      desc: s.description || s.name,
      apply: '/' + s.short_id,
    });
  }
  // 已启用 MCP 服务生成 /mcp:<name> 候选（提示模型调用该服务的工具）
  for (const m of store.mcps) {
    if (!m.enabled) continue;
    items.push({
      key: 'mcp:' + m.name,
      icon: '🔌',
      label: '/mcp:' + m.name,
      desc: m.comment || `MCP 服务 · ${m.tools_count} 个工具`,
      apply: '/mcp:' + m.name,
    });
  }
  suggestItems.value = items
    .filter(it => it.label.slice(1).toLowerCase().startsWith(q))
    .slice(0, 8);
  suggestIndex.value = 0;
}

function fetchFileItems(query: string) {
  window.clearTimeout(fileSuggestTimer);
  fileSuggestTimer = window.setTimeout(async () => {
    const api = window.pywebview && window.pywebview.api;
    if (!api) {
      suggestItems.value = [];
      return;
    }
    try {
      const res = await api.search_workspace_files(query, 8);
      // 防抖期间用户可能已离开 @ 语境：过期结果直接丢弃
      if (!suggest.value || suggest.value.mode !== 'file') return;
      const files: string[] = (res && res.files) || [];
      suggestItems.value = files.map(f => ({
        key: f,
        icon: '📄',
        label: f,
        desc: '',
        apply: '@' + f,
      }));
      suggestIndex.value = 0;
    } catch {
      suggestItems.value = [];
    }
  }, 150);
}

function detectToken() {
  const ta = textareaRef.value;
  if (!ta) {
    suggest.value = null;
    return;
  }
  const pos = ta.selectionStart ?? inputVal.value.length;
  const before = inputVal.value.slice(0, pos);
  // 斜杠命令：词首为 /（行首或空白之后），词内不含空白
  const sm = before.match(/(?:^|\s)\/([^\s]*)$/);
  if (sm) {
    const query = sm[1];
    suggest.value = { mode: 'command', start: pos - query.length - 1, query };
    buildCommandItems(query);
    return;
  }
  // @ 文件引用：光标前最后一个 @ 后的非空白串作为匹配词
  const am = before.match(/@([^\s@]*)$/);
  if (am) {
    const query = am[1];
    suggest.value = { mode: 'file', start: pos - query.length - 1, query };
    fetchFileItems(query);
    return;
  }
  // 不在补全语境：状态与候选列表一并关闭（候选残留会导致面板关不掉）
  suggest.value = null;
  suggestItems.value = [];
}

function applySuggest(item: SuggestItem) {
  const s = suggest.value;
  if (!s) return;
  const ta = textareaRef.value;
  const pos = ta ? (ta.selectionStart ?? inputVal.value.length) : inputVal.value.length;
  const inserted = item.apply + ' ';
  inputVal.value = inputVal.value.slice(0, s.start) + inserted + inputVal.value.slice(pos);
  // 选中即关闭：状态与候选列表一起清空（InputSuggest 按 items.length 显隐）
  suggest.value = null;
  suggestItems.value = [];
  nextTick(() => {
    if (ta) {
      ta.focus();
      const cursor = s.start + inserted.length;
      ta.setSelectionRange(cursor, cursor);
    }
  });
}

function onKeydown(e: KeyboardEvent) {
  // 补全面板打开时优先消费导航键
  if (suggest.value && suggestItems.value.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      suggestIndex.value = (suggestIndex.value + 1) % suggestItems.value.length;
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      suggestIndex.value = (suggestIndex.value - 1 + suggestItems.value.length) % suggestItems.value.length;
      return;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault();
      applySuggest(suggestItems.value[suggestIndex.value]);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      suggest.value = null;
      suggestItems.value = [];
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  } else if (e.key === 'Escape') {
    store.stopCurrentTurn();
  }
}

onUnmounted(() => window.clearTimeout(fileSuggestTimer));

async function handlePickFiles() {
  await pickAttachments();
}

function handleSend() {
  if (store.running) {
    store.stopCurrentTurn();
    return;
  }

  const text = inputVal.value;
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
