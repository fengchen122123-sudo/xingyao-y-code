<template>
  <div id="git-panel" data-test="git-panel">
    <!-- 未连接 / 非仓库状态 -->
    <div v-if="!store.snapshot" class="git-note" data-test="git-empty">
      <template v-if="store.error && store.error.code === 'not_a_repository'">
        当前目录不是 Git 仓库
      </template>
      <template v-else-if="store.loading">加载中…</template>
      <template v-else-if="store.error">{{ store.error.message }}</template>
      <template v-else>选择工作区后显示 Git 状态</template>
    </div>

    <template v-else>
      <!-- 信任提示（§4.5：未信任仓库只允许硬化元数据查询） -->
      <div v-if="store.trustLevel !== 'trusted'" class="git-trust" data-test="git-trust-banner">
        <span>此仓库未信任：部分操作可能触发仓库内脚本，需先确认</span>
        <div class="git-trust-actions">
          <button data-test="git-allow-once" @click="store.allowOnce()">允许本次</button>
          <button data-test="git-trust-repo" @click="store.trustRepository()">信任此仓库</button>
        </div>
      </div>

      <!-- 错误横幅 -->
      <div v-if="store.error" class="git-error" data-test="git-error-banner">
        {{ store.error.message }}
        <button v-if="store.error.retryable" @click="store.refresh()">重试</button>
      </div>

      <!-- 分支与同步摘要（§11.2） -->
      <div class="git-head">
        <span class="git-branch" data-test="git-branch-label">
          ⎇ {{ store.branchLabel }}
          <span v-if="store.isDirty" class="git-dirty-mark">*</span>
        </span>
        <span v-if="store.syncLabel" class="git-sync" data-test="git-sync">{{ store.syncLabel }}</span>
        <span v-if="store.snapshot!.operation_kind !== 'idle'" class="git-op" data-test="git-operation">
          {{ operationLabel }}
        </span>
        <span class="git-flex"></span>
        <button class="git-mini" data-test="git-refresh" title="刷新" @click="store.refresh()">⟳</button>
      </div>
      <div v-if="store.snapshot!.upstream || store.snapshot!.ahead || store.snapshot!.behind" class="git-upstream">
        上游: {{ store.snapshot!.upstream }} {{ store.syncLabel }}
      </div>

      <!-- 提交信息输入区 -->
      <div class="git-commit-box">
        <input
          v-model="store.commitTitle"
          data-test="git-commit-title"
          class="git-commit-title"
          placeholder="提交说明标题"
          @keydown.enter="onCommit"
        />
        <textarea
          v-model="store.commitBody"
          data-test="git-commit-body"
          class="git-commit-body"
          rows="2"
          placeholder="正文（可选）"
        ></textarea>
        <div class="git-commit-actions">
          <button data-test="git-commit-btn" :disabled="store.snapshot!.staged.length === 0 || !store.commitTitle.trim()" @click="onCommit">提交</button>
          <button data-test="git-stash-btn" :disabled="!store.isDirty" title="Stash 全部变更" @click="store.stash(false)">Stash</button>
          <span class="git-flex"></span>
          <button class="git-mini" data-test="git-fetch-btn" @click="store.fetch()">Fetch</button>
          <button class="git-mini" data-test="git-pull-btn" @click="store.pull()">Pull</button>
          <button class="git-mini" data-test="git-push-btn" @click="store.push()">Push</button>
        </div>
      </div>

      <!-- 冲突组 -->
      <div v-if="store.snapshot!.conflicts.length" class="git-group">
        <div class="git-group-title git-group-conflict">冲突 ({{ store.snapshot!.conflicts.length }})</div>
        <div v-for="c in store.snapshot!.conflicts" :key="c.path" class="git-file-row git-conflict-row" data-test="git-conflict-row">
          <span class="git-file-path" :title="c.path">{{ c.path }}</span>
          <span class="git-file-kind">{{ c.kind }}</span>
        </div>
      </div>

      <!-- 已暂存组 -->
      <div v-if="store.snapshot!.staged.length" class="git-group" data-test="git-group-staged">
        <div class="git-group-title">已暂存 ({{ store.snapshot!.staged.length }})</div>
        <div v-for="f in store.snapshot!.staged" :key="'s-' + f.path" class="git-file-row" data-test="git-staged-row">
          <span class="git-file-status">{{ f.index_status }}</span>
          <span class="git-file-path" :title="f.orig_path ? f.orig_path + ' → ' + f.path : f.path" @click="openDiff(f.path, true)">{{ f.path }}</span>
          <button class="git-mini" data-test="git-unstage-btn" title="取消暂存" @click="store.unstage(f.path)">-</button>
        </div>
      </div>

      <!-- 变更组（未暂存） -->
      <div v-if="store.snapshot!.unstaged.length" class="git-group" data-test="git-group-changes">
        <div class="git-group-title">变更 ({{ store.snapshot!.unstaged.length }})</div>
        <div v-for="f in store.snapshot!.unstaged" :key="'w-' + f.path" class="git-file-row" data-test="git-change-row">
          <span class="git-file-status">{{ f.worktree_status }}</span>
          <span class="git-file-path" @click="openDiff(f.path, false)">{{ f.path }}</span>
          <button class="git-mini" data-test="git-stage-btn" title="暂存" @click="store.stage(f.path)">+</button>
        </div>
        <div class="git-group-actions">
          <button class="git-mini" data-test="git-stage-all-btn" @click="store.stageAll()">全部暂存</button>
        </div>
      </div>

      <!-- 未跟踪组 -->
      <div v-if="store.snapshot!.untracked.length" class="git-group" data-test="git-group-untracked">
        <div class="git-group-title">未跟踪 ({{ store.snapshot!.untracked.length }})</div>
        <div v-for="p in store.snapshot!.untracked" :key="'u-' + p" class="git-file-row" data-test="git-untracked-row">
          <span class="git-file-status">?</span>
          <span class="git-file-path">{{ p }}</span>
          <button class="git-mini" data-test="git-stage-untracked-btn" @click="store.stage(p)">+</button>
        </div>
      </div>

      <!-- Diff 视图 -->
      <GitDiffView v-if="store.diff" :diff="store.diff" />

      <!-- 高风险确认对话框（§13 两段式：服务端 preview + 一次性令牌） -->
      <div v-if="store.pendingConfirm" class="git-confirm-mask" data-test="git-confirm-modal">
        <div class="git-confirm">
          <div class="git-confirm-title">高风险操作确认</div>
          <div class="git-confirm-text">{{ store.pendingConfirm.preview.confirm_text }}</div>
          <div class="git-confirm-risk">风险级别: {{ store.pendingConfirm.preview.risk_level }}</div>
          <input
            v-if="store.pendingConfirm.preview.typed_confirm_required"
            v-model="store.pendingConfirm.typedInput"
            data-test="git-confirm-input"
            class="git-commit-title"
            placeholder="输入目标分支名或短 Hash 以确认"
          />
          <div class="git-confirm-actions">
            <button data-test="git-confirm-cancel" @click="store.cancelConfirm()">取消</button>
            <button
              data-test="git-confirm-ok"
              class="git-danger"
              :disabled="store.pendingConfirm.preview.typed_confirm_required && !store.pendingConfirm.typedInput.trim()"
              @click="onConfirmOk"
            >确认执行</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useGitStore } from '../stores/gitStore';
