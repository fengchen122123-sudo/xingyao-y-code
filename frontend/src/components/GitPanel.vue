<template>
  <div id="git-panel" data-test="git-panel">
    <div v-if="!store.snapshot" class="git-note" data-test="git-empty">
      <div class="git-note-badge">
        <AppIcon name="git-branch" :size="20" :stroke-width="1.6" class="git-note-icon" />
      </div>
      <div class="git-note-text">
        <template v-if="store.error && store.error.code === 'not_a_repository'">
          当前目录不是 Git 仓库
        </template>
        <template v-else-if="store.loading">加载中…</template>
        <template v-else-if="store.error">{{ store.error.message }}</template>
        <template v-else>选择工作区后显示 Git 状态</template>
      </div>
    </div>

    <template v-else>
      <div v-if="store.trustLevel !== 'trusted'" class="git-trust" data-test="git-trust-banner">
        <div class="git-trust-text">
          <AppIcon name="triangle-alert" :size="14" :stroke-width="2" class="git-trust-icon" />
          <span>此仓库未信任：部分操作可能触发仓库内脚本，需先确认</span>
        </div>
        <div class="git-trust-actions">
          <button class="git-pill ghost-amber" data-test="git-allow-once" @click="store.allowOnce()">允许本次</button>
          <button class="git-pill solid-amber" data-test="git-trust-repo" @click="store.trustRepository()">信任此仓库</button>
        </div>
      </div>

      <div v-if="store.error" class="git-error" data-test="git-error-banner">
        <AppIcon name="triangle-alert" :size="14" :stroke-width="2" class="git-error-icon" />
        <span class="git-error-text">{{ store.error.message }}</span>
        <button v-if="store.error.retryable" class="git-pill ghost-red" @click="store.refresh()">重试</button>
      </div>

      <div ref="repoCardRef" class="git-repo-card">
        <div class="git-head">
          <button
            type="button"
            class="git-branch-btn"
            data-test="git-branch-label"
            :aria-expanded="branchMenuOpen"
            title="点击切换或新建分支"
            @click="toggleBranchMenu"
          >
            <AppIcon name="git-branch" :size="13" :stroke-width="2" class="git-branch-icon" />
            <span class="git-branch-name">{{ store.branchLabel }}</span>
            <span v-if="store.isDirty" class="git-dirty-dot" title="工作区有未提交修改"></span>
            <AppIcon name="chevron-down" :size="12" :stroke-width="2" class="git-branch-caret" />
          </button>

          <span v-if="store.syncLabel" class="git-sync" data-test="git-sync">{{ store.syncLabel }}</span>
          <span v-if="store.snapshot!.operation_kind !== 'idle'" class="git-op" data-test="git-operation">
            {{ operationLabel }}
          </span>
          <span class="git-flex"></span>
          <button
            type="button"
            class="git-icon-btn"
            data-test="git-refresh"
            title="刷新仓库状态"
            @click="store.refresh()"
          >
            <AppIcon name="refresh" :size="13" :stroke-width="2" :class="{ 'icon-spin': store.loading }" />
          </button>
        </div>

        <div class="git-upstream-row">
          <span class="git-upstream" :title="store.snapshot!.upstream || ''">
            <template v-if="store.snapshot!.upstream">上游 {{ store.snapshot!.upstream }}</template>
            <template v-else>尚未关联上游分支</template>
          </span>
          <div class="git-remote-actions">
            <button
              type="button"
              class="git-remote-btn"
              data-test="git-fetch-btn"
              :disabled="syncBusy"
              title="从远端获取最新引用 (git fetch)"
              @click="runSync('fetch')"
            >Fetch</button>
            <button
              type="button"
              class="git-remote-btn"
              data-test="git-pull-btn"
              :disabled="syncBusy"
              title="拉取并合并上游更新 (git pull)"
              @click="runSync('pull')"
            >
              <AppIcon name="download" :size="11" :stroke-width="2" />
              <span>Pull</span>
            </button>
            <button
              type="button"
              class="git-remote-btn"
              :class="{ 'is-ahead': store.snapshot!.ahead > 0 }"
              data-test="git-push-btn"
              :disabled="syncBusy"
              title="推送本地提交到远端 (git push)"
              @click="runSync('push')"
            >
              <AppIcon name="upload" :size="11" :stroke-width="2" />
              <span>Push</span>
            </button>
          </div>
        </div>

        <div v-if="branchMenuOpen" class="git-branch-menu" @click.stop>
          <div v-if="store.branches.length > 6" class="git-branch-search-wrap">
            <AppIcon name="search" :size="12" :stroke-width="2" class="git-branch-search-ico" />
            <input
              v-model="branchFilter"
              class="git-branch-search"
              placeholder="筛选分支…"
            />
          </div>
          <div class="git-branch-list">
            <div v-if="branchLoading" class="git-branch-empty">加载分支列表…</div>
            <template v-else>
              <div v-if="localBranches.length" class="git-branch-section-title">本地分支</div>
              <button
                v-for="b in localBranches"
                :key="'local-' + b.name"
                type="button"
                class="git-branch-item"
                :class="{ current: b.is_current }"
                :disabled="branchBusy"
                @click="onSwitchBranch(b.name)"
              >
                <AppIcon name="git-branch" :size="12" :stroke-width="2" />
                <span class="git-branch-item-name">{{ b.name }}</span>
                <span v-if="b.ahead || b.behind" class="git-branch-item-sync">
                  <template v-if="b.ahead">↑{{ b.ahead }}</template>
                  <template v-if="b.behind">↓{{ b.behind }}</template>
                </span>
                <AppIcon v-if="b.is_current" name="check" :size="12" :stroke-width="2.2" class="git-branch-check" />
              </button>

              <template v-if="remoteBranches.length">
                <div class="git-branch-section-title">远程分支</div>
                <button
                  v-for="b in remoteBranches"
                  :key="'remote-' + b.name"
                  type="button"
                  class="git-branch-item"
                  :disabled="branchBusy"
                  @click="onSwitchBranch(b.name)"
                >
                  <AppIcon name="git-branch" :size="12" :stroke-width="2" />
                  <span class="git-branch-item-name">{{ b.name }}</span>
                </button>
              </template>

              <div v-if="!localBranches.length && !remoteBranches.length" class="git-branch-empty">
                无匹配分支
              </div>
            </template>
          </div>
          <div class="git-branch-create">
            <input
              v-model="newBranchName"
              class="git-branch-create-input"
              placeholder="输入新分支名…"
              @keydown.enter.prevent="onCreateBranch"
            />
            <button
              type="button"
              class="git-branch-create-btn"
              :disabled="!newBranchName.trim() || branchBusy"
              @click="onCreateBranch"
            >
              <AppIcon name="plus" :size="11" :stroke-width="2.2" />
              <span>新建</span>
            </button>
          </div>
        </div>
      </div>

      <div class="git-commit-box">
        <div class="git-commit-head">
          <input
            v-model="store.commitTitle"
            data-test="git-commit-title"
            class="git-commit-title"
            placeholder="输入提交说明标题…"
            @keydown.enter="onCommit"
          />
          <button
            type="button"
            class="git-ai-btn"
            data-test="git-ai-commit-msg"
            title="发送可见对话消息，由 AI 生成提交说明建议"
            @click="aiAssist('commit_message')"
          >
            <AppIcon name="sparkles" :size="12" :stroke-width="2" />
            <span>AI 生成</span>
          </button>
        </div>
        <div class="git-commit-body-wrap">
          <textarea
            v-model="store.commitBody"
            data-test="git-commit-body"
            class="git-commit-body"
            rows="2"
            placeholder="正文说明（可选）…"
          ></textarea>
        </div>
        <div class="git-commit-actions">
          <button
            type="button"
            class="git-pill git-stash-btn"
            data-test="git-stash-btn"
            :disabled="!store.isDirty"
            title="将工作区当前改动暂存到 Stash"
            @click="store.stash(false)"
          >
            <AppIcon name="archive" :size="12" :stroke-width="2" />
            <span>Stash</span>
          </button>
          <span class="git-commit-hint">{{ commitHint }}</span>
          <span class="git-flex"></span>
          <button
            type="button"
            class="git-pill git-primary"
            data-test="git-commit-btn"
            :disabled="store.snapshot!.staged.length === 0 || !store.commitTitle.trim()"
            :title="store.snapshot!.staged.length === 0 ? '请先暂存至少一个文件' : !store.commitTitle.trim() ? '请输入提交说明标题' : '提交已暂存更改'"
            @click="onCommit"
          >
            <AppIcon name="check" :size="12" :stroke-width="2.2" />
            <span>提交<template v-if="store.snapshot!.staged.length"> ({{ store.snapshot!.staged.length }})</template></span>
          </button>
        </div>
      </div>

      <div class="git-toolbar">
        <div class="git-view-switch">
          <button
            type="button"
            data-test="git-view-status"
            :class="{ active: viewMode === 'status' }"
            @click="viewMode = 'status'"
          >
            <AppIcon name="layers" :size="12" :stroke-width="2" />
            <span>变更</span>
            <span v-if="totalChanges > 0" class="git-tab-count">{{ totalChanges }}</span>
          </button>
          <button
            type="button"
            data-test="git-view-history"
            :class="{ active: viewMode === 'history' }"
            @click="viewMode = 'history'"
          >
            <AppIcon name="history" :size="12" :stroke-width="2" />
            <span>历史</span>
          </button>
        </div>
        <span class="git-flex"></span>
        <button
          v-if="viewMode === 'status' && store.snapshot!.unstaged.length + store.snapshot!.untracked.length > 0"
          type="button"
          class="git-link"
          title="暂存所有修改和未跟踪文件"
          @click="store.stageAll()"
        >全部暂存</button>
      </div>

      <GitHistory
        v-if="viewMode === 'history' && store.snapshot"
        @open-commit="openCommitDiff"
      />

      <template v-else-if="store.snapshot && viewMode === 'status'">
        <div v-if="totalChanges === 0" class="git-clean-card">
          <AppIcon name="check-circle" :size="22" :stroke-width="1.6" class="git-clean-icon" />
          <div class="git-clean-title">工作区干净</div>
          <div class="git-clean-sub">当前没有待暂存或待提交的文件更改</div>
        </div>

        <div v-else class="git-groups">
          <div v-if="store.snapshot!.conflicts.length" class="git-group git-group-conflict">
            <div class="git-group-head" :class="{ collapsed: collapsedGroups.conflicts }">
              <button type="button" class="git-group-toggle conflict-title" @click="toggleGroup('conflicts')">
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" class="git-group-caret" :class="{ open: !collapsedGroups.conflicts }" />
                <span>合并冲突</span>
                <span class="git-count red">{{ store.snapshot!.conflicts.length }}</span>
              </button>
            </div>
            <div v-show="!collapsedGroups.conflicts" class="git-group-body">
              <div
                v-for="c in store.snapshot!.conflicts"
                :key="c.path"
                class="git-file-row conflict-row"
                data-test="git-conflict-row"
                title="点击打开冲突解决器"
                @click="store.conflictResolverPath = c.path"
              >
                <span class="git-file-status st-conflict">!</span>
                <div class="git-file-path" :title="c.path">
                  <span class="git-file-name">{{ splitGitPath(c.path).name }}</span>
                  <span v-if="splitGitPath(c.path).dir" class="git-file-dir">{{ splitGitPath(c.path).dir }}</span>
                </div>
                <span class="git-file-kind">{{ c.kind }}</span>
              </div>
            </div>
          </div>

          <div v-if="store.snapshot!.staged.length" class="git-group" data-test="git-group-staged">
            <div class="git-group-head" :class="{ collapsed: collapsedGroups.staged }">
              <button type="button" class="git-group-toggle" @click="toggleGroup('staged')">
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" class="git-group-caret" :class="{ open: !collapsedGroups.staged }" />
                <span>已暂存</span>
                <span class="git-count green">{{ store.snapshot!.staged.length }}</span>
              </button>
              <button type="button" class="git-group-link muted" @click="store.unstageAll()">全部取消</button>
            </div>
            <div v-show="!collapsedGroups.staged" class="git-group-body">
              <div v-for="f in store.snapshot!.staged" :key="'s-' + f.path" class="git-file-row" data-test="git-staged-row">
                <span class="git-file-status" :class="`st-${statusKind(f.index_status)}`">{{ f.index_status }}</span>
                <div
                  class="git-file-path"
                  :title="f.orig_path ? f.orig_path + ' → ' + f.path : f.path"
                  @click="openDiff(f.path, true)"
                >
                  <span class="git-file-name">{{ splitGitPath(f.path).name }}</span>
                  <span v-if="splitGitPath(f.path).dir" class="git-file-dir">{{ splitGitPath(f.path).dir }}</span>
                </div>
                <div class="git-row-actions">
                  <button
                    type="button"
                    class="git-act"
                    data-test="git-unstage-btn"
                    title="取消暂存"
                    @click.stop="store.unstage(f.path)"
                  >
                    <AppIcon name="minus" :size="11" :stroke-width="2.2" />
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div v-if="store.snapshot!.unstaged.length" class="git-group" data-test="git-group-changes">
            <div class="git-group-head" :class="{ collapsed: collapsedGroups.unstaged }">
              <button type="button" class="git-group-toggle" @click="toggleGroup('unstaged')">
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" class="git-group-caret" :class="{ open: !collapsedGroups.unstaged }" />
                <span>未暂存变更</span>
                <span class="git-count amber">{{ store.snapshot!.unstaged.length }}</span>
              </button>
              <button type="button" class="git-group-link" data-test="git-stage-all-btn" @click="store.stageAll()">全部暂存</button>
            </div>
            <div v-show="!collapsedGroups.unstaged" class="git-group-body">
              <div v-for="f in store.snapshot!.unstaged" :key="'w-' + f.path" class="git-file-row" data-test="git-change-row">
                <span class="git-file-status" :class="`st-${statusKind(f.worktree_status)}`">{{ f.worktree_status }}</span>
                <div class="git-file-path" :title="f.path" @click="openDiff(f.path, false)">
                  <span class="git-file-name">{{ splitGitPath(f.path).name }}</span>
                  <span v-if="splitGitPath(f.path).dir" class="git-file-dir">{{ splitGitPath(f.path).dir }}</span>
                </div>
                <div class="git-row-actions">
                  <button
                    type="button"
                    class="git-act danger"
                    data-test="git-discard-btn"
                    title="撤销更改（将进入高风险确认）"
                    @click.stop="store.discard(f.path)"
                  >
                    <AppIcon name="undo" :size="11" :stroke-width="2" />
                  </button>
                  <button
                    type="button"
                    class="git-act"
                    data-test="git-stage-btn"
                    title="暂存"
                    @click.stop="store.stage(f.path)"
                  >
                    <AppIcon name="plus" :size="11" :stroke-width="2.2" />
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div v-if="store.snapshot!.untracked.length" class="git-group" data-test="git-group-untracked">
            <div class="git-group-head" :class="{ collapsed: collapsedGroups.untracked }">
              <button type="button" class="git-group-toggle" @click="toggleGroup('untracked')">
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" class="git-group-caret" :class="{ open: !collapsedGroups.untracked }" />
                <span>未跟踪</span>
                <span class="git-count">{{ store.snapshot!.untracked.length }}</span>
              </button>
            </div>
            <div v-show="!collapsedGroups.untracked" class="git-group-body">
              <div v-for="p in store.snapshot!.untracked" :key="'u-' + p" class="git-file-row" data-test="git-untracked-row">
                <span class="git-file-status st-other">?</span>
                <div class="git-file-path" :title="p">
                  <span class="git-file-name">{{ splitGitPath(p).name }}</span>
                  <span v-if="splitGitPath(p).dir" class="git-file-dir">{{ splitGitPath(p).dir }}</span>
                </div>
                <div class="git-row-actions">
                  <button
                    type="button"
                    class="git-act"
                    data-test="git-stage-untracked-btn"
                    title="暂存"
                    @click.stop="store.stage(p)"
                  >
                    <AppIcon name="plus" :size="11" :stroke-width="2.2" />
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>

      <Teleport to="body">
        <div v-if="diffModal" class="git-diff-mask" data-test="git-diff-modal" @click.self="closeDiffModal">
          <div class="git-diff-modal">
            <div class="git-diff-modal-head">
              <span
                class="git-diff-modal-kind"
                :class="diffModal.kind === 'commit' ? 'is-commit' : diffModal.staged ? 'is-staged' : 'is-unstaged'"
              >
                <template v-if="diffModal.kind === 'commit'">提交 {{ diffModal.subtitle }}</template>
                <template v-else>{{ diffModal.staged ? '已暂存' : '工作区变更' }}</template>
              </span>

              <div v-if="diffModal.kind === 'file'" class="git-diff-modal-title" :title="diffModal.path">
                <span class="git-diff-modal-name">{{ splitGitPath(diffModal.path).name }}</span>
                <span v-if="splitGitPath(diffModal.path).dir" class="git-diff-modal-dir">{{ splitGitPath(diffModal.path).dir }}</span>
              </div>
              <div v-else class="git-diff-modal-title" :title="diffModal.path">
                <span class="git-diff-modal-name">{{ diffModal.path }}</span>
              </div>

              <div v-if="!store.diffLoading && store.diff && !store.diff.is_binary" class="git-diff-stats">
                <span class="git-stat-add">+{{ diffStats.added }}</span>
                <span class="git-stat-del">-{{ diffStats.deleted }}</span>
              </div>

              <button
                v-if="diffModal.kind === 'file'"
                type="button"
                class="git-modal-action-btn"
                @click="toggleModalStage"
              >
                {{ diffModal.staged ? '取消暂存' : '暂存此文件' }}
              </button>

              <button class="git-diff-close" data-test="git-diff-modal-close" title="关闭 (Esc)" @click="closeDiffModal">
                <AppIcon name="x" :size="16" :stroke-width="2" />
              </button>
            </div>
            <div class="git-diff-modal-body">
              <div v-if="store.diffLoading" class="git-diff-note">加载 Diff 中…</div>
              <GitDiffView v-else-if="store.diff" :diff="store.diff" />
              <div v-else class="git-diff-note">无法加载该 Diff</div>
            </div>
          </div>
        </div>
      </Teleport>

      <Teleport to="body">
        <div
          v-if="store.conflictResolverPath"
          class="git-diff-mask"
          data-test="git-conflict-modal"
          @click.self="store.conflictResolverPath = ''"
        >
          <div class="git-conflict-modal">
            <GitConflictResolver :path="store.conflictResolverPath" />
          </div>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="store.pendingConfirm" class="git-confirm-mask" data-test="git-confirm-modal">
          <div class="git-confirm">
            <div class="git-confirm-title">
              <AppIcon name="triangle-alert" :size="16" :stroke-width="2" />
              <span>高风险操作确认</span>
            </div>
            <div class="git-confirm-text">{{ store.pendingConfirm.preview.confirm_text }}</div>
            <div class="git-confirm-risk">风险级别：{{ store.pendingConfirm.preview.risk_level }}</div>
            <input
              v-if="store.pendingConfirm.preview.typed_confirm_required"
              v-model="store.pendingConfirm.typedInput"
              data-test="git-confirm-input"
              class="git-confirm-input"
              placeholder="输入目标分支名或短 Hash 以确认"
            />
            <div class="git-confirm-actions">
              <button class="git-pill" data-test="git-confirm-cancel" @click="store.cancelConfirm()">取消</button>
              <button
                data-test="git-confirm-ok"
                class="git-pill git-danger"
                :disabled="store.pendingConfirm.preview.typed_confirm_required && !store.pendingConfirm.typedInput.trim()"
                @click="onConfirmOk"
              >确认执行</button>
            </div>
          </div>
        </div>
      </Teleport>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useGitStore } from '../stores/gitStore';
