import { defineStore } from 'pinia';
import type {
  GitBranchDto,
  GitCommitDto,
  GitDiffDto,
  GitErrorDto,
  GitRemoteDto,
  GitRiskPreviewDto,
  GitSnapshotDto,
  GitStashDto,
  GitTagDto,
} from '../types/pywebview.d';

interface GitConfirmState {
  operation: string;
  params: Record<string, unknown>;
  preview: GitRiskPreviewDto;
  typedInput: string;
}

function normalizeWorkspaceKey(path: string): string {
  return path.replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
}

function isSameWorkspace(a: string, b: string): boolean {
  return normalizeWorkspaceKey(a) === normalizeWorkspaceKey(b);
}

function callApi<T>(name: string, ...args: unknown[]): Promise<T> {
  const api = window.pywebview && window.pywebview.api;
  if (!api) {
    return Promise.resolve({ ok: false, error: { code: 'not_connected', message: '后端未连接', retryable: true, command: '' } } as unknown as T);
  }
  const fn = (api as unknown as Record<string, (...a: unknown[]) => Promise<T>>)[name];
  if (typeof fn !== 'function') {
    return Promise.resolve({ ok: false, error: { code: 'git_command_failed', message: `后端缺少方法 ${name}`, retryable: false, command: '' } } as unknown as T);
  }
  return fn.apply(api, args);
}

// 快照请求串号：git_get_status 在大仓库要数秒，期间用户切会话/换工作区时
// workdir 已被 $reset 或覆盖，若旧工作区的在途响应照常落地，会把上一仓库的
// 快照串台到当前会话（表现为公共对话显示上一个项目的分支角标）。
// 串号放模块级而非 state：$reset 会把计数归零，与在途旧请求撞号；
// 且必须跨 reset 单调，旧请求的收尾分支才能可靠识别自己已过期。
let refreshSerial = 0;

