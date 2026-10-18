import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

// 移植自二改包 src/components/Sidebar.moveSession.test.ts，按本仓（官方 1.3.6）现状调整：
// - 会话分组多了「秘书」（folder_path='SECRETARY'）：它不是磁盘目录，不能作为移动目标，
//   秘书会话本身也不提供「移动到…」入口，故新增两条对应用例；
// - 目标条目文案走本仓现有的 getFolderTitle 口径。
const wrappers: VueWrapper[] = [];
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount());
  delete (window as any).pywebview;
});

// 秘书分组 + 两个项目分组 + 一个「对话」(GLOBAL) 分组
function sessionGroups() {
  return [
    {
      folder_path: 'SECRETARY',
      folder_name: '秘书',
      is_secretary: true,
      is_current: false,
      sessions: [
        {
          name: 'session_sec001',
          display_name: '星河',
          secretary_name: '星河',
          updated_at: '',
          workdir: 'GLOBAL',
        },
      ],
    },
    {
      folder_path: 'GLOBAL',
      folder_name: '对话',
      is_global: true,
      is_current: false,
      sessions: [{ name: 'chat-1', display_name: '闲聊', updated_at: '', workdir: 'GLOBAL' }],
    },
    {
      folder_path: 'F:\\projA',
      folder_name: 'projA',
      is_current: true,
      sessions: [{ name: 'task-a', display_name: '任务A', updated_at: '', workdir: 'F:\\projA' }],
    },
    {
      folder_path: 'F:\\projB',
      folder_name: 'projB',
      is_current: false,
      sessions: [{ name: 'task-b', display_name: '任务B', updated_at: '', workdir: 'F:\\projB' }],
    },
  ];
}

function setup(api: Record<string, any> = {}) {
  (window as any).pywebview = { api };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = sessionGroups() as any;
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] }, attachTo: document.body });
  wrappers.push(wrapper);
  return { wrapper, store, pinia };
}

// 按会话显示名定位会话项（分组渲染顺序不稳定，不能靠下标）
function sessionIndex(wrapper: VueWrapper, text: string) {
  return wrapper.findAll('.session-item').findIndex((w) => w.text().includes(text));
}

async function openMenu(wrapper: VueWrapper, sessionText: string) {
  const idx = sessionIndex(wrapper, sessionText);
  expect(idx).toBeGreaterThanOrEqual(0);
  const el = wrapper.findAll('.session-item')[idx].element as HTMLElement;
  el.dispatchEvent(
    new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 60 }),
  );
  await nextTick();
}

async function openMoveMenu(wrapper: VueWrapper, sessionText: string) {
  await openMenu(wrapper, sessionText);
  await wrapper.find('[data-test="ctx-move-to"]').trigger('click');
  await nextTick();
}

function moveTargetLabels(wrapper: VueWrapper) {
  return wrapper.findAll('[data-test="move-target"], [data-test="move-target-global"]')
    .map((w) => w.text());
}

it('会话右键菜单里有「移动到…」入口', async () => {
  const { wrapper } = setup();
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);

  await openMenu(wrapper, '闲聊');

  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ctx-move-to"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ctx-move-to"]').text()).toBe('移动到…');
});

it('秘书会话不提供「移动到…」入口（归属由 source 决定，后端也拒绝）', async () => {
  const { wrapper } = setup();
  await openMenu(wrapper, '星河');

  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ctx-move-to"]').exists()).toBe(false);
});

it('点「移动到…」弹出目标菜单，右键菜单同时收起', async () => {
  const { wrapper } = setup();
  await openMoveMenu(wrapper, '闲聊');

  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(true);
  // 两个菜单互斥：不能同时挂在界面上
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);
});

it('「对话」分组的会话：目标只列项目分组，不重复提供「对话」，也不含秘书分组', async () => {
  const { wrapper } = setup();
  await openMoveMenu(wrapper, '闲聊');

  expect(wrapper.find('[data-test="move-target-global"]').exists()).toBe(false);
  const labels = moveTargetLabels(wrapper);
  expect(labels).toContain('projA');
  expect(labels).toContain('projB');
  expect(labels).not.toContain('对话');
  expect(labels).not.toContain('秘书');
});

it('项目分组的会话：目标含「对话」与其它项目，且不含自己所在分组', async () => {
  const { wrapper } = setup();
  await openMoveMenu(wrapper, '任务A');

  expect(wrapper.find('[data-test="move-target-global"]').exists()).toBe(true);
  const labels = moveTargetLabels(wrapper);
  expect(labels).toContain('对话');
  expect(labels).toContain('projB');
  // 当前所在分组不能作为目标
  expect(labels).not.toContain('projA');
});

it('选目标后调用 move_session_to_workdir，并带上正确的会话名与目标目录', async () => {
  const move = vi.fn().mockResolvedValue({ ok: true, moved: true, sessions: [] });
  const { wrapper } = setup({ move_session_to_workdir: move });

  await openMoveMenu(wrapper, '任务A');
  const target = wrapper.findAll('[data-test="move-target"]').find((w) => w.text() === 'projB');
  expect(target).toBeTruthy();
  await target!.trigger('click');
  await nextTick();

  expect(move).toHaveBeenCalledWith('task-a', 'F:\\projB');
});