import { useAppStore } from '../stores/appStore';
import { countDiffStats, splitGitPath } from '../utils/gitFormat';
import GitDiffView from './GitDiffView.vue';
import GitHistory from './GitHistory.vue';
import GitConflictResolver from './GitConflictResolver.vue';
import AppIcon from './AppIcon.vue';
import type { GitCommitDto } from '../types/pywebview.d';

interface DiffModalState {
  kind: 'file' | 'commit';
  path: string;
  staged: boolean;
  subtitle?: string;
}

const store = useGitStore();
const appStore = useAppStore();
const viewMode = ref<'status' | 'history'>('status');
const diffModal = ref<DiffModalState | null>(null);

const repoCardRef = ref<HTMLElement | null>(null);
const branchMenuOpen = ref(false);
const branchLoading = ref(false);
const branchBusy = ref(false);
const branchFilter = ref('');
const newBranchName = ref('');
const syncBusy = ref(false);

const collapsedGroups = reactive({
  conflicts: false,
  staged: false,
  unstaged: false,
  untracked: false,
});

const totalChanges = computed(() => {
  const s = store.snapshot;
  if (!s) return 0;
  return s.conflicts.length + s.staged.length + s.unstaged.length + s.untracked.length;
});

const commitHint = computed(() => {
  const count = store.snapshot?.staged.length ?? 0;
  return count > 0 ? `已暂存 ${count} 项` : '请先暂存变更';
});

