import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 归档区批量恢复 / 批量删除的 store 层契约：一次桥接调用处理多个会话，
// 直接采用后端回传的两侧快照（不做乐观增删），并按成功项回传名字列表。

function archivedEntry(name: string, archivedAt: string) {
  return {
    name,
    display_name: name,
    count: 1,
    workdir: 'GLOBAL',
    is_global: true,
    updated_at: archivedAt,
    archived_at: archivedAt,
  };
}

function groups(names: string[]) {
  return [{ folder_path: 'GLOBAL', folder_name: '全局对话', sessions: names.map(n => ({ name: n, display_name: n })) }];
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('batchUnarchiveSessions 调桥接批量取消归档，采用两侧快照并回传成功项', async () => {
  const unarchive_sessions = vi.fn(async () => ({
    ok: true,
    success: true,
    count: 2,
    names: ['a', 'b'],
    failed: [],
    sessions: groups(['a', 'b']),
    archived_sessions: [],
  }));
  (window as any).pywebview = { api: { unarchive_sessions } };
  const store = useAppStore();
  store.archivedSessions = [archivedEntry('a', 'x'), archivedEntry('b', 'y')] as any;

  const done = await store.batchUnarchiveSessions(['a', 'b']);

  expect(unarchive_sessions).toHaveBeenCalledWith(['a', 'b']);
  expect(done).toEqual(['a', 'b']);
  expect(store.archivedSessions).toEqual([]);
  expect(store.sessionGroups[0].sessions.map(s => s.name)).toEqual(['a', 'b']);
});

it('batchUnarchiveSessions 部分失败时提示失败条数，仍回传成功项', async () => {
  const unarchive_sessions = vi.fn(async () => ({
    ok: true,
    success: true,
    count: 1,
    names: ['a'],
    failed: [{ name: 'b', msg: '会话 b 不存在' }],
    sessions: groups(['a']),
    archived_sessions: [archivedEntry('b', 'y')],
  }));
  (window as any).pywebview = { api: { unarchive_sessions } };
  const store = useAppStore();
  const notify = vi.spyOn(store, 'notifyModal');

  const done = await store.batchUnarchiveSessions(['a', 'b']);

  expect(done).toEqual(['a']);
  expect(notify).toHaveBeenCalledTimes(1);
  expect(String(notify.mock.calls[0][1])).toContain('1 个失败');
});

// 可逆操作不弹确认框：恢复只走桥接，不产生 askConfirm
it('batchUnarchiveSessions 不弹二次确认框', async () => {
  const unarchive_sessions = vi.fn(async () => ({
    ok: true, success: true, count: 1, names: ['a'], failed: [],
    sessions: groups(['a']), archived_sessions: [],
  }));
  (window as any).pywebview = { api: { unarchive_sessions } };
  const store = useAppStore();
  const confirmSpy = vi.spyOn(store, 'askConfirm');

  await store.batchUnarchiveSessions(['a']);

  expect(confirmSpy).not.toHaveBeenCalled();
});

it('batchUnarchiveSessions 空名单不发请求', async () => {
  const unarchive_sessions = vi.fn(async () => ({ ok: true, names: [], failed: [] }));
  (window as any).pywebview = { api: { unarchive_sessions } };
  const store = useAppStore();

  const done = await store.batchUnarchiveSessions([]);

  expect(done).toEqual([]);
  expect(unarchive_sessions).not.toHaveBeenCalled();
});

it('batchDeleteArchivedSessions 采用快照并清理本地运行态残留', async () => {
  const delete_archived_sessions = vi.fn(async () => ({
    ok: true,
    success: true,
    count: 1,
    names: ['a'],
    failed: [],
    sessions: groups(['keep']),
    archived_sessions: [],
    current_session: 'keep',
  }));
  (window as any).pywebview = { api: { delete_archived_sessions } };
  const store = useAppStore();
  store.currentSessionName = 'keep';
  store.runningSessions = ['a', 'keep'];
  store.queuedSessions = ['a'];
  store.messageQueues['a'] = ['排队消息'] as any;
  store.sessionRuntimeByName['a'] = { messages: [] } as any;

  const done = await store.batchDeleteArchivedSessions(['a']);

  expect(delete_archived_sessions).toHaveBeenCalledWith(['a']);
  expect(done).toEqual(['a']);
  expect(store.runningSessions).toEqual(['keep']);
  expect(store.queuedSessions).toEqual([]);
  expect(store.messageQueues['a']).toBeUndefined();
  expect(store.sessionRuntimeByName['a']).toBeUndefined();
  expect(store.archivedSessions).toEqual([]);
});

it('batchDeleteArchivedSessions 整体失败时提示错误且不回传成功项', async () => {
  const delete_archived_sessions = vi.fn(async () => ({
    ok: false, success: false, count: 0, names: [],
    failed: [{ name: 'a', msg: '已不在归档区，已跳过' }],
    msg: '选中的会话都不在归档区',
  }));
  (window as any).pywebview = { api: { delete_archived_sessions } };
  const store = useAppStore();
  const notify = vi.spyOn(store, 'notifyModal');

  const done = await store.batchDeleteArchivedSessions(['a']);

  expect(done).toEqual([]);
  expect(notify).toHaveBeenCalledTimes(1);
  expect(String(notify.mock.calls[0][1])).toContain('已不在归档区');
});

it('桥接抛错时不吞异常：提示失败并保持列表原状', async () => {
  const delete_archived_sessions = vi.fn(async () => {
    throw new Error('桥断开');
  });
  (window as any).pywebview = { api: { delete_archived_sessions } };
  const store = useAppStore();
  store.archivedSessions = [archivedEntry('a', 'x')] as any;
  const notify = vi.spyOn(store, 'notifyModal');

  const done = await store.batchDeleteArchivedSessions(['a']);

  expect(done).toEqual([]);
  expect(store.archivedSessions).toHaveLength(1);
  expect(String(notify.mock.calls[0][1])).toContain('删除失败');
});

it('桥事件 session_deleted 到达时同步两侧列表（多端同步）', () => {
  const store = useAppStore();
  store.archivedSessions = [archivedEntry('a', 'x')] as any;

  store.handleBridgeEvent({
    type: 'session_deleted',
    data: { names: ['a'], sessions: groups(['keep']), archived_sessions: [] },
  } as any);

  expect(store.sessionGroups[0].sessions.map(s => s.name)).toEqual(['keep']);
  expect(store.archivedSessions).toEqual([]);
});