it('选「对话」目标时传 GLOBAL', async () => {
  const move = vi.fn().mockResolvedValue({ ok: true, moved: true, sessions: [] });
  const { wrapper } = setup({ move_session_to_workdir: move });

  await openMoveMenu(wrapper, '任务A');
  await wrapper.find('[data-test="move-target-global"]').trigger('click');
  await nextTick();

  expect(move).toHaveBeenCalledWith('task-a', 'GLOBAL');
});

it('选完目标后目标菜单关闭', async () => {
  const move = vi.fn().mockResolvedValue({ ok: true, moved: true, sessions: [] });
  const { wrapper } = setup({ move_session_to_workdir: move });

  await openMoveMenu(wrapper, '闲聊');
  await wrapper.findAll('[data-test="move-target"]')[0].trigger('click');
  await nextTick();

  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(false);
});

it('点击菜单外任意位置会关掉目标菜单', async () => {
  const { wrapper } = setup();
  await openMoveMenu(wrapper, '闲聊');
  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(true);

  document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await nextTick();

  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(false);
});

it('按 Esc 也会关掉目标菜单', async () => {
  const { wrapper } = setup();
  await openMoveMenu(wrapper, '闲聊');
  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(true);

  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await nextTick();

  expect(wrapper.find('[data-test="session-move-menu"]').exists()).toBe(false);
});

describe('store.moveSessionToWorkdir', () => {
  function storeSetup(api: Record<string, any>, currentSessionName = 'task-a') {
    const { store, pinia } = setup(api);
    store.currentSessionName = currentSessionName;
    store.workdir = currentSessionName === 'chat-1' ? 'GLOBAL' : 'F:\\projA';
    store.currentWorkdirLabel = currentSessionName === 'chat-1' ? '对话' : 'projA';
    return { store, pinia };
  }

  it('移动当前会话：分组树整份覆盖，工作区标签与文件树切到新目录', async () => {
    const nextGroups = sessionGroups();
    nextGroups[2].sessions = [];
    nextGroups[3].sessions = [
      { name: 'task-a', display_name: '任务A', updated_at: '', workdir: 'F:\\projB' },
    ];
    const move = vi.fn().mockResolvedValue({
      ok: true,
      moved: true,
      workdir: 'F:\\projB',
      sessions: nextGroups,
    });
    const tree = vi.fn().mockResolvedValue({ ok: true, file_tree: [{ n: 'x', p: 'F:\\projB\\x' }] });
    const { store } = storeSetup({ move_session_to_workdir: move, get_file_tree: tree });

    await store.moveSessionToWorkdir('task-a', 'F:\\projB');
    await nextTick();

    expect(move).toHaveBeenCalledWith('task-a', 'F:\\projB');
    expect(store.sessionGroups).toEqual(nextGroups as any);
    expect(store.workdir).toBe('F:\\projB');
    expect(store.currentWorkdirLabel).toBe('projB');
    expect(store.currentSessionName).toBe('task-a');
  });

  it('移回「对话」：清空文件树，标签回落「对话」', async () => {
    const move = vi.fn().mockResolvedValue({
      ok: true,
      moved: true,
      workdir: 'GLOBAL',
      sessions: sessionGroups(),
    });
    const { store } = storeSetup({ move_session_to_workdir: move });
    store.fileTree = [{ n: 'old', p: 'F:\\projA\\old' }] as any;

    await store.moveSessionToWorkdir('task-a', 'GLOBAL');
    await nextTick();

    expect(store.workdir).toBe('GLOBAL');
    expect(store.currentWorkdirLabel).toBe('对话');
    expect(store.fileTree).toEqual([]);
  });

  it('移动的不是当前会话：只刷新分组，不动工作区与文件树', async () => {
    const move = vi.fn().mockResolvedValue({
      ok: true,
      moved: true,
      workdir: 'F:\\projB',
      sessions: sessionGroups(),
    });
    const { store } = storeSetup({ move_session_to_workdir: move }, 'task-a');
    const treeBefore = [{ n: 'keep', p: 'F:\\projA\\keep' }] as any;
    store.fileTree = treeBefore;

    await store.moveSessionToWorkdir('task-b', 'F:\\projB');
    await nextTick();

    expect(store.workdir).toBe('F:\\projA');
    // 文件树内容原样保留（不经 toBe：pinia 状态里是响应式代理，不是同一个引用）
    expect(store.fileTree).toEqual(treeBefore);
  });

  it('空目标不发请求', async () => {
    const move = vi.fn().mockResolvedValue({ ok: true });
    const { store } = storeSetup({ move_session_to_workdir: move });

    await store.moveSessionToWorkdir('task-a', '   ');
    await nextTick();

    expect(move).not.toHaveBeenCalled();
  });

  it('秘书会话不发请求（归属由 source 决定）', async () => {
    const move = vi.fn().mockResolvedValue({ ok: true });
    const { store } = storeSetup({ move_session_to_workdir: move }, 'task-a');

    await store.moveSessionToWorkdir('session_sec001', 'F:\\projB');
    await nextTick();

    expect(move).not.toHaveBeenCalled();
  });

  it('后端拒绝时弹可见提示，不改工作区', async () => {
    const move = vi.fn().mockResolvedValue({ ok: false, msg: '该会话正在运行回合' });
    const { store } = storeSetup({ move_session_to_workdir: move });

    await store.moveSessionToWorkdir('task-a', 'F:\\projB');
    await nextTick();

    expect(store.workdir).toBe('F:\\projA');
    expect(store.confirmModal.show).toBe(true);
    expect(store.confirmModal.title).toContain('会话管理');
    expect(store.confirmModal.message).toContain('该会话正在运行回合');
  });
});