export const useGitStore = defineStore('git', {
  state: () => ({
    workdir: '' as string,
    snapshot: null as GitSnapshotDto | null,
    loading: false,
    error: null as GitErrorDto | null,
    trustLevel: 'untrusted' as 'trusted' | 'allowed_once' | 'untrusted',
    branches: [] as GitBranchDto[],
    tags: [] as GitTagDto[],
    remotes: [] as GitRemoteDto[],
    stashes: [] as GitStashDto[],
    history: [] as GitCommitDto[],
    historyHasMore: false,
    commitTitle: '',
    commitBody: '',
    selectedPath: '',
    selectedStaged: false,
    diff: null as GitDiffDto | null,
    diffLoading: false,
    pendingConfirm: null as GitConfirmState | null,
    panelOpenNonce: 0,
    conflictResolverPath: '',
    _refreshInFlight: false,
    _refreshQueued: false,
  }),

  getters: {
    branchLabel(state): string {
      const s = state.snapshot;
      if (!s) return '';
      if (s.head_state === 'detached') return `HEAD@${(s.head_oid || '').slice(0, 7)}`;
      return s.branch_name || (s.head_state === 'unborn' ? '空分支' : '');
    },
    syncLabel(state): string {
      const s = state.snapshot;
      if (!s) return '';
      const parts: string[] = [];
      if (s.ahead > 0) parts.push(`↑${s.ahead}`);
      if (s.behind > 0) parts.push(`↓${s.behind}`);
      return parts.join(' ');
    },
    dirtyLabel(state): string {
      const s = state.snapshot;
      if (!s) return '';
      const n = s.staged.length + s.unstaged.length + s.untracked.length;
      if (s.conflicts.length > 0) return `${s.conflicts.length} conflicts`;
      return n > 0 ? `${n} changes` : 'clean';
    },
    isDirty(state): boolean {
      const s = state.snapshot;
      if (!s) return false;
      return s.staged.length + s.unstaged.length + s.untracked.length > 0;
    },
    statusbarText(): string {
      if (!this.snapshot) return '';
      const dirtyMark = this.isDirty ? '*' : '';
      const sync = this.syncLabel ? ` ${this.syncLabel}` : '';
      const op = this.snapshot.operation_kind !== 'idle'
      && this.snapshot.operation_kind !== 'unsupported_external'
        ? ` ${this.snapshot.operation_kind.toUpperCase()}`
        : '';
      return `⎇ ${this.branchLabel}${dirtyMark}${sync}${op}`;
    },
  },

  actions: {
    _setWorkdir(workdir: string) {
      if (this.workdir !== workdir) {
        this.workdir = workdir;
        this.$reset();
        this.workdir = workdir;
      }
    },

    async ensureLoaded(workdir: string) {
      this._setWorkdir(workdir);
      if (!workdir) return;
      await this.refresh();
    },

    async refresh() {
      const workdir = this.workdir;
      if (!workdir) return;
      if (this._refreshInFlight) {
        this._refreshQueued = true;
        return;
      }
      this._refreshInFlight = true;
      const serial = ++refreshSerial;
      this.loading = true;
      try {
        const res = await callApi<any>('git_get_status', workdir);
        if (serial !== refreshSerial || this.workdir !== workdir) return;
        this._applyResult(res);
      } finally {
        if (serial === refreshSerial) {
          this._refreshInFlight = false;
          this.loading = false;
          if (this._refreshQueued) {
            this._refreshQueued = false;
            void this.refresh();
          }
        }
      }
    },

    _applyResult(res: any) {
      if (res.ok) {
        this.snapshot = res.snapshot ?? this.snapshot;
        this.error = null;
      } else {
        this.error = res.error ?? null;
      }
    },

    async loadTrust() {
      const workdir = this.workdir;
      const res = await callApi<any>('git_get_trust', workdir);
      if (res.ok && this.workdir === workdir) this.trustLevel = res.level;
    },

    async trustRepository() {
      await callApi('git_trust_repository', this.workdir);
      await this.loadTrust();
      await this.refresh();
    },

    async allowOnce() {
      await callApi('git_allow_once', this.workdir);
      await this.loadTrust();
      await this.refresh();
    },

    async loadBranches() {
      const res = await callApi<any>('git_list_branches', this.workdir);
      if (res.ok) this.branches = res.branches;
    },

    async switchBranch(target: string) {
      const res = await callApi<any>('git_switch_branch', this.workdir, target);
      this._applyResult(res);
      if (res.ok) await Promise.all([this.loadBranches(), this.refresh()]);
    },

    async createBranch(name: string, switchAfter: boolean) {
      const res = await callApi<any>('git_create_branch', this.workdir, name, '', switchAfter);
      this._applyResult(res);
      if (res.ok) await this.loadBranches();
    },

    async stage(path: string) {
      const res = await callApi<any>('git_stage', this.workdir, [path]);
      this._applyResult(res);
    },

    async stageAll() {
      const res = await callApi<any>('git_stage_all', this.workdir);
      this._applyResult(res);
    },

    async unstage(path: string) {
      const res = await callApi<any>('git_unstage', this.workdir, [path]);
      this._applyResult(res);
    },

    async unstageAll() {
      const paths = this.snapshot?.staged.map((f) => f.path) ?? [];
      if (paths.length === 0) return;
      const res = await callApi<any>('git_unstage', this.workdir, paths);
      this._applyResult(res);
    },

    async discard(path: string) {
      const res = await callApi<any>('git_preview_risk', this.workdir, 'discard_changes', { paths: [path] });
      this._applyResult(res);
      if (res.ok) this._armConfirm('discard_changes', { paths: [path] }, res.preview);
    },

    async commit() {
      const res = await callApi<any>('git_commit', this.workdir, this.commitTitle, this.commitBody, false);
      this._applyResult(res);
      if (res.ok) {
        this.commitTitle = '';
        this.commitBody = '';
      }
    },

    async stash(includeUntracked: boolean) {
      const res = await callApi<any>('git_stash_create', this.workdir, this.commitTitle || 'stash', includeUntracked);
      this._applyResult(res);
      if (res.ok) {
        this.commitTitle = '';
        this.commitBody = '';
      }
    },

    async fetch() {
      const res = await callApi<any>('git_fetch', this.workdir, '', false);
      this._applyResult(res);
    },

    async pull() {
      const res = await callApi<any>('git_pull', this.workdir);
      this._applyResult(res);
    },

    async push() {
      const res = await callApi<any>('git_push', this.workdir, 'origin', '', false, '');
      this._applyResult(res);
    },

    async loadHistory(limit = 50) {
      const res = await callApi<any>('git_load_history', this.workdir, 0, limit);
      this._applyResult(res);
      if (res.ok) {
        this.history = res.commits;
        this.historyHasMore = !!res.has_more;
      }
    },

    async loadDiff(path: string, staged: boolean) {
      this.selectedPath = path;
      this.selectedStaged = staged;
      this.diffLoading = true;
      try {
        const res = await callApi<any>('git_load_diff', this.workdir, path, staged, '', '');
        this._applyResult(res);
        this.diff = res.ok ? res.diff : null;
      } finally {
        this.diffLoading = false;
      }
    },

    async loadCommitDiff(left: string, right: string) {
      this.selectedPath = `commit:${right}`;
      this.selectedStaged = false;
      this.diffLoading = true;
      try {
        const res = await callApi<any>('git_load_diff', this.workdir, '', false, left, right);
        this._applyResult(res);
        this.diff = res.ok ? res.diff : null;
      } finally {
        this.diffLoading = false;
      }
    },

    async previewConfirm(operation: string, params: Record<string, unknown>) {
      const res = await callApi<any>('git_preview_risk', this.workdir, operation, params);
      this._applyResult(res);
      if (res.ok) this._armConfirm(operation, params, res.preview);
    },

    _armConfirm(operation: string, params: Record<string, unknown>, preview: GitRiskPreviewDto) {
      if (!preview.confirm_required) {
        void this.executeConfirmed(operation, params, '');
        return;
      }
      this.pendingConfirm = { operation, params, preview, typedInput: '' };
    },

    async executeConfirmed(operation: string, params: Record<string, unknown>, token: string) {
      const res = await callApi<any>('git_confirm_operation', this.workdir, operation, params, token);
      this._applyResult(res);
      this.pendingConfirm = null;
      await this.refresh();
    },

    cancelConfirm() {
      this.pendingConfirm = null;
    },

    onExternalChange(data: { workspace?: string }) {
      const ws = data && data.workspace;
      if (!ws || isSameWorkspace(ws, this.workdir)) {
        void this.refresh();
      }
    },

    requestOpenPanel() {
      this.panelOpenNonce += 1;
    },

    async aiAssist(kind: string) {
      const res = await callApi<any>('git_ai_assist', this.workdir, kind);
      this._applyResult(res);
      return res;
    },
  },
});
