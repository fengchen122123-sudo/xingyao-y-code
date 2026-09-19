<template>
  <div class="git-history" data-test="git-history">
    <div class="git-history-actions">
      <button data-test="git-history-refresh" @click="store.loadHistory()">刷新</button>
      <button data-test="git-history-reflog" @click="loadReflog">Reflog</button>
    </div>
    <div v-if="store.history.length === 0" class="git-note">暂无提交</div>
    <div
      v-for="(c, i) in store.history"
      :key="c.oid"
      class="git-commit-row"
      :class="{ selected: store.selectedPath === 'commit:' + c.oid }"
      :data-test="'git-commit-row-' + i"
      @click="openCommit(c)"
    >
      <span class="git-graph" :class="{ merge: c.parents.length > 1 }">{{ graphChar(c, i) }}</span>
      <span class="git-commit-short">{{ c.short }}</span>
      <span class="git-commit-summary" :title="c.summary">{{ c.summary }}</span>
      <span class="git-commit-refs" v-if="c.refs.length">{{ c.refs.join(' ') }}</span>
    </div>
    <div v-if="store.historyHasMore" class="git-note">仅显示最近 {{ store.history.length }} 条</div>

    <!-- Reflog 恢复（§5.11） -->
    <template v-if="reflog.length">
      <div class="git-group-title">Reflog（创建恢复分支可找回历史位置/误删分支）</div>
      <div v-for="(e, i) in reflog" :key="i" class="git-file-row" :data-test="'git-reflog-row-' + i">
        <span class="git-commit-short">{{ e.oid.slice(0, 7) }}</span>
        <span class="git-commit-summary" :title="e.message">{{ e.message }}</span>
        <button class="git-mini" :data-test="'git-recover-' + i" title="从此位置创建恢复分支" @click.stop="recover(e)">恢复分支</button>
      </div>
    </template>

    <!-- 选中提交的 Diff -->
    <GitDiffView v-if="commitDiff" :diff="commitDiff" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useGitStore } from '../stores/gitStore';
import GitDiffView from './GitDiffView.vue';
import type { GitCommitDto, GitDiffDto, GitReflogDto } from '../types/pywebview.d';

const store = useGitStore();
const reflog = ref<GitReflogDto[]>([]);
const commitDiff = ref<GitDiffDto | null>(null);

// 拓扑由父提交关系计算（§5.9）：合并提交画 ◇，普通提交画 ●，父链断开画 ○
function graphChar(c: GitCommitDto, i: number): string {
  if (c.parents.length > 1) return '◇';
  const parent = store.history[i + 1];
  if (!parent) return '●';
  return c.parents.includes(parent.oid) ? '│' : '○';
}

async function openCommit(c: GitCommitDto) {
  store.selectedPath = 'commit:' + c.oid;
  const res = await (window.pywebview?.api.git_load_diff(store.workdir, '', false, c.oid + '~1', c.oid) ?? Promise.resolve({ ok: false }));
  commitDiff.value = res.ok && res.diff ? res.diff : null;
}

async function loadReflog() {
  const api = window.pywebview?.api;
  if (!api) return;
  const res = await api.git_load_reflog(store.workdir, 0, 20);
  reflog.value = res.ok && res.entries ? res.entries : [];
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
</script>

<style scoped>
.git-history { display: flex; flex-direction: column; gap: 2px; overflow: auto; }
.git-history-actions { display: flex; gap: 6px; margin-bottom: 4px; }
.git-history-actions button {
  background: transparent; border: 1px solid rgba(127,127,127,.4); color: inherit;
  border-radius: 4px; cursor: pointer; padding: 2px 8px; font-size: 12px;
}
.git-note { color: var(--text-dim, #888); padding: 4px; font-size: 12px; }
.git-commit-row {
  display: flex; align-items: center; gap: 6px; padding: 2px 4px; border-radius: 4px;
  cursor: pointer; font-size: 12px;
}
.git-commit-row:hover, .git-commit-row.selected { background: rgba(127,127,127,.14); }
.git-graph { width: 14px; text-align: center; color: var(--accent); font-family: monospace; }
.git-graph.merge { color: var(--yellow); }
.git-commit-short { font-family: monospace; color: var(--text-dim, #888); }
.git-commit-summary { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.git-commit-refs { color: var(--green); font-size: 11px; }
.git-file-row { display: flex; align-items: center; gap: 6px; padding: 2px 4px; border-radius: 4px; }
.git-group-title { font-size: 12px; color: var(--text-dim, #888); margin: 6px 0 2px; }
.git-mini {
  background: transparent; border: 1px solid transparent; color: inherit;
  cursor: pointer; border-radius: 4px; padding: 1px 6px; font-size: 12px;
}
.git-mini:hover { border-color: rgba(127,127,127,.4); }
</style>
