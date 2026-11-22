import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GitPanel from './GitPanel.vue';
import { useGitStore } from '../stores/gitStore';
import type { GitSnapshotDto } from '../types/pywebview.d';

function fakeSnapshot(overrides: Partial<GitSnapshotDto> = {}): GitSnapshotDto {
  return {
    repository: { worktree_root: 'E:/proj', common_git_dir: 'E:/proj/.git', git_version: '2.51.2', is_linked_worktree: false },
    head_oid: 'abc1234',
    branch_name: 'dev',
    head_state: 'normal',
    upstream: null,
    ahead: 0,
    behind: 0,
    staged: [{ path: 'staged.py', index_status: 'A', worktree_status: '.', orig_path: null, is_binary: false }],
    unstaged: [{ path: 'changed.py', index_status: '.', worktree_status: 'M', orig_path: null, is_binary: false }],
    untracked: ['new.py'],
    ignored_count: 0,
    conflicts: [{ path: 'conflict.py', kind: 'both_modified' }],
    stash_count: 0,
    operation_kind: 'idle',
    operation_phase: 'none',
    identity_state: 'ok',
    worktree_version: 'w-hash',
    refs_version: 'r-hash',
    ...overrides,
  };
}

async function mountWithSnapshot(overrides: Partial<GitSnapshotDto> = {}) {
  const snap = fakeSnapshot(overrides);
  const store = useGitStore();
  (store as unknown as { snapshot: GitSnapshotDto }).snapshot = snap;
  store.workdir = 'E:/proj';
  store.trustLevel = 'trusted';
  const wrapper = mount(GitPanel, {
    global: { stubs: { teleport: true } },
  });
  await wrapper.vm.$nextTick();
  return { wrapper, store, snap };
}