const diffStats = computed(() => countDiffStats(store.diff));

const filteredBranches = computed(() => {
  const q = branchFilter.value.trim().toLowerCase();
  if (!q) return store.branches;
  return store.branches.filter((b) => b.name.toLowerCase().includes(q));
});

const localBranches = computed(() => filteredBranches.value.filter((b) => !b.is_remote));
const remoteBranches = computed(() => filteredBranches.value.filter((b) => b.is_remote));

function toggleGroup(key: keyof typeof collapsedGroups) {
  collapsedGroups[key] = !collapsedGroups[key];
}

async function toggleBranchMenu() {
  branchMenuOpen.value = !branchMenuOpen.value;
  if (branchMenuOpen.value) {
    branchFilter.value = '';
    newBranchName.value = '';
    branchLoading.value = true;
    try {
      await store.loadBranches();
    } finally {
      branchLoading.value = false;
    }
  }
}

async function onSwitchBranch(name: string) {
  if (branchBusy.value) return;
  branchBusy.value = true;
  try {
    await store.switchBranch(name);
    branchMenuOpen.value = false;
  } finally {
    branchBusy.value = false;
  }
}

async function onCreateBranch() {
  const name = newBranchName.value.trim();
  if (!name || branchBusy.value) return;
  branchBusy.value = true;
  try {
    await store.createBranch(name, true);
    newBranchName.value = '';
    branchMenuOpen.value = false;
  } finally {
    branchBusy.value = false;
  }
}

