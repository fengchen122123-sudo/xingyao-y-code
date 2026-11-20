<template>
  <div class="git-history" data-test="git-history">
    <div class="gh-bar">
      <span class="gh-hint">最近提交记录（点击查看改动）</span>
      <div class="gh-actions">
        <button
          class="gh-btn"
          :class="{ active: showReflog }"
          data-test="git-history-reflog"
          @click="toggleReflog"
        >Reflog</button>
        <button
          class="gh-btn"
          data-test="git-history-refresh"
          :disabled="loadingHistory"
          @click="reloadHistory"
        >
          <AppIcon name="refresh" :size="12" :stroke-width="2" :class="{ 'icon-spin': loadingHistory }" />
          <span>刷新</span>
        </button>
      </div>
    </div>

    <!-- Reflog 恢复（§5.11） -->
    <div v-if="showReflog" class="gh-reflog-card">
      <div class="gh-reflog-title">Reflog 操作日志（可创建恢复分支找回历史位置/误删分支）</div>
      <div v-if="reflogLoading" class="gh-empty">加载 Reflog 中…</div>
      <div v-else-if="reflog.length === 0" class="gh-empty">暂无 Reflog 记录</div>
      <div v-else class="gh-reflog-list">
        <div
          v-for="(e, i) in reflog"
          :key="i"
          class="gh-reflog-row"
          :data-test="'git-reflog-row-' + i"
        >
          <span class="gh-hash">{{ e.oid.slice(0, 7) }}</span>
          <span class="gh-reflog-msg" :title="e.message">{{ e.message }}</span>
          <button
            class="gh-recover-btn"
            :data-test="'git-recover-' + i"
            title="从此位置创建恢复分支"
            @click.stop="recover(e)"
          >恢复分支</button>
        </div>
      </div>
    </div>

    <div v-if="store.history.length === 0" class="gh-empty-card">
      {{ loadingHistory ? '加载提交历史中…' : '暂无提交记录' }}
    </div>

    <div v-else class="gh-list-card">
      <div
        v-for="(c, i) in store.history"
        :key="c.oid"
        class="git-commit-row"
        :class="{ selected: store.selectedPath === 'commit:' + c.oid }"
        :data-test="'git-commit-row-' + i"
        @click="openCommit(c)"
      >
        <div class="gh-rail" aria-hidden="true">
          <span v-if="i < store.history.length - 1" class="gh-line"></span>
          <span class="gh-node" :class="nodeKind(c, i)"></span>
        </div>
        <div class="gh-content">
          <div class="gh-summary" :title="c.summary">{{ c.summary }}</div>
          <div class="gh-meta">
            <span class="gh-hash">{{ c.short }}</span>
            <span class="gh-author" v-if="c.author_name || c.committer_time">
              {{ metaText(c) }}
            </span>
            <span
              v-for="r in c.refs"
              :key="r"
              class="gh-ref"
              :class="{ head: r.includes('HEAD') }"
            >{{ r }}</span>
          </div>
        </div>
      </div>

      <div v-if="store.historyHasMore" class="gh-more-wrap">
        <button class="gh-more-btn" :disabled="loadingHistory" @click="loadMore">
          加载更多（已显示 {{ store.history.length }} 条）
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { useGitStore } from '../stores/gitStore';
import { formatRelativeCommitTime } from '../utils/gitFormat';
import AppIcon from './AppIcon.vue';
import type { GitCommitDto, GitReflogDto } from '../types/pywebview.d';

const emit = defineEmits<{
  (e: 'open-commit', commit: GitCommitDto): void;
}>();

const store = useGitStore();
const reflog = ref<GitReflogDto[]>([]);
const showReflog = ref(false);
const loadingHistory = ref(false);
const reflogLoading = ref(false);

function nodeKind(c: GitCommitDto, i: number): string {
  if (c.parents.length > 1) return 'merge';
  const next = store.history[i + 1];
  if (next && !c.parents.includes(next.oid)) return 'branch';
  return 'normal';
}

function metaText(c: GitCommitDto): string {
  const time = formatRelativeCommitTime(c.committer_time || c.author_time);
  if (c.author_name && time) return `${c.author_name} · ${time}`;
  return c.author_name || time;
}

async function reloadHistory() {
  if (!store.workdir) return;
  loadingHistory.value = true;
  try {
    await store.loadHistory(Math.max(50, store.history.length));
  } finally {
    loadingHistory.value = false;
  }
}

async function loadMore() {
  loadingHistory.value = true;
  try {
    await store.loadHistory(store.history.length + 50);
  } finally {
    loadingHistory.value = false;
  }
}

