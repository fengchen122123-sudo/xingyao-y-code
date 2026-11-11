<template>
  <div
    v-if="store.sessionSearchModal.show"
    class="session-search-mask"
    data-test="session-search-mask"
    @mousedown.self="close()"
  >
    <div class="session-search-modal" data-test="session-search-modal">
      <!-- 顶部搜索输入区 -->
      <div class="search-head">
        <span class="search-icon">
          <AppIcon name="search" :size="18" />
        </span>
        <input
          ref="inputRef"
          v-model="query"
          class="search-input"
          data-test="session-search-input"
          placeholder="搜索会话名称或消息内容... (支持关键词、中文、代码片段)"
          autocomplete="off"
          spellcheck="false"
          @input="onInput"
          @keydown="onKeydown"
        />
        <button
          v-if="query"
          class="clear-btn"
          title="清空输入"
          @click="clearQuery"
        >
          ✕
        </button>
        <span class="esc-badge" title="按 ESC 键退出">ESC</span>
      </div>

      <!-- 搜索范围过滤条 -->
      <div class="filter-bar">
        <div class="scope-tabs">
          <span class="filter-label">目标:</span>
          <button
            class="tab-btn"
            :class="{ active: currentScope === 'all' }"
            data-test="scope-all"
            @click="setScope('all')"
          >
            全部匹配
          </button>
          <button
            class="tab-btn"
            :class="{ active: currentScope === 'title' }"
            data-test="scope-title"
            @click="setScope('title')"
          >
            仅按标题
          </button>
          <button
            class="tab-btn"
            :class="{ active: currentScope === 'content' }"
            data-test="scope-content"
            @click="setScope('content')"
          >
            仅按内容 (全文)
          </button>
        </div>

        <div class="project-scope">
          <span class="filter-label">范围:</span>
          <select v-model="projectScope" class="project-select" @change="doSearch">
            <option value="all">所有项目</option>
            <option v-if="currentProjectWorkdir && currentProjectWorkdir !== 'GLOBAL'" value="current">
              当前项目 ({{ currentProjectName }})
            </option>
            <option value="global">通用对话 (自由)</option>
          </select>
          <!-- 归档会话默认不参与检索（与侧栏主列表口径一致），需要时手动打开 -->
          <label class="archive-toggle" title="同时检索已归档会话（默认不检索）">
            <input
              v-model="includeArchived"
              type="checkbox"
              data-test="search-include-archived"
              @change="doSearch"
            />
            <span>包含已归档</span>
          </label>
        </div>
      </div>

      <!-- 搜索结果列表区 -->
      <div ref="resultsContainerRef" class="results-container custom-scrollbar" data-test="search-results">
        <!-- 加载中指示 -->
        <div v-if="loading" class="state-box">
          <span class="loading-spinner"></span>
          <span>正在检索会话历史…</span>
        </div>

        <!-- 空输入引导 -->
        <div v-else-if="!query.trim()" class="state-box">
          <span class="empty-ico"><AppIcon name="search" :size="28" /></span>
          <p class="empty-title">输入关键词搜索会话</p>
          <p class="empty-desc">支持即时模糊匹配会话标题、用户提问与助手回复内容</p>
        </div>

        <!-- 无匹配结果 -->
        <div v-else-if="results.length === 0" class="state-box">
          <span class="empty-ico">🔍</span>
          <p class="empty-title">未找到与 "{{ query }}" 相关的会话</p>
          <p class="empty-desc">请尝试调整关键词、缩短搜索词或切换搜索目标范围</p>
        </div>

        <!-- 结果列表 -->
        <template v-else>
          <div
            v-for="(item, index) in results"
            :key="`${item.session_name}_${item.type}_${item.msg_id || index}`"
            class="result-item"
            :class="{ selected: index === selectedIndex }"
            :data-index="index"
            @click="jumpToSession(item)"
            @mouseenter="selectedIndex = index"
          >
            <!-- 头部：角色徽章 + 会话标题 + 所属目录/时间 -->
            <div class="item-head">
              <div class="head-left">
                <span
                  class="type-badge"
                  :class="badgeClass(item)"
                >
                  {{ badgeText(item) }}
                </span>
                <span class="session-title truncate" v-html="highlight(item.display_name)"></span>
              </div>
              <div class="head-right">
                <span class="folder-tag" :title="item.workdir">
                  📁 {{ item.workdir_label }}
                </span>
                <span class="time-tag">{{ relativeTime(item.updated_at) }}</span>
              </div>
            </div>

            <!-- 命中上下文切片（内容匹配时呈现） -->
            <div
              v-if="item.snippet"
              class="item-snippet"
              v-html="highlight(item.snippet)"
            ></div>
          </div>
        </template>
      </div>

      <!-- 底部快捷键与汇总条 -->
      <div class="search-foot">
        <div class="foot-keys">
          <span><kbd>↑</kbd><kbd>↓</kbd> 导航</span>
          <span><kbd>Enter</kbd> 跳转</span>
          <span><kbd>Esc</kbd> 退出</span>
        </div>
        <div class="foot-summary">
          <span v-if="results.length > 0">找到 {{ results.length }} 条匹配结果</span>
          <span v-else-if="query.trim() && !loading">0 条结果</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();