describe('GitPanel 源代码管理面板', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    (window as unknown as { pywebview?: unknown }).pywebview = undefined;
  });

  it('非仓库错误显示"不是 Git 仓库"提示，不显示伪造状态', async () => {
    const store = useGitStore();
    (store as unknown as { error: unknown }).error = {
      code: 'not_a_repository', message: '目录不是 Git 仓库', retryable: false, command: '',
    };
    const wrapper = mount(GitPanel);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="git-empty"]').text()).toContain('不是 Git 仓库');
    expect(wrapper.find('[data-test="git-branch-label"]').exists()).toBe(false);
  });

  it('分组渲染：冲突/已暂存/变更/未跟踪四组各自可见', async () => {
    const { wrapper } = await mountWithSnapshot();
    expect(wrapper.find('[data-test="git-group-staged"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="git-group-changes"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="git-group-untracked"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="git-conflict-row"]').text()).toContain('both_modified');
    expect(wrapper.find('[data-test="git-branch-label"]').text()).toContain('dev');
  });

  it('暂存区为空时提交按钮禁用（禁用原因可见）', async () => {
    const snap = fakeSnapshot();
    snap.staged = [];
    const { wrapper } = await mountWithSnapshot(snap);
    const btn = wrapper.get('[data-test="git-commit-btn"]');
    expect((btn.element as HTMLButtonElement).disabled).toBe(true);
  });

  it('点击文件路径请求对应 Diff（工作区/暂存区区分）', async () => {
    const store = useGitStore();
    const loadDiff = vi.spyOn(store as unknown as { loadDiff: (p: string, s: boolean) => Promise<void> }, 'loadDiff').mockResolvedValue(undefined);
    const { wrapper } = await mountWithSnapshot();
    const rows = wrapper.findAll('[data-test="git-change-row"] .git-file-path');
    await rows[0].trigger('click');
    expect(loadDiff).toHaveBeenCalledWith('changed.py', false);
    const stagedRows = wrapper.findAll('[data-test="git-staged-row"] .git-file-path');
    await stagedRows[0].trigger('click');
    expect(loadDiff).toHaveBeenCalledWith('staged.py', true);
  });

  it('高风险确认对话框渲染并提交令牌', async () => {
    const store = useGitStore();
    await mountWithSnapshot();
    store.pendingConfirm = {
      operation: 'reset_hard',
      params: { target: 'HEAD~1' },
      preview: {
        operation: 'reset_hard', risk_level: 'unrecoverable', confirm_required: true,
        typed_confirm_required: true, confirm_text: '将丢失提交', versions: { worktree_version: 'w', refs_version: 'r' }, token: 'tok-9',
      },
      typedInput: '',
    };
    mount(GitPanel);
    await nextTick();
    const modal = document.body.querySelector('[data-test="git-confirm-modal"]');
    expect(modal).not.toBeNull();
    const input = modal!.querySelector('[data-test="git-confirm-input"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    const okBtn = modal!.querySelector('[data-test="git-confirm-ok"]') as HTMLButtonElement;
    // 未输入确认文本时禁用（不可恢复操作需输入分支名/短 Hash）
    expect(okBtn.disabled).toBe(true);
    input.value = 'HEAD~1';
    input.dispatchEvent(new Event('input'));
    await Promise.resolve();
    expect(okBtn.disabled).toBe(false);
    const execSpy = vi.spyOn(store, 'executeConfirmed').mockResolvedValue(undefined);
    okBtn.click();
    await Promise.resolve();
    expect(execSpy).toHaveBeenCalledWith('reset_hard', { target: 'HEAD~1' }, 'tok-9');
    modal!.remove();
  });

  it('文件列表分层展示文件名与目录，并触发撤销更改与分支切换', async () => {
    const store = useGitStore();
    const loadBranches = vi.spyOn(store, 'loadBranches').mockImplementation(async () => {
      store.branches = [
        { name: 'dev', is_remote: false, is_current: true, upstream: 'origin/dev', ahead: 0, behind: 0, upstream_gone: false, oid: '111' },
        { name: 'main', is_remote: false, is_current: false, upstream: 'origin/main', ahead: 0, behind: 0, upstream_gone: false, oid: '222' },
      ];
    });
    const switchBranch = vi.spyOn(store, 'switchBranch').mockResolvedValue(undefined);
    const discard = vi.spyOn(store, 'discard').mockResolvedValue(undefined);

    const { wrapper } = await mountWithSnapshot({
      unstaged: [{ path: 'frontend/src/App.vue', index_status: '.', worktree_status: 'M', orig_path: null, is_binary: false }],
    });

    const changeRow = wrapper.get('[data-test="git-change-row"]');
    expect(changeRow.get('.git-file-name').text()).toBe('App.vue');
    expect(changeRow.get('.git-file-dir').text()).toBe('frontend/src/');

    await changeRow.get('[data-test="git-discard-btn"]').trigger('click');
    expect(discard).toHaveBeenCalledWith('frontend/src/App.vue');

    await wrapper.get('[data-test="git-branch-label"]').trigger('click');
    await nextTick();
    expect(loadBranches).toHaveBeenCalledOnce();
    const branchItems = wrapper.findAll('.git-branch-item');
    expect(branchItems).toHaveLength(2);
    await branchItems[1].trigger('click');
    expect(switchBranch).toHaveBeenCalledWith('main');
  });

  it('干净工作区显示空态卡；历史视图点击提交打开 Diff 弹窗', async () => {
    const { wrapper, store } = await mountWithSnapshot({
      staged: [],
      unstaged: [],
      untracked: [],
      conflicts: [],
    });
    expect(wrapper.find('.git-clean-card').exists()).toBe(true);

    store.history = [
      {
        oid: 'abc123456789',
        short: 'abc1234',
        parents: ['p000111'],
        author_name: 'Tester',
        author_email: 't@example.com',
        author_time: 1_700_000_000,
        committer_time: 1_700_000_000,
        summary: 'feat: polish git ui',
        refs: ['HEAD -> dev'],
      },
    ];
    const loadCommitDiff = vi.spyOn(store, 'loadCommitDiff').mockResolvedValue(undefined);

    await wrapper.get('[data-test="git-view-history"]').trigger('click');
    await nextTick();
    expect(wrapper.find('[data-test="git-history"]').exists()).toBe(true);

    await wrapper.get('[data-test="git-commit-row-0"]').trigger('click');
    await nextTick();
    expect(loadCommitDiff).toHaveBeenCalledWith('p000111', 'abc123456789');
    expect(wrapper.find('[data-test="git-diff-modal"]').exists()).toBe(true);
  });
});