function openCommit(c: GitCommitDto) {
  emit('open-commit', c);
}

async function toggleReflog() {
  showReflog.value = !showReflog.value;
  if (showReflog.value && reflog.value.length === 0) {
    await loadReflog();
  }
}

async function loadReflog() {
  const api = window.pywebview?.api;
  if (!api) return;
  reflogLoading.value = true;
  try {
    const res = await api.git_load_reflog(store.workdir, 0, 20);
    reflog.value = res.ok && res.entries ? res.entries : [];
  } finally {
    reflogLoading.value = false;
  }
}

async function recover(e: GitReflogDto) {
  const name = `recover-${e.oid.slice(0, 7)}`;
  await apiSafe('git_recover_branch', store.workdir, e.oid, name);
  await store.refresh();
  await store.loadBranches();
}

async function apiSafe(name: string, ...args: unknown[]) {
  const api = window.pywebview?.api as unknown as Record<string, (...a: unknown[]) => Promise<unknown>> | undefined;
  if (api && typeof api[name] === 'function') return api[name](...args);
  return { ok: false };
}

onMounted(() => {
  if (store.workdir && store.history.length === 0) {
    void reloadHistory();
  }
});

watch(
  () => store.workdir,
  (wd) => {
    reflog.value = [];
    if (wd) void reloadHistory();
  },
);
</script>

<style scoped>
.git-history {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.gh-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.gh-hint {
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gh-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.gh-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 9px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text-2, var(--text-dim));
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
}
.gh-btn:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
  border-color: var(--xy-border-strong, var(--border));
}
.gh-btn.active {
  background: var(--xy-accent-soft, var(--accent-soft));
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 40%, transparent);
  color: var(--xy-accent, var(--accent));
}
.gh-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

/* Reflog 卡片 */
.gh-reflog-card {
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 12px;
  padding: 9px 10px;
  box-shadow: var(--xy-shadow-sm, none);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.gh-reflog-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--xy-text-2, var(--text-dim));
}
.gh-reflog-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gh-reflog-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 6px;
  border-radius: 7px;
  transition: background 0.12s;
}
.gh-reflog-row:hover {
  background: var(--xy-hover, var(--bg3));
}
.gh-reflog-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--xy-text-2, var(--text-dim));
}
.gh-recover-btn {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 6px;
  border: none;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
}
.gh-recover-btn:hover {
  filter: brightness(0.95);
}

/* 提交列表卡片 */
.gh-empty-card {
  background: var(--xy-card, var(--bg1));
  border: 1px dashed var(--xy-border-strong, var(--border));
  border-radius: 12px;
  padding: 28px 14px;
  text-align: center;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 12px;
}
.gh-empty {
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  padding: 6px 2px;
}
.gh-list-card {
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 12px;
  padding: 6px;
  box-shadow: var(--xy-shadow-sm, none);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.git-commit-row {
  display: flex;
  align-items: stretch;
  gap: 10px;
  padding: 7px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s;
}
.git-commit-row:hover {
  background: var(--xy-hover, var(--bg3));
}
.git-commit-row.selected {
  background: var(--xy-accent-soft, var(--accent-soft));
}
.gh-rail {
  position: relative;
  width: 12px;
  flex-shrink: 0;
  display: flex;
  justify-content: center;
  padding-top: 5px;
}
.gh-line {
  position: absolute;
  top: 14px;
  bottom: -14px;
  width: 1.5px;
  background: var(--xy-border-strong, var(--border));
}
.gh-node {
  position: relative;
  z-index: 1;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 2px var(--xy-card, var(--bg1));
}
.gh-node.merge {
  background: var(--xy-card, var(--bg1));
  border: 2px solid var(--xy-amber, var(--orange));
  box-shadow: none;
}
.gh-node.branch {
  background: var(--xy-card, var(--bg1));
  border: 2px solid var(--xy-text-3, var(--text-faint));
  box-shadow: none;
}
.gh-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.gh-summary {
  font-size: 12.5px;
  font-weight: 500;
  line-height: 1.4;
  color: var(--xy-text, var(--text));
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.gh-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
}
.gh-hash {
  font-family: var(--mono, Consolas, monospace);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-2, var(--text-dim));
  flex-shrink: 0;
}
.gh-author {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gh-ref {
  font-size: 11px;
  font-weight: 500;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 14%, transparent));
  color: var(--xy-green, var(--green));
}
.gh-ref.head {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.gh-more-wrap {
  padding: 6px 4px 2px;
  text-align: center;
}
.gh-more-btn {
  width: 100%;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.gh-more-btn:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.gh-more-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
