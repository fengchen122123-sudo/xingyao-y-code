// gitStore 单元测试（规格 §17.3 前端测试选集：状态分组 / 外部事件刷新 / 确认流程）
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useGitStore } from './gitStore';
import type { GitSnapshotDto } from '../types/pywebview.d';

function fakeSnapshot(overrides: Partial<GitSnapshotDto> = {}): GitSnapshotDto {
  return {
    repository: { worktree_root: 'E:/proj', common_git_dir: 'E:/proj/.git', git_version: '2.51.2', is_linked_worktree: false },
    head_oid: 'abc1234',
    branch_name: 'dev',
    head_state: 'normal',
    upstream: 'origin/dev',
    ahead: 2,
    behind: 1,
    staged: [{ path: 'a.py', index_status: 'M', worktree_status: '.', orig_path: null, is_binary: false }],
    unstaged: [{ path: 'b.py', index_status: '.', worktree_status: 'M', orig_path: null, is_binary: false }],
    untracked: ['c.py'],
    ignored_count: 0,
    conflicts: [],
    stash_count: 0,
    operation_kind: 'idle',
    operation_phase: 'none',
    identity_state: 'ok',
    worktree_version: 'w-hash',
    refs_version: 'r-hash',
    ...overrides,
  };
}

function mockApi(methods: Record<string, unknown>) {
  (window as unknown as { pywebview: unknown }).pywebview = {
    api: methods as Record<string, (...a: unknown[]) => Promise<unknown>>,
  };
}