import { useAppStore } from '../stores/appStore';
import GitDiffView from './GitDiffView.vue';

const store = useGitStore();
const appStore = useAppStore();

const operationLabel = computed(() => {
  const kind = store.snapshot?.operation_kind || 'idle';
  if (kind === 'idle') return '';
  if (kind === 'unsupported_external') return '外部操作进行中';
  const phase = store.snapshot?.operation_phase || 'none';
  const kindLabel: Record<string, string> = {
    merge: 'MERGE', rebase: 'REBASE', cherry_pick: 'CHERRY-PICK', revert: 'REVERT',
  };
  const phaseLabel: Record<string, string> = {
    paused_for_conflict: '冲突待解决', paused_for_edit: '等待编辑', interrupted: '已中断',
  };
  return `${kindLabel[kind] || kind} ${phaseLabel[phase] || ''}`.trim();
});

function openDiff(path: string, staged: boolean) {
  void store.loadDiff(path, staged);
}

async function onCommit() {
  await store.commit();
}

async function onConfirmOk() {
  const pending = store.pendingConfirm;
  if (!pending) return;
  await store.executeConfirmed(pending.operation, pending.params, pending.preview.token || '');
}

onMounted(() => {
  const workdir = appStore.workdir || '';
  if (workdir) void store.ensureLoaded(workdir);
});