async function runSync(op: 'fetch' | 'pull' | 'push') {
  if (syncBusy.value) return;
  syncBusy.value = true;
  try {
    await store[op]();
  } finally {
    syncBusy.value = false;
  }
}

function closeDiffModal() {
  diffModal.value = null;
  store.diff = null;
}

function onGlobalKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return;
  if (diffModal.value) {
    closeDiffModal();
  } else if (store.conflictResolverPath) {
    store.conflictResolverPath = '';
  } else if (branchMenuOpen.value) {
    branchMenuOpen.value = false;
  }
}

function onDocumentPointerDown(event: MouseEvent) {
  if (!branchMenuOpen.value) return;
  const host = repoCardRef.value;
  if (host && event.target instanceof Node && !host.contains(event.target)) {
    branchMenuOpen.value = false;
  }
}

async function aiAssist(kind: string) {
  if (!window.confirm('将发送一条可见对话消息，请 AI 基于已暂存 Diff 生成提交说明建议。继续？')) return;
  await store.aiAssist(kind);
}

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

function statusKind(letter: string): string {
  if (letter === 'A') return 'added';
  if (letter === 'D') return 'deleted';
  if (letter === 'M' || letter === 'R' || letter === 'C') return 'modified';
  return 'other';
}

function openDiff(path: string, staged: boolean) {
  diffModal.value = { kind: 'file', path, staged };
  void store.loadDiff(path, staged);
}

