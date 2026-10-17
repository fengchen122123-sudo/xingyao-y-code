import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, expect, it, vi } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

// 归档入口收敛为「会话行内 ⤓ 归档按钮」：侧栏底部不再有「已归档」折叠区
// （归档列表改由设置页承载）。这里沿用 rename/groupCollapse 两个测试文件的写法：
// 不 mock store，直接灌数据后断言真实渲染与真实桥接调用。
const wrappers: VueWrapper[] = [];
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount());
  delete (window as any).pywebview;
  localStorage.clear();
});

function sessionGroups() {
  return [
    {
      folder_path: 'GLOBAL',
      folder_name: '对话',
      is_global: true,
      is_current: true,
      sessions: [
        { name: 'session-a', display_name: '会话 A', updated_at: '', workdir: 'GLOBAL' },
        { name: 'session-b', display_name: '会话 B', updated_at: '', workdir: 'GLOBAL' },
      ],
    },
  ];
}

/** store 侧的归档列表快照（侧栏已不渲染，仅用于校验数据同步） */
function archivedList() {
  return [
    {
      name: 'session-old-1',
      display_name: '旧会话一',
      count: 3,
      workdir: 'GLOBAL',
      is_global: true,
      updated_at: '2026-10-01 10:00:00',
      archived_at: '2026-10-09 09:00:00',
    },
    {
      name: 'session-old-2',
      display_name: '旧会话二',
      count: 1,
      workdir: 'D:/proj/a',
      is_global: false,
      updated_at: '2026-09-20 10:00:00',
      archived_at: '2026-10-10 11:30:00',
    },
  ];
}

function setup(api: Record<string, any> = {}, archived: any[] = archivedList()) {
  (window as any).pywebview = { api };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = sessionGroups() as any;
  store.archivedSessions = archived as any;
  store.currentSessionName = 'session-a';
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return { wrapper, store };
}

it('侧栏不再渲染底部「已归档」折叠区（归档列表已移交设置页）', async () => {
  const { wrapper } = setup();

  expect(wrapper.find('[data-test="archive-section"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="archive-toggle"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="archive-body"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="archive-item"]').exists()).toBe(false);
});

it('会话行 hover 操作区新增归档按钮：当前活跃会话置灰并给出 tooltip', async () => {
  const { wrapper } = setup();

  const btns = wrapper.findAll('[data-test="session-archive-btn"]');
  expect(btns.length).toBe(2);

  // 第 0 条是当前活跃会话（store.currentSessionName = session-a）
  expect(btns[0].attributes('disabled')).toBeDefined();
  expect(btns[0].classes()).toContain('disabled');
  expect(btns[0].attributes('title')).toContain('不能归档');

  expect(btns[1].attributes('disabled')).toBeUndefined();
  expect(btns[1].attributes('title')).toContain('归档');
});

it('当前活跃会话禁止归档：store 层直接拒绝，不调用桥接', async () => {
  const archive_session = vi.fn();
  const { store } = setup({ archive_session });

  const ok = await store.archiveSession('session-a');
  await flushPromises();

  expect(ok).toBe(false);
  expect(archive_session).not.toHaveBeenCalled();
  expect(store.confirmModal.message).toContain('不能归档当前活跃会话');
});

it('归档沿用两段确认：先点 ⤓ 只进确认态，点「确认」才调用 archive_session，成功后静默完成', async () => {
  const archive_session = vi.fn(async () => ({
    ok: true,
    name: 'session-b',
    archived_at: '2026-10-11 12:00:00',
  }));
  const get_archived_sessions = vi.fn(async () => [
    ...archivedList(),
    {
      name: 'session-b',
      display_name: '会话 B',
      count: 0,
      workdir: 'GLOBAL',
      is_global: true,
      updated_at: '',
      archived_at: '2026-10-11 12:00:00',
    },
  ]);
  const { wrapper, store } = setup({ archive_session, get_archived_sessions });

  // 首击：只进入确认态，不调桥接、不动列表
  await wrapper.findAll('[data-test="session-archive-btn"]')[1].trigger('click');
  await flushPromises();
  expect(archive_session).not.toHaveBeenCalled();
  expect(store.sessionGroups[0].sessions.map((s) => s.name)).toEqual(['session-a', 'session-b']);
  const confirmBtn = wrapper.find('[data-test="session-archive-confirm"]');
  expect(confirmBtn.exists()).toBe(true);
  expect(confirmBtn.text()).toContain('确认');

  // 次击「确认」：真正归档，会话移出主列表、归档列表刷新
  await confirmBtn.trigger('click');
  await flushPromises();

  expect(archive_session).toHaveBeenCalledTimes(1);
  expect(archive_session).toHaveBeenCalledWith('session-b');
  expect(store.sessionGroups[0].sessions.map((s) => s.name)).toEqual(['session-a']);
  expect(store.archivedSessions.map((s) => s.name)).toContain('session-b');

  // 成功后静默完成：不再弹「已归档」提示框，确认态也已退出
  expect(store.confirmModal.show).toBe(false);
  expect(wrapper.find('[data-test="session-archive-confirm"]').exists()).toBe(false);
});

it('归档确认态下点击条目其它区域取消确认，且不切换会话', async () => {
  const archive_session = vi.fn();
  const { wrapper, store } = setup({ archive_session });
  const switchSpy = vi.spyOn(store, 'switchSession').mockResolvedValue(undefined);

  await wrapper.findAll('[data-test="session-archive-btn"]')[1].trigger('click');
  await flushPromises();
  expect(wrapper.find('[data-test="session-archive-confirm"]').exists()).toBe(true);

  await wrapper.findAll('.session-item')[1].trigger('click');
  await flushPromises();

  expect(wrapper.find('[data-test="session-archive-confirm"]').exists()).toBe(false);
  expect(archive_session).not.toHaveBeenCalled();
  expect(switchSpy).not.toHaveBeenCalled();
});

it('归档失败（后端拒绝）时提示原因，且不动主列表与归档列表', async () => {
  const archive_session = vi.fn(async () => ({ ok: false, msg: '会话不存在' }));
  const { store } = setup({ archive_session });
  const before = JSON.parse(JSON.stringify(store.sessionGroups));

  await store.archiveSession('session-b');
  await flushPromises();

  expect(store.confirmModal.message).toContain('归档失败');
  expect(store.confirmModal.message).toContain('会话不存在');
  expect(store.sessionGroups).toEqual(before);
  expect(store.archivedSessions.map((s) => s.name)).toEqual(['session-old-1', 'session-old-2']);
});

it('桥接事件 session_archived / session_unarchived 到达时同步刷新两侧列表', async () => {
  const groups = sessionGroups();
  const remaining = [{ ...groups[0], sessions: [groups[0].sessions[0]] }];
  const { store } = setup({});

  store.handleBridgeEvent({
    type: 'session_archived',
    data: {
      name: 'session-b',
      session_name: 'session-b',
      archived_at: '2026-10-11 12:00:00',
      sessions: remaining,
      archived_sessions: [archivedList()[0]],
    },
  } as any);

  expect(store.sessionGroups[0].sessions.map((s) => s.name)).toEqual(['session-a']);
  expect(store.archivedSessions.map((s) => s.name)).toEqual(['session-old-1']);

  store.handleBridgeEvent({
    type: 'session_unarchived',
    data: {
      name: 'session-old-1',
      session_name: 'session-old-1',
      archived_at: null,
      sessions: groups,
      archived_sessions: [],
    },
  } as any);

  expect(store.sessionGroups[0].sessions.map((s) => s.name)).toEqual(['session-a', 'session-b']);
  expect(store.archivedSessions).toEqual([]);
});