// 会话切换 → 跟随工作区重新加载（§17.3 会话切换不串仓库状态）
import { watch } from 'vue';
watch(
  () => appStore.workdir,
  (wd) => {
    if (wd) void store.ensureLoaded(wd);
  }
);
</script>

<style scoped>
#git-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: auto;
  padding: 8px;
  gap: 6px;
}
.git-note { color: var(--text-dim, #888); padding: 12px 4px; }
.git-flex { flex: 1; }
.git-head { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.git-dirty-mark { color: #e5a037; }
.git-sync { color: #64b5f6; }
.git-op { color: #e5a037; font-size: 12px; }
.git-upstream { color: var(--text-dim, #888); font-size: 12px; }
.git-mini {
  background: transparent;
  border: 1px solid transparent;
  color: inherit;
  cursor: pointer;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
}
.git-mini:hover { border-color: rgba(127, 127, 127, 0.4); }
.git-commit-box { display: flex; flex-direction: column; gap: 4px; }
.git-commit-title, .git-commit-body {
  width: 100%;
  box-sizing: border-box;
  background: transparent;
  border: 1px solid rgba(127, 127, 127, 0.35);
  border-radius: 4px;
  color: inherit;
  padding: 4px 8px;
  font-size: 13px;
}
.git-commit-actions { display: flex; gap: 6px; align-items: center; }
.git-commit-actions button {
  background: rgba(100, 181, 246, 0.15);
  border: 1px solid rgba(100, 181, 246, 0.4);
  color: inherit;
  border-radius: 4px;
  cursor: pointer;
  padding: 2px 10px;
}
.git-commit-actions button:disabled { opacity: 0.45; cursor: default; }
.git-group-title { font-size: 12px; color: var(--text-dim, #888); margin: 4px 0 2px; }
.git-group-title.git-group-conflict { color: #e57373; font-weight: 600; }
.git-file-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px;
  border-radius: 4px;
}
.git-file-row:hover { background: rgba(127, 127, 127, 0.12); }
.git-file-status { width: 14px; color: #e5a037; font-family: monospace; }
.git-file-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  direction: rtl;
  text-align: left;
}
.git-file-kind { color: var(--text-dim, #888); font-size: 11px; }
.git-conflict-row .git-file-path { color: #e57373; }
.git-group-actions { display: flex; justify-content: flex-end; }
.git-trust {
  border: 1px solid rgba(229, 160, 55, 0.5);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.git-trust-actions { display: flex; gap: 6px; }
.git-trust-actions button {
  background: rgba(229, 160, 55, 0.15);
  border: 1px solid rgba(229, 160, 55, 0.5);
  color: inherit;
  border-radius: 4px;
  cursor: pointer;
  padding: 2px 8px;
}
.git-error {
  border: 1px solid rgba(229, 115, 115, 0.5);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  color: #e57373;
  display: flex;
  gap: 6px;
  align-items: center;
}
.git-confirm-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 60;
}
.git-confirm {
  background: var(--panel-bg, #222);
  border: 1px solid rgba(229, 115, 115, 0.6);
  border-radius: 8px;
  padding: 14px 16px;
  width: min(420px, 90vw);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.git-confirm-title { font-weight: 700; color: #e57373; }
.git-confirm-text { font-size: 13px; }
.git-confirm-risk { font-size: 12px; color: var(--text-dim, #888); }
.git-confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }
.git-confirm-actions button {
  border-radius: 4px;
  border: 1px solid rgba(127, 127, 127, 0.4);
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 4px 12px;
}
.git-confirm-actions .git-danger {
  background: rgba(229, 115, 115, 0.2);
  border-color: rgba(229, 115, 115, 0.6);
}
.git-confirm-actions button:disabled { opacity: 0.45; cursor: default; }
</style>