function openCommitDiff(commit: GitCommitDto) {
  store.selectedPath = 'commit:' + commit.oid;
  const left = commit.parents[0] || `${commit.oid}~1`;
  diffModal.value = {
    kind: 'commit',
    path: commit.summary || commit.short,
    staged: false,
    subtitle: commit.short,
  };
  void store.loadCommitDiff(left, commit.oid);
}

async function toggleModalStage() {
  if (!diffModal.value || diffModal.value.kind !== 'file') return;
  const { path, staged } = diffModal.value;
  if (staged) {
    await store.unstage(path);
  } else {
    await store.stage(path);
  }
  if (store.error) return;
  const nextStaged = !staged;
  diffModal.value = { kind: 'file', path, staged: nextStaged };
  await store.loadDiff(path, nextStaged);
}

async function onCommit() {
  if (!store.snapshot || store.snapshot.staged.length === 0 || !store.commitTitle.trim()) return;
  await store.commit();
}

async function onConfirmOk() {
  const pending = store.pendingConfirm;
  if (!pending) return;
  await store.executeConfirmed(pending.operation, pending.params, pending.preview.token || '');
}

onMounted(() => {
  const workdir = appStore.workdir || '';
  if (workdir) {
    void store.ensureLoaded(workdir);
    void store.loadTrust();
  }
  window.addEventListener('keydown', onGlobalKeydown);
  document.addEventListener('mousedown', onDocumentPointerDown);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown);
  document.removeEventListener('mousedown', onDocumentPointerDown);
});

watch(
  () => appStore.workdir,
  (wd) => {
    branchMenuOpen.value = false;
    if (wd) {
      void store.ensureLoaded(wd);
      void store.loadTrust();
    }
  },
);
</script>

<style scoped>
#git-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0;
  gap: 10px;
  font-size: 12.5px;
  color: var(--xy-text, var(--text));
}

.git-note {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--xy-text-3, var(--text-faint, #888));
  padding: 48px 14px;
  text-align: center;
}
.git-note-badge {
  width: 44px;
  height: 44px;
  border-radius: 14px;
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--xy-shadow-sm, none);
}
.git-note-icon {
  color: var(--xy-text-3, var(--text-faint));
}
.git-note-text {
  font-size: 12.5px;
  line-height: 1.5;
}