const query = ref('');
const currentScope = ref<'all' | 'title' | 'content'>('all');
const projectScope = ref<'all' | 'current' | 'global'>('all');
// 是否把已归档会话纳入检索（默认关闭，与侧栏主列表口径一致）
const includeArchived = ref(false);
const results = ref<any[]>([]);
const loading = ref(false);
const selectedIndex = ref(0);

const inputRef = ref<HTMLInputElement | null>(null);
const resultsContainerRef = ref<HTMLElement | null>(null);

let debounceTimer: any = null;

const currentProjectWorkdir = computed(() => {
  return store.workdir || '';
});

const currentProjectName = computed(() => {
  const w = currentProjectWorkdir.value;
  if (!w || w === 'GLOBAL') return '自由对话';
  const parts = w.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || w;
});

function close() {
  store.closeSessionSearchModal();
}

function clearQuery() {
  query.value = '';
  results.value = [];
  selectedIndex.value = 0;
  nextTick(() => inputRef.value?.focus());
}

function setScope(scope: 'all' | 'title' | 'content') {
  currentScope.value = scope;
  doSearch();
}

function onInput() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    doSearch();
  }, 160);
}

async function doSearch() {
  const q = query.value.trim();
  if (!q) {
    results.value = [];
    selectedIndex.value = 0;
    loading.value = false;
    return;
  }

  let workdirFilter: string | undefined = undefined;
  if (projectScope.value === 'current') {
    workdirFilter = currentProjectWorkdir.value || undefined;
  } else if (projectScope.value === 'global') {
    workdirFilter = 'GLOBAL';
  }

  loading.value = true;
  try {
    const res = await store.searchSessionsFull(
      q,
      currentScope.value,
      workdirFilter,
      30,
      includeArchived.value,
    );
    if (res && res.ok && Array.isArray(res.results)) {
      results.value = res.results;
      selectedIndex.value = 0;
    } else {
      results.value = [];
    }
  } catch (err) {
    console.error('会话搜索失败:', err);
    results.value = [];
  } finally {
    loading.value = false;
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault();
    close();
    return;
  }
  if (results.value.length === 0) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    selectedIndex.value = (selectedIndex.value + 1) % results.value.length;
    scrollSelectedIntoView();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    selectedIndex.value = (selectedIndex.value - 1 + results.value.length) % results.value.length;
    scrollSelectedIntoView();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (results.value[selectedIndex.value]) {
      jumpToSession(results.value[selectedIndex.value]);
    }
  }
}

