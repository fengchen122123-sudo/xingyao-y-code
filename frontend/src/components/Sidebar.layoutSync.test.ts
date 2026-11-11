import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

const GROUP_KEY = 'ycode-sidebar-group-order';
const SESSION_KEY = 'ycode-sidebar-session-order';
const PROJECT_A = 'D:/proj/a';
const PROJECT_B = 'D:/proj/b';

function makeSession(name: string) {
  return { name, display_name: name, updated_at: '', workdir: PROJECT_A };
}

function makeGroup(path: string, names: string[]) {
  return {
    folder_path: path,
    folder_name: path.split('/').pop(),
    is_global: false,
    is_current: false,
    sessions: names.map(makeSession),
  };
}

function makeGlobal() {
  return { folder_path: 'GLOBAL', folder_name: '对话', is_global: true, is_current: false, sessions: [] };
}

/** api 传 undefined 表示"没有 pywebview 桥"（CLI/纯浏览器场景） */
function mountSidebar(groups: any[], api?: Record<string, any>) {
  if (api) (window as any).pywebview = { api };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = groups as any;
  return mount(Sidebar, { global: { plugins: [pinia] } });
}

function orderOf(wrapper: any): string[] {
  return wrapper.findAll('.group-header').map((h: any) => h.attributes('data-group-path'));
}

function sessionNames(wrapper: any, groupIndex = 0): string[] {
  return wrapper
    .findAll('.session-group')
    [groupIndex].findAll('.session-item')
    .map((n: any) => n.attributes('data-session-name'));
}

function groupByPath(wrapper: any, path: string) {
  return wrapper.findAll('.session-group')[orderOf(wrapper).indexOf(path)];
}

async function dragGroup(wrapper: any, path: string) {
  await groupByPath(wrapper, path).find('.group-header').trigger('dragstart', {
    dataTransfer: { effectAllowed: '', setData: () => {} },
  });
}

async function dropGroup(wrapper: any, path: string, clientY: number) {
  const el = groupByPath(wrapper, path);
  await el.trigger('dragover', { clientY });
  await el.trigger('drop');
}

beforeEach(() => {
  localStorage.clear();
  delete (window as any).pywebview;
  (Element.prototype as any).getBoundingClientRect = () => ({
    top: 0, bottom: 20, height: 20, left: 0, right: 0, width: 0, x: 0, y: 0,
    toJSON: () => ({}),
  });
});