.git-flex { flex: 1; }
.git-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--xy-text-2, var(--text-dim, #888));
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.git-icon-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.git-pill {
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text-2, var(--text));
  border-radius: 8px;
  cursor: pointer;
  padding: 4px 10px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: background 0.12s, border-color 0.12s, color 0.12s, opacity 0.12s;
}
.git-pill:hover:not(:disabled) {
  border-color: var(--xy-accent, var(--accent));
  color: var(--xy-text, var(--accent));
  background: var(--xy-hover, var(--bg3));
}
.git-pill:disabled { opacity: 0.45; cursor: default; }
.git-pill.git-primary {
  background: var(--xy-accent, var(--accent));
  border-color: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #fff);
  font-weight: 600;
  padding: 4px 13px;
}
.git-pill.git-primary:hover:not(:disabled) {
  color: var(--xy-accent-fg, #fff);
  filter: brightness(1.06);
}
.git-pill.git-danger {
  background: var(--xy-rose, var(--red));
  border-color: var(--xy-rose, var(--red));
  color: #fff;
  font-weight: 600;
}
.git-pill.git-danger:hover:not(:disabled) {
  color: #fff;
  filter: brightness(1.06);
}
.git-pill.ghost-amber {
  background: var(--xy-card, var(--bg1));
  border-color: color-mix(in srgb, var(--xy-amber, var(--orange)) 42%, var(--xy-border, var(--border)));
  color: var(--xy-amber, var(--orange));
}
.git-pill.ghost-amber:hover:not(:disabled) {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 12%, transparent));
}
.git-pill.solid-amber {
  background: var(--xy-amber, var(--orange));
  border-color: var(--xy-amber, var(--orange));
  color: #fff;
  font-weight: 600;
}
.git-pill.solid-amber:hover:not(:disabled) {
  color: #fff;
  filter: brightness(1.06);
}
.git-pill.ghost-red {
  background: var(--xy-card, var(--bg1));
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 42%, var(--xy-border, var(--border)));
  color: var(--xy-rose, var(--red));
}
.git-link {
  border: none;
  background: transparent;
  color: var(--xy-accent, var(--accent));
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  padding: 3px 8px;
  border-radius: 7px;
  transition: background 0.12s;
}
.git-link:hover {
  background: var(--xy-accent-soft, var(--accent-soft));
}

.git-repo-card {
  position: relative;
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 12px;
  padding: 9px 10px 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  box-shadow: var(--xy-shadow-sm, none);
}
.git-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.git-branch-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  padding: 4px 8px;
  border: none;
  border-radius: 8px;
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s;
}
.git-branch-btn:hover {
  background: var(--xy-active, var(--bg3));
}
.git-branch-icon {
  color: var(--xy-accent, var(--accent));
  flex-shrink: 0;
}
.git-branch-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.git-dirty-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--xy-amber, var(--orange));
  flex-shrink: 0;
}
.git-branch-caret {
  color: var(--xy-text-3, var(--text-faint));
  flex-shrink: 0;
}
.git-sync {
  font-size: 11px;
  font-weight: 600;
  color: var(--xy-accent, var(--accent));
  background: var(--xy-accent-soft, color-mix(in srgb, var(--accent) 12%, transparent));
  border-radius: 999px;
  padding: 2px 7px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.git-op {
  font-size: 11px;
  font-weight: 600;
  color: var(--xy-rose, var(--orange));
  background: var(--xy-rose-soft, color-mix(in srgb, var(--orange) 12%, transparent));
  border-radius: 999px;
  padding: 2px 7px;
  flex-shrink: 0;
}
.git-upstream-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 7px;
  border-top: 1px solid var(--xy-border, var(--border));
}
.git-upstream {
  color: var(--xy-text-3, var(--text-faint, #888));
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.git-remote-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.git-remote-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 7px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--xy-text-2, var(--text-dim));
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.git-remote-btn:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.git-remote-btn.is-ahead {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-weight: 600;
}
.git-remote-btn:disabled {
  opacity: 0.45;
  cursor: default;
}

.git-branch-menu {
  position: absolute;
  top: 44px;
  left: 8px;
  right: 8px;
  z-index: 30;
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 12px;
  padding: 6px;
  box-shadow: var(--xy-shadow-pop, 0 12px 32px rgba(0, 0, 0, 0.25));
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.git-branch-search-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 8px;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
}
.git-branch-search-ico {
  color: var(--xy-text-3, var(--text-faint));
  flex-shrink: 0;
}
.git-branch-search {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--xy-text, var(--text));
  font-family: inherit;
  font-size: 12px;
  outline: none;
}
.git-branch-list {
  max-height: 210px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.git-branch-section-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--xy-text-3, var(--text-faint));
  padding: 4px 6px 2px;
}
.git-branch-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--xy-text, var(--text));
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s;
}
.git-branch-item:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
}
.git-branch-item.current {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-weight: 600;
}
.git-branch-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.git-branch-item-sync {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
  font-variant-numeric: tabular-nums;
}
.git-branch-check {
  color: var(--xy-accent, var(--accent));
  flex-shrink: 0;
}
.git-branch-empty {
  padding: 12px 8px;
  text-align: center;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.git-branch-create {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--xy-border, var(--border));
}
.git-branch-create-input {
  flex: 1;
  min-width: 0;
  padding: 4px 8px;
  border-radius: 7px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text, var(--text));
  font-family: inherit;
  font-size: 12px;
  outline: none;
}
.git-branch-create-input:focus {
  border-color: var(--xy-accent, var(--accent));
}
.git-branch-create-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 4px 9px;
  border: none;
  border-radius: 7px;
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #fff);
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
}
.git-branch-create-btn:disabled {
  opacity: 0.45;
  cursor: default;
}

