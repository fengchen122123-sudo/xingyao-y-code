<template>
  <div id="git-panel" data-test="git-panel">
    <!-- 未连接 / 非仓库状态 -->
    <div v-if="!store.snapshot" class="git-note" data-test="git-empty">
      <AppIcon name="git-branch" :size="22" :stroke-width="1.5" class="git-note-icon" />
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
        <div class="git-trust-text">
          <AppIcon name="triangle-alert" :size="14" :stroke-width="2" class="git-trust-icon" />
          <span>此仓库未信任：部分操作可能触发仓库内脚本，需先确认</span>
        </div>
        <div class="git-trust-actions">
          <button class="git-pill ghost-amber" data-test="git-allow-once" @click="store.allowOnce()">允许本次</button>
          <button class="git-pill solid-amber" data-test="git-trust-repo" @click="store.trustRepository()">信任此仓库</button>
        </div>
      </div>

      <!-- 错误横幅 -->
      <div v-if="store.error" class="git-error" data-test="git-error-banner">
        <span class="git-error-text">{{ store.error.message }}</span>
        <button v-if="store.error.retryable" class="git-pill ghost-red" @click="store.refresh()">重试</button>
      </div>

      <!-- 仓库概览卡：分支 / 同步 / 操作状态（§11.2） -->
      <div class="git-repo-card">
        <div class="git-head">
          <span class="git-branch" data-test="git-branch-label">
            <AppIcon name="git-branch" :size="14" :stroke-width="2" class="git-branch-icon" />
            <span class="git-branch-name">{{ store.branchLabel }}</span>
            <span v-if="store.isDirty" class="git-dirty-mark">*</span>
          </span>
          <span v-if="store.syncLabel" class="git-sync" data-test="git-sync">{{ store.syncLabel }}</span>
          <span v-if="store.snapshot!.operation_kind !== 'idle'" class="git-op" data-test="git-operation">
            {{ operationLabel }}
          </span>
          <span class="git-flex"></span>
          <button class="git-icon-btn" data-test="git-refresh" title="刷新" @click="store.refresh()">
            <AppIcon name="refresh" :size="13" :stroke-width="2" />
          </button>
        </div>
        <div v-if="store.snapshot!.upstream || store.snapshot!.ahead || store.snapshot!.behind" class="git-upstream">
          上游 {{ store.snapshot!.upstream }} {{ store.syncLabel }}
        </div>
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
          <button class="git-pill git-primary" data-test="git-commit-btn" :disabled="store.snapshot!.staged.length === 0 || !store.commitTitle.trim()" @click="onCommit">提交</button>
          <button class="git-pill" data-test="git-stash-btn" :disabled="!store.isDirty" title="Stash 全部变更" @click="store.stash(false)">Stash</button>
          <span class="git-flex"></span>
          <button class="git-pill ghost" data-test="git-fetch-btn" @click="store.fetch()">Fetch</button>
          <button class="git-pill ghost" data-test="git-pull-btn" @click="store.pull()">Pull</button>
          <button class="git-pill ghost" data-test="git-push-btn" @click="store.push()">Push</button>
        </div>
      </div>

      <!-- 视图切换：状态 / 历史（§5.9 提交图入口） -->
      <div class="git-toolbar">
        <div class="git-view-switch">
          <button data-test="git-view-status" :class="{ active: viewMode === 'status' }" @click="viewMode = 'status'">状态</button>
          <button data-test="git-view-history" :class="{ active: viewMode === 'history' }" @click="viewMode = 'history'">历史</button>
        </div>
        <span class="git-flex"></span>
        <button v-if="viewMode === 'status'" class="git-pill ghost sparkle" data-test="git-ai-commit-msg" title="发送可见对话消息，由 AI 生成提交说明建议" @click="aiAssist('commit_message')">
          <AppIcon name="sparkles" :size="12" :stroke-width="2" /> AI 提交说明
        </button>
      </div>

      <!-- 历史视图 -->
      <GitHistory v-if="viewMode === 'history' && store.snapshot" />

      <template v-else-if="store.snapshot && viewMode === 'status'">
      <!-- 冲突组 -->
      <div v-if="store.snapshot!.conflicts.length" class="git-group git-group-conflict">
        <div class="git-group-title conflict-title">冲突 <span class="git-count red">{{ store.snapshot!.conflicts.length }}</span></div>
        <div
          v-for="c in store.snapshot!.conflicts"
          :key="c.path"
          class="git-file-row conflict-row"
          data-test="git-conflict-row"
          title="点击打开冲突解决器"
          @click="store.conflictResolverPath = c.path"
        >
          <span class="git-file-status st-conflict">!</span>
          <span class="git-file-path" :title="c.path">{{ c.path }}</span>
          <span class="git-file-kind">{{ c.kind }}</span>
        </div>
      </div>

      <!-- 已暂存组 -->
      <div v-if="store.snapshot!.staged.length" class="git-group" data-test="git-group-staged">
        <div class="git-group-title">已暂存 <span class="git-count">{{ store.snapshot!.staged.length }}</span></div>
        <div v-for="f in store.snapshot!.staged" :key="'s-' + f.path" class="git-file-row" data-test="git-staged-row">
          <span class="git-file-status" :class="`st-${statusKind(f.index_status)}`">{{ f.index_status }}</span>
          <span class="git-file-path" :title="f.orig_path ? f.orig_path + ' → ' + f.path : f.path" @click="openDiff(f.path, true)">{{ f.path }}</span>
          <button class="git-act" data-test="git-unstage-btn" title="取消暂存" @click="store.unstage(f.path)">−</button>
        </div>
      </div>

      <!-- 变更组（未暂存） -->
      <div v-if="store.snapshot!.unstaged.length" class="git-group" data-test="git-group-changes">
        <div class="git-group-title">
          变更 <span class="git-count">{{ store.snapshot!.unstaged.length }}</span>
          <span class="git-flex"></span>
          <button class="git-link" data-test="git-stage-all-btn" @click="store.stageAll()">全部暂存</button>
        </div>
        <div v-for="f in store.snapshot!.unstaged" :key="'w-' + f.path" class="git-file-row" data-test="git-change-row">
          <span class="git-file-status" :class="`st-${statusKind(f.worktree_status)}`">{{ f.worktree_status }}</span>
          <span class="git-file-path" @click="openDiff(f.path, false)">{{ f.path }}</span>
          <button class="git-act" data-test="git-stage-btn" title="暂存" @click="store.stage(f.path)">+</button>
        </div>
      </div>

      <!-- 未跟踪组 -->
      <div v-if="store.snapshot!.untracked.length" class="git-group" data-test="git-group-untracked">
        <div class="git-group-title">未跟踪 <span class="git-count">{{ store.snapshot!.untracked.length }}</span></div>
        <div v-for="p in store.snapshot!.untracked" :key="'u-' + p" class="git-file-row" data-test="git-untracked-row">
          <span class="git-file-status st-other">?</span>
          <span class="git-file-path">{{ p }}</span>
          <button class="git-act" data-test="git-stage-untracked-btn" @click="store.stage(p)">+</button>
        </div>
      </div>

      <!-- 三方冲突解决器 -->
      <GitConflictResolver v-if="store.conflictResolverPath" :path="store.conflictResolverPath" />

      <!-- Diff 审查弹窗：点击变更文件后以大窗口展示改动，替代面板底部内联视图。
           Teleport 到 body：右面板容器有层叠上下文，fixed 弹窗会被聊天区盖住 -->
      <Teleport to="body">
        <div v-if="diffModal" class="git-diff-mask" data-test="git-diff-modal" @click.self="closeDiffModal">
          <div class="git-diff-modal">
            <div class="git-diff-modal-head">
              <span class="git-diff-modal-kind">{{ diffModal.staged ? '已暂存' : '工作区变更' }}</span>
              <span class="git-diff-modal-path" :title="diffModal.path">{{ diffModal.path }}</span>
              <button class="git-diff-close" data-test="git-diff-modal-close" title="关闭 (Esc)" @click="closeDiffModal">
                <AppIcon name="x" :size="16" :stroke-width="2" />
              </button>
            </div>
            <div class="git-diff-modal-body">
              <div v-if="store.diffLoading" class="git-diff-note">加载 Diff 中…</div>
              <GitDiffView v-else-if="store.diff" :diff="store.diff" />
              <div v-else class="git-diff-note">无法加载该文件的 Diff</div>
            </div>
          </div>
        </div>
      </Teleport>
      </template>

      <!-- 三方冲突解决器 -->
      <GitConflictResolver v-if="store.conflictResolverPath" :path="store.conflictResolverPath" />

      <!-- 高风险确认对话框（§13 两段式：服务端 preview + 一次性令牌）。
           同样 Teleport 到 body，避免被聊天区层叠盖住 -->
      <Teleport to="body">
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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useGitStore } from '../stores/gitStore';
import { useAppStore } from '../stores/appStore';
import GitDiffView from './GitDiffView.vue';
import GitHistory from './GitHistory.vue';
import GitConflictResolver from './GitConflictResolver.vue';
import AppIcon from './AppIcon.vue';