function scrollSelectedIntoView() {
  nextTick(() => {
    if (!resultsContainerRef.value) return;
    const selectedEl = resultsContainerRef.value.querySelector('.result-item.selected') as HTMLElement;
    if (selectedEl) {
      selectedEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });
}

async function jumpToSession(item: any) {
  if (!item || !item.session_name) return;
  close();
  await store.switchSession(item.session_name, item.display_name, item.workdir);
}

function badgeClass(item: any): string {
  if (item.type === 'title') return 'badge-title';
  if (item.role === 'user') return 'badge-user';
  return 'badge-assistant';
}

function badgeText(item: any): string {
  if (item.type === 'title') return '标题匹配';
  if (item.role === 'user') return '用户提问';
  if (item.role === 'assistant') return '助手回复';
  return '内容匹配';
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function highlight(text: string | null | undefined): string {
  if (!text) return '';
  const escaped = escapeHtml(text);
  const q = query.value.trim();
  if (!q) return escaped;
  const escapedQ = escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return escaped.replace(new RegExp(`(${escapedQ})`, 'gi'), '<mark>$1</mark>');
}

function relativeTime(isoStr: string | null | undefined): string {
  if (!isoStr) return '';
  try {
    const ts = new Date(isoStr.replace(' ', 'T')).getTime();
    if (isNaN(ts)) return isoStr;
    const diff = Math.floor((Date.now() - ts) / 1000);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}天前`;
    return isoStr.split(' ')[0] || isoStr;
  } catch {
    return isoStr || '';
  }
}

watch(
  () => store.sessionSearchModal.show,
  (val) => {
    if (val) {
      nextTick(() => {
        if (inputRef.value) {
          inputRef.value.focus();
          inputRef.value.select();
        }
      });
      if (query.value.trim()) {
        doSearch();
      }
    } else {
      clearTimeout(debounceTimer);
    }
  },
);
</script>

<style scoped>
.session-search-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(4px);
  z-index: 1200;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 70px;
  animation: fadeIn 0.15s ease;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.session-search-modal {
  width: 640px;
  max-width: 92vw;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.65), 0 0 0 1px var(--border);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  animation: pop 0.18s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes pop {
  from {
    transform: scale(0.96) translateY(-8px);
    opacity: 0;
  }
  to {
    transform: scale(1) translateY(0);
    opacity: 1;
  }
}

/* 顶部搜索条 */
.search-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
}

.search-icon {
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
}

.search-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  font-size: 14.5px;
  color: var(--text);
  font-family: inherit;
}

.search-input::placeholder {
  color: var(--text-faint);
  font-size: 13.5px;
}

.clear-btn {
  background: transparent;
  border: none;
  color: var(--text-faint);
  font-size: 13px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
  transition: all 0.15s;
}

.clear-btn:hover {
  color: var(--text);
  background: var(--bg3);
}

.esc-badge {
  font-size: 10px;
  color: var(--text-faint);
  background: var(--bg1);
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  font-family: monospace;
}

/* 过滤栏 */
.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.scope-tabs, .project-scope {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-label {
  color: var(--text-faint);
  font-size: 11px;
  margin-right: 2px;
}

.tab-btn {
  background: transparent;
  border: none;
  color: var(--text-faint);
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.tab-btn:hover {
  color: var(--text);
  background: var(--bg2);
}

.tab-btn.active {
  color: #ffffff;
  background: var(--accent);
  font-weight: 500;
}

.project-select {
  background: var(--bg2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 11.5px;
  outline: none;
  cursor: pointer;
}

/* 「包含已归档」开关：与范围选择器同排，弱化为次级过滤项 */
.archive-toggle {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--text-faint);
  font-size: 11.5px;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.archive-toggle input {
  accent-color: var(--accent);
  cursor: pointer;
  margin: 0;
}

.archive-toggle:hover {
  color: var(--text);
}

/* 结果区 */
.results-container {
  max-height: 390px;
  min-height: 120px;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.state-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  text-align: center;
  color: var(--text-faint);
  gap: 8px;
}

.state-box .empty-ico {
  font-size: 26px;
  opacity: 0.7;
}

.state-box .empty-title {
  font-size: 13.5px;
  color: var(--text);
  font-weight: 500;
  margin: 0;
}

.state-box .empty-desc {
  font-size: 11.5px;
  color: var(--text-faint);
  margin: 0;
}

.loading-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 结果条目 */
.result-item {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid transparent;
  background: var(--bg2);
  cursor: pointer;
  transition: all 0.12s ease;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.result-item:hover, .result-item.selected {
  background: var(--bg3);
  border-color: var(--border);
}

.result-item.selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-soft, rgba(124, 92, 255, 0.25));
}

.item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.session-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}

.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-faint);
  flex-shrink: 0;
}

.folder-tag {
  background: var(--bg1);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.time-tag {
  font-size: 10.5px;
}

/* 徽章样式 */
.type-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
  white-space: nowrap;
  flex-shrink: 0;
}

.badge-title {
  background: rgba(56, 189, 248, 0.15);
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.3);
}

.badge-user {
  background: rgba(168, 85, 247, 0.15);
  color: #c084fc;
  border: 1px solid rgba(168, 85, 247, 0.3);
}

.badge-assistant {
  background: rgba(46, 204, 113, 0.15);
  color: #2ecc71;
  border: 1px solid rgba(46, 204, 113, 0.3);
}

/* 消息切片 */
.item-snippet {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-dim);
  background: var(--bg1);
  padding: 6px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  word-break: break-all;
}

:deep(mark) {
  background: var(--search-mark-bg, rgba(245, 158, 11, 0.28));
  color: var(--search-mark-text, #fde047);
  padding: 1px 3px;
  border-radius: 3px;
  font-weight: 600;
}

/* 底部操作条 */
.search-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-faint);
}

.foot-keys {
  display: flex;
  align-items: center;
  gap: 12px;
}

.foot-keys kbd {
  background: var(--bg1);
  border: 1px solid var(--border);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 10px;
  font-family: monospace;
  margin-right: 3px;
}
</style>

<style>
/* 浅色主题（白色主题）专项适配：解决浅色底色下高亮文字变白看不清的问题 */
:root.light .session-search-mask,
body.light .session-search-mask {
  background: rgba(0, 0, 0, 0.35);
}

:root.light .session-search-modal,
body.light .session-search-modal {
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15), 0 0 0 1px var(--border);
}

:root.light .session-search-modal mark,
body.light .session-search-modal mark {
  background: #fef08a !important;
  color: #854d0e !important;
}

:root.light .session-search-modal .badge-title,
body.light .session-search-modal .badge-title {
  background: rgba(2, 132, 199, 0.12);
  color: #0369a1;
  border-color: rgba(2, 132, 199, 0.3);
}

:root.light .session-search-modal .badge-user,
body.light .session-search-modal .badge-user {
  background: rgba(147, 51, 234, 0.12);
  color: #7e22ce;
  border-color: rgba(147, 51, 234, 0.3);
}

:root.light .session-search-modal .badge-assistant,
body.light .session-search-modal .badge-assistant {
  background: rgba(22, 163, 74, 0.12);
  color: #15803d;
  border-color: rgba(22, 163, 74, 0.3);
}
</style>