.git-commit-box {
  display: flex;
  flex-direction: column;
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 12px;
  overflow: hidden;
  box-shadow: var(--xy-shadow-sm, none);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.git-commit-box:focus-within {
  border-color: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 3px var(--xy-accent-soft, rgba(37, 99, 235, 0.1)), var(--xy-shadow-sm, none);
}
.git-commit-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 8px 7px 11px;
  border-bottom: 1px solid var(--xy-border, var(--border));
}
.git-commit-title {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--xy-text, var(--text));
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  outline: none;
}
.git-commit-title::placeholder {
  color: var(--xy-text-3, var(--text-faint, #888));
  font-weight: 400;
}
.git-ai-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: none;
  border-radius: 7px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.12s;
}
.git-ai-btn:hover {
  filter: brightness(0.95);
}
.git-commit-body-wrap {
  padding: 7px 11px;
}
.git-commit-body {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--xy-text-2, var(--text-dim));
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
  resize: none;
  min-height: 38px;
  outline: none;
}
.git-commit-body::placeholder {
  color: var(--xy-text-3, var(--text-faint, #888));
}
.git-commit-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  background: var(--xy-surface, var(--bg1));
  border-top: 1px solid var(--xy-border, var(--border));
}
.git-stash-btn {
  padding: 3px 9px;
}
.git-commit-hint {
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.git-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.git-view-switch {
  display: inline-flex;
  gap: 2px;
  background: var(--xy-active, var(--bg2));
  border-radius: 10px;
  padding: 3px;
}
.git-view-switch button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: transparent;
  border: none;
  color: var(--xy-text-2, var(--text-dim, #888));
  border-radius: 7px;
  cursor: pointer;
  padding: 4px 11px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  transition: background 0.14s, color 0.14s, box-shadow 0.14s;
}
.git-view-switch button:hover:not(.active) {
  color: var(--xy-text, var(--text));
}
.git-view-switch button.active {
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text, var(--text));
  box-shadow: var(--xy-shadow-sm, none);
}
.git-tab-count {
  padding: 0 6px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-2, var(--text-dim));
}

.git-clean-card {
  background: var(--xy-card, var(--bg1));
  border: 1px dashed var(--xy-border-strong, var(--border));
  border-radius: 12px;
  padding: 28px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 6px;
}
.git-clean-icon {
  color: var(--xy-green, var(--green));
  opacity: 0.85;
  margin-bottom: 2px;
}
.git-clean-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--xy-text-2, var(--text));
}
.git-clean-sub {
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
}

.git-groups {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 6px;
}
.git-group {
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 12px;
  overflow: hidden;
  box-shadow: var(--xy-shadow-sm, none);
}
.git-group.git-group-conflict {
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 38%, var(--xy-border, var(--border)));
}
.git-group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 6px 8px;
  border-bottom: 1px solid var(--xy-border, var(--border));
}
.git-group-head.collapsed {
  border-bottom: none;
}
.git-group-toggle {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: transparent;
  padding: 2px 0;
  color: var(--xy-text-2, var(--text-dim, #888));
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.git-group-toggle.conflict-title {
  color: var(--xy-rose, var(--red));
}
.git-group-caret {
  color: var(--xy-text-3, var(--text-faint));
  transition: transform 0.15s ease;
  flex-shrink: 0;
}
.git-group-caret.open {
  transform: rotate(90deg);
}
.git-count {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-2, var(--text-dim, #888));
  border-radius: 999px;
  padding: 0 7px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.git-count.green {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 15%, transparent));
  color: var(--xy-green, var(--green));
}
.git-count.amber {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 15%, transparent));
  color: var(--xy-amber, var(--orange));
}
.git-count.red {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 15%, transparent));
  color: var(--xy-rose, var(--red));
}
.git-group-link {
  border: none;
  background: transparent;
  color: var(--xy-accent, var(--accent));
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  padding: 2px 6px;
  border-radius: 6px;
  cursor: pointer;
  flex-shrink: 0;
}
.git-group-link:hover {
  background: var(--xy-accent-soft, var(--accent-soft));
}
.git-group-link.muted {
  color: var(--xy-text-3, var(--text-faint));
}
.git-group-link.muted:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}