const store = useGitStore();
const appStore = useAppStore();
const viewMode = ref<'status' | 'history'>('status');
// Diff 审查弹窗：null = 关闭；记录当前查看的文件与暂存侧（供标题展示）
const diffModal = ref<{ path: string; staged: boolean } | null>(null);

function closeDiffModal() {
  diffModal.value = null;
  store.diff = null;
}

function onDiffModalKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && diffModal.value) closeDiffModal();
}

async function aiAssist(kind: string) {
  // §5.15：先展示用途，用户确认后发送可见模板消息（不注入隐藏提示）
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

// 文件状态字母 → 语义色组（A 新增绿 / D 删除红 / M·R·C 修改橙 / 其余灰）
function statusKind(letter: string): string {
  if (letter === 'A') return 'added';
  if (letter === 'D') return 'deleted';
  if (letter === 'M' || letter === 'R' || letter === 'C') return 'modified';
  return 'other';
}

function openDiff(path: string, staged: boolean) {
  diffModal.value = { path, staged };
  void store.loadDiff(path, staged);
}

// 移除旧的重复 watch（保留会话切换联动）

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

// 弹窗打开期间监听 Esc 关闭；关闭与卸载时摘除监听
watch(diffModal, (open) => {
  if (open) window.addEventListener('keydown', onDiffModalKeydown);
  else window.removeEventListener('keydown', onDiffModalKeydown);
});
onBeforeUnmount(() => window.removeEventListener('keydown', onDiffModalKeydown));

// 会话切换 → 跟随工作区重新加载（§17.3 会话切换不串仓库状态）
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
  padding: 10px;
  gap: 8px;
  font-size: 12.5px;
}