it('启动时用后端快照校正：localStorage 被清掉也能恢复分组顺序', async () => {
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({
      ok: true,
      group_order: [PROJECT_B, PROJECT_A],
      session_order: {},
    }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  const w = mountSidebar([makeGlobal(), makeGroup(PROJECT_A, ['a1']), makeGroup(PROJECT_B, ['b1'])], api);

  await flushPromises();

  expect(orderOf(w)).toEqual(['GLOBAL', PROJECT_B, PROJECT_A]);
  // 校正结果回写本地缓存，下次首屏不必等后端
  expect(JSON.parse(localStorage.getItem(GROUP_KEY) as string)).toEqual([PROJECT_B, PROJECT_A]);
  w.unmount();
});

it('会话顺序同样由后端快照校正', async () => {
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({
      ok: true,
      group_order: [],
      session_order: { [PROJECT_A]: ['a2', 'a1'] },
    }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  const w = mountSidebar([makeGroup(PROJECT_A, ['a1', 'a2'])], api);

  await flushPromises();

  expect(sessionNames(w)).toEqual(['a2', 'a1']);
  // 会话顺序的校正结果同样回写本地缓存
  expect(JSON.parse(localStorage.getItem(SESSION_KEY) as string)).toEqual({
    [PROJECT_A]: ['a2', 'a1'],
  });
  w.unmount();
});

it('后端为空快照时反向补写：把已有的本地排序带到后端，升级不丢历史', async () => {
  localStorage.setItem(GROUP_KEY, JSON.stringify([PROJECT_B, PROJECT_A]));
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({ ok: true, group_order: [], session_order: {} }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  const w = mountSidebar([makeGlobal(), makeGroup(PROJECT_A, ['a1']), makeGroup(PROJECT_B, ['b1'])], api);

  await flushPromises();

  expect(api.save_ui_layout).toHaveBeenCalledWith({
    group_order: [PROJECT_B, PROJECT_A],
    session_order: {},
  });
  // 本地顺序照旧生效，不被空快照覆盖
  expect(orderOf(w)).toEqual(['GLOBAL', PROJECT_B, PROJECT_A]);
  w.unmount();
});

it('拖拽排序后同时推给后端（localStorage 只是即时缓存）', async () => {
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({
      ok: true,
      group_order: [PROJECT_B, PROJECT_A],
      session_order: {},
    }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  const w = mountSidebar([makeGlobal(), makeGroup(PROJECT_A, ['a1']), makeGroup(PROJECT_B, ['b1'])], api);
  await flushPromises();
  api.save_ui_layout.mockClear();

  await dragGroup(w, PROJECT_B);
  await dropGroup(w, PROJECT_A, 15);

  expect(api.save_ui_layout).toHaveBeenCalledWith({
    group_order: [PROJECT_A, PROJECT_B],
    session_order: {},
  });
  expect(JSON.parse(localStorage.getItem(GROUP_KEY) as string)).toEqual([PROJECT_A, PROJECT_B]);
  w.unmount();
});

it('右键菜单调整会话顺序同样推给后端', async () => {
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({ ok: true, group_order: [], session_order: {} }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  const w = mountSidebar([makeGroup(PROJECT_A, ['a1', 'a2'])], api);
  await flushPromises();
  api.save_ui_layout.mockClear();

  const items = w.findAll('.session-item');
  await items[1].trigger('contextmenu', { clientX: 10, clientY: 10 });
  await w.get('[data-test="ctx-move-top"]').trigger('click');

  expect(api.save_ui_layout).toHaveBeenCalledWith({
    group_order: [],
    session_order: { [PROJECT_A]: ['a2', 'a1'] },
  });
  w.unmount();
});

it('桥延迟就绪时也能校正：api 先注入空对象，方法后注入并触发 pywebviewready', async () => {
  const groups = [makeGlobal(), makeGroup(PROJECT_A, ['a1']), makeGroup(PROJECT_B, ['b1'])];
  // pywebview 第一步：window.pywebview.api 已存在但方法还没填进来
  (window as any).pywebview = { api: {} };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = groups as any;
  const w = mount(Sidebar, { global: { plugins: [pinia] } });
  await flushPromises();

  // 抢跑阶段不该改动任何顺序（方法不存在 → 静默跳过）
  expect(orderOf(w)).toEqual(['GLOBAL', PROJECT_A, PROJECT_B]);

  // 第二步：finish.js 注入方法并派发 pywebviewready
  const api = {
    get_ui_layout: vi.fn().mockResolvedValue({
      ok: true,
      group_order: [PROJECT_B, PROJECT_A],
      session_order: {},
    }),
    save_ui_layout: vi.fn().mockResolvedValue({ ok: true }),
  };
  (window as any).pywebview = { api };
  window.dispatchEvent(new Event('pywebviewready'));
  await flushPromises();

  expect(api.get_ui_layout).toHaveBeenCalled();
  expect(orderOf(w)).toEqual(['GLOBAL', PROJECT_B, PROJECT_A]);
  w.unmount();
});

it('没有 pywebview 桥（或后端报错）时静默降级，排序仍走 localStorage', async () => {  // 无桥
  const w1 = mountSidebar([makeGlobal(), makeGroup(PROJECT_A, ['a1']), makeGroup(PROJECT_B, ['b1'])]);
  await flushPromises();
  await dragGroup(w1, PROJECT_B);
  await dropGroup(w1, PROJECT_A, 15);
  expect(JSON.parse(localStorage.getItem(GROUP_KEY) as string)).toEqual([PROJECT_A, PROJECT_B]);
  w1.unmount();

  localStorage.clear();

  // 后端抛错
  const api = {
    get_ui_layout: vi.fn().mockRejectedValue(new Error('backend down')),
    save_ui_layout: vi.fn().mockRejectedValue(new Error('backend down')),
  };
  const w2 = mountSidebar([makeGlobal(), makeGroup(PROJECT_A, ['a1'])], api);
  await flushPromises();

  expect(orderOf(w2)).toEqual(['GLOBAL', PROJECT_A]);
  w2.unmount();
});
