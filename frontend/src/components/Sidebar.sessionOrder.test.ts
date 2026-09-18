import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

const ORDER_KEY = 'ycode-sidebar-session-order';
const PROJECT = 'D:/proj/a';

function makeSession(name: string) {
  return { name, display_name: name, updated_at: '', workdir: PROJECT };
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

function makeGlobalGroup(names: string[]) {
  return {
    folder_path: 'GLOBAL',
    folder_name: '对话',
    is_global: true,
    is_current: false,
    sessions: names.map(makeSession),
  };
}

function mountSidebar(groups: any[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = groups as any;
  return mount(Sidebar, { global: { plugins: [pinia] } });
}

/** 指定分组当前渲染出的会话名顺序 */
function sessionNames(wrapper: any, groupIndex = 0): string[] {
  return wrapper
    .findAll('.session-group')
    [groupIndex].findAll('.session-item')
    .map((n: any) => n.attributes('data-session-name'));
}

/** 按会话名定位条目（不依赖下标，顺序变化后依然可靠） */
function sessionItem(wrapper: any, groupIndex: number, name: string) {
  const items = wrapper.findAll('.session-group')[groupIndex].findAll('.session-item');
  const names = items.map((n: any) => n.attributes('data-session-name'));
  return items[names.indexOf(name)];
}

async function dragSession(wrapper: any, groupIndex: number, name: string) {
  await sessionItem(wrapper, groupIndex, name).trigger('dragstart', {
    dataTransfer: { effectAllowed: '', setData: () => {} },
  });
}

/** 落到目标的 y 坐标：上半区（0~10）插到前，下半区（10~20）插到后 */
async function dropSession(wrapper: any, groupIndex: number, name: string, clientY: number) {
  const el = sessionItem(wrapper, groupIndex, name);
  await el.trigger('dragover', { clientY });
  await el.trigger('drop');
}

const HALF_BEFORE = 5;
const HALF_AFTER = 15;

beforeEach(() => {
  localStorage.clear();
  delete (window as any).pywebview;
  // jsdom 无布局引擎，getBoundingClientRect 恒为 0，会让"上半区/下半区"判定失效；
  // 统一桩成 top=0 / height=20 的固定盒，使落点中线逻辑真正被覆盖
  (Element.prototype as any).getBoundingClientRect = () => ({
    top: 0, bottom: 20, height: 20, left: 0, right: 0, width: 0, x: 0, y: 0,
    toJSON: () => ({}),
  });
});

it('拖到目标下半区时插到目标之后，顺序落盘并在重新挂载后保持', async () => {
  const groups = [makeGroup(PROJECT, ['s1', 's2', 's3'])];
  const w1 = mountSidebar(groups);
  expect(sessionNames(w1)).toEqual(['s1', 's2', 's3']);

  await dragSession(w1, 0, 's1');
  await dropSession(w1, 0, 's2', HALF_AFTER);

  expect(sessionNames(w1)).toEqual(['s2', 's1', 's3']);
  expect(JSON.parse(localStorage.getItem(ORDER_KEY) as string)).toEqual({
    [PROJECT]: ['s2', 's1', 's3'],
  });
  w1.unmount();

  // 重新挂载（等价于重启前端）后仍是用户排好的顺序
  const w2 = mountSidebar(groups);
  expect(sessionNames(w2)).toEqual(['s2', 's1', 's3']);
  w2.unmount();
});

it('拖到目标上半区时插到目标之前', async () => {
  const w = mountSidebar([makeGroup(PROJECT, ['s1', 's2', 's3'])]);

  await dragSession(w, 0, 's3');
  await dropSession(w, 0, 's1', HALF_BEFORE);

  expect(sessionNames(w)).toEqual(['s3', 's1', 's2']);
  w.unmount();
});

it('未记录的新会话排在最前，不打乱已排好的顺序', async () => {
  localStorage.setItem(ORDER_KEY, JSON.stringify({ [PROJECT]: ['s2', 's1'] }));

  // 后端按更新时间给出的顺序里，新会话 s3 在最前
  const w = mountSidebar([makeGroup(PROJECT, ['s3', 's1', 's2'])]);

  expect(sessionNames(w)).toEqual(['s3', 's2', 's1']);
  w.unmount();
});

it('折叠态「显示更多」按自定义顺序取前 5 条，置顶的会话不会被切掉', async () => {
  localStorage.setItem(
    ORDER_KEY,
    JSON.stringify({ [PROJECT]: ['s6', 's1', 's2', 's3', 's4', 's5'] }),
  );

  const w = mountSidebar([makeGroup(PROJECT, ['s1', 's2', 's3', 's4', 's5', 's6'])]);

  expect(sessionNames(w)).toEqual(['s6', 's1', 's2', 's3', 's4']);
  expect(w.get('.more-toggle').text()).toContain('显示更多');
  w.unmount();
});

it('「对话」分组内的会话同样支持排序', async () => {
  const w = mountSidebar([makeGlobalGroup(['g1', 'g2'])]);

  await dragSession(w, 0, 'g1');
  await dropSession(w, 0, 'g2', HALF_AFTER);

  expect(sessionNames(w, 0)).toEqual(['g2', 'g1']);
  w.unmount();
});

it('跨分组拖拽会话被忽略，不改变任何顺序', async () => {
  const w = mountSidebar([
    makeGroup('D:/proj/a', ['a1', 'a2']),
    makeGroup('D:/proj/b', ['b1', 'b2']),
  ]);

  await dragSession(w, 0, 'a1');
  await dropSession(w, 1, 'b1', HALF_AFTER);

  expect(sessionNames(w, 0)).toEqual(['a1', 'a2']);
  expect(sessionNames(w, 1)).toEqual(['b1', 'b2']);
  expect(localStorage.getItem(ORDER_KEY)).toBeNull();
  w.unmount();
});

it('右键菜单「上移 / 下移 / 置顶」按动作调整会话顺序，重命名/删除项保留', async () => {
  const w = mountSidebar([makeGroup(PROJECT, ['s1', 's2', 's3'])]);
  const openMenu = async (name: string) => {
    await sessionItem(w, 0, name).trigger('contextmenu', { clientX: 10, clientY: 10 });
  };

  await openMenu('s2');
  expect(w.find('[data-test="session-ctx-menu"]').exists()).toBe(true);
  await w.get('[data-test="ctx-move-up"]').trigger('click');
  expect(sessionNames(w)).toEqual(['s2', 's1', 's3']);
  expect(w.find('[data-test="session-ctx-menu"]').exists()).toBe(false);

  await openMenu('s1');
  await w.get('[data-test="ctx-move-down"]').trigger('click');
  expect(sessionNames(w)).toEqual(['s2', 's3', 's1']);

  await openMenu('s1');
  await w.get('[data-test="ctx-move-top"]').trigger('click');
  expect(sessionNames(w)).toEqual(['s1', 's2', 's3']);
  expect(JSON.parse(localStorage.getItem(ORDER_KEY) as string)).toEqual({
    [PROJECT]: ['s1', 's2', 's3'],
  });

  // 排序项是新增的，原有重命名/删除入口不能被顶掉
  await openMenu('s2');
  expect(w.find('[data-test="ctx-rename"]').exists()).toBe(true);
  expect(w.find('[data-test="ctx-delete"]').exists()).toBe(true);
  w.unmount();
});

it('首个会话不能上移/置顶、末个不能下移，菜单项置灰且逻辑拒绝越界', async () => {
  const w = mountSidebar([makeGroup(PROJECT, ['s1', 's2', 's3'])]);

  await sessionItem(w, 0, 's1').trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.get('[data-test="ctx-move-up"]').classes()).toContain('disabled');
  expect(w.get('[data-test="ctx-move-top"]').classes()).toContain('disabled');
  expect(w.get('[data-test="ctx-move-down"]').classes()).not.toContain('disabled');
  // 置灰只是交互降级，逻辑层同样要拒绝越界移动
  await w.get('[data-test="ctx-move-up"]').trigger('click');
  expect(sessionNames(w)).toEqual(['s1', 's2', 's3']);

  await sessionItem(w, 0, 's3').trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.get('[data-test="ctx-move-down"]').classes()).toContain('disabled');
  w.unmount();
});

it('会话顺序存储损坏时退回后端顺序，不抛错', () => {
  localStorage.setItem(ORDER_KEY, '{不是合法 JSON');
  const w = mountSidebar([makeGroup(PROJECT, ['s1', 's2'])]);

  expect(sessionNames(w)).toEqual(['s1', 's2']);
  w.unmount();
});

it('不同分组的自定义顺序互不影响', async () => {
  localStorage.setItem(
    ORDER_KEY,
    JSON.stringify({ 'D:/proj/a': ['a2', 'a1'], 'D:/proj/b': ['b1', 'b2'] }),
  );
  const w = mountSidebar([
    makeGroup('D:/proj/a', ['a1', 'a2']),
    makeGroup('D:/proj/b', ['b1', 'b2']),
  ]);

  expect(sessionNames(w, 0)).toEqual(['a2', 'a1']);
  expect(sessionNames(w, 1)).toEqual(['b1', 'b2']);
  w.unmount();
});