/* ---------- 空态 ---------- */
.git-note {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--text-faint, #888);
  padding: 48px 12px;
  text-align: center;
}
.git-note-icon {
  opacity: 0.45;
}

/* ---------- 通用小件 ---------- */
.git-flex { flex: 1; }
.git-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--text-dim, #888);
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.git-icon-btn:hover {
  background: var(--bg3);
  color: var(--text);
}
.git-pill {
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  border-radius: 999px;
  cursor: pointer;
  padding: 3px 11px;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: background 0.12s, border-color 0.12s, color 0.12s, opacity 0.12s;
}
.git-pill:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.git-pill:disabled { opacity: 0.45; cursor: default; }
.git-pill.ghost { background: transparent; }
.git-pill.ghost:hover:not(:disabled) { background: var(--bg3); }
.git-pill.git-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
  padding: 3px 16px;
}
.git-pill.git-primary:hover:not(:disabled) { color: #fff; filter: brightness(1.08); }
.git-pill.git-danger {
  background: var(--red);
  border-color: var(--red);
  color: #fff;
  font-weight: 600;
}
.git-pill.git-danger:hover:not(:disabled) { color: #fff; }
.git-pill.ghost-amber {
  background: transparent;
  border-color: color-mix(in srgb, var(--orange) 45%, var(--border));
  color: var(--orange);
}
.git-pill.ghost-amber:hover:not(:disabled) { background: color-mix(in srgb, var(--orange) 12%, transparent); }
.git-pill.solid-amber {
  background: color-mix(in srgb, var(--orange) 16%, transparent);
  border-color: var(--orange);
  color: var(--orange);
  font-weight: 600;
}
.git-pill.ghost-red {
  background: transparent;
  border-color: color-mix(in srgb, var(--red) 45%, var(--border));
  color: var(--red);
}
.git-link {
  border: none;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}
.git-link:hover { text-decoration: underline; }

/* ---------- 仓库概览卡 ---------- */
.git-repo-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.git-head { display: flex; align-items: center; gap: 8px; }
.git-branch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 700;
  font-size: 13.5px;
}
.git-branch-icon { color: var(--accent); }
.git-dirty-mark { color: var(--orange); }
.git-sync {
  font-size: 11px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: 999px;
  padding: 1px 7px;
  font-variant-numeric: tabular-nums;
}
.git-op {
  font-size: 11px;
  color: var(--orange);
  border: 1px solid color-mix(in srgb, var(--orange) 45%, var(--border));
  border-radius: 999px;
  padding: 1px 7px;
}
.git-upstream {
  color: var(--text-faint, #888);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 提交卡 ---------- */
.git-commit-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px;
}
.git-commit-title, .git-commit-body {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg3);
  border: 1px solid transparent;
  border-radius: 7px;
  color: inherit;
  padding: 5px 8px;
  font-size: 12.5px;
  transition: border-color 0.15s, background 0.15s;
}
.git-commit-title::placeholder, .git-commit-body::placeholder { color: var(--text-faint, #888); }
.git-commit-title:focus, .git-commit-body:focus {
  outline: none;
  background: var(--bg1);
  border-color: var(--accent);
}
.git-commit-body { resize: vertical; min-height: 44px; font-family: inherit; line-height: 1.5; }
.git-commit-actions { display: flex; gap: 6px; align-items: center; }

/* ---------- 工具行：分段切换 + AI ---------- */
.git-toolbar { display: flex; gap: 6px; align-items: center; }
.git-view-switch {
  display: inline-flex;
  gap: 2px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px;
}
.git-view-switch button {
  background: transparent;
  border: none;
  color: var(--text-dim, #888);
  border-radius: 999px;
  cursor: pointer;
  padding: 3px 14px;
  font-size: 12px;
  transition: background 0.12s, color 0.12s;
}
.git-view-switch button.active {
  background: var(--accent);
  color: #fff;
  font-weight: 600;
}
.git-pill.sparkle { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: transparent; }
.git-pill.sparkle:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 10%, transparent); }

/* ---------- 文件分组 ---------- */
.git-group {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.git-group-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim, #888);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 4px 4px 2px;
}
.git-count {
  background: var(--bg3);
  color: var(--text-dim, #888);
  border-radius: 999px;
  padding: 0 7px;
  font-size: 10.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.git-count.red { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }
.git-group-title.conflict-title { color: var(--red); }
.git-file-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 6px;
  border-radius: 6px;
  transition: background 0.1s;
}
.git-file-row:hover { background: var(--bg2); }
.git-file-status {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 10.5px;
  font-weight: 700;
}
.git-file-status.st-added { background: color-mix(in srgb, var(--green) 18%, transparent); color: var(--green); }
.git-file-status.st-deleted { background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }
.git-file-status.st-modified { background: color-mix(in srgb, var(--orange) 18%, transparent); color: var(--orange); }
.git-file-status.st-conflict { background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }
.git-file-status.st-other { background: var(--bg3); color: var(--text-dim, #888); }
.git-file-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  direction: rtl;
  text-align: left;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
}
.git-file-row:hover .git-file-path { color: var(--accent); }
.git-file-kind {
  flex-shrink: 0;
  color: var(--text-faint, #888);
  font-size: 10.5px;
}
.conflict-row { cursor: pointer; }
.conflict-row .git-file-path { color: var(--red); }
/* 行尾暂存/取消暂存小圆钮：hover 才显形，列表视觉保持干净 */
.git-act {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim, #888);
  border-radius: 50%;
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 0;
  opacity: 0;
  transition: opacity 0.1s, color 0.1s, border-color 0.1s;
}
.git-file-row:hover .git-act { opacity: 1; }
.git-act:hover { color: var(--accent); border-color: var(--accent); }

/* ---------- 信任 / 错误横幅 ---------- */
.git-trust {
  border: 1px solid color-mix(in srgb, var(--orange) 40%, var(--border));
  background: color-mix(in srgb, var(--orange) 8%, transparent);
  border-radius: 10px;
  padding: 8px 10px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.git-trust-text { display: flex; gap: 7px; align-items: flex-start; line-height: 1.5; }
.git-trust-icon { flex-shrink: 0; margin-top: 1px; color: var(--orange); }
.git-trust-actions { display: flex; gap: 6px; }
.git-error {
  border: 1px solid color-mix(in srgb, var(--red) 40%, var(--border));
  background: color-mix(in srgb, var(--red) 8%, transparent);
  border-radius: 10px;
  padding: 8px 10px;
  font-size: 12px;
  color: var(--red);
  display: flex;
  gap: 8px;
  align-items: center;
}
.git-error-text { flex: 1; line-height: 1.5; }

/* ---------- 高风险确认弹窗 ---------- */
.git-confirm-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  /* 已 Teleport 到 body：压过应用层全部面板/抽屉（最高 3000），低于顶层引导 5000 */
  z-index: 4000;
}
.git-confirm {
  background: var(--bg2);
  border: 1px solid color-mix(in srgb, var(--red) 55%, var(--border));
  border-radius: 14px;
  padding: 16px 18px;
  width: min(420px, 90vw);
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
}
.git-confirm-title { font-weight: 700; color: var(--red); font-size: 14px; }
.git-confirm-text { font-size: 13px; line-height: 1.6; }
.git-confirm-risk { font-size: 12px; color: var(--text-dim, #888); }
.git-confirm-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 2px; }

/* ---------- Diff 审查弹窗：大窗口展示改动，点击遮罩/Esc 关闭 ---------- */
.git-diff-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  /* 已 Teleport 到 body：压过应用层全部面板/抽屉（最高 3000），低于顶层引导 5000 */
  z-index: 4000;
}
.git-diff-modal {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 14px;
  width: min(1000px, 94vw);
  height: min(86vh, 920px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.55);
}
.git-diff-modal-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.git-diff-modal-kind {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
  border-radius: 999px;
  padding: 2px 9px;
}
.git-diff-modal-path {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  direction: rtl;
  text-align: left;
}
.git-diff-close {
  margin-left: auto;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--text-dim, #888);
  border-radius: 6px;
  cursor: pointer;
}
.git-diff-close:hover { background: var(--bg3); color: var(--red); }
.git-diff-modal-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.git-diff-note { color: var(--text-faint, #888); padding: 20px; text-align: center; }
</style>