.git-group-body {
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.git-file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 7px;
  border-radius: 8px;
  transition: background 0.1s;
}
.git-file-row:hover {
  background: var(--xy-hover, var(--bg2));
}
.git-file-status {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 5px;
  font-family: var(--mono, Consolas, monospace);
  font-size: 11px;
  font-weight: 700;
}
.git-file-status.st-added {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 16%, transparent));
  color: var(--xy-green, var(--green));
}
.git-file-status.st-deleted {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 16%, transparent));
  color: var(--xy-rose, var(--red));
}
.git-file-status.st-modified {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 16%, transparent));
  color: var(--xy-amber, var(--orange));
}
.git-file-status.st-conflict {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 16%, transparent));
  color: var(--xy-rose, var(--red));
}
.git-file-status.st-other {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-dim, #888));
}
.git-file-path {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
  cursor: pointer;
}
.git-file-name {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--xy-text, var(--text));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 1;
  min-width: 0;
}
.git-file-dir {
  font-family: var(--mono, Consolas, monospace);
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.git-file-row:hover .git-file-name {
  color: var(--xy-accent, var(--accent));
}
.git-file-kind {
  flex-shrink: 0;
  color: var(--xy-rose, var(--text-faint));
  background: var(--xy-rose-soft, transparent);
  border-radius: 6px;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 500;
}
.conflict-row {
  cursor: pointer;
}
.conflict-row .git-file-name {
  color: var(--xy-rose, var(--red));
}
.git-row-actions {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.12s;
}
.git-file-row:hover .git-row-actions,
.git-file-row:focus-within .git-row-actions {
  opacity: 1;
}
.git-act {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text-2, var(--text-dim, #888));
  border-radius: 6px;
  cursor: pointer;
  padding: 0;
  transition: color 0.12s, border-color 0.12s, background 0.12s;
}
.git-act:hover {
  color: var(--xy-accent, var(--accent));
  border-color: var(--xy-accent, var(--accent));
}
.git-act.danger:hover {
  color: var(--xy-rose, var(--red));
  border-color: var(--xy-rose, var(--red));
}

.git-trust {
  border: 1px solid color-mix(in srgb, var(--xy-amber, var(--orange)) 38%, var(--xy-border, var(--border)));
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 10%, transparent));
  border-radius: 12px;
  padding: 10px 11px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.git-trust-text {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  line-height: 1.5;
  color: var(--xy-text, var(--text));
}
.git-trust-icon {
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--xy-amber, var(--orange));
}
.git-trust-actions {
  display: flex;
  gap: 6px;
  padding-left: 21px;
}
.git-error {
  border: 1px solid color-mix(in srgb, var(--xy-rose, var(--red)) 38%, var(--xy-border, var(--border)));
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 10%, transparent));
  border-radius: 12px;
  padding: 9px 11px;
  font-size: 12px;
  color: var(--xy-rose, var(--red));
  display: flex;
  gap: 8px;
  align-items: center;
}
.git-error-icon {
  flex-shrink: 0;
}
.git-error-text {
  flex: 1;
  line-height: 1.5;
}

.git-confirm-mask {
  position: fixed;
  inset: 0;
  padding: 16px;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 4000;
}
.git-confirm {
  background: var(--xy-card, var(--bg2));
  border: 1px solid color-mix(in srgb, var(--xy-rose, var(--red)) 40%, var(--xy-border-strong, var(--border)));
  border-radius: 16px;
  padding: 20px 22px 18px;
  width: min(416px, calc(100vw - 32px));
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: var(--xy-shadow-pop, 0 16px 40px rgba(0, 0, 0, 0.35));
}
.git-confirm-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--xy-rose, var(--red));
  font-size: 15px;
}
.git-confirm-text {
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--xy-text, var(--text));
}
.git-confirm-risk {
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 10%, transparent));
  color: var(--xy-rose, var(--red));
}
.git-confirm-input {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-surface, var(--bg1));
  color: var(--xy-text, var(--text));
  font-family: var(--mono, monospace);
  font-size: 12.5px;
  outline: none;
}
.git-confirm-input:focus {
  border-color: var(--xy-rose, var(--red));
}
.git-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}

.git-diff-mask {
  position: fixed;
  inset: 0;
  padding: 16px;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 4000;
}
.git-diff-modal,
.git-conflict-modal {
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  width: min(1000px, 94vw);
  height: min(86vh, 920px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: var(--xy-shadow-pop, 0 16px 40px rgba(0, 0, 0, 0.35));
}
.git-diff-modal-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--xy-border, var(--border));
}
.git-diff-modal-kind {
  flex-shrink: 0;
  font-size: 11.5px;
  font-weight: 600;
  border-radius: 999px;
  padding: 2px 10px;
}
.git-diff-modal-kind.is-unstaged {
  color: var(--xy-amber, var(--orange));
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 12%, transparent));
}
.git-diff-modal-kind.is-staged {
  color: var(--xy-green, var(--green));
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 12%, transparent));
}
.git-diff-modal-kind.is-commit {
  color: var(--xy-accent, var(--accent));
  background: var(--xy-accent-soft, var(--accent-soft));
}
.git-diff-modal-title {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.git-diff-modal-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.git-diff-modal-dir {
  font-family: var(--mono, Consolas, monospace);
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.git-diff-stats {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono, Consolas, monospace);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.git-stat-add { color: var(--xy-green, var(--green)); }
.git-stat-del { color: var(--xy-rose, var(--red)); }
.git-modal-action-btn {
  flex-shrink: 0;
  padding: 4px 10px;
  border-radius: 8px;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-surface, var(--bg1));
  color: var(--xy-text-2, var(--text));
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 0.12s, color 0.12s;
}
.git-modal-action-btn:hover {
  border-color: var(--xy-accent, var(--accent));
  color: var(--xy-accent, var(--accent));
}
.git-diff-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--xy-text-3, var(--text-dim, #888));
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.git-diff-close:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-rose, var(--red));
}
.git-diff-modal-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.git-diff-note {
  color: var(--xy-text-3, var(--text-faint, #888));
  padding: 24px;
  text-align: center;
}
</style>