describe('gitStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it('ensureLoaded 拉取快照并派生状态栏文本（分支*↑↓）', async () => {
    const get_status = vi.fn().mockResolvedValue({ ok: true, snapshot: fakeSnapshot() });
    const get_trust = vi.fn().mockResolvedValue({ ok: true, level: 'trusted' });
    mockApi({ git_get_status: get_status, git_get_trust: get_trust });

    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    expect(get_status).toHaveBeenCalledWith('E:/proj');
    expect(store.branchLabel).toBe('dev');
    expect(store.syncLabel).toBe('↑2 ↓1');
    expect(store.isDirty).toBe(true);
    expect(store.statusbarText).toBe('⎇ dev* ↑2 ↓1');
  });

  it('clean 工作区状态栏无脏标记', async () => {
    const snap = fakeSnapshot();
    snap.staged = [];
    snap.unstaged = [];
    snap.untracked = [];
    snap.ahead = 0;
    snap.behind = 0;
    mockApi({
      git_get_status: vi.fn().mockResolvedValue({ ok: true, snapshot: snap }),
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    expect(store.statusbarText).toBe('⎇ dev');
  });

  it('detached HEAD 分支标签使用短哈希', async () => {
    const snap = fakeSnapshot({ head_state: 'detached', branch_name: null });
    snap.staged = []; snap.unstaged = []; snap.untracked = []; snap.ahead = 0; snap.behind = 0;
    mockApi({
      git_get_status: vi.fn().mockResolvedValue({ ok: true, snapshot: snap }),
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    expect(store.branchLabel).toBe('HEAD@abc1234');
  });

  it('错误结果落入 error 并在界面数据上可见', async () => {
    mockApi({
      git_get_status: vi.fn().mockResolvedValue({
        ok: false,
        error: { code: 'not_a_repository', message: '目录不是 Git 仓库', retryable: false, command: '' },
      }),
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    expect(store.error?.code).toBe('not_a_repository');
    expect(store.snapshot).toBeNull();
  });

  it('git_state_changed 外部事件触发当前工作区重读（§10）', async () => {
    const get_status = vi.fn().mockResolvedValue({ ok: true, snapshot: fakeSnapshot() });
    mockApi({
      git_get_status: get_status,
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    expect(get_status).toHaveBeenCalledTimes(1);
    store.onExternalChange({ workspace: 'E:/proj' });
    await new Promise((r) => setTimeout(r, 0));
    expect(get_status).toHaveBeenCalledTimes(2);
    // 其他 Worktree 的事件不触发本工作区刷新
    store.onExternalChange({ workspace: 'E:/other' });
    await new Promise((r) => setTimeout(r, 0));
    expect(get_status).toHaveBeenCalledTimes(2);
    // 后端事件里的 workspace 经 normcase 归一（Windows：小写 + 反斜杠），
    // 前端 workdir 保留磁盘真实大小写；两者必须视为同一工作区，否则外部变化永不刷新
    store.onExternalChange({ workspace: 'e:\\proj' });
    await new Promise((r) => setTimeout(r, 0));
    expect(get_status).toHaveBeenCalledTimes(3);
  });

  it('切会话后在途的旧工作区快照响应被丢弃，不串台分支角标', async () => {
    // 复现路径：项目会话（大仓库）快照请求在途 → 切到公共对话（workdir 清空
    // 并 $reset）→ 旧响应迟到落地。无串号防护时旧仓库快照会重新填入 snapshot，
    // 公共对话持续显示上一个项目的分支（git_get_status 实测秒级，窗口很大）
    let resolveStale!: () => void;
    const staleGate = new Promise<void>((resolve) => { resolveStale = resolve; });
    const get_status = vi.fn()
      .mockImplementationOnce(() => staleGate)
      .mockImplementationOnce(() => Promise.resolve({
        ok: true,
        snapshot: fakeSnapshot({
          branch_name: 'newproj',
          repository: { worktree_root: 'D:/new', common_git_dir: 'D:/new/.git', git_version: '2.51.2', is_linked_worktree: false },
        }),
      }));
    mockApi({
      git_get_status: get_status,
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
    });
    const store = useGitStore();
    const stale = store.ensureLoaded('E:/proj');
    await store.ensureLoaded('D:/new');
    expect(store.branchLabel).toBe('newproj');
    // 旧工作区响应此刻才返回：必须被丢弃，快照保持新工作区数据
    resolveStale();
    await stale;
    expect(store.workdir).toBe('D:/new');
    expect(store.branchLabel).toBe('newproj');
  });

  it('切到公共对话（workdir 清空）后迟到的快照响应不落地', async () => {
    let resolveStale!: () => void;
    const staleGate = new Promise<void>((resolve) => { resolveStale = resolve; });
    const get_status = vi.fn().mockImplementationOnce(() => staleGate);
    mockApi({
      git_get_status: get_status,
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
    });
    const store = useGitStore();
    const stale = store.ensureLoaded('E:/proj');
    // 模拟 App.vue 切到 GLOBAL 会话：workdir 置空 + $reset
    store.workdir = '';
    store.$reset();
    resolveStale();
    await stale;
    expect(store.snapshot).toBeNull();
    expect(store.branchLabel).toBe('');
  });

  it('stage 调用后以后端新快照刷新界面（仓库为唯一事实源）', async () => {
    const before = fakeSnapshot();
    const after = fakeSnapshot();
    after.staged = [...before.staged, { path: 'b.py', index_status: 'A', worktree_status: '.', orig_path: null, is_binary: false }];
    after.unstaged = [];
    const git_stage = vi.fn().mockResolvedValue({ ok: true, snapshot: after });
    mockApi({
      git_get_status: vi.fn().mockResolvedValue({ ok: true, snapshot: before }),
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
      git_stage,
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    await store.stage('b.py');
    expect(git_stage).toHaveBeenCalledWith('E:/proj', ['b.py']);
    expect(store.snapshot?.staged).toHaveLength(2);
    expect(store.snapshot?.unstaged).toHaveLength(0);
  });

  it('高风险操作挂起 pendingConfirm，确认后带令牌执行（§13）', async () => {
    const preview = {
      operation: 'reset_hard',
      risk_level: 'unrecoverable',
      confirm_required: true,
      typed_confirm_required: false,
      confirm_text: '确认执行 reset_hard',
      versions: { worktree_version: 'w', refs_version: 'r' },
      token: 'tok-1',
    };
    const git_confirm_operation = vi.fn().mockResolvedValue({ ok: true, snapshot: fakeSnapshot() });
    mockApi({
      git_get_status: vi.fn().mockResolvedValue({ ok: true, snapshot: fakeSnapshot() }),
      git_get_trust: vi.fn().mockResolvedValue({ ok: true, level: 'trusted' }),
      git_preview_risk: vi.fn().mockResolvedValue({ ok: true, preview }),
      git_confirm_operation,
    });
    const store = useGitStore();
    await store.ensureLoaded('E:/proj');
    await store.previewConfirm('reset_hard', { target: 'HEAD~1' });
    expect(store.pendingConfirm).not.toBeNull();
    expect(store.pendingConfirm?.preview.token).toBe('tok-1');
    await store.executeConfirmed('reset_hard', { target: 'HEAD~1' }, 'tok-1');
    expect(git_confirm_operation).toHaveBeenCalledWith('E:/proj', 'reset_hard', { target: 'HEAD~1' }, 'tok-1');
    expect(store.pendingConfirm).toBeNull();
  });
});